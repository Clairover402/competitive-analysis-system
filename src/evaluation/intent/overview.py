# -*- coding: utf-8 -*-
"""意图评估的人读总览：把一批机器产物渲染成 `SUMMARY.md`。

【职责】
  做   读 `trend.json`（指标序列）+ 最新一轮 `runs/<ts>/run.json`，渲染常驻总览。
  不做 跑分（runner.py）、导出数据集（golden.py）、写每轮 summary.md（也由 runner 落）。

【关键决策】
  · 常驻总览**重建**而非追加 ← 与 ragas 的 SUMMARY.md 同一策略：幂等。
    删掉某个 run 目录、手改坏了，跑一次就自动对齐。
  · 顶部只放四件事：规模、主指标、分层、与上一轮之差。混淆矩阵和错例留在
    每轮 `summary.md` —— 常驻总览回答「现在什么水平」，不回答「这轮错在哪」。
  · ★ 口径块是硬要求，不是客套 ← 主指标有 ±2 条（±1.4 点）的跑动噪声
    （2026-10-29 实测：同批 139 条连跑两次，净 -2 条，全在 L3）。不写这句，
    下一个人必然盯着总准确率调参，把随机波动当收益。
  · 单列「计入条数」← run 的 total 是 149，主指标只算剔除边界句后的 n。
    两个数不并排放，读的人会以为 149 条全对或全错。

【读法】
  产物 = `evaluation_reports/intent/SUMMARY.md`
  单独重刷 = `python -m src.evaluation.intent.overview`
"""

from __future__ import annotations

import json
from pathlib import Path

from src.evaluation.common.paths import relative
from src.evaluation.common.report import table
from src.evaluation.intent.golden import GOLDEN_DIR, RUN_DIR, collect_trend

SUMMARY_MD = "SUMMARY.md"

# 噪声带（点数，= 百分比）。低于它的差异一律不当作收益 —— 见模块头第 3 条。
NOISE_POINTS = 3.0


def _pct(v: float | None) -> str:
    return f"{v:.1%}" if isinstance(v, (int, float)) else "—"


def _miss(n, correct) -> str:
    """错几条。`_acc` 只给 n / correct / accuracy，没有现成的错数字段。"""
    if not isinstance(n, int) or not isinstance(correct, int):
        return "—"
    return str(n - correct) if n - correct else "0"


