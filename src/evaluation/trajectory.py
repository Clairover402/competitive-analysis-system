"""Agent 轨迹评估 —— 从 agent_logs 重建决策路径，算编排级与工具级指标。

【职责】
  做：把某个 task 的 agent_logs 重建成「第几轮想了什么 → 调了谁 → 用什么工具 →
      结果如何」的轨迹，算 L1 编排级 + L2 工具级指标，输出 Markdown 报告；
      `--persist` 时把指标 upsert 进 trajectory_metrics（一任务一行，供 Grafana 直读）。
  不做：不调 LLM、不改任务/报告等业务状态（唯一写操作是幂等 upsert 指标表）。

【输入 → 输出】
  输入：asyncpg 连接池里的 agent_logs（action ∈ think / observe / tool_call / 业务动作）
  输出：stdout 的 Markdown 报告（--json 则输出结构化 dict）
        + 可选：trajectory_metrics 的指标行（--persist）
  用法：
      python -m src.evaluation.trajectory --task <task_id>
      python -m src.evaluation.trajectory --since 7d --limit 50
      python -m src.evaluation.trajectory --since 2h --persist   # 定时落库

【关键决策】
  ① 只评 Supervisor，不评 Pipeline：Pipeline 四个节点三条固定边，路径编译期确定，
     评轨迹等于评「代码有没有跑对」；Supervisor 是 LLM 动态决策，才有路径好坏。
     Pipeline 任务进来时 L1 全 n/a、只有 L2，报告里会显式说明。
  ② 只做黑盒属性型指标，不做 golden trajectory（无标注、且同一任务允许多条合理路径），
     也不做 LLM judge 评轨迹（贵且不稳，规则能抓的问题先抓干净）。
  ③ 缺数据一律标 n/a 并写明「缺什么」，不当 0 —— 日志是 fire-and-forget，
     「没发生」和「没记上」是两件事（同 run_archive 的 compare 纪律）。
     落库时 n/a → NULL，同样是这个理由。
  ④ 阈值不在本模块写死：先跑分布再用 `--since` 批量定，避免拍脑袋。
  ⑤ 指标落库由 CLI + crontab 驱动，不用任务结束钩子：不耦合主链路、
     失败可重跑、历史可回填，幂等由 ON CONFLICT 保证。

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

# 批量落库只处理「最后一条日志距今满 10 分钟」的任务。
# 任务还在写日志时算出来的 termination 可能是假的 incomplete。
SETTLE_MINUTES = 10

# 各 Agent action 的必填参数（来自 A2A AgentCard input_schema.required）。
# 只用来判「键有没有丢」——参数值是摘要（如 "list(len=3)"），判不了空。
#
# ⚠️ writer / quality 不在此列：ff4e4e29 实测 round 3 只有 {"title": ...}、
# round 4 是 {}，因为 analysis_results / report_markdown 由 Supervisor state 直接
# 流转给下游节点，不由 LLM 在 arguments 里传。把它们算作缺键会让**每个健康任务**
# 的参数完整率恒为 0.5 —— 假警报比没有指标更糟。
_REQUIRED_ARGS: dict[str, list[str]] = {
    "collector": ["competitors", "dimensions"],
    "analyzer": ["competitors", "dimensions"],
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
    # L3 检索侧数值（compute_retrieval 的产物）。日志本身不含这些，
    # 由调用方从 retrieval_snapshot 单独查出来挂上——保持 build_trace 纯函数。
    retrieval: dict | None = None

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


def _as_list(value: Any) -> list:
    """同上，用于 jsonb 数组列（retrieved_urls 这类）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


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
                # degraded 只有非空值（如 "json_error"）才有意义：LLM 决策解析失败后降级退出
                "degraded": response.get("degraded") or "",
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

    四类原因按「谁覆盖了谁」排优先级：
      ① forced_finish —— act 硬拦截重写上限。此时**最后一条 think 写的是 writer**，
         按 think 判会误报成 incomplete，所以必须优先读 act 的埋点日志。
      ② json_error —— think 两次解析失败降级为 finish。同样是「看起来正常结束」的故障。
      ③ finish / max_rounds / incomplete —— 正常路径，按最后一条 think 判。
    """
    if not trace.thinks:
        return _metric("n/a", "无 think 日志（非 Supervisor 任务）")

    forced = next((e for e in trace.business if e.action == "forced_finish"), None)
    if forced is not None:
        rewrites = forced.request.get("rewrite_count")
        return _metric(
            "forced_finish",
            f"重写已达上限（rewrite_count={rewrites}），act 强制接受当前报告",
        )

    last = trace.thinks[-1]
    if last.get("degraded"):
        return _metric("json_error", f"决策解析降级（{last['degraded']}），安全退出")
    if last["agent"] == "finish" or last["action"] == "finish":
        return _metric("finish", "LLM 主动决策结束")
    rounds = max(t["round"] for t in trace.thinks)
    if rounds >= DEFAULT_MAX_ROUNDS:
        return _metric("max_rounds", f"轮次耗尽（{rounds}/{DEFAULT_MAX_ROUNDS}）")
    return _metric("incomplete", "既没 finish 也没到上限，轨迹中断（可能进程被杀）")


def _round_stats(trace: Trace) -> tuple[int, int] | None:
    """(总轮次, 有效轮次)。无 think 日志返回 None（算不出来，不是 0）。

    有效 = 该轮有 observe、状态 completed、且 observation 非空
    （finish 轮和失败轮都不产生新业务状态）。
    """
    if not trace.thinks:
        return None
    rounds_used = max(t["round"] for t in trace.thinks)
    effective = {
        o["round"] for o in trace.observes
        if o["status"] == "completed" and o["observation"]
    }
    effective &= {t["round"] for t in trace.thinks}
    return rounds_used, len(effective)


def _round_efficiency(trace: Trace) -> dict:
    """轮次效率 = 有效轮次 / 总轮次。"""
    stats = _round_stats(trace)
    if stats is None:
        return _metric("n/a", "无 think 日志")
    rounds_used, effective = stats
    ratio = effective / rounds_used if rounds_used else 0.0
    return _metric(round(ratio, 3), f"有效轮次 {effective} / 总轮次 {rounds_used}")


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


def _fetch_buckets(trace: Trace) -> tuple[int, Counter[str]] | None:
    """(抓取次数, 失败分桶)。无 web_fetch 调用返回 None。

    ⚠️ 只能分「抛异常」的失败；web_fetch 若以正常返回的形式带回错误
    （如 dict 带 error 键），这里会算成成功。当前 tools_web 是抛异常的，口径成立。
    """
    fetches = _tools(trace, "web_fetch")
    if not fetches:
        return None

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
    return len(fetches), buckets


def _fetch_failure_buckets(trace: Trace) -> dict:
    """抓取失败按错误类型分桶 —— 把「2/7 页」这类人工诊断变成可回归的数字。"""
    stats = _fetch_buckets(trace)
    if stats is None:
        return _metric("n/a", "无 web_fetch 调用")
    total, buckets = stats
    fail_rate = sum(buckets.values()) / total
    detail = ", ".join(f"{k}×{v}" for k, v in buckets.most_common()) or "无失败"
    return _metric(round(fail_rate, 3), f"{detail}，共 {total} 次抓取")


def _duplicate_fetch_rate(trace: Trace) -> dict:
    urls = [t["args"].get("url") for t in _tools(trace, "web_fetch") if t["args"].get("url")]
    if not urls:
        return _metric("n/a", "无 web_fetch 或未记录 url")
    dup = len(urls) - len(set(urls))
    return _metric(round(dup / len(urls), 3), f"{dup} 次重复 / {len(urls)} 次抓取")


def _embed_stats(trace: Trace) -> tuple[int, int, int] | None:
    """(embed_query 次数, embed_texts 批数, 入库条数)。无调用返回 None。"""
    batch_calls = _tools(trace, "embed_texts")
    query_calls = _tools(trace, "embed_query")
    if not batch_calls and not query_calls:
        return None
    total_chunks = sum(c["n"] for c in batch_calls if isinstance(c["n"], int))
    return len(query_calls), len(batch_calls), total_chunks


def _embedding_calls(trace: Trace) -> dict:
    stats = _embed_stats(trace)
    if stats is None:
        return _metric("n/a", "无 embedding 调用")
    queries, batches, total_chunks = stats
    sizes = [c["n"] for c in _tools(trace, "embed_texts") if isinstance(c["n"], int)]
    avg = round(statistics.fmean(sizes), 1) if sizes else None
    return _metric(
        queries + batches,
        f"embed_query {queries} 次（应=维度数）、"
        f"embed_texts {batches} 批 / {total_chunks} 条，平均批大小 {avg}",
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


def _funnel_counts(trace: Trace) -> tuple[int, int, int, int] | None:
    """(搜索命中条数, 抓取次数, 抓到正文页数, 入库 chunk 数)。无采集日志返回 None。"""
    searches = _tools(trace, "web_search")
    fetches = _tools(trace, "web_fetch")
    embeds = _tools(trace, "embed_texts")
    if not searches and not fetches and not embeds:
        return None
    return (
        sum(s["n"] or 0 for s in searches),
        len(fetches),
        sum(1 for f in fetches if f["ok"] and (f["chars"] or 0) > 0),
        sum(e["n"] or 0 for e in embeds),
    )


def _funnel(trace: Trace) -> dict:
    """采集漏斗：搜索结果 → 抓取次数 → 抓到正文 → 最终 chunk。"""
    counts = _funnel_counts(trace)
    if counts is None:
        return _metric("n/a", "无采集链路工具日志")
    return _metric(
        " → ".join(str(c) for c in counts),
        "搜索命中条数 → 抓取次数 → 抓到正文页数 → 入库 chunk 数",
    )


def _rerank_under_k(trace: Trace) -> int | None:
    """返回数 < 本次请求 top_k 的次数。

    单次比较而非拿 top_k 均值比：均值会掩盖「一次 50 要 3、一次 3 要 50」这类偏斜。
    没记 top_k 的调用无法判，跳过。
    """
    reranks = _tools(trace, "rerank")
    if not reranks:
        return None
    under = 0
    for item in reranks:
        requested = item["args"].get("top_k")
        if isinstance(requested, int) and isinstance(item["n"], int) and item["n"] < requested:
            under += 1
    return under


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
        under = _rerank_under_k(trace) or 0
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


# ── L3 检索侧指标（数据源：retrieval_snapshot）──────────────────────────
#
# L2 度量「采集端有没有把料搬回来」，L3 度量「搬回来的料够不够喂给每个竞品」。
# 两者不能混：搜索空结果率高是采不到；竞品覆盖偏斜是有料但全堆在某个竞品上。


def compute_retrieval(
    snapshots: list[dict],
    competitors: list[str] | None = None,
) -> dict | None:
    """把 retrieval_snapshot 行压成 4 个检索侧数值；无快照返回 None（不是 0）。

    Args:
        snapshots: 每行一条 (维度, 竞品) 快照，含 recalled_urls / reranked_urls。
        competitors: 任务全量竞品名（来自 tasks.competitors）。
            ⚠️ 必须传：analyzer 对「没找到料」的竞品是 skip，**根本不落快照**，
            只按快照里的竞品算，最该报警的「某竞品 0 页」会隐身。
            ⚠️ 已知局限：实测 55 个有快照的任务里 13 个 tasks.competitors 是空数组
            （竞品由 Supervisor 现场发现），这些任务的空缺识别不出来，
            skew 只反映「已有快照的竞品之间」的均衡度——偏高，不可当健康证明。

    三个指标的语义：
      · recall_avg / rerank_avg —— 每格平均召回几条、精排后留几条。
        两者要一起看：召回 3 条 / 精排 3 条 ≠ 召回 50 / 精排 3，前者是池子空。
      · coverage_skew —— min / median（按竞品聚合去重页数）。1.0=均衡，0=有竞品彻底没料。
        ⚠️ 只在「每行 recalled_urls 是该竞品自己那份」时才有意义。2026-10-20 之前
        analyzer 落的是维度级混池（同维度各竞品完全相同），那批任务的 skew 恒接近 1，
        批量统计里若同时含新旧任务，均值会被历史数据拉向健康——按 created_at 分段看。
      · cross_dim_dup —— 同任务不同维度复用同一批 URL 的比例。
        高说明「几个维度其实在答同一个问题」，维度拆分没起作用。
    """
    if not snapshots:
        return None

    recalled = [len(_as_list(r.get("recalled_urls"))) for r in snapshots]
    reranked = [len(_as_list(r.get("reranked_urls"))) for r in snapshots]
    count = len(snapshots)

    # 每竞品去重页数：同一 URL 在多个维度被召回只算一页
    per_competitor: dict[str, set[str]] = {}
    for row in snapshots:
        name = row.get("competitor")
        if not name:
            continue
        per_competitor.setdefault(name, set()).update(_as_list(row.get("recalled_urls")))
    # 没落快照的竞品按 0 页计入，否则最严重的空缺反而看不见
    for name in competitors or []:
        per_competitor.setdefault(name, set())

    skew: float | None = None
    if len(per_competitor) >= 2:
        pages = sorted(len(urls) for urls in per_competitor.values())
        median = statistics.median(pages)
        skew = round(pages[0] / median, 3) if median else 0.0

    # 每维度一份去重 URL 集合，再看跨维度重复
    per_dimension: dict[str, set[str]] = {}
    for row in snapshots:
        dimension = row.get("dimension") or ""
        per_dimension.setdefault(dimension, set()).update(_as_list(row.get("reranked_urls")))
    dup: float | None = None
    if len(per_dimension) >= 2:
        reuse_total = sum(len(urls) for urls in per_dimension.values())
        distinct = len(set().union(*per_dimension.values()))
        dup = round(1 - distinct / reuse_total, 3) if reuse_total else None

    return {
        "snapshots": count,
        "recall_avg": round(statistics.fmean(recalled), 1),
        "rerank_avg": round(statistics.fmean(reranked), 1),
        "coverage_skew": skew,
        "cross_dim_dup": dup,
    }


def l3_metrics(trace: Trace) -> dict[str, dict]:
    """L3 检索侧四项（展示用）。trace.retrieval 为 None 时全 n/a。"""
    data = trace.retrieval
    if not data:
        return {
            "检索快照": _metric("n/a", "无 retrieval_snapshot（任务未走到分析阶段）"),
            "召回 / 精排": _metric("n/a", "无快照"),
            "竞品覆盖偏斜": _metric("n/a", "无快照"),
            "跨维度重复率": _metric("n/a", "无快照"),
        }
    return {
        "检索快照": _metric(data["snapshots"], "竞品×维度级快照行数"),
        "召回 / 精排": _metric(
            f"{data['recall_avg']} → {data['rerank_avg']}",
            "每格平均召回条数 → 精排后保留条数（差值过大说明候选池被竞品过滤掉大半）",
        ),
        "竞品覆盖偏斜": _metric(
            data["coverage_skew"] if data["coverage_skew"] is not None else "n/a",
            "最少竞品页数 / 中位数；1.0=均衡，接近 0=有竞品几乎没料",
        ),
        "跨维度重复率": _metric(
            data["cross_dim_dup"] if data["cross_dim_dup"] is not None else "n/a",
            "不同维度复用同一批 URL 的比例；高说明维度拆分没起作用",
        ),
    }


# ── 指标落库（宽表 trajectory_metrics）──────────────────────────────────

# 口径版本。指标定义改了（比如 forced_finish 上线、算法换了）就 +1，
# 旧行靠它识别、决定要不要全量重算 —— 见计划文档「风险 4」。
# v1 → v2：参数完整率不再把 writer/quality 计入（state 流转的参数不算「LLM 漏传」）。
# v2 → v3：termination 新增 forced_finish / json_error 两类（依赖 act/think 新埋点）。
# v3 → v4：新增 L3 检索侧 5 列（数据源 retrieval_snapshot，日志侧无变化）。
METRICS_VERSION = 4

# 与 schema.sql 的 trajectory_metrics 列顺序一一对应（不含 task_id / computed_at）。
_ROW_COLUMNS = (
    "is_supervisor", "metrics_version", "termination", "rounds", "rounds_effective",
    "illegal_transitions", "repeat_rate", "failed_round_rate", "arg_completeness",
    "observation_responsiveness", "tool_calls", "search_empty_rate", "fetch_fail_rate",
    "fetch_fail_detail", "dup_fetch_rate", "embed_calls", "embed_chunks",
    "rerank_gap", "rerank_under_k", "funnel_search", "funnel_fetch",
    "funnel_pages", "funnel_chunks",
    # L3：检索侧（来源 retrieval_snapshot，不是 agent_logs）
    "retrieval_cells", "recall_avg", "rerank_avg", "coverage_skew", "cross_dim_dup",
)

_UPSERT_SQL = (
    f"INSERT INTO trajectory_metrics (task_id, {', '.join(_ROW_COLUMNS)}) "
    f"VALUES ($1, {', '.join(f'${i}' for i in range(2, len(_ROW_COLUMNS) + 2))}) "
    "ON CONFLICT (task_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _ROW_COLUMNS)
    + ", computed_at = NOW()"
)


def _num(metric: dict) -> float | int | None:
    """从 {value, note} 里取数值；'n/a' 与非数值一律 None。

    NULL 与 0 必须分清：NULL = 算不出来（没埋点），0 = 确实发生了 0 次。
    """
    value = metric.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def to_row(trace: Trace) -> dict:
    """把 Trace 压成 trajectory_metrics 的一行（纯函数）。

    数值一律复用 l1_metrics / l2_metrics 背后的同一个计算函数，
    不在这里重算 —— 否则面板和 CLI 迟早出现两个不同的数。
    """
    rounds = _round_stats(trace)
    fetch = _fetch_buckets(trace)
    embed = _embed_stats(trace)
    funnel = _funnel_counts(trace)
    l1 = l1_metrics(trace)
    l2 = l2_metrics(trace)

    return {
        "task_id": trace.task_id,
        "is_supervisor": trace.is_supervisor,
        "metrics_version": METRICS_VERSION,
        # 终止原因存字符串（n/a 也要存）：面板靠 WHERE termination <> 'n/a' 过滤
        "termination": l1["终止原因"]["value"],
        "rounds": rounds[0] if rounds else None,
        "rounds_effective": rounds[1] if rounds else None,
        "illegal_transitions": _num(l1["非法转移"]),
        "repeat_rate": _num(l1["重复动作率"]),
        "failed_round_rate": _num(l1["失败轮占比"]),
        "arg_completeness": _num(l1["参数完整率"]),
        "observation_responsiveness": _num(l1["观察响应性"]),
        "tool_calls": _num(l2["工具调用次数"]),
        "search_empty_rate": _num(l2["搜索空结果率"]),
        "fetch_fail_rate": _num(l2["抓取失败率"]),
        "fetch_fail_detail": dict(fetch[1]) if fetch else None,
        "dup_fetch_rate": _num(l2["重复抓取率"]),
        "embed_calls": (embed[0] + embed[1]) if embed else None,
        "embed_chunks": embed[2] if embed else None,
        "rerank_gap": _num(l2["rerank 区分度"]),
        "rerank_under_k": _rerank_under_k(trace),
        "funnel_search": funnel[0] if funnel else None,
        "funnel_fetch": funnel[1] if funnel else None,
        "funnel_pages": funnel[2] if funnel else None,
        "funnel_chunks": funnel[3] if funnel else None,
        # L3：retrieval 由调用方从 retrieval_snapshot 算好挂在 trace 上；
        # 没有就是 None——「任务没走到分析阶段」和「有分析但召回 0 条」是两件事。
        "retrieval_cells": trace.retrieval["snapshots"] if trace.retrieval else None,
        "recall_avg": trace.retrieval["recall_avg"] if trace.retrieval else None,
        "rerank_avg": trace.retrieval["rerank_avg"] if trace.retrieval else None,
        "coverage_skew": trace.retrieval["coverage_skew"] if trace.retrieval else None,
        "cross_dim_dup": trace.retrieval["cross_dim_dup"] if trace.retrieval else None,
    }


def _row_params(row: dict) -> tuple:
    """dict → SQL 参数元组。jsonb 列必须 dumps：asyncpg 不让 dict 直传 jsonb。"""
    values = []
    for column in _ROW_COLUMNS:
        value = row.get(column)
        if column == "fetch_fail_detail" and value is not None:
            value = json.dumps(value, ensure_ascii=False)
        values.append(value)
    return (row["task_id"], *values)


async def persist_rows(rows: list[dict]) -> tuple[int, int]:
    """幂等 upsert 指标行，返回 (写入数, 跳过数)。

    跳过两类：task_id 不是合法 UUID（测试/演示数据）；tasks 表里不存在的
    task_id（外键会拒绝，先把它们挑出来，不让整批 executemany 一起失败）。
    """
    import uuid as _uuid

    from src.config import Settings
    from src.db.connection import close_pool, create_pool

    if not rows:
        return 0, 0

    valid: list[dict] = []
    skipped = 0
    for row in rows:
        try:
            _uuid.UUID(str(row["task_id"]))
        except (ValueError, AttributeError):
            skipped += 1
            continue
        valid.append(row)
    if not valid:
        return 0, skipped

    pool = await create_pool(Settings())
    try:
        existing = await pool.fetch(
            "SELECT id::text FROM tasks WHERE id = ANY($1::uuid[])",
            [row["task_id"] for row in valid],
        )
        known = {r["id"] for r in existing}
        kept = [row for row in valid if row["task_id"] in known]
        skipped += len(valid) - len(kept)
        if not kept:
            return 0, skipped
        await pool.executemany(_UPSERT_SQL, [_row_params(row) for row in kept])
        return len(kept), skipped
    finally:
        await close_pool()


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

    l3 = l3_metrics(trace)
    lines += [
        "",
        "## L3 检索侧（料够不够喂给每个竞品）",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
    ]
    for name, item in l3.items():
        lines.append(f"| {name} | {_fmt(item['value'])} | {item['note']} |")

    anomalies = _anomalies(trace, l1, l2, l3)
    lines += ["", "## 异常明细", ""]
    lines += [f"- {a}" for a in anomalies] if anomalies else ["- 无"]

    lines += ["", "## 结论", "", _verdict(l1, l2, trace), ""]
    return "\n".join(lines)


def _anomalies(trace: Trace, l1: dict, l2: dict, l3: dict | None = None) -> list[str]:
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

    l3 = l3 or {}
    skew = l3.get("竞品覆盖偏斜", {}).get("value")
    if isinstance(skew, (int, float)) and skew < 0.3:
        out.append(
            f"竞品覆盖偏斜 {skew}——最少竞品的召回页数不到中位数的三成，"
            "该竞品的结论多半是模型自己编的"
        )
    dup = l3.get("跨维度重复率", {}).get("value")
    if isinstance(dup, (int, float)) and dup >= 0.5:
        out.append(f"跨维度重复率 {dup}——多个维度在用同一批证据，维度拆分没起作用")
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
    if trace.retrieval:
        skew = trace.retrieval.get("coverage_skew")
        if isinstance(skew, (int, float)) and skew < 0.3:
            return (
                "采集总量正常但竞品间严重偏斜——少数竞品的结论是在「没料」的情况下生成的，"
                "先补该竞品的采集再谈报告质量。"
            )
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

    retrieval_rows = [t.retrieval for t in traces if t.retrieval]
    lines += ["", "## 检索侧分布（有快照的任务）", ""]
    if retrieval_rows:
        recalls = [r["recall_avg"] for r in retrieval_rows]
        reranks = [r["rerank_avg"] for r in retrieval_rows]
        skews = [r["coverage_skew"] for r in retrieval_rows if r["coverage_skew"] is not None]
        dups = [r["cross_dim_dup"] for r in retrieval_rows if r["cross_dim_dup"] is not None]
        lines.append(f"- 每格召回：均值 {round(statistics.fmean(recalls), 1)} 条"
                     f"（样本 {len(recalls)} 个任务）")
        lines.append(f"- 精排后保留：均值 {round(statistics.fmean(reranks), 1)} 条"
                     f"；保留率 {round(100 * sum(reranks) / sum(recalls), 1)}%"
                     if sum(recalls) else "- 精排后保留：n/a")
        lines.append(f"- 竞品覆盖偏斜：最低 {min(skews)}，均值 {round(statistics.fmean(skews), 3)}"
                     if skews else "- 竞品覆盖偏斜：n/a（单竞品任务无法比较）")
        lines.append(f"- 跨维度重复率：均值 {round(statistics.fmean(dups), 3)}"
                     if dups else "- 跨维度重复率：n/a（单维度任务无法比较）")
    else:
        lines.append("- n/a（这些任务没有 retrieval_snapshot）")

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
               FROM agent_logs
               WHERE created_at >= $1
               GROUP BY task_id
               HAVING MAX(created_at) < NOW() - ($3::int * INTERVAL '1 minute')
               ORDER BY last_ts DESC LIMIT $2""",
            _parse_since(since),
            limit,
            SETTLE_MINUTES,
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


