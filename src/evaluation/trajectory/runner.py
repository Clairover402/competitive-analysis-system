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
      python -m src.evaluation.trajectory.runner --task <task_id>
      python -m src.evaluation.trajectory.runner --since 7d --limit 50
      python -m src.evaluation.trajectory.runner --since 2h --persist   # 定时落库

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
import re
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

# 抓取正文少于这个字符数算「没拿到正文」。与 tools_web 的 Jina 兜底判据同源
# （那边是 cjk < 200 就重试一次），同一个「太少」的标准不要在两处各写一个数字。
NEAR_EMPTY_CHARS = 200

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
                # effective 是 2026-10-30 新增字段：本轮有没有真的产出业务状态。
                # 旧日志没有这个键 → None，由 _effective_flag 走回退判据。
                "effective": request.get("effective"),
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
                # 搜索结果的引擎分布（2026-10-30）：{"tavily": 7} 或 {"synthesize": 6}
                "engines": response.get("engines") or {},
                # 渲染兜底标记：playwright = httpx 拿骨架后由本地 chromium 补的正文
                "fallback": response.get("fallback") or "",
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


def _effective_flag(observe: dict) -> bool | None:
    """单轮是否产出了业务状态（None = 判不了）。

    【两级判据（2026-10-30）】
      ① 新埋点有 `effective` 布尔字段 → 直接读。它在 observe 节点里由
         「调度成功 ∧ 有 observation ∧ 没有业务级空转 ∧ 不是终止轮」算出。
      ② 旧埋点（2026-10-30 前无该字段）→ 退回 status + observation 非空。
         ⚠️ 这条回退**看不见业务级空转**：collector 拿到 0 页时 status 仍是
         completed。全库 observe 86 条 status 100% completed、0 条 failed ——
         正因为如此，新埋点必须显式加字段，不能靠 status 推断。
    """
    raw = observe.get("effective")
    if isinstance(raw, bool):
        return raw
    if observe.get("status") != "completed":
        return False
    return bool(observe.get("observation"))


def _round_accounting(trace: Trace) -> dict | None:
    """按轮统计：总轮次 / 可计轮次 / 有效轮次 / 无效明细。三项 L1 指标共用。

    【可计轮次 = 总轮次 − 终止轮】
      finish 轮按定义不产生业务状态（think 决策结束 → act 短路 → observe 空）。
      留在分母里，任何健康任务都恒等于 (n-1)/n：5 轮 0.800、6 轮 0.833 ——
      指标测的其实是「结束在第几轮」，不是「每轮有没有白跑」。

    【为什么把三份统计合成一个函数】
      失败轮 / 观察响应性 / 轮次效率原本各算各的，2026-10-30 发现它们全挂在
      status 上因而全部失效。口径分散在三处时，改判据必有一处漏改，而漏改的
      后果是**数字错但不报错**。

    【effective=None 的语义】
      有 think 但零 observe（2026-10-19 埋点上线前的任务）：有效轮次是「没采到」
      不是「一轮都没生效」，返回 None 给上层降级成 n/a，不记 0。
    """
    if not trace.thinks:
        return None

    rounds = sorted({t["round"] for t in trace.thinks if t["round"]})
    total = rounds[-1] if rounds else 0
    reason = _termination_reason(trace)["value"]
    finish_round = total if reason == "finish" else None
    countable = [r for r in rounds if r != finish_round]

    if not trace.observes:
        return {
            "total": total, "countable": len(countable), "reason": reason,
            "finish_round": finish_round, "effective": None,
            "noop": [], "unlogged": True,
        }

    obs_by_round = {o["round"]: o for o in trace.observes}
    effective_rounds: set[int] = set()
    noop: list[tuple[int, str]] = []
    for rnd in countable:
        ob = obs_by_round.get(rnd)
        if ob is None:
            noop.append((rnd, "无 observe 记录"))
        elif _effective_flag(ob):
            effective_rounds.add(rnd)
        elif ob.get("status") != "completed":
            noop.append((rnd, f"调度失败({ob.get('status')})"))
        else:
            noop.append((rnd, "业务空转"))

    return {
        "total": total, "countable": len(countable), "reason": reason,
        "finish_round": finish_round, "effective": len(effective_rounds),
        "noop": noop, "unlogged": False,
    }