def _latest_run() -> dict | None:
    """最新一轮的明细。

    ⚠️ 按 run.json 里的 `ts` 选，不按目录名 —— 目录名是给人看的，格式一旦变过
    （2026-10-30 迁移时旧目录是 `20261030085846`、新的是 `20261030-085846`，
    `-` 的字典序小于数字），按名字排序就会把旧轮当成最新轮。**不报错，只答错。**
    """
    best: dict | None = None
    for path in sorted(RUN_DIR.glob("*/run.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if best is None or (payload.get("ts") or "") > (best.get("ts") or ""):
            best = payload
    return best


def build_overview(rows: list[dict], run: dict | None) -> str:
    """渲染 SUMMARY.md。`rows` 来自 collect_trend（升序），`run` 是最新一轮明细。"""
    lines = [
        "# 意图识别评估总览",
        "",
        "> 常驻总览，每次跑分自动重建；事实源是 `runs/*/run.json` 与 `trend.json`。",
        "> ⚠️ 本目录全部产物**不入库**（见 .gitignore），clone 下来是空的。",
        "",
    ]

    if not rows:
        lines += ["（还没有评估记录）", ""]
        return "\n".join(lines)

    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    # ★ 只有口径相同才给 Δ：L3 开关一变，分母和层分布全变（本轮 25 条 vs 上轮 139 条），
    #   这种「提升」纯粹是比较方式造成的。宁可空着，也不能给一个假的方向。
    same_setup = prev is not None and prev.get("l3_enabled") == last.get("l3_enabled")
    if same_setup:
        compare = "　".join([
            f"Δ准确率 {_delta_points(last.get('accuracy'), prev.get('accuracy'))}",
            f"Δ接管率 {_delta_points(last.get('early_exit_rate'), prev.get('early_exit_rate'))}",
        ])
    elif prev is None:
        compare = "—（首轮，无对照）"
    else:
        compare = "—（上一轮 L3 开关不同，分母不可比）"

    lines += [
        "## 最新一轮",
        "",
        f"- 时间：`{last['ts']}`　L3：{'开' if last.get('l3_enabled') else '关'}",
        f"- 主指标：**{_pct(last.get('accuracy'))}**"
        f"（计入 {last.get('n')} 条 / 共 {last.get('total')} 条）",
        "- 分层：" + "　".join(
            f"{k} {_pct(last.get(k))}" for k in ("L2", "L3", "L4")
        ) + f"　零 LLM 接管 {_pct(last.get('early_exit_rate'))}",
        f"- 与上一轮：{compare}",
        "",
    ]

    if run:
        per = run.get("per_intent") or {}
        if per:
            lines += [
                "### 各意图召回",
                "",
                table(
                    ["意图", "样本", "准确率", "错"],
                    [
                        [
                            k,
                            v.get("n", "—"),
                            _pct(v.get("accuracy")),
                            _miss(v.get("n"), v.get("correct")),
                        ]
                        for k, v in per.items()
                    ],
                ),
                "",
            ]
        errors = run.get("errors") or []
        if errors:
            lines += [f"错例 {len(errors)} 条（明细见该轮 summary.md）：", ""]
            for r in errors[:5]:
                lines.append(f"- `{r['gold']}→{r['pred']}`（{r['layer']}）{r['text']}")
            if len(errors) > 5:
                lines.append(f"- …另有 {len(errors) - 5} 条")
            lines.append("")

    lines += [
        "## 口径（读之前先看）",
        "",
        f"1. **差异 < {NOISE_POINTS:g} 点不作数**：主指标有 ±2 条（±1.4 点）跑动噪声，"
        "同批数据连跑两次就会翻几条，全在 L3。要证明收益，看结构性事实"
        "（如「margin≥0.15 的样本零错误」），不要看总准确率的小数点。",
        "2. **接管率 ≠ 准确率**：L2 接管是「零 LLM 判完且过阈值」的比例，"
        "层对了不代表意图对；准确率才是结果。",
        "3. **`CASES` 与 `holdout` 不可合流**：前者是改 prompt 时参照的题库，"
        "后者只在最后用一次。在同一批上量出的提升只说明「修了那几条错例」。",
        "4. **`--no-l3` 的轮次不可与全量轮次横比**：L3 关掉时下游全归 L2/L4，"
        "分母和层分布都变了。",
        "",
        "## 历史趋势",
        "",
    ]
    lines.append(
        table(
            ["时间", "准确率", "计入", "L2", "L3", "L4", "接管率", "analysis", "lookup", "trace", "unknown", "L3"],
            [
                [
                    r["ts"][:16],
                    _pct(r.get("accuracy")),
                    r.get("n"),
                    _pct(r.get("L2")),
                    _pct(r.get("L3")),
                    _pct(r.get("L4")),
                    _pct(r.get("early_exit_rate")),
                    _pct((r.get("recall") or {}).get("analysis")),
                    _pct((r.get("recall") or {}).get("lookup")),
                    _pct((r.get("recall") or {}).get("trace")),
                    _pct((r.get("recall") or {}).get("unknown")),
                    "开" if r.get("l3_enabled") else "关",
                ]
                for r in rows
            ],
        )
    )
    lines.append("")
    return "\n".join(lines)


def _delta_points(now: float | None, prev: float | None) -> str:
    """差值以「点」表述（0.012 → +1.2 点）。百分比之差用小数更容易被读错。"""
    if not isinstance(now, (int, float)) or not isinstance(prev, (int, float)):
        return "—"
    return f"{(now - prev) * 100:+.1f} 点"


def write_overview() -> Path:
    """重建常驻总览。跑分结束时由 runner 调用，也可单独跑。"""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / SUMMARY_MD
    path.write_text(build_overview(collect_trend(), _latest_run()), encoding="utf-8", newline="\n")
    return path


def main() -> None:
    path = write_overview()
    print(f"已重建：{relative(path)}")


if __name__ == "__main__":
    main()
