# -*- coding: utf-8 -*-
"""发布门禁 —— 用一段确定性代码回答「这套代码今天能不能交」。

【职责】
  做   ① 读库内报告，算三条规则判据（结构 / 出处 / 数值冲突）；
       ② 读四类评估的**机器可读产物**，按阈值判 pass/fail；
       ③ 一条命令打印两段 + 结论 + 退出码（0 全过 / 1 有红 / 2 环境错）。
  不做 重跑评估（RAGAS 一轮几十次 LLM，塞进来门禁就会慢到没人愿意跑，
       而跑得慢的门禁一定会被跳过）；不做 LLM 打分（同一模型既当运动员又当裁判）。

【关键决策】
  · **SKIP 判失败**，不判通过 ← 「没数据」不等于「没问题」，跟项目里
    `n/a ≠ 0` 是同一条纪律。缺证据时默认答案是不信任。
  · **规则判据 > 模型评分** ← Quality Agent 的五维分不可复算：同一份报告
    今天 87、明天 83。会漂的指标不能当门禁（见 gate_rules 模块头）。
  · **Verified Success 替代完成率** ← `completed` 只说明代码没抛异常，不说明
    报告合格。任务级三档（verified / failed / unverifiable），unverifiable 是
    「参数缺失、规则判不了」，**留在分母里但单列** —— 证明不了合格就不算合格。
  · **阈值外置到 config/eval_thresholds.yaml** ← 改阈值不该改代码，否则每次
    调阈值都要过 code review，最后没人敢调，阈值就烂在代码里。
  · **部分运行的产物不计入** ← intent 的 `--no-l3` 产物只判了 24/149 条，
    拿它报 100% 是自欺。门禁只认「口径完整」的那一轮，并把它跳过了哪一轮打出来。
  · **产物过期 = 无证据** ← 三个月没跑过评估，比一份看起来还行的旧报告更该报警。
  · 指标型**只读产物**，不重跑 ← 见「不做」。

【读法】
  python -m src.evaluation.gate                 # 全量门禁，看退出码
  python -m src.evaluation.gate --inject        # 确定性用例（工具失败注入 + 规则样本）
  python -m src.evaluation.gate --json          # 机器读
  ⚠️ 退出码：0 全 PASS；1 有 FAIL 或 SKIP；2 环境错（连不上库等）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.evaluation.common.paths import REPORTS_ROOT, relative
from src.evaluation.gate_rules import (
    UNVERIFIABLE,
    VERIFIED,
    check_conflicts,
    check_source_coverage,
    check_structure,
    classify_report,
)

# 缺省阈值。本文件是唯一权威，yaml 只做覆盖 —— 理由见模块头第三条。
DEFAULT_THRESHOLDS: dict[str, float] = {
    "structure_complete": 1.0,
    "source_coverage": 0.90,
    "conflicts_max": 0,
    # 任务级 Verified Success：分母是窗口内**全部任务**，不是「有报告的任务」——
    # 后者会把「压根没写出报告」的那几条从分母里漏掉，成功率凭空变高。
    "verified_success_rate": 0.80,
    "intent_accuracy": 0.95,
    "holdout_accuracy": 0.90,
    "retrieval_faithfulness": 0.85,
    "memory_supersede_recall": 0.90,
    "memory_supersede_precision": 0.90,
    "fetch_fail_rate_max": 0.60,
    "artifact_max_age_days": 30,
    "report_window_days": 30,
}

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Check:
    key: str
    label: str
    kind: str                 # rule / metric
    status: str
    value: float | None
    threshold: float
    n: int
    detail: str = ""          # 红/黄的那句话：指向具体位置或给出重跑命令
    as_percent: bool = True   # False = 计数型（冲突数）
    at_most: bool = False     # True = 越小越好

    def render_value(self) -> str:
        if self.value is None:
            return "—"
        return f"{self.value:.1%}" if self.as_percent else f"{self.value:g}"

    def render_threshold(self) -> str:
        op = "≤" if self.at_most else "≥"
        body = f"{self.threshold:.0%}" if self.as_percent else f"{self.threshold:g}"
        return f"{op} {body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "kind": self.kind,
            "status": self.status, "value": self.value,
            "threshold": self.threshold, "n": self.n, "detail": self.detail,
        }


# ═════════════════════════════════════════════════════════════════════════════
# §1 阈值装载
# ═════════════════════════════════════════════════════════════════════════════


def load_thresholds(path: Path | None = None) -> dict[str, float]:
    """读 yaml 覆盖缺省值。文件不存在 → 静默用缺省（门禁不能因为没配置文件就跑不了）。

    ⚠️ 未知 key 直接报错而不是忽略：写错一个词就等于那条阈值**没生效**，
    而输出上完全看不出来 —— 这类静默失效正是门禁最该防的。
    """
    merged = dict(DEFAULT_THRESHOLDS)
    if path is None:
        path = REPORTS_ROOT.parent / "config" / "eval_thresholds.yaml"
    if not path.exists():
        return merged
    import yaml  # 局部导入：没装 pyyaml 时仍能用缺省值跑

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = set(data) - set(DEFAULT_THRESHOLDS)
    if unknown:
        raise ValueError(f"{relative(path)} 里有未知阈值项：{sorted(unknown)}")
    for key, value in data.items():
        merged[key] = float(value)
    return merged


# ═════════════════════════════════════════════════════════════════════════════
# §2 数据源：库内报告 + 轨迹指标（一次连接，两个 async 查询）
# ═════════════════════════════════════════════════════════════════════════════

_REPORTS_SQL = """
WITH latest AS (
    -- 同一任务重写三次只算一份：取最新 version（并列取最新时间）
    SELECT DISTINCT ON (task_id) id, task_id, content, version, created_at
    FROM reports
    ORDER BY task_id, version DESC, created_at DESC
)
SELECT l.id::text          AS report_id,
       l.task_id::text     AS task_id,
       l.content           AS content,
       l.version           AS version,
       l.created_at        AS created_at,
       t.title             AS title,
       t.competitors       AS competitors,
       t.dimensions        AS dimensions