def _round_stats(trace: Trace) -> tuple[int, int | None] | None:
    """(总轮次, 有效轮次) —— to_row 落库只要这两个数，沿用旧签名。"""
    acc = _round_accounting(trace)
    if acc is None:
        return None
    return acc["total"], acc["effective"]


def _round_efficiency(trace: Trace) -> dict:
    """轮次效率 = 有效轮次 / **可计轮次**。

    【为什么分母不是「总轮次」（2026-10-30 改）】
      finish 轮按定义不产生业务状态（think 决策结束 → act 短路 → observe 空）。
      把它留在分母里，任何健康任务都恒等于 (n-1)/n：5 轮 0.800、6 轮 0.833、
      10 轮 0.900 —— **指标测的是「结束在第几轮」，不是「每轮有没有白跑」**。
      实测 2026-10-30 补跑的 8 个任务：7 个 0.800 + 1 个 0.833，一个失败轮都没有，
      指标却在「波动」，这就是分母错了的样子。

    【为什么分子不是「有 tool_call 的轮次」】
      writer / quality 两轮**根本没有 tool_call**：它们不做检索，只消费上游 state。
      实测 50619e03：最后一条 tool_call 在 21:53:19，writer 的 think 在 21:53:23、
      quality 在 21:54:09 —— 两轮之间一条工具日志都没有，但「写了报告」「评了分」
      都真的发生了。用 tool_call 做判据会把这两个有产出的轮判成无效（5 轮掉到 0.4）。

    【分子读 effective，不读 status（2026-10-30 第二次修）】
      status 只覆盖调度异常。实测全库 observe 86 条 100% completed、0 条 failed，
      于是分子恒等于分母、指标恒 1.000 —— 而同期抓取失败率中位 53%，说明确实
      有大量空转轮。现在 effective 把「collector 0 页 / analyzer 0 维度 /
      writer 空报告」判成无效，分子才第一次动得起来。
    """
    acc = _round_accounting(trace)
    if acc is None:
        return _metric("n/a", "无 think 日志")
    if acc["effective"] is None:
        return _metric("n/a", "无 observe 日志（埋点缺失，有效轮次算不出来）")
    if acc["countable"] <= 0:
        return _metric("n/a", f"只有终止轮（{acc['reason']}），没有可计的业务轮次")

    ratio = acc["effective"] / acc["countable"]
    tail = "finish 轮不计" if acc["finish_round"] else f"{acc['reason']}，全部轮次计入"
    return _metric(
        round(ratio, 3),
        f"有效轮次 {acc['effective']} / 可计轮次 {acc['countable']}（{tail}）",
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


def _failed_rounds(trace: Trace) -> dict:
    """无效轮占比 = 没产出业务状态的业务轮 / 可计轮次。

    【改读 effective（2026-10-30）】
      原实现只数 `status == "failed"`，而全库 observe 86 条 status 100% completed
      ⇒ 恒 0。status 覆盖不到的业务级空转（collector 0 页、analyzer 0 维度、
      writer 空报告）现在由 effective 兜住，note 里按原因分开写，方便区分
      「调度坏了」和「调度成功但没料」——这两种的处理方式完全不同。
    """
    acc = _round_accounting(trace)
    if acc is None:
        return _metric("n/a", "无 think 日志")
    if acc["effective"] is None:
        return _metric("n/a", "无 observe 日志（埋点缺失，无效轮算不出来）")
    if acc["countable"] <= 0:
        return _metric("n/a", "没有可计的业务轮次")

    rate = len(acc["noop"]) / acc["countable"]
    if not acc["noop"]:
        return _metric(round(rate, 3), f"无无效轮 / 可计 {acc['countable']} 轮")
    detail = "；".join(f"第{r}轮 {why}" for r, why in acc["noop"][:4])
    more = f" 等 {len(acc['noop'])} 轮" if len(acc["noop"]) > 4 else ""
    return _metric(round(rate, 3), f"{detail}{more} / 可计 {acc['countable']} 轮")


def _arg_value_empty(value: Any) -> bool:
    """参数值是不是空的 —— 能识别落库摘要里的 list(len=0) / dict(len=0)。

    日志里的参数值是 `_digest_decision_args` 压过的摘要，不是原生结构：
      · 列表 → `"list(len=3)"`      · 字典 → `"dict(len=2)"`
      · 字符串 → 原文（>80 字符截断并附 `…(len=N)`）
    所以「键在但值是空列表」这件事**能从摘要里读出来** —— 原注释说「判不了空」
    只对了一半：判不了空，但判得了 len=0。
    """
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, str):
        text = value.strip()
        for prefix in ("list(len=", "dict(len="):
            if text.startswith(prefix) and text.endswith(")"):
                try:
                    return int(text[len(prefix):-1]) == 0
                except ValueError:
                    return False
    return False


