# -*- coding: utf-8 -*-
"""轨迹评估的人读总览：把 `trajectory_metrics` 渲染成 `SUMMARY.md`。

【职责】
  做   读库全量指标，按 Pipeline / Supervisor 分组，渲染常驻总览。
  不做 算指标（runner.py）、落库（runner --persist）。

【关键决策】
  · 数据源是库不是运行目录 ← 与另外三类评估不同：轨迹指标由 `runner --persist`
    幂等 upsert 进表，没有 `runs/<ts>/` 这种一次一份的目录。所以总览是「按表全量
    重建」，不是「追加最新一轮」。删掉几行、手改坏了，跑一次就自动对齐。
  · ★ 埋点覆盖度放在最前面，不是附录 ← 2026-10-30 实测：14 个 Supervisor 任务里
    13 个只有 `think`、没有 `observe` / `tool_call`（两个埋点 2026-10-19 才上线）。
    若不把覆盖度摆出来，读的人必然把「埋点缺失算出的 NULL/0」读成「有效轮次低」。
    这不是数据质量问题，是**读者会被误导**的问题。
  · `n/a` 与 `0` 分开 ← 与 `runner._num` 同一口径：NULL = 算不出来（没埋点），
    0 = 确实发生了 0 次。均值只对非 NULL 求，旁边必须标 n。
  · 样本 < 8 不给结论性措辞 ← 与 memory 评估「8 条小样本只比哪几条翻了」的纪律一致。
    这里只在样本不足时加一行提示，不替读的人下判断。
  · Pipeline 与 Supervisor 分组，不混算 ← Pipeline 的轨迹在编译期就确定了，
    `termination` 恒为 `n/a`，混进来会把 Supervisor 的分母算错。

【读法】
  产物 = `evaluation_reports/trajectory/SUMMARY.md`
  重建 = `python -m src.evaluation.trajectory.overview`
"""

from __future__ import annotations

import asyncio
import statistics
from collections import Counter

from src.evaluation.common.paths import relative, reports_dir
from src.evaluation.common.report import ratio, save, table

SUMMARY_MD = "SUMMARY.md"

# 样本少于这个数时提示「不足以谈均值」。与 memory 评估的小样本纪律取同一量级。
MIN_SAMPLE = 8

# 表里要展示的指标：字段 → (中文名, 是否百分比, 小数位)
ORCH_METRICS: list[tuple[str, str, bool, int]] = [
    ("rounds", "总轮次", False, 1),
    ("rounds_effective", "有效轮次", False, 1),
    ("repeat_rate", "重复动作率", True, 3),
    ("failed_round_rate", "失败轮占比", True, 3),
    ("arg_completeness", "参数完整率", True, 3),
    ("observation_responsiveness", "观察响应性", True, 3),
    ("illegal_transitions", "非法转移", False, 1),
]

# 派生指标：表里没有这一列，由 (有效轮次 / 可计轮次) 现算。
# 不落库是有意的——比率可以从两列推出来，多存一列就多一处口径漂移。
DERIVED_EFFICIENCY = "轮次效率（有效/可计，finish 轮不计）"

TOOL_METRICS: list[tuple[str, str, bool, int]] = [
    ("tool_calls", "工具调用次数", False, 1),
    ("search_empty_rate", "搜索空结果率", True, 3),
    ("fetch_fail_rate", "抓取失败率", True, 3),
    ("dup_fetch_rate", "重复抓取率", True, 3),
    ("embed_calls", "embed 调用", False, 1),
    ("embed_chunks", "embed 片段", False, 1),
    ("rerank_gap", "rerank 区分度", False, 4),
    ("rerank_under_k", "精排不足 top_k", False, 1),
]

RETRIEVAL_METRICS: list[tuple[str, str, bool, int]] = [
    ("retrieval_cells", "检索格数", False, 1),
    ("recall_avg", "召回条数均值", False, 2),
    ("rerank_avg", "精排条数均值", False, 2),
    ("coverage_skew", "覆盖倾斜", False, 2),
    ("cross_dim_dup", "跨维度重复", False, 2),
]

FUNNEL_METRICS: list[tuple[str, str]] = [
    ("funnel_search", "搜索次数"),
    ("funnel_fetch", "抓取次数"),
    ("funnel_pages", "入库页数"),
    ("funnel_chunks", "切片数"),
]


# ── 取数 ────────────────────────────────────────────────────────────────


async def fetch_metrics() -> tuple[list[dict], dict[str, dict]]:
    """读指标行 + 每个任务的埋点条数。

    返回 (指标行, {task_id: {thinks, observes, tools}})。
    埋点计数单独查一次：它来自 `agent_logs`，和 `trajectory_metrics` 不同表，
    放进同一条 SQL 会让「没有日志的任务」整行消失。
    """
    from src.config import Settings
    from src.db.connection import close_pool, create_pool

    pool = await create_pool(Settings())
    try:
        rows = await pool.fetch(
            """
            SELECT * FROM trajectory_metrics
            WHERE metrics_version = (
                SELECT max(metrics_version) FROM trajectory_metrics
            )
            ORDER BY computed_at
            """
        )
        counts = await pool.fetch(
            """
            SELECT task_id,
                   count(*) FILTER (WHERE action = 'think')     AS thinks,
                   count(*) FILTER (WHERE action = 'observe')   AS observes,
                   count(*) FILTER (WHERE action = 'tool_call') AS tools
              FROM agent_logs
             WHERE action IN ('think', 'observe', 'tool_call')
             GROUP BY task_id
            """
        )
    finally:
        await close_pool()

    return [dict(r) for r in rows], {str(r["task_id"]): dict(r) for r in counts}