async def fetch_retrieval(task_ids: list[str]) -> dict[str, dict]:
    """取每个任务的检索快照 + 全量竞品列表，返回 {task_id: {snapshots, competitors}}。

    为什么要连 tasks 表拿 competitors：analyzer 对「没找到料」的竞品是直接 skip、
    **不落快照**。只看快照等于让最该报警的空缺（某竞品 0 页）隐身。
    """
    from src.config import Settings
    from src.db.connection import close_pool, create_pool

    if not task_ids:
        return {}

    pool = await create_pool(Settings())
    try:
        snaps = await pool.fetch(
            """SELECT task_id, dimension, competitor, recalled_urls, reranked_urls
               FROM retrieval_snapshot WHERE task_id = ANY($1::uuid[])""",
            task_ids,
        )
        tasks = await pool.fetch(
            """SELECT id::text AS id, competitors
               FROM tasks WHERE id = ANY($1::uuid[])""",
            task_ids,
        )
        names = {r["id"]: _as_list(r["competitors"]) for r in tasks}
        grouped: dict[str, dict] = {
            tid: {"snapshots": [], "competitors": names.get(tid, [])} for tid in task_ids
        }
        for row in snaps:
            item = dict(row)
            grouped[str(item["task_id"])]["snapshots"].append(item)
        return grouped
    finally:
        await close_pool()