def _arg_completeness(trace: Trace) -> dict:
    """必填参数有没有丢键、或者键在但值为空。

    【判据（2026-10-30 加强）】
      ① 键缺失 → 计入不完整（原判据）
      ② 键在但值是空列表/空字典 → **也计入不完整**（新判据）
      只判 ① 时，LLM 输出 `{"competitors": [], "dimensions": ["定价"]}` 会被判成
      100% 完整，而 collector 拿到空竞品列表必然空转 —— 这正是「参数完整率恒 1.0」
      和「采集 0 页」能同时存在的缝。摘要里已有 len=N，判 ② 不需要改埋点。
    """
    if not trace.thinks:
        return _metric("n/a", "无 think 日志")

    checked = 0
    ok = 0
    unlogged = 0
    missing: list[str] = []
    for item in trace.thinks:
        required = _REQUIRED_ARGS.get(item["agent"])
        if not required:
            continue
        args = item.get("arguments") or {}
        if not args:
            # 该轮决策没有落 arguments —— 2026-10-19 前的 think 日志没有这个字段。
            # 跳过而不是判缺键：否则「没埋点」会被算成「参数全丢」，得出 0% 的假警报。
            unlogged += 1
            continue
        checked += 1
        absent = [k for k in required if k not in args]
        blank = [k for k in required if k in args and _arg_value_empty(args[k])]
        if absent:
            missing.append(f"第{item['round']}轮 {item['agent']} 缺 {absent}")
        elif blank:
            missing.append(f"第{item['round']}轮 {item['agent']} 空值 {blank}")
        else:
            ok += 1

    if not checked:
        note = (
            f"{unlogged} 轮未落 arguments（埋点缺失）"
            if unlogged else "没有需要参数的决策轮"
        )
        return _metric("n/a", note)
    return _metric(round(ok / checked, 3), "；".join(missing) if missing else f"{ok}/{checked} 轮键齐全且非空")


def _observation_responsiveness(trace: Trace) -> dict:
    """无效轮之后有没有换策略 —— 「看到失败仍原样重试」是死循环的典型前兆。

    无效轮的口径与失败轮占比一致（读 effective），两者必须同源：否则会出现
    「失败轮 3 个」但「观察响应性：无失败轮」这种自相矛盾的报告。
    """
    acc = _round_accounting(trace)
    if acc is None or acc["effective"] is None:
        return _metric("n/a", "无 observe 日志")
    noop_rounds = [r for r, _ in acc["noop"]]
    if not noop_rounds:
        return _metric("n/a", "无无效轮")

    by_round = {t["round"]: t for t in trace.thinks}
    blind_retry = 0
    comparable = 0
    for rnd in noop_rounds:
        cur = by_round.get(rnd)
        nxt = by_round.get(rnd + 1)
        if cur is None or nxt is None:
            continue
        comparable += 1
        same_agent = cur["agent"] == nxt["agent"]
        same_args = json.dumps(cur["arguments"], sort_keys=True, default=str) == \
            json.dumps(nxt["arguments"], sort_keys=True, default=str)
        if same_agent and same_args:
            blind_retry += 1

    if not comparable:
        return _metric("n/a", "无效轮后面没有下一轮决策")
    return _metric(
        round(blind_retry / comparable, 3),
        f"{blind_retry} 次原样重试 / {comparable} 次可比对",
    )