FROM latest l
JOIN tasks t ON t.id = l.task_id
WHERE l.created_at >= now() - make_interval(days => $1)
ORDER BY l.created_at DESC
"""

_TRAJECTORY_SQL = """
SELECT count(*)                                  AS n,
       avg(fetch_fail_rate)                      AS fetch_fail_rate,
       max(computed_at)                          AS newest
FROM trajectory_metrics
WHERE computed_at >= now() - make_interval(days => $1)
  AND fetch_fail_rate IS NOT NULL
"""

# Verified Success 的分母。★ 必须单独数：_REPORTS_SQL 是 INNER JOIN reports，
# 压根没写出报告的任务根本不会出现在那份结果里 —— 用 len(reports) 当分母，
# 等于把「没交付」从成功率里删掉，越失败数字越好看。
_TASKS_SQL = """
SELECT count(*) AS n
FROM tasks
WHERE created_at >= now() - make_interval(days => $1)
"""


def _json_list(value: Any) -> list[str]:
    """JSONB 列在 asyncpg 里可能是 str（未注册 codec）也可能是 list。"""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


async def fetch_sources(window_days: int) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """返回 (报告行, 轨迹聚合, 窗口内任务总数)。任务总数供 Verified Success 做分母。"""
    from src.config import Settings
    from src.db.connection import close_pool, create_pool

    pool = await create_pool(Settings())
    try:
        reports = [dict(r) for r in await pool.fetch(_REPORTS_SQL, window_days)]
        traj = dict(await pool.fetchrow(_TRAJECTORY_SQL, window_days))
        task_total = await pool.fetchval(_TASKS_SQL, window_days) or 0
    finally:
        await close_pool()
    return reports, traj, int(task_total)


# ═════════════════════════════════════════════════════════════════════════════
# §3 规则型三项
# ═════════════════════════════════════════════════════════════════════════════


def _ambiguous_hint(ambiguous: int, notes: list[str]) -> str:
    """冲突为 0 时，把「规则判不了、需要人看」的那几处带出来。

    ★ 不并进 conflicts：这些多半是「生态应用 A 4280/年、B 4900/年」这种
      多实体清单，记成冲突就是误报。但也不能不说 —— 不说就又回到
      「静默吞掉」的老路（2026-11-01 复核：8 份真实报告里 3 处都属这一类）。
    """
    if not ambiguous:
        return ""
    # 一行放不下三条完整句子：去掉「（同段并列，判不出…）」这类注解，只留定位。
    shown = "；".join(n.split("（")[0][:52] for n in notes[:2])
    return f"另有 {ambiguous} 处同段并列待人工确认：{shown}" + ("…" if ambiguous > 2 else "")


def rule_checks(reports: list[dict[str, Any]], th: dict[str, float],
                no_report: int = 0) -> list[Check]:
    """把 n 份报告聚成四条判据。n=0 → 前三条 SKIP（空库不能算通过）。

    `no_report` = 窗口内**没有最新版报告**的任务数（连库时由 fetch_sources 给，
    纯函数测试里默认 0）。它只进 Verified Success 的分母，不进 R1/R2/R3 ——
    那三条的语义是「报告写得怎么样」，没报告就没有可评的对象。
    """
    label_verified = "Verified Success 率"
    if not reports:
        note = "库内没有统计窗口内的报告，先跑一批任务再看"
        # ⚠️ 有任务但一条报告都没写出来 ≠ 无证据：这是确定的「一单没成」。
        #   判 SKIP 会让「全灭」和「还没跑」在输出上长得一模一样。
        if no_report:
            vs = Check("verified_success_rate", label_verified, "rule", FAIL, 0.0,
                       th["verified_success_rate"], no_report,
                       f"{no_report} 条任务均未产出报告")
        else:
            vs = Check("verified_success_rate", label_verified, "rule", SKIP, None,
                       th["verified_success_rate"], 0, note)
        return [
            Check("structure_complete", "报告结构完整率", "rule", SKIP, None,
                  th["structure_complete"], 0, note),
            Check("source_coverage", "出处覆盖率", "rule", SKIP, None,
                  th["source_coverage"], 0, note),
            Check("conflicts_max", "数值冲突", "rule", SKIP, None,
                  th["conflicts_max"], 0, note, as_percent=False, at_most=True),
            vs,
        ]

    cells_ok = cells_total = 0
    src_ok = src_total = 0
    conflicts = 0
    ambiguous = 0
    conflict_notes: list[str] = []
    ambiguous_notes: list[str] = []
    struct_gaps: list[str] = []
    src_gaps: list[str] = []

    verified = unverifiable = 0
    failed_notes: list[str] = []

    for row in reports:
        competitors = _json_list(row["competitors"])
        dimensions = _json_list(row["dimensions"])
        content = row["content"] or ""
        title = (row["title"] or "")[:18]

        # 任务级结论单独判一次（内部重跑三条规则）：宁可多扫一遍文本，
        # 也不在这里拼第二套判据 —— 两份判据迟早会分叉，而分叉时没有任何报错。
        verdict = classify_report(content, competitors, dimensions)
        if verdict.state == VERIFIED:
            verified += 1
        elif verdict.state == UNVERIFIABLE:
            unverifiable += 1
        else:
            failed_notes.append(f"{title}：{verdict.reason}")

        s1 = check_structure(content, competitors, dimensions)
        cells_ok += s1.ok
        cells_total += s1.total
        struct_gaps += [f"{title}：{x}" for x in s1.notes[:2]]

        s2 = check_source_coverage(content)
        src_ok += s2.ok
        src_total += s2.total
        src_gaps += [f"{title}：{x}" for x in s2.notes[:2]]

        s3 = check_conflicts(content, competitors)
        conflicts += s3.conflicts
        ambiguous += s3.ambiguous
        conflict_notes += [f"{title}：{x}" for x in s3.details[:2]]
        ambiguous_notes += [f"{title}：{x}" for x in s3.ambiguous_details[:1]]

    def ratio(ok: int, total: int) -> float | None:
        return ok / total if total else None

    def status_of(value: float | None, threshold: float, at_most: bool = False) -> str:
        if value is None:
            return SKIP
        return PASS if (value <= threshold if at_most else value >= threshold) else FAIL

    v1 = ratio(cells_ok, cells_total)
    v2 = ratio(src_ok, src_total)

    # ── 任务级 Verified Success ──
    # 分母含「无从判」与「无报告」：证明不了合格就不算合格（同 SKIP 判失败）。
    # 但 detail 必须把可验证口径一起打出来 —— 否则 12 条判不了会把 80% 的阈值
    # 压成一条永远修不动的红，读者也看不出「低是因为判不了，不是因为写得差」。
    denominator = len(reports) + no_report
    v4 = ratio(verified, denominator)
    checkable = verified + (len(reports) - verified - unverifiable)
    detail_parts: list[str] = []
    if failed_notes:
        detail_parts.append("未过：" + "；".join(failed_notes[:2]))
    if unverifiable:
        detail_parts.append(f"另有 {unverifiable} 条无从判（竞品/维度为空，属评估链路缺口）")
    if no_report:
        detail_parts.append(f"{no_report} 条无报告")
    if checkable and checkable < denominator:
        detail_parts.append(f"可验证口径 {verified}/{checkable}={verified / checkable:.0%}")

    return [
        Check(
            "structure_complete", "报告结构完整率", "rule",
            status_of(v1, th["structure_complete"]), v1,
            th["structure_complete"], len(reports),
            "；".join(struct_gaps[:4]),
        ),
        Check(
            "source_coverage", "出处覆盖率", "rule",
            status_of(v2, th["source_coverage"]), v2,
            th["source_coverage"], len(reports),
            "；".join(src_gaps[:4]) or ("无「逐维度深度分析」小节" if v2 is None else ""),
        ),
        Check(
            "conflicts_max", "数值冲突", "rule",
            status_of(float(conflicts), th["conflicts_max"], at_most=True), float(conflicts),
            th["conflicts_max"], len(reports),
            "；".join(conflict_notes[:4]) or _ambiguous_hint(ambiguous, ambiguous_notes),
            as_percent=False, at_most=True,
        ),
        Check(
            "verified_success_rate", "Verified Success 率", "rule",
            status_of(v4, th["verified_success_rate"]), v4,
            th["verified_success_rate"], denominator,
            "；".join(detail_parts),
        ),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# §4 指标型：读产物
# ═════════════════════════════════════════════════════════════════════════════


def _runs(directory: Path) -> list[Path]:
    """`runs/<ts>/` 按时间倒序。目录名就是时间戳，不用读文件。"""
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if p.is_dir()), reverse=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _age_days(stamp: str | None, fallback: Path) -> float:
    """产物多久之前的（天）。优先 run.json 自带 ts，退化为目录名，再退化为 mtime。

    ⚠️ 三档退化不是周全，是必要：四类产物的时间戳来源各不相同
    （intent 写 ts、ragas 写 finished_at、memory 只有目录名）。
    统一成一个函数，别在四处各写一遍 strptime。
    """
    # (格式, 该格式渲染出来的字符串长度) —— 用 len(fmt) 现算会错，
    # 因为 `%Y` 是 2 个字符但渲染 4 位。
    layouts = (("%Y-%m-%dT%H:%M:%S", 19), ("%Y%m%d-%H%M%S", 15), ("%Y%m%d-%H%M", 13))
    for text in (stamp, fallback.name):
        if not text:
            continue
        for fmt, size in layouts:
            try:
                return (datetime.now() - datetime.strptime(text[:size], fmt)).total_seconds() / 86400
            except ValueError:
                continue
    try:
        return (datetime.now() - datetime.fromtimestamp(fallback.stat().st_mtime)).total_seconds() / 86400
    except OSError:
        return float("inf")


def _stale_check(key: str, label: str, th: dict[str, float], max_age: float,
                 n: int, where: str) -> Check:
    return Check(
        key, label, "metric", SKIP, None, th.get(key, 0), n,
        f"产物 {max_age:.0f} 天前（阈值 {th['artifact_max_age_days']:.0f} 天），视为无证据：{where}",
    )


def _metric(key: str, label: str, th: dict[str, float], value: float | None,
            n: int, detail: str = "", at_most: bool = False) -> Check:
    threshold = th.get(key, 0.0)
    if value is None or n == 0:
        return Check(key, label, "metric", SKIP, None, threshold, n, detail or "没有可用样本")
    ok = value <= threshold if at_most else value >= threshold
    return Check(key, label, "metric", PASS if ok else FAIL, value, threshold, n, detail,
                 at_most=at_most)


def check_intent(th: dict[str, float]) -> Check:
    """意图准确率：只认**完整漏斗**的那一轮。

    ⚠️ `--no-l3` 的产物 n=24/149，它的 100% 不是「意图识别 100%」，
       是「L2 能接走的那 24 条全对」。拿它进门禁等于自己骗自己。
    """
    label = "意图准确率"
    runs = _runs(REPORTS_ROOT / "intent" / "runs")
    skipped: list[str] = []
    for run in runs:
        data = _read_json(run / "run.json")
        if data is None:
            continue
        if not data.get("l3_enabled"):
            skipped.append(run.name)
            continue
        main = data.get("main") or {}
        n = int(main.get("n") or 0)
        if n == 0:
            continue
        age = _age_days(data.get("ts"), run)
        if age > th["artifact_max_age_days"]:
            return _stale_check("intent_accuracy", label, th, age, n, relative(run))
        detail = f"产物 {age:.0f} 天前 · {run.name}"
        if skipped:
            detail += f" · 跳过 {len(skipped)} 轮非完整漏斗（{skipped[0]} 为 --no-l3）"
        return _metric("intent_accuracy", label, th, float(main["accuracy"]), n, detail)
    return Check("intent_accuracy", label, "metric", SKIP, None, th["intent_accuracy"], 0,
                 "没有完整漏斗的意图评估产物：python -m src.evaluation.intent.runner")


def check_holdout(th: dict[str, float]) -> Check:
    """验收集准确率：24 句不在 CASES 里的新说法，防「照着错例改 prompt」。"""
    label = "holdout 准确率"
    runs = _runs(REPORTS_ROOT / "intent" / "holdout")
    for run in runs:
        data = _read_json(run / "run.json")
        if data is None or not data.get("total"):
            continue
        age = _age_days(data.get("ts"), run)
        n = int(data["total"])
        if age > th["artifact_max_age_days"]:
            return _stale_check("holdout_accuracy", label, th, age, n, relative(run))
        detail = f"产物 {age:.0f} 天前 · {run.name}"
        misses = data.get("misses") or []
        if misses:
            detail += f" · 错 {len(misses)} 条"
        return _metric("holdout_accuracy", label, th, float(data["accuracy"]), n, detail)
    return Check("holdout_accuracy", label, "metric", SKIP, None, th["holdout_accuracy"], 0,
                 "没有验收集产物：python -m src.evaluation.intent.holdout")


def check_retrieval(th: dict[str, float]) -> Check:
    """检索侧只门禁 faithfulness（有没有编造），不门禁 context_recall。

    ★ 这是刻意的：recall 的口径依赖 golden reference 的粒度（我写参考时就写了
      3 个要点，检索捞到 2 个就是 0.67），它适合当**观察指标**看趋势，
      拿来当门禁就得先证明「参考粒度和真实需求对齐」——这一步我没做。
      faithfulness 不一样：答案是「从上下文推出来的」还是「编的」，
      RAGAS 的判定口径单侧明确，低于阈值就是有编造。
    """
    label = "检索忠实度"
    runs = _runs(REPORTS_ROOT / "ragas" / "runs")
    for run in runs:
        data = _read_json(run / "run.json")
        if data is None:
            continue
        cases = data.get("cases") or {}
        scores = [
            float(c["aggregates"]["faithfulness"])
            for c in cases.values()
            if isinstance(c.get("aggregates"), dict) and "faithfulness" in c["aggregates"]
        ]
        if not scores:
            continue
        manifest = data.get("manifest") or {}
        age = _age_days(manifest.get("finished_at") or manifest.get("started_at"), run)
        if age > th["artifact_max_age_days"]:
            return _stale_check("retrieval_faithfulness", label, th, age, len(scores),
                                relative(run))
        value = sum(scores) / len(scores)
        return _metric("retrieval_faithfulness", label, th, value, len(scores),
                       f"产物 {age:.0f} 天前 · {run.name} · 逐 case 均值")
    return Check("retrieval_faithfulness", label, "metric", SKIP, None,
                 th["retrieval_faithfulness"], 0,
                 "没有 RAGAS 产物：python -m src.evaluation.ragas.runner")


def check_memory(th: dict[str, float]) -> list[Check]:
    """长期记忆取代：召回（该顶掉的顶掉了吗）与精确（不该顶掉的顶掉了吗）。

    ★ 两面都必须判。只判召回时，「专业版/企业版」两条并存被误合成一条照样
      满分 —— 而误合并丢掉是一条真信息，比漏取代更贵。
      2026-11-01 复核：首跑只读了 recall（80% 红），同轮 precision 66.7%
      **根本没进判定口径**，等于红灯的由来说不准（那条 80% 完全由「论断反转」
      一条错例造成，误合并没被看见）。补上精确一侧后两盏灯各说各的。
    """
    sides = (
        ("memory_supersede_recall", "记忆取代召回", "supersede_recall"),
        ("memory_supersede_precision", "记忆取代精确", "supersede_precision"),
    )
    for run in _runs(REPORTS_ROOT / "memory" / "runs"):
        data = _read_json(run / "run.json")
        if data is None:
            continue
        if not int((data.get("supersede_recall") or {}).get("n") or 0):
            continue
        age = _age_days(None, run)
        out: list[Check] = []
        for key, label, field in sides:
            stat = data.get(field) or {}
            n = int(stat.get("n") or 0)
            if not n:
                out.append(Check(
                    key, label, "metric", SKIP, None, th[key], 0,
                    f"{run.name} 这一轮没有 {field}（老产物，重跑 memory.runner）",
                ))
                continue
            if age > th["artifact_max_age_days"]:
                out.append(_stale_check(key, label, th, age, n, relative(run)))
                continue
            wrong = int(stat.get("false_merge") or 0)
            out.append(_metric(
                key, label, th, int(stat.get("correct") or 0) / n, n,
                f"产物 {age:.0f} 天前 · {run.name} · n={n}（粒度 {100 / n:.0f}%）"
                + (f" · 误合并 {wrong}" if wrong else ""),
            ))
        return out

    return [
        Check(key, label, "metric", SKIP, None, th[key], 0,
              "没有记忆评估产物：python -m src.evaluation.memory.runner")
        for key, label, _field in sides
    ]


def check_trajectory(traj: dict[str, Any], th: dict[str, float]) -> Check:
    """抓取失败率：读 `trajectory_metrics` 表，不读文件。

    ⚠️ 与计划文档的一处偏差：轨迹评估的机读产物在**库里**（`--persist` 写
       `trajectory_metrics`），`evaluation_reports/trajectory/` 下只有人写的
       SUMMARY.md。门禁去查文件只能永远 SKIP，所以改读表。
    """
    label = "抓取失败率"
    n = int(traj.get("n") or 0)
    if n == 0:
        return Check("fetch_fail_rate_max", label, "metric", SKIP, None,
                     th["fetch_fail_rate_max"], 0,
                     "库内无轨迹指标：python -m src.evaluation.trajectory.runner --since 30d --persist",
                     at_most=True)
    newest = traj.get("newest")
    age = (datetime.now(newest.tzinfo) - newest).total_seconds() / 86400 if newest else 0.0
    if age > th["artifact_max_age_days"]:
        return _stale_check("fetch_fail_rate_max", label, th, age, n, "trajectory_metrics")
    return _metric("fetch_fail_rate_max", label, th, float(traj["fetch_fail_rate"]), n,
                   f"库内 {n} 个任务 · 最近 {age:.0f} 天前计算", at_most=True)


# ═════════════════════════════════════════════════════════════════════════════
# §5 渲染
# ═════════════════════════════════════════════════════════════════════════════


def _width(text: str) -> int:
    """终端显示宽度：CJK 全角算 2。对不齐的表格没人看。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def render(checks: list[Check], *, reports: int, window_days: int) -> str:
    lines = [f"=== 发布门禁 · {datetime.now():%Y-%m-%d %H:%M} ==="]
    lines.append(f"库内报告 {reports} 份（近 {window_days:.0f} 天，每任务取最新版本）")
    lines.append("")

    # 分组标题里不再带「Verified Success」：它现在是自己那一行的指标名，
    # 标题和条目同名会让人以为只有一项。
    for kind, header in (("rule", "规则型（可复算）"), ("metric", "指标型")):
        group = [c for c in checks if c.kind == kind]
        if not group:
            continue
        lines.append(header)
        for c in group:
            # 指标型即使 PASS 也把「产物几天前 / 读的哪一轮」打出来 ——
            # 「过了」和「用三周前的数据过的」是两件事，后者是 D3 要暴露的东西。
            # 规则型 PASS 也打 detail：R3 的「另有 N 处待人工确认」就挂在这里，
            # 只输出「0 冲突」而不说还剩几处没判，等于把不确定性藏起来。
            show_detail = bool(c.detail)
            lines.append(
                f"  [{c.status}] {_pad(c.label, 20)}"
                f"{c.render_value():>9}   n={c.n:<4} 阈值 {c.render_threshold()}"
                + (f"    ← {c.detail}" if show_detail else "")
            )
        lines.append("")

    failed = [c for c in checks if c.status == FAIL]
    skipped = [c for c in checks if c.status == SKIP]
    if not failed and not skipped:
        lines.append(f"结论：{len(checks)} 项全过 → exit 0")
    else:
        parts = []
        if failed:
            parts.append(f"{len(failed)} 项未通过（{'、'.join(c.label for c in failed)}）")
        if skipped:
            parts.append(f"{len(skipped)} 项无证据（{'、'.join(c.label for c in skipped)}）")
        lines.append(f"结论：{'；'.join(parts)} → exit 1")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# §6 入口
