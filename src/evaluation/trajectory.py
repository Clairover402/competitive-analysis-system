"""Agent 轨迹评估 —— 从 agent_logs 重建决策路径，算编排级与工具级指标。

【职责】
  做：把某个 task 的 agent_logs 重建成「第几轮想了什么 → 调了谁 → 用什么工具 →
      结果如何」的轨迹，算 L1 编排级 + L2 工具级指标，输出 Markdown 报告。
  不做：不调 LLM、不建表、不落 run 目录、不改任何业务状态（纯读）。

【输入 → 输出】
  输入：asyncpg 连接池里的 agent_logs（action ∈ think / observe / tool_call / 业务动作）
  输出：stdout 的 Markdown 报告（--json 则输出结构化 dict）
  用法：
      python -m src.evaluation.trajectory --task <task_id>
      python -m src.evaluation.trajectory --since 7d --limit 50

【关键决策】
  ① 只评 Supervisor，不评 Pipeline：Pipeline 四个节点三条固定边，路径编译期确定，
     评轨迹等于评「代码有没有跑对」；Supervisor 是 LLM 动态决策，才有路径好坏。
     Pipeline 任务进来时 L1 全 n/a、只有 L2，报告里会显式说明。
  ② 只做黑盒属性型指标，不做 golden trajectory（无标注、且同一任务允许多条合理路径），
     也不做 LLM judge 评轨迹（贵且不稳，规则能抓的问题先抓干净）。
  ③ 缺数据一律标 n/a 并写明「缺什么」，不当 0 —— 日志是 fire-and-forget，
     「没发生」和「没记上」是两件事（同 run_archive 的 compare 纪律）。
  ④ 阈值不在本模块写死：先跑分布再用 `--since` 批量定，避免拍脑袋。

【读法】
  L1 看「这任务健不健康」，L2 看「卡在哪一步」。L1 说 collector 被调了 2 次，
  L2 才说第 2 次 8 条 URL 里 6 条 403。两者必须配对读，轨迹好也不等于报告好。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Supervisor 的轮次硬上限（supervisor.py 里由 state.max_rounds 决定，默认 10）。
# 日志里不落 max_rounds，这里用同一个默认值；改上限时两处要一起改。
DEFAULT_MAX_ROUNDS = 10

# 各 Agent action 的必填参数（来自 A2A AgentCard input_schema.required）。
# 只用来判「键有没有丢」——参数值是摘要（如 "list(len=3)"），判不了空。
_REQUIRED_ARGS: dict[str, list[str]] = {
    "collector": ["competitors", "dimensions"],
    "analyzer": ["competitors", "dimensions"],
    "writer": ["analysis_results"],
    "quality": ["report_markdown"],
}

# 合法调度顺序：后面的 Agent 必须见过前面的
_ORDER_PREREQ: dict[str, str] = {
    "analyzer": "collector",
    "writer": "analyzer",
    "quality": "writer",
}


# ── 数据结构 ────────────────────────────────────────────────────────────


@dataclass
class Event:
    """一条原始日志。"""

    ts: datetime
    agent: str
    action: str
    request: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class Trace:
    """重建后的轨迹。四类事件分开存，指标各自消费。"""

    task_id: str
    thinks: list[dict] = field(default_factory=list)      # 编排级决策
    observes: list[dict] = field(default_factory=list)    # 每轮观察
    tools: list[dict] = field(default_factory=list)       # 工具调用
    business: list[Event] = field(default_factory=list)   # Agent 业务动作日志
    raw_count: int = 0

    @property
    def is_supervisor(self) -> bool:
        """有 think 日志才算 Supervisor 任务。"""
        return bool(self.thinks)


# ── 轨迹重建 ────────────────────────────────────────────────────────────


def _as_dict(value: Any) -> dict:
    """agent_logs 的 jsonb 列可能被驱动回成 str（老行/测试桩），统一成 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"_value": parsed}
        except json.JSONDecodeError:
            return {"_raw": value}
    return {}