def l1_metrics(trace: Trace) -> dict[str, dict]:
    """L1 编排级六项。"""
    return {
        "终止原因": _termination_reason(trace),
        "轮次效率": _round_efficiency(trace),
        "非法转移": _illegal_transitions(trace),
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
    """搜索层「不可信结果」占比 = synthesize 构造的 URL / 全部搜索返回条数。

    ⚠️ 2026-10-30 口径变更（原名「搜索空结果率」，实测恒为 0，等于没测）
      三层 fallback 的最后一层 `_synthesize_urls` **不搜索**，只按 query 拼出
      site:zhihu.com 这类 URL 返回，列表因此永远非空 —— 「空结果率」在结构上
      不可能非 0。真正该报警的是「有多少结果是拼出来的」：这个数高意味着
      Tavily/Bing/Sogou 全挂，下游抓的是一堆虚构 URL，既浪费渲染成本，
      又把无关页面（实测 cryptorank.io 的 Obsidian 资本公司）写进向量库。

    旧日志没有 engines 字段 → 记 n/a，不猜。
    """
    searches = _tools(trace, "web_search")
    if not searches:
        return _metric("n/a", "无 web_search 调用")
    by_engine: Counter[str] = Counter()
    for s in searches:
        for eng, cnt in (s.get("engines") or {}).items():
            by_engine[eng] += int(cnt or 0)
    total = sum(by_engine.values())
    if not total:
        return _metric("n/a", "无引擎标记（旧日志或搜索无返回）")
    synth = by_engine.get("synthesize", 0)
    detail = " ".join(f"{k}×{v}" for k, v in by_engine.most_common())
    return _metric(round(synth / total, 3), f"synthesize {synth}/{total}（{detail}）")


def _playwright_fallback_rate(trace: Trace) -> dict:
    """渲染兜底命中率 = 靠本地 chromium 救回正文的抓取 / 全部抓取。

    【为什么单列，不并进失败分桶】
      这些是 httpx 失败、但被渲染救回来的抓取。算进失败会把失败率抬高，
      算进成功则看不出这层到底值不值。单列才能回答「兜底救了多少」——
      实测单任务 7/10，即七成页面只有渲染才拿得到。
    """
    fetches = _tools(trace, "web_fetch")
    if not fetches:
        return _metric("n/a", "无 web_fetch 调用")
    rendered = sum(1 for f in fetches if f.get("fallback") == "playwright")
    return _metric(
        round(rendered / len(fetches), 3), f"{rendered}/{len(fetches)} 次靠渲染取得"
    )


def _fetch_buckets(trace: Trace) -> tuple[int, Counter[str]] | None:
    """(抓取次数, 失败分桶)。无 web_fetch 调用返回 None。

    【失败判据 = 抛异常 或 正文 < NEAR_EMPTY_CHARS（2026-10-30 改）】
      ❌ 旧口径只看 `ok`。但 `tools_web.web_fetch` 遇 4xx/5xx **不抛异常**——
         它把 `result["error"]="HTTP 403"` 塞进返回 dict 后正常返回，日志包装只记
         `{"ok": true, "chars": 0}`。于是 403 被算成成功。
      ★ 那句「当前 tools_web 是抛异常的，口径成立」的旧注释正是这个 bug 活下来的原因：
        实现改了，注释没改，指标静默美化。
      📊 实测（2026-10-30 补跑的 8 个 supervisor 任务）：web_fetch 返回 0 字符占
         19%~60%（中位约 32%），同期指标报 0.000 —— 差 30 个百分点，方向是「美化」。

    阈值 200 与 Jina Reader 兜底同源（那边判 `cjk < 200` 就认为没拿到正文）。
    chars 缺失时不当失败：宁可漏报，也不能把「没记上」判成「失败」（同 n/a 纪律）。
    """
    fetches = _tools(trace, "web_fetch")
    if not fetches:
        return None

    buckets: Counter[str] = Counter()
    for item in fetches:
        if not item["ok"]:
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
                buckets["其他异常"] += 1
            continue

        chars = item.get("chars")
        if chars is None:
            continue                      # 无字符数字段 → 判不了，不算失败
        if chars == 0:
            buckets["空正文"] += 1          # 403/404/SPA 骨架：工具没报错，但一个字没拿到
        elif chars < NEAR_EMPTY_CHARS:
            buckets[f"近空<{NEAR_EMPTY_CHARS}字"] += 1
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


def _dimension_count(trace: Trace) -> int | None:
    """从 think 的 arguments 摘要里读维度数（形如 "list(len=2)"）。

    为什么不从 tasks.dimensions 读：那是**请求时**的维度；探索模式下维度由
    Supervisor 中途补全，两者可能不同。think 里的才是本轮真正派下去的那份。
    """
    for item in trace.thinks:
        if item["agent"] not in ("collector", "analyzer"):
            continue
        value = (item.get("arguments") or {}).get("dimensions")
        if isinstance(value, str):
            match = re.fullmatch(r"list\(len=(\d+)\)", value.strip())
            if match:
                return int(match.group(1))
    return None


def _embed_coverage(trace: Trace) -> dict:
    """embed 覆盖率 = embed_query 次数 / 维度数。

    analyzer 每个维度要 embed 一次 query 去检索。次数 < 维度数 ⇒ 有维度根本没做
    向量检索（结论多半来自模型自述而非材料）。大于 1 是重试，note 里说明。
    实测（2026-10-30 十个 supervisor 任务）：两条维度却常只 embed 2 次，
    原先只写在「embedding 调用」的 note 文字里，没有数值，统计时看不见。
    """
    queries = len(_tools(trace, "embed_query"))
    dims = _dimension_count(trace)
    if not dims:
        return _metric("n/a", "读不到维度数（think 未落 dimensions 摘要）")
    if queries == 0:
        return _metric(0.0, f"0 次 embed_query / 维度 {dims} 个 —— 完全没做向量检索")
    ratio = round(queries / dims, 3)
    extra = f"（{queries} 次其中含重试）" if queries > dims else ""
    return _metric(ratio, f"embed_query {queries} 次 / 维度 {dims} 个{extra}")


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
    """L2 工具级指标（9 项）。"""
    return {
        "工具调用次数": _tool_counts(trace),
        "搜索空结果率": _empty_search_rate(trace),
        "抓取失败率": _fetch_failure_buckets(trace),
        "渲染兜底命中率": _playwright_fallback_rate(trace),
        "重复抓取率": _duplicate_fetch_rate(trace),
        "embedding 调用": _embedding_calls(trace),
        "embed 覆盖率": _embed_coverage(trace),
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

    recall_mean = statistics.fmean(recalled)
    rerank_mean = statistics.fmean(reranked)
    # 精排筛除率：精排到底筛掉了多少。实测 2026-10-30 十个任务全是 0.0
    # （召回 4.6 → 精排 4.6，一条没筛），因为候选池 ≤ top_k=15，rerank 只排序不筛选。
    # 只看 recall_avg / rerank_avg 两个相等的数，人眼看不出来"没问题"还是"没生效"。
    rerank_drop = round(1 - rerank_mean / recall_mean, 3) if recall_mean else None

    return {
        "snapshots": count,
        "recall_avg": round(recall_mean, 1),
        "rerank_avg": round(rerank_mean, 1),
        "rerank_drop": rerank_drop,
        "coverage_skew": skew,
        "cross_dim_dup": dup,
    }


def l3_metrics(trace: Trace) -> dict[str, dict]:
    """L3 检索侧四项（展示用）。trace.retrieval 为 None 时全 n/a。"""
    data = trace.retrieval
    if not data:
        return {
            "检索快照": _metric("n/a", "无 retrieval_snapshot（任务未走到分析阶段）"),
            "精排筛除率": _metric("n/a", "无快照"),
            "竞品覆盖偏斜": _metric("n/a", "无快照"),
            "跨维度重复率": _metric("n/a", "无快照"),
        }
    return {
        "检索快照": _metric(data["snapshots"], "竞品×维度级快照行数"),
        "精排筛除率": _metric(
            data["rerank_drop"] if data.get("rerank_drop") is not None else "n/a",
            f"召回 {data['recall_avg']} → 精排 {data['rerank_avg']}；"
            "为 0 说明一条都没筛掉（候选池 ≤ top_k，精排只排序不筛选）",
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
# v4 → v5（2026-10-30）两处口径同时改，都会让老行不可直接比：
#   ① fetch_fail_rate：判据从「只看 ok」改成「ok=False 或 chars<200」。
#      旧算法下 403 被算成功（tools_web 不抛异常），实测 8 个任务里报 0.000 而真实约 32%。
#   ② round_efficiency：分母从「总轮次」改成「可计轮次」（finish 轮退出分母）。
#      旧算法下健康任务恒等于 (n-1)/n，看起来在波动其实测的是「结束在第几轮」。
# v5 → v6（2026-10-30）失败轮 / 观察响应性 / 轮次效率改读 observe.effective：
#   原来三项都读 status，而 status 只覆盖调度异常（failed 仅来自占位符竞品拦截与
#   send_task 抛异常）。实测全库 observe 86 条 status 100% completed ⇒
#   失败轮恒 0、观察响应性恒 n/a、轮次效率把采集 0 页的轮算成有效、恒 1.000。
#   同期抓取失败率中位 53%，说明空转轮大量存在，只是指标看不见。
#   同时：删「重复动作率」（4 agent 各跑一次的 DAG 上结构恒 0，与好坏无关）；
#   「参数完整率」增判空值（摘要里已有 len=N）；新增「embed 覆盖率」「精排筛除率」。
#   ⚠️ v6 与 v5 的三项指标不可直接比：v5 是常数，不是基线。
# v6 → v7（2026-10-30）：新增 playwright_fallback_rate（渲染兜底命中率），
#   并把 search_empty_rate 的口径从「空结果率」改成「synthesize 构造 URL 占比」——
#   旧口径受三层 fallback 影响恒为 0，等于没测。
METRICS_VERSION = 7

# 与 schema.sql 的 trajectory_metrics 列顺序一一对应（不含 task_id / computed_at）。
_ROW_COLUMNS = (
    "is_supervisor", "metrics_version", "termination", "rounds", "rounds_effective",
    "illegal_transitions", "repeat_rate", "failed_round_rate", "arg_completeness",
    "observation_responsiveness", "tool_calls", "search_empty_rate", "fetch_fail_rate",
    "fetch_fail_detail", "dup_fetch_rate", "embed_calls", "embed_chunks",
    "playwright_fallback_rate", "embed_coverage",
    "rerank_gap", "rerank_under_k", "funnel_search", "funnel_fetch",
    "funnel_pages", "funnel_chunks",
    # L3：检索侧（来源 retrieval_snapshot，不是 agent_logs）
    "retrieval_cells", "recall_avg", "rerank_avg", "rerank_drop_rate",
    "coverage_skew", "cross_dim_dup",
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
        # repeat_rate 列保留但恒写 None（2026-10-30）：重复动作率已删——它判的是
        # 「相邻两轮 agent+参数相同」，而 Supervisor 是 4 个 agent 各跑一次的 DAG，
        # 相邻两轮几乎不可能是同一个 agent ⇒ 结构上恒 0，与系统好坏无关。
        # 工具级重复另有 dup_fetch_rate 在测。列留着是为了不动 Grafana 面板。
        "repeat_rate": None,
        "failed_round_rate": _num(l1["失败轮占比"]),
        "arg_completeness": _num(l1["参数完整率"]),
        "observation_responsiveness": _num(l1["观察响应性"]),
        "tool_calls": _num(l2["工具调用次数"]),
        "search_empty_rate": _num(l2["搜索空结果率"]),
        "fetch_fail_rate": _num(l2["抓取失败率"]),
        "fetch_fail_detail": dict(fetch[1]) if fetch else None,
        "playwright_fallback_rate": _num(l2["渲染兜底命中率"]),
        "dup_fetch_rate": _num(l2["重复抓取率"]),
        "embed_calls": (embed[0] + embed[1]) if embed else None,
        "embed_chunks": embed[2] if embed else None,
        "embed_coverage": _num(l2["embed 覆盖率"]),
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
        "rerank_drop_rate": (
            trace.retrieval.get("rerank_drop") if trace.retrieval else None
        ),
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
    if isinstance(l1["失败轮占比"]["value"], float) and l1["失败轮占比"]["value"] > 0:
        out.append(
            f"无效轮占 {l1['失败轮占比']['value']}（{l1['失败轮占比']['note']}）"
            "—— 调度成功但没有业务产出"
        )
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