# ═════════════════════════════════════════════════════════════════════════════


def build_checks(reports: list[dict[str, Any]], traj: dict[str, Any],
                 th: dict[str, float], no_report: int = 0) -> list[Check]:
    checks = rule_checks(reports, th, no_report)
    checks += [
        check_intent(th),
        check_holdout(th),
        check_retrieval(th),
        *check_memory(th),
        check_trajectory(traj, th),
    ]
    return checks


def run_gate(args: argparse.Namespace) -> int:
    # 阈值装载必须一起包进来：yaml 写错（未知 key、值不是数字）属于「配置坏了」，
    # 不是「指标没达标」—— 前者 exit 2、后者 exit 1，CI 上要能分开看。
    # ⚠️ 这行原先在 try 之外，实测直接抛 traceback + exit 1（2026-11-01 复核）。
    try:
        th = load_thresholds(Path(args.thresholds) if args.thresholds else None)
        if args.window_days is not None:
            th["report_window_days"] = float(args.window_days)
        if args.max_age_days is not None:
            th["artifact_max_age_days"] = float(args.max_age_days)
    except Exception as exc:  # noqa: BLE001 — 配置错误的唯一职责是给出人话 + exit 2
        print(f"门禁无法运行（阈值配置有问题）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        reports, traj, task_total = asyncio.run(fetch_sources(int(th["report_window_days"])))
    except Exception as exc:  # noqa: BLE001 — 环境错误的唯一职责就是给出人话 + exit 2
        print(f"门禁无法运行（数据库不可用？）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    # 窗口内有任务、却查不到最新版报告 = 这单没交出东西。取 max(...,0) 是因为
    # 两张表的窗口口径不同（任务按 created_at，报告也按 created_at 但只到 task 粒度），
    # 极端情况下会算出负数 —— 宁可少算一条无报告，也不能让分母变小。
    no_report = max(task_total - len(reports), 0)
    checks = build_checks(reports, traj, th, no_report)
    if args.json:
        print(json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"),
             "reports": len(reports), "checks": [c.to_dict() for c in checks]},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render(checks, reports=len(reports), window_days=th["report_window_days"]))

    return 0 if all(c.status == PASS for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布门禁：规则判据 + 评估产物 → exit code")
    parser.add_argument("--thresholds", help="阈值 yaml 路径（默认 config/eval_thresholds.yaml）")
    parser.add_argument("--window-days", type=int, help="覆盖报告统计窗口（验收/复盘用）")
    parser.add_argument("--max-age-days", type=int, help="覆盖产物时效上限（验收/复盘用）")
    parser.add_argument("--inject", action="store_true", help="跑确定性用例（工具失败注入 + 规则样本）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    if args.inject:
        from src.evaluation import gate_cases

        return gate_cases.run_all()
    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