# ── CLI ─────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 轨迹评估（默认只读，--persist 落库）")
    parser.add_argument("--task", help="单个 task_id")
    parser.add_argument("--since", help="批量时间窗，如 7d / 24h / 30m")
    parser.add_argument("--limit", type=int, default=50, help="批量最多取多少个任务")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="把指标 upsert 进 trajectory_metrics（幂等，可重复跑）",
    )
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
        if args.since:
            print(
                f"没有查到日志（--since 只处理最后写入距今超过 {SETTLE_MINUTES} 分钟的任务）。"
            )
        else:
            print("没有查到日志。")
        return 1

    traces = [build_trace(tid, rows) for tid, rows in grouped.items()]

    # L3 的数据源是 retrieval_snapshot，不在 agent_logs 里，单独取一次。
    # --persist 时失败必须中止：否则 L3 五列会被写成 NULL，覆盖掉上一轮的正确值。
    try:
        retrieval = asyncio.run(fetch_retrieval(list(grouped)))
    except Exception as exc:
        message = f"读取 retrieval_snapshot 失败：{type(exc).__name__}: {exc}"
        if args.persist:
            print(message + "（已中止落库，避免把 L3 列写成 NULL）", file=sys.stderr)
            return 2
        print(message + "（本次报告 L3 全为 n/a）", file=sys.stderr)
        retrieval = {}
    for trace in traces:
        info = retrieval.get(trace.task_id)
        if info:
            trace.retrieval = compute_retrieval(info["snapshots"], info["competitors"])

    if args.persist:
        rows = [to_row(t) for t in traces]
        try:
            written, skipped = asyncio.run(persist_rows(rows))
        except Exception as exc:
            print(f"落库失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        # 走 stderr：--json 的 stdout 要保持是纯 JSON
        print(f"落库 {written} 行" + (f"，跳过 {skipped} 行" if skipped else ""), file=sys.stderr)

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
                "l3": l3_metrics(t),
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