def _round_of(payload: dict) -> int:
    """取轮次号，缺失记 0（不猜）。"""
    for key in ("round", "current_round"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    return 0


def build_trace(task_id: str, rows: list[dict]) -> Trace:
    """把 agent_logs 行重建成 Trace。纯函数，不碰 DB。

    分类只认 action 字段：
      think / observe / tool_call 是评估自己埋的点，其余全部归 business。
    """
    trace = Trace(task_id=task_id, raw_count=len(rows))

    for row in rows:
        action = row.get("action") or ""
        request = _as_dict(row.get("request"))
        response = _as_dict(row.get("response"))

        if action == "think":
            trace.thinks.append({
                "round": _round_of(request) or _round_of(response),
                "agent": response.get("agent") or response.get("action") or "",
                "action": response.get("action") or "",
                "thought": response.get("thought") or "",
                "reason": response.get("reason") or "",
                "arguments": _as_dict(response.get("arguments")),
                "ts": row.get("created_at"),
            })
        elif action == "observe":
            trace.observes.append({
                "round": _round_of(request),
                "agent": request.get("agent") or "",
                "status": request.get("status") or "",
                "observation": response.get("observation") or "",
                "ts": row.get("created_at"),
            })
        elif action == "tool_call":
            trace.tools.append({
                "tool": request.get("tool") or "",
                "args": _as_dict(request.get("args")),
                "ok": bool(response.get("ok")),
                "kind": response.get("kind"),
                "n": response.get("n"),
                "chars": response.get("chars"),
                "score_top1": response.get("score_top1"),
                "score_median": response.get("score_median"),
                "score_min": response.get("score_min"),
                "error": row.get("error"),
                "duration_ms": row.get("duration_ms"),
                "ts": row.get("created_at"),
            })
        else:
            trace.business.append(Event(
                ts=row.get("created_at"),
                agent=row.get("agent_name") or "",
                action=action,
                request=request,
                response=response,
                error=row.get("error"),
                duration_ms=row.get("duration_ms"),
            ))

    trace.thinks.sort(key=lambda t: (t["round"], str(t["ts"] or "")))
    trace.observes.sort(key=lambda o: (o["round"], str(o["ts"] or "")))
    trace.tools.sort(key=lambda t: str(t["ts"] or ""))
    return trace


# ── L1 编排级指标 ───────────────────────────────────────────────────────


def _metric(value: Any, note: str = "") -> dict:
    return {"value": value, "note": note}


def _termination_reason(trace: Trace) -> dict:
    """判断任务怎么结束的 —— 最强的健康度信号。

    forced_finish（act 节点因重写上限强制结束）目前判不出来：
    act 的返回值不落日志，observe 只能看到 agent='finish' 的空观察。
    要判需要在 act 里补一条日志，属后续增强，这里如实标 n/a。
    """
    if not trace.thinks:
        return _metric("n/a", "无 think 日志（非 Supervisor 任务）")
    last = trace.thinks[-1]
    if last["agent"] == "finish" or last["action"] == "finish":
        return _metric("finish", "LLM 主动决策结束")
    rounds = max(t["round"] for t in trace.thinks)
    if rounds >= DEFAULT_MAX_ROUNDS:
        return _metric("max_rounds", f"轮次耗尽（{rounds}/{DEFAULT_MAX_ROUNDS}）")
    return _metric("incomplete", "既没 finish 也没到上限，轨迹中断（可能进程被杀）")


def _round_efficiency(trace: Trace) -> dict:
    """轮次效率 = 有效轮次 / 总轮次。

    有效 = 该轮有 observe、状态 completed、且 observation 非空
    （finish 轮和失败轮都不产生新业务状态）。
    """
    if not trace.thinks:
        return _metric("n/a", "无 think 日志")

    rounds_used = max(t["round"] for t in trace.thinks)
    effective = {
        o["round"] for o in trace.observes
        if o["status"] == "completed" and o["observation"]
    }
    effective &= {t["round"] for t in trace.thinks}
    ratio = len(effective) / rounds_used if rounds_used else 0.0
    return _metric(
        round(ratio, 3),
        f"有效轮次 {len(effective)} / 总轮次 {rounds_used}",
    )


def _illegal_transitions(trace: Trace) -> dict:
    """违反调度前置条件：analyzer 前没 collector、writer 前没 analyzer 等。"""
    if not trace.thinks:
        return _metric("n/a", "无 think 日志")

    seen: set[str] = set()
    violations: list[str] = []
    for item in trace.thinks:
        agent = item["agent"]
        if not agent or agent == "finish":
            continue
        prereq = _ORDER_PREREQ.get(agent)
        if prereq and prereq not in seen:
            violations.append(f"第{item['round']}轮 {agent} 之前没有 {prereq}")
        seen.add(agent)

    return _metric(len(violations), "；".join(violations) if violations else "无")


def _repeat_actions(trace: Trace) -> dict:
    """连续两轮 (agent, 参数摘要) 完全相同 = 原地打转。"""
    if len(trace.thinks) < 2:
        return _metric("n/a", "决策轮次不足 2")

    repeats = 0
    for prev, cur in zip(trace.thinks, trace.thinks[1:]):
        same_agent = prev["agent"] == cur["agent"]
        same_args = json.dumps(prev["arguments"], sort_keys=True, default=str) == \
            json.dumps(cur["arguments"], sort_keys=True, default=str)
        if same_agent and same_args and cur["agent"] != "finish":
            repeats += 1

    total = len(trace.thinks) - 1
    return _metric(round(repeats / total, 3), f"{repeats} 次连续重复 / {total} 次相邻比较")


def _failed_rounds(trace: Trace) -> dict:
    """失败轮占比。"""
    if not trace.thinks:
        return _metric("n/a", "无 think 日志")
    rounds_used = max(t["round"] for t in trace.thinks)
    failed = sum(1 for o in trace.observes if o["status"] == "failed")
    return _metric(
        round(failed / rounds_used, 3) if rounds_used else 0.0,
        f"失败 {failed} 轮 / 总 {rounds_used} 轮",
    )


def _arg_completeness(trace: Trace) -> dict:
    """必填参数有没有丢键。

    只判键存在：日志里参数是摘要（"list(len=3)"），空列表与有内容无法区分。
    要判空需要把长度也落进摘要，属后续增强。
    """
    if not trace.thinks:
        return _metric("n/a", "无 think 日志")

    checked = 0
    ok = 0
    missing: list[str] = []
    for item in trace.thinks:
        required = _REQUIRED_ARGS.get(item["agent"])
        if not required:
            continue
        checked += 1
        absent = [k for k in required if k not in item["arguments"]]
        if absent:
            missing.append(f"第{item['round']}轮 {item['agent']} 缺 {absent}")
        else:
            ok += 1

    if not checked:
        return _metric("n/a", "没有需要参数的决策轮")
    return _metric(round(ok / checked, 3), "；".join(missing) if missing else f"{ok}/{checked} 轮键齐全")


def _observation_responsiveness(trace: Trace) -> dict:
    """失败之后有没有换策略 —— 「看到失败仍原样重试」是死循环的典型前兆。"""
    by_round = {t["round"]: t for t in trace.thinks}
    failed_rounds = [o["round"] for o in trace.observes if o["status"] == "failed"]
    if not failed_rounds:
        return _metric("n/a", "无失败轮")

    blind_retry = 0
    for rnd in failed_rounds:
        cur = by_round.get(rnd)
        nxt = by_round.get(rnd + 1)
        if cur is None or nxt is None:
            continue
        same_agent = cur["agent"] == nxt["agent"]
        same_args = json.dumps(cur["arguments"], sort_keys=True, default=str) == \
            json.dumps(nxt["arguments"], sort_keys=True, default=str)
        if same_agent and same_args:
            blind_retry += 1

    comparable = len([r for r in failed_rounds if r + 1 in by_round])
    if not comparable:
        return _metric("n/a", "失败轮后面没有下一轮决策")
    return _metric(
        round(blind_retry / comparable, 3),
        f"{blind_retry} 次原样重试 / {comparable} 次可比对",
    )


def l1_metrics(trace: Trace) -> dict[str, dict]:
    """L1 编排级七项。"""
    return {
        "终止原因": _termination_reason(trace),
        "轮次效率": _round_efficiency(trace),
        "非法转移": _illegal_transitions(trace),
        "重复动作率": _repeat_actions(trace),
        "失败轮占比": _failed_rounds(trace),
        "参数完整率": _arg_completeness(trace),
        "观察响应性": _observation_responsiveness(trace),
    }


# ── L2 工具级指标 ───────────────────────────────────────────────────────


def _tools(trace: Trace, name: str) -> list[dict]:
    return [t for t in trace.tools if t["tool"] == name]


def _tool_counts(trace: Trace) -> dict:
    if not trace.tools:
        return _metric("n/a", "无 tool_call 日志（埋点上线前的任务）")
    counter = Counter(t["tool"] for t in trace.tools)
    detail = ", ".join(f"{k}×{v}" for k, v in counter.most_common())
    return _metric(len(trace.tools), detail)


def _empty_search_rate(trace: Trace) -> dict:
    searches = _tools(trace, "web_search")
    if not searches:
        return _metric("n/a", "无 web_search 调用")
    empty = sum(1 for s in searches if (s["n"] or 0) == 0)
    return _metric(round(empty / len(searches), 3), f"{empty} 次空结果 / {len(searches)} 次搜索")


def _fetch_failure_buckets(trace: Trace) -> dict:
    """抓取失败按错误类型分桶 —— 把「2/7 页」这类人工诊断变成可回归的数字。

    ⚠️ 只能分「抛异常」的失败；web_fetch 若以正常返回的形式带回错误
    （如 dict 带 error 键），这里会算成成功。当前 tools_web 是抛异常的，口径成立。
    """
    fetches = _tools(trace, "web_fetch")
    if not fetches:
        return _metric("n/a", "无 web_fetch 调用")

    buckets: Counter[str] = Counter()
    for item in fetches:
        if item["ok"]:
            continue
        err = (item["error"] or "").lower()
        if "timeout" in err:
            buckets["timeout"] += 1
        elif "403" in err:
            buckets["403"] += 1
        elif "ssl" in err:
            buckets["ssl"] += 1
        elif "connect" in err:
            buckets["连接失败"] += 1
        else:
            buckets["其他"] += 1

    fail_rate = sum(buckets.values()) / len(fetches)
    detail = ", ".join(f"{k}×{v}" for k, v in buckets.most_common()) or "无失败"
    return _metric(round(fail_rate, 3), f"{detail}，共 {len(fetches)} 次抓取")


def _duplicate_fetch_rate(trace: Trace) -> dict:
    urls = [t["args"].get("url") for t in _tools(trace, "web_fetch") if t["args"].get("url")]
    if not urls:
        return _metric("n/a", "无 web_fetch 或未记录 url")
    dup = len(urls) - len(set(urls))
    return _metric(round(dup / len(urls), 3), f"{dup} 次重复 / {len(urls)} 次抓取")


def _embedding_calls(trace: Trace) -> dict:
    batch_calls = _tools(trace, "embed_texts")
    query_calls = _tools(trace, "embed_query")
    if not batch_calls and not query_calls:
        return _metric("n/a", "无 embedding 调用")

    sizes = [c["n"] for c in batch_calls if isinstance(c["n"], int)]
    total_chunks = sum(sizes)
    avg = round(statistics.fmean(sizes), 1) if sizes else None
    return _metric(
        len(query_calls) + len(batch_calls),
        f"embed_query {len(query_calls)} 次（应=维度数）、"
        f"embed_texts {len(batch_calls)} 批 / {total_chunks} 条，平均批大小 {avg}",
    )


def _rerank_discrimination(trace: Trace) -> dict:
    """精排区分度：top1 - median 越小，说明分数越挤、越分不开。"""
    reranks = _tools(trace, "rerank")
    if not reranks:
        return _metric("n/a", "无 rerank 调用")
    gaps = [
        r["score_top1"] - r["score_median"]
        for r in reranks
        if isinstance(r["score_top1"], (int, float)) and isinstance(r["score_median"], (int, float))
    ]
    if not gaps:
        return _metric("n/a", "rerank 无分数摘要（旧埋点）")
    return _metric(
        round(statistics.fmean(gaps), 4),
        f"{len(gaps)} 次精排的平均 (top1 - median)；越接近 0 越说明分不开",
    )


def _funnel(trace: Trace) -> dict:
    """采集漏斗：搜索结果 → 抓取次数 → 抓到正文 → 最终 chunk。"""
    searches = _tools(trace, "web_search")
    fetches = _tools(trace, "web_fetch")
    embeds = _tools(trace, "embed_texts")
    if not searches and not fetches and not embeds:
        return _metric("n/a", "无采集链路工具日志")

    search_hits = sum(s["n"] or 0 for s in searches)
    fetched = sum(1 for f in fetches if f["ok"] and (f["chars"] or 0) > 0)
    chunks = sum(e["n"] or 0 for e in embeds)
    return _metric(
        f"{search_hits} → {len(fetches)} → {fetched} → {chunks}",
        "搜索命中条数 → 抓取次数 → 抓到正文页数 → 入库 chunk 数",
    )


def _rerank_filtering(trace: Trace) -> dict:
    """精排有没有真的截断（候选池 ≤ top_k 时只排序不筛，维度区分度会消失）。"""
    reranks = _tools(trace, "rerank")
    if not reranks:
        return _metric("n/a", "无 rerank 调用")
    sizes = [r["n"] for r in reranks if isinstance(r["n"], int)]
    if not sizes:
        return _metric("n/a", "rerank 无返回条数摘要")
    requested = [
        r["args"].get("top_k") for r in reranks if isinstance(r["args"].get("top_k"), int)
    ]
    detail = f"平均返回 {round(statistics.fmean(sizes), 1)} 条"
    if requested:
        detail += f"，请求 top_k 平均 {round(statistics.fmean(requested), 1)}"
        under = sum(1 for s in sizes if requested and s < statistics.fmean(requested))
        if under:
            detail += f"；{under} 次返回不足（候选池不够，精排没起筛选作用）"
    return _metric(len(sizes), detail)


def l2_metrics(trace: Trace) -> dict[str, dict]:
    """L2 工具级指标（能算的 8 项；跨维度重复率、每竞品覆盖度需更多埋点，暂缺）。"""
    return {
        "工具调用次数": _tool_counts(trace),
        "搜索空结果率": _empty_search_rate(trace),
        "抓取失败率": _fetch_failure_buckets(trace),
        "重复抓取率": _duplicate_fetch_rate(trace),
        "embedding 调用": _embedding_calls(trace),
        "rerank 区分度": _rerank_discrimination(trace),
        "精排筛除情况": _rerank_filtering(trace),
        "采集漏斗": _funnel(trace),
    }


# ── 报告渲染 ────────────────────────────────────────────────────────────


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_case(trace: Trace) -> str:
    """单任务报告：先 L1 症状，再 L2 根因，最后一句话结论。"""
    l1 = l1_metrics(trace)
    l2 = l2_metrics(trace)
    lines = [
        f"# 轨迹评估 · {trace.task_id}",
        "",
        f"- 日志条数：{trace.raw_count}（think {len(trace.thinks)} / observe {len(trace.observes)} / "
        f"tool_call {len(trace.tools)} / 业务 {len(trace.business)}）",
        f"- 链路类型：{'Supervisor（LLM 动态决策）' if trace.is_supervisor else 'Pipeline 或非 Supervisor（无编排级轨迹）'}",
        "",
        "## L1 编排级（这任务健不健康）",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
    ]
    for name, item in l1.items():
        lines.append(f"| {name} | {_fmt(item['value'])} | {item['note']} |")

    lines += [
        "",
        "## L2 工具级（卡在哪一步）",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
    ]
    for name, item in l2.items():
        lines.append(f"| {name} | {_fmt(item['value'])} | {item['note']} |")

    anomalies = _anomalies(trace, l1, l2)
    lines += ["", "## 异常明细", ""]
    lines += [f"- {a}" for a in anomalies] if anomalies else ["- 无"]

    lines += ["", "## 结论", "", _verdict(l1, l2, trace), ""]
    return "\n".join(lines)


def _anomalies(trace: Trace, l1: dict, l2: dict) -> list[str]:
    """把指标翻译成人能直接照着改的话。"""
    out: list[str] = []
    if l1["终止原因"]["value"] not in ("finish", "n/a"):
        out.append(f"非正常结束：{l1['终止原因']['value']}（{l1['终止原因']['note']}）")
    if l1["非法转移"]["value"] not in (0, "n/a"):
        out.append(f"调度顺序违规：{l1['非法转移']['note']}")
    if isinstance(l1["重复动作率"]["value"], float) and l1["重复动作率"]["value"] >= 0.3:
        out.append(f"连续重复动作占比 {l1['重复动作率']['value']}，疑似原地打转")
    if isinstance(l1["观察响应性"]["value"], float) and l1["观察响应性"]["value"] >= 0.5:
        out.append(f"失败后原样重试占 {l1['观察响应性']['value']}，未换策略")
    if isinstance(l2["搜索空结果率"]["value"], float) and l2["搜索空结果率"]["value"] >= 0.3:
        out.append(f"搜索空结果率 {l2['搜索空结果率']['value']}，采集端可能拿不到料")
    if isinstance(l2["抓取失败率"]["value"], float) and l2["抓取失败率"]["value"] >= 0.3:
        out.append(f"抓取失败率 {l2['抓取失败率']['value']}（{l2['抓取失败率']['note']}）")
    if isinstance(l2["rerank 区分度"]["value"], float) and l2["rerank 区分度"]["value"] < 0.05:
        out.append(f"rerank 区分度仅 {l2['rerank 区分度']['value']}，精排基本没在区分")
    return out


def _verdict(l1: dict, l2: dict, trace: Trace) -> str:
    if not trace.is_supervisor:
        return (
            "该任务没有编排级日志（走的是 Pipeline 或埋点上线前执行）。"
            "只能看工具级：重点检查搜索空结果率、抓取失败率与采集漏斗，"
            "这三项决定了后面 RAGAS 分数的天花板。"
        )
    if l1["终止原因"]["value"] != "finish":
        return "任务非正常结束，先修编排稳定性；此时任何报告质量指标都不可信。"
    if isinstance(l2["搜索空结果率"]["value"], float) and l2["搜索空结果率"]["value"] >= 0.3:
        return "编排正常但采集端空转明显——问题在「有没有料」，不在「模型好不好」。"
    return "编排与采集均未见明显异常；若报告质量仍差，瓶颈在检索/生成侧，转 RAGAS 那套指标看。"


def render_batch(traces: list[Trace]) -> str:
    """批量报告：只给分布，不逐条铺开。"""
    if not traces:
        return "# 轨迹评估 · 批量\n\n没有查到任务日志。"

    supervisors = [t for t in traces if t.is_supervisor]
    reasons = Counter(l1_metrics(t)["终止原因"]["value"] for t in supervisors)
    empty_rates = [
        l2_metrics(t)["搜索空结果率"]["value"]
        for t in traces
        if isinstance(l2_metrics(t)["搜索空结果率"]["value"], float)
    ]
    fail_rates = [
        l2_metrics(t)["抓取失败率"]["value"]
        for t in traces
        if isinstance(l2_metrics(t)["抓取失败率"]["value"], float)
    ]

    lines = [
        f"# 轨迹评估 · 批量（{len(traces)} 个任务）",
        "",
        f"- Supervisor 任务：{len(supervisors)}；其余 {len(traces) - len(supervisors)} 个无编排级日志",
        "",
        "## 终止原因分布（仅 Supervisor）",
        "",
        "| 原因 | 任务数 |",
        "|---|---|",
    ]
    for reason, count in reasons.most_common():
        lines.append(f"| {reason} | {count} |")

    lines += ["", "## 采集侧分布", ""]
    if empty_rates:
        lines.append(f"- 搜索空结果率：均值 {round(statistics.fmean(empty_rates), 3)}，"
                     f"最大 {round(max(empty_rates), 3)}，样本 {len(empty_rates)}")
    else:
        lines.append("- 搜索空结果率：n/a（无 web_search 审计）")
    if fail_rates:
        lines.append(f"- 抓取失败率：均值 {round(statistics.fmean(fail_rates), 3)}，"
                     f"最大 {round(max(fail_rates), 3)}，样本 {len(fail_rates)}")
    else:
        lines.append("- 抓取失败率：n/a（无 web_fetch 审计）")

    lines += ["", "## 逐任务", "", "| task_id | 终止原因 | 轮次效率 | 搜索空结果率 |", "|---|---|---|---|"]
    for trace in traces:
        l1 = l1_metrics(trace)
        l2 = l2_metrics(trace)
        lines.append(
            f"| {trace.task_id[:8]} | {_fmt(l1['终止原因']['value'])} | "
            f"{_fmt(l1['轮次效率']['value'])} | {_fmt(l2['搜索空结果率']['value'])} |"
        )

    lines += ["", "> 阈值未定：先看分布再定异常线，不要拿单次数字下结论。", ""]
    return "\n".join(lines)


# ── 数据读取 ────────────────────────────────────────────────────────────


def _parse_since(text: str) -> datetime:
    """支持 7d / 24h / 30m，也接受 ISO 日期。"""
    units = {"d": "days", "h": "hours", "m": "minutes"}
    if len(text) > 1 and text[-1] in units and text[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(**{units[text[-1]]: int(text[:-1])})
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


async def fetch_rows(
    task_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> dict[str, list[dict]]:
    """按 task 取日志，返回 {task_id: rows}。一次连接，不 N+1。"""
    from src.config import Settings
    from src.db.connection import close_pool, create_pool

    pool = await create_pool(Settings())
    try:
        if task_id:
            rows = await pool.fetch(
                """SELECT task_id, created_at, agent_name, action, request, response,
                          error, duration_ms
                   FROM agent_logs WHERE task_id = $1
                   ORDER BY created_at, id""",
                task_id,
            )
            return {task_id: [dict(r) for r in rows]}

        if not since:
            raise ValueError("必须给 --task 或 --since")

        tasks = await pool.fetch(
            """SELECT task_id, MAX(created_at) AS last_ts
               FROM agent_logs WHERE created_at >= $1
               GROUP BY task_id ORDER BY last_ts DESC LIMIT $2""",
            _parse_since(since),
            limit,
        )
        if not tasks:
            return {}

        ids = [str(r["task_id"]) for r in tasks]
        rows = await pool.fetch(
            """SELECT task_id, created_at, agent_name, action, request, response,
                      error, duration_ms
               FROM agent_logs WHERE task_id = ANY($1::uuid[])
               ORDER BY task_id, created_at, id""",
            ids,
        )
        grouped: dict[str, list[dict]] = {tid: [] for tid in ids}
        for row in rows:
            grouped[str(row["task_id"])].append(dict(row))
        return grouped
    finally:
        # 走 close_pool 而不是 pool.close()：create_pool 是全局单例，
        # 直接 close 会留下一个「已关闭但还挂在模块变量上」的池。
        await close_pool()


# ── CLI ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 轨迹评估（只读）")
    parser.add_argument("--task", help="单个 task_id")
    parser.add_argument("--since", help="批量时间窗，如 7d / 24h / 30m")
    parser.add_argument("--limit", type=int, default=50, help="批量最多取多少个任务")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args(argv)

    if not args.task and not args.since:
        parser.error("必须指定 --task 或 --since")

    logging.basicConfig(level=logging.WARNING)
    try:
        grouped = asyncio.run(fetch_rows(args.task, args.since, args.limit))
    except Exception as exc:
        # 评估器是只读工具，连不上库时给一句人话，不要把 asyncpg 的堆栈糊到脸上
        print(f"读取 agent_logs 失败（数据库不可用？）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not grouped:
        print("没有查到日志。")
        return 1

    traces = [build_trace(tid, rows) for tid, rows in grouped.items()]

    if args.json:
        payload = [
            {
                "task_id": t.task_id,
                "is_supervisor": t.is_supervisor,
                "counts": {
                    "think": len(t.thinks), "observe": len(t.observes),
                    "tool_call": len(t.tools), "business": len(t.business),
                },
                "l1": l1_metrics(t),
                "l2": l2_metrics(t),
            }
            for t in traces
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if len(traces) == 1:
        print(render_case(traces[0]))
    else:
        print(render_batch(traces))
    return 0


if __name__ == "__main__":
    sys.exit(main())