# ── 小工具 ──────────────────────────────────────────────────────────────


def _fmt(value, is_pct: bool, digits: int) -> str:
    if value is None:
        return "—"
    if is_pct:
        return f"{value:.1%}"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _stat_line(rows: list[dict], field: str, is_pct: bool, digits: int) -> str:
    """`中位数 [最小–最大] n=K`；只有一个样本时不给区间。"""
    vals = [r.get(field) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return "—（全部 n/a）"
    if len(vals) == 1:
        return f"{_fmt(vals[0], is_pct, digits)}（n=1）"
    med = statistics.median(vals)
    return (
        f"{_fmt(med, is_pct, digits)} "
        f"[{_fmt(min(vals), is_pct, digits)}–{_fmt(max(vals), is_pct, digits)}] n={len(vals)}"
    )


def _coverage(rows: list[dict], counts: dict[str, dict], key: str) -> tuple[int, int]:
    """(有该埋点数据的任务数, 总任务数)。"""
    have = sum(1 for r in rows if (counts.get(str(r["task_id"])) or {}).get(key, 0) > 0)
    return have, len(rows)


def _mean_line(vals: list) -> str:
    """`均值（n=K）`。非数值（None / 'n/a'）不计入分母。"""
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    return f"{statistics.mean(nums):.1f}（n={len(nums)}）" if nums else "—"


def _efficiency_stat(rows: list[dict]) -> str:
    """有效轮次 / **可计轮次**。

    ⚠️ 分母必须与 `runner._round_efficiency` 同口径：finish 轮按定义不产生业务状态
    （think 决定结束 → act 短路 → observe 空），留在分母里会让健康任务恒等于
    (n-1)/n，指标测的就成了「结束在第几轮」而不是「每轮有没有白跑」。
    2026-10-30 口径从「总轮次」改成「可计轮次」时，这里没跟上，报告一度显示
    80% 而真实是 100%——**同一个指标两个算法，比没有指标更糟**。

    库表里只有 rounds 和 rounds_effective 两列，终止原因另存一列，
    所以这里用 termination 反推需要扣掉的轮次，不额外落库（多一列就多一处漂移）。
    """
    ratios = []
    for r in rows:
        effective, rounds = r.get("rounds_effective"), r.get("rounds")
        if effective is None or not rounds:
            continue  # 缺埋点：不算成 0
        countable = rounds - (1 if r.get("termination") == "finish" else 0)
        if countable <= 0:
            continue  # 只有终止轮，没有可计的业务轮次
        ratios.append(effective / countable)
    if not ratios:
        return "—（全部 n/a）"
    if len(ratios) == 1:
        return f"{ratios[0]:.1%}（n=1）"
    return (
        f"{statistics.median(ratios):.1%} "
        f"[{min(ratios):.1%}–{max(ratios):.1%}] n={len(ratios)}"
    )


def _metric_table(rows: list[dict], group: list[tuple[str, str, bool, int]]) -> str:
    return table(
        ["指标", "取值"],
        [[name, _stat_line(rows, field, is_pct, digits)] for field, name, is_pct, digits in group],
    )


# ── 渲染 ────────────────────────────────────────────────────────────────


def build_overview(rows: list[dict], counts: dict[str, dict]) -> str:
    lines = [
        "# 轨迹评估总览",
        "",
        "> 常驻总览，每次由库里 `trajectory_metrics` 全量重建；事实源是同名数据表。",
        "> ⚠️ 本目录全部产物**不入库**（见 .gitignore），clone 下来是空的。",
        "",
    ]

    if not rows:
        lines += ["（还没有轨迹指标行，先跑 `runner --since 30d --persist`）", ""]
        return "\n".join(lines)

    supervisor = [r for r in rows if r.get("is_supervisor")]
    pipeline = [r for r in rows if not r.get("is_supervisor")]

    lines += [
        "## 样本",
        "",
        table(
            ["分组", "任务数", "说明"],
            [
                ["Supervisor", len(supervisor), "LLM 动态决策，轨迹有意义"],
                ["Pipeline", len(pipeline), "路径编译期确定，`termination` 恒为 n/a"],
                ["合计", len(rows), ""],
            ],
        ),
        "",
        "### 埋点覆盖度（读任何数字之前先看这里）",
        "",
    ]

    cov_rows = []
    for key, label in (("thinks", "think（编排决策）"), ("observes", "observe（执行反馈）"), ("tools", "tool_call（工具调用）")):
        have, total = _coverage(supervisor, counts, key)
        cov_rows.append([label, ratio(have, total)])
    lines += [
        table(["埋点类型（Supervisor 组）", "有数据的任务"], cov_rows),
        "",
    ]

    # 埋点缺失会让「有效轮次 / 参数完整率 / 观察响应性」算成 0 或 NULL。
    # 这句话必须写在数字前面，不能放在文末脚注。
    if supervisor:
        have_obs = _coverage(supervisor, counts, "observes")[0]
        have_tool = _coverage(supervisor, counts, "tools")[0]
        if have_obs < len(supervisor) or have_tool < len(supervisor):
            lines += [
                f"> ⚠️ **只有 {have_obs}/{len(supervisor)} 个任务有 observe 日志、"
                f"{have_tool}/{len(supervisor)} 个有 tool_call 日志。**",
                "> 缺埋点的任务上，「有效轮次 / 参数完整率 / 观察响应性 / 工具级全部指标」",
                "> 算不出来，以 n/a 记（不是 0）。下表每项后面的 `n=` 才是该项的真实样本量，",
                "> **不要用任务总数当分母。**",
                "",
            ]

    if supervisor:
        lines += ["## 终止原因（Supervisor）", ""]
        term = Counter(r.get("termination") or "n/a" for r in supervisor)
        lines += [
            table(
                ["终止原因", "任务数", "含义"],
                [
                    [k, v, note]
                    for k, v, note in [
                        (k, v, _TERMINATION_NOTE.get(k, ""))
                        for k, v in term.most_common()
                    ]
                ],
            ),
            "",
        ]

        lines += ["## 编排级指标（Supervisor）", ""]
        lines += [_metric_table(supervisor, ORCH_METRICS), ""]
        lines += [f"- {DERIVED_EFFICIENCY}：{_efficiency_stat(supervisor)}", ""]
        # 「观察响应性」只在出现失败轮时可测（runner._observation_responsiveness
        # 无失败轮直接返回 n/a）。不解释一句，读的人会以为指标没实现。
        lines += [
            "> 「观察响应性」只在出现失败轮时可测；当前样本的 observe 全部 completed，",
            "> 故整列为 n/a——这表示「没有失败可测」，不是埋点缺失。",
            "",
        ]

        if len(supervisor) < MIN_SAMPLE:
            lines += [
                f"> 样本仅 {len(supervisor)} 个（< {MIN_SAMPLE}），以上数值只作方向参考，",
                "> 不构成分布结论。要谈「平均重复动作率」，先把样本补到 8 个以上。",
                "",
            ]

        tool_metrics = [m for m in TOOL_METRICS]
        if any(r.get("tool_calls") is not None for r in supervisor):
            lines += ["## 工具级指标（Supervisor）", ""]
            lines += [_metric_table(supervisor, tool_metrics), ""]

    # 检索侧对 Pipeline 也有值（retrieval_snapshot 与 route 无关），两组都算。
    if any(r.get("retrieval_cells") is not None for r in rows):
        lines += ["## 检索侧指标（全部任务）", ""]
        lines += [_metric_table(rows, RETRIEVAL_METRICS), ""]

        funnel_vals = {
            field: [r.get(field) for r in rows if r.get(field) is not None]
            for field, _ in FUNNEL_METRICS
        }
        if any(funnel_vals.values()):
            lines += ["### 采集漏斗（均值）", ""]
            lines += [
                table(
                    ["环节", "均值"],
                    [[name, _mean_line(funnel_vals[field])] for field, name in FUNNEL_METRICS],
                ),
                "",
            ]

    lines += [
        "## 口径（读之前先看）",
        "",
        "1. **`—` 不等于 0**：`—` 是「算不出来」（无埋点 / 任务没走到该阶段），"
        "`0` 才是「确实发生了 0 次」。把两者混读，会把采集缺陷看成系统正常。",
        "2. **`n=` 才是分母**：每项指标后面的 `n` 是该指标的真实样本量，"
        "不同指标之间不必相同，也不等于任务总数。",
        "3. **Pipeline 不进过程指标**：它的路径在编译期就确定了，"
        "评它的轨迹等于评代码；`termination` 恒为 n/a 是设计如此，不是缺失。",
        "4. **本轮是「按表重建」而非「追加一轮」**：没有 run 目录、没有 Δ 列。"
        "要比较两版，先 `--persist` 覆盖再跑一次，或用 Grafana 看时间序列。",
        "5. **小样本不谈分布**：Supervisor 组样本 < 8 时，任何「均值」都只是几个点的算术平均。",
        "",
    ]
    return "\n".join(lines)


_TERMINATION_NOTE = {
    "finish": "LLM 主动决策结束",
    "max_rounds": "轮次耗尽（疑似路径震荡）",
    "json_error": "决策解析降级后安全退出",
    "incomplete": "轨迹中断（进程被杀 / 还在跑）",
    "n/a": "无 think 日志",
}


def write_overview() -> str:
    """重建常驻总览，返回相对路径。"""
    rows, counts = asyncio.run(fetch_metrics())
    path = reports_dir("trajectory") / SUMMARY_MD
    return save(path, build_overview(rows, counts))


def main() -> None:
    print(f"已重建：{write_overview()}")


if __name__ == "__main__":
    main()
