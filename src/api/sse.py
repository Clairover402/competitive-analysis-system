"""SSE 进度推送 — 从 agent_logs 表轮询读取并推送 Server-Sent Events。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 架构 — SSE 设计决策】
═══════════════════════════════════════════════════════════════════════════════

● 完整链路（从 Agent 执行到前端展示）：

  Agent 执行动作             AuditLogger.log()              SSE event_generator 轮询
      │                          │                              │
      ├─ web_search("飞书") ──→ INSERT INTO agent_logs ──→ 1秒后读到 ──→ yield progress 事件
      │                        (task_id, agent,                │
      │                         action, response)               │
      ├─ analyze("定价") ────→ INSERT INTO agent_logs ──→ 1秒后读到 ──→ yield progress 事件
      │                                                         │
      ├─ writer("报告") ─────→ INSERT INTO agent_logs ──→ 1秒后读到 ──→ yield progress 事件
      │                                                         │
      └─ tasks.status=completed ─────────────────────────→ 检查到 completed ──→ yield complete → break

  生产端：Agent 调用 AuditLogger.log() 写入 agent_logs 表（Harness 已有基础设施，零开发成本）
  消费端：event_generator() 每秒用 created_at > last_created_at 增量查新日志，逐条转 SSE
  解耦点：生产方不感知消费方——Agent 不知道 SSE 在轮询，SSE 不依赖 Agent 主动推送

───────────────────────────────────────────────────────────────────────────────

● 为什么从 agent_logs 表轮询，而不是用 asyncio.Queue / WebSocket？

  ▸ 零耦合（与 Harness 审计日志复用）：
    — Harness 的 AuditLogger.log() 在每次 Agent 调用时写入 agent_logs
    — SSE 直接读这张表——不需要新建管道、不需要 Agent 感知 SSE 通道
    — 生产端写 DB、消费端读 DB，二者完全解耦
    — 典型场景：一个 task 产生 20~50 条 agent_logs → 每次 1 行 SELECT + 1 次 yield

  ▸ 1 秒延迟在竞品分析场景可接受：
    — 竞品分析任务耗时几分钟到十几分钟
    — 1 秒轮询间隔在这个时间尺度上几乎感觉不到
    — 如果要做到真正的实时推送（毫秒级），需要让 pipeline/supervisor 引擎
      感知 SSE 通道 → 增加耦合 → 不值得

  ▸ SSE 比 WebSocket 更轻量：
    — SSE 是纯 HTTP 单向流（服务端→客户端），不需要协议升级握手
    — 进度推送是单向的——客户端只需接收，不需要回传指令
    — nginx 原生支持 SSE 代理（proxy_buffering off），WebSocket 需要额外配置
      （proxy_set_header Upgrade + Connection）

───────────────────────────────────────────────────────────────────────────────

● SSE 事件类型（由前端 EventSource 按 event 字段分发）：

  事件类型       触发条件                     data 内容
  ─────────────────────────────────────────────────────────────────────
  progress     每次 Agent 调用完成           agent / action / message / progress_pct
  progress     错误日志（error 非空）        同上 + error=true
  complete     tasks.status = "completed"    task_id / report_id / quality_score / elapsed_ms
  error        任务失败 / 超时                task_id / message / elapsed_ms

  前端收到 complete 或 error 事件后关闭 EventSource 连接

───────────────────────────────────────────────────────────────────────────────

● 为什么用 created_at > last_created_at 而不是 id > last_id？

  agent_logs 表的 id 是 UUID（随机生成），不保证插入顺序。
  两个几乎同时写入的日志，后插入的 UUID 可能字典序更小 → 用 id > last_id
  会跳过这条日志。created_at 是 timestamptz（插入时的服务器时间），
  严格按物理顺序递增 → 不会漏。

───────────────────────────────────────────────────────────────────────────────

● 断连不中断后台任务

  asyncio.CancelledError 只清理 SSE 连接（EventGenerator 协程取消），
  不关闭后台 task。task 由 POST /api/tasks 创建的 asyncio.create_task()
  独立运行——与 SSE 连接无关。客户端可以重连重新建立 SSE 流。

───────────────────────────────────────────────────────────────────────────────

● 轮询超时保护

  总超时 300 秒（5 分钟）。超过后 yield error 事件 + break。
  目的是防止"任务卡死但状态永远不变成 failed"时 SSE 连接永久挂起。
  300 秒对齐最长的 Pipeline 执行预期（采集+分析+撰写+质检 < 5 分钟）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncpg import Pool

logger = logging.getLogger(__name__)

# ── Agent 名称 → 进度百分比映射 ──
# 按流水线阶段分配权重：
#   Collector 拿 25%，Analyzer 拿 25%（累计 50%），
#   Writer 拿 25%（累计 75%），Quality 拿 20%（累计 95%），
#   最后 complete 事件置为 1.0（100%）。
# Supervisor 模式下各 agent 调用不分先后，
#   Supervisor = 0.50 作为"调度中"的估计值。
_PROGRESS_MAP: dict[str, float] = {
    "collector": 0.25,   # 采集占 25%
    "analyzer": 0.50,    # 分析占 25%（累计 50%）
    "writer": 0.75,      # 撰写占 25%（累计 75%）
    "quality": 0.95,     # 质检占 20%（累计 95%）
    "supervisor": 0.50,  # Supervisor 模式下："正在决策中"
}


def _estimate_progress(agent_name: str) -> float:
    """根据 agent 名称映射进度百分比。"""
    return _PROGRESS_MAP.get(agent_name, 0.5)


async def event_generator(task_id: str, pool, total_timeout: float = 300.0):
    """SSE 事件生成器——从 agent_logs 表轮询进度。

    【完整流程 — 8 步】

    Step 1: 初始化 last_created_at = "1970-01-01"（确保读第一条日志）
    Step 2: while True 入循环
    Step 3: 超时保护（超过 total_timeout → yield error + return）
    Step 4: 增量查询 agent_logs：SELECT ... WHERE created_at > last_created_at
    Step 5: 逐条转 SSE progress 事件（区分正常日志和错误日志）
    Step 6: 查询 tasks.status，判断是否完成
    Step 7: completed → 查 reports 表拿最终报告信息 → yield complete + return
            failed → yield error + return
            running/pending → 继续
    Step 8: await asyncio.sleep(1)，回到 Step 3

    【L4 工程 — 为什么 Step 5-6 分两步？】
    前端在收到 progress 事件时更新进度条，在收到 complete 事件时跳转结果页。
    如果合并成一步，前端无法在中途展示进度——只能等最终结果。

    【生产部署配置 — nginx 代理 SSE 需要的额外配置】
    proxy_buffering off;         ← 关键：关闭缓冲，否则 SSE 事件被 nginx 攒一起发
    proxy_cache off;
    proxy_read_timeout 300s;     ← 与 total_timeout 对齐
    chunked_transfer_encoding on;

    【端到端延迟】
    Agent 执行 → AuditLogger.log() INSERT
    → 0~1 秒后 event_generator 读到 → EventSourceResponse 写入 HTTP 响应流
    → nginx 代理发出 → 浏览器 EventSource.onmessage 触发
    端到端延迟 ≈ 1 秒（轮询间隔）+ 网络 RTT（< 50ms）

    Args:
        task_id: 任务 UUID
        pool: asyncpg 连接池
        total_timeout: 总超时秒数（默认 300 秒 = 5 分钟）
            — 对齐最长 Pipeline 执行预期（采集+分析+撰写+质检 < 5 分钟）
            — 防止任务卡死但 status 永远不变时 SSE 连接永久挂起

    Yields:
        dict: {"event": "progress"|"complete"|"error", "data": "<JSON 字符串>"}
              前端 EventSource 根据 event 字段分发到不同处理函数：
              — addEventListener("progress", ...)  → 更新进度条
              — addEventListener("complete", ...)  → 跳转结果页
              — addEventListener("error", ...)     → 显示错误提示
    """
    # ── Step 1: 初始化阅读位置 ──
    # 用时间戳跟踪已读过的最新一条日志
    # 为什么不选 UUID → agent_logs 表 id 是 UUID，不保证插入顺序，
    #   后插入的 UUID 字典序可能更小，用 id > last_id 会漏日志
    # created_at 是 timestamptz，严格按物理时间递增 → 不会漏
    last_created_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
    # 记录轮询开始时间——用于超时保护和 elapsed_ms 上报
    start_time = asyncio.get_running_loop().time()

    try:
        # ── Step 2: 轮询循环 ──
        while True:
            # ── Step 3: 超时保护 ──
            # 场景：Agent 卡在某个 LLM 调用上（API 超时），tasks.status
            # 仍然是 "running"，但永远不会变成 "completed"。
            # 不加超时保护，SSE 连接会永久挂起 → 浏览器侧永远转圈。
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > total_timeout:
                yield {
                    "event": "error",
                    "data": json.dumps({
                        "task_id": task_id,
                        "message": f"任务执行超时（{total_timeout} 秒）",
                        "elapsed_ms": int(elapsed * 1000),
                    }, ensure_ascii=False),
                }
                return

            # ── Step 4: 增量查询 agent_logs ──
            # WHERE created_at > last_created_at 只读"上次读到之后"的新日志
            # 第一轮 last_created_at = "1970-01-01" → 读到全部已有日志
            # 后续轮次 last_created_at = 上一轮最后一条的 created_at → 只读新增
            # try/except 保护：DB 短暂不可用时不崩溃，本轮轮空，下轮重试
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """SELECT id, agent_name, action, response,
                                  error, duration_ms, created_at
                           FROM agent_logs
                           WHERE task_id = $1
                             AND created_at > $2::timestamptz
                           ORDER BY created_at ASC""",
                        task_id, last_created_at,
                    )
            except Exception as e:
                logger.warning("SSE 轮询 agent_logs 失败: %s", e)
                rows = []

            # ── Step 5: 逐条转 SSE progress 事件 ──
            for row in rows:
                # 更新阅读位置到本条日志的时间戳
                # 因为 ORDER BY created_at ASC，最后一条就是新的阅读位置
                last_created_at = row["created_at"]

                agent = row["agent_name"] or "unknown"
                action = row["action"] or ""
                error_msg = row["error"]
                response = _normalize_response(row["response"])
                duration_ms = row["duration_ms"]

                if error_msg:
                    # ── 错误日志 → 带 error=true 的 progress 事件 ──
                    # 前端收到后可以标红显示，但不中断进度展示
                    message = f"{agent}: {error_msg}"
                    progress = _estimate_progress(agent)
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "agent": agent,
                            "action": action,
                            "message": message,
                            "progress_pct": progress,
                            "duration_ms": duration_ms,
                            "error": True,
                        }, ensure_ascii=False),
                    }
                else:
                    # ── 正常日志 → 标准 progress 事件 ──
                    message = _format_progress_message(agent, action, response, duration_ms)
                    progress = _estimate_progress(agent)
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "agent": agent,
                            "action": action,
                            "message": message,
                            "progress_pct": progress,
                            "duration_ms": duration_ms,
                        }, ensure_ascii=False),
                    }

            # ── Step 6: 检查任务状态 ──
            # 不依赖 agent_logs 的最后一条来判断完成——
            # 有时日志写入和 status 更新之间有时间差，直接读 tasks 表最准确
            try:
                async with pool.acquire() as conn:
                    task_row = await conn.fetchrow(
                        "SELECT status FROM tasks WHERE id = $1", task_id
                    )
            except Exception as e:
                logger.warning("SSE 状态查询失败 task_id=%s: %s", task_id, e)
                task_row = None

            if task_row:
                status = task_row["status"]
                if status == "completed":
                    # ── 查询最终报告信息 ──
                    # completed 事件附带 report_id + quality_score，
                    # 前端可以据此预先知道"报告 ID"并决定跳转链接
                    try:
                        async with pool.acquire() as conn:
                            report = await conn.fetchrow(
                                """SELECT id, quality_score, version
                                   FROM reports
                                   WHERE task_id = $1
                                   ORDER BY version DESC
                                   LIMIT 1""",
                                task_id,
                            )
                    except Exception as e:
                        logger.warning("SSE 报告查询失败 task_id=%s: %s", task_id, e)
                        report = None

                    data = {
                        "task_id": task_id,
                        "status": "completed",
                        "elapsed_ms": int(elapsed * 1000),
                    }
                    if report:
                        data["report_id"] = str(report["id"])
                        data["quality_score"] = (
                            float(report["quality_score"])
                            if report["quality_score"] is not None
                            else None
                        )
                    yield {
                        "event": "complete",
                        "data": json.dumps(data, ensure_ascii=False),
                    }
                    return  # ← 跳出 while True，结束生成器

                elif status == "failed":
                    yield {
                        "event": "error",
                        "data": json.dumps({
                            "task_id": task_id,
                            "message": "任务执行失败",
                            "elapsed_ms": int(elapsed * 1000),
                        }, ensure_ascii=False),
                    }
                    return  # ← 跳出 while True，结束生成器

            # ── Step 8: 等待 1 秒 ──
            # 1 秒间隔的选择：
            #   < 1 秒 → PG 查询频率太高（每秒多次 SELECT），浪费连接池
            #   > 2 秒 → 用户感知延迟明显（Agent 完成后还要再等 2 秒才看到结果）
            #   1 秒 → 在 DB 压力和用户体验之间取平衡点
            await asyncio.sleep(1)

    except asyncio.CancelledError:
        # ── 客户端断开连接 → 清理退出，不关闭后台 task ──
        # 关键：这里只清理 SSE 连接（EventGenerator 协程被取消），
        # 不关闭后台 task——task 由 POST /api/tasks 创建的
        # asyncio.create_task() 独立运行。
        # 客户端可以重连 GET /api/tasks/{id}/stream 重新建立 SSE 流，
        # 重复的日志不会重复展示（因为 last_created_at 基于创建时间，幂等）。
        logger.info("SSE 客户端断开: task_id=%s", task_id)
        return


def _normalize_response(raw: object) -> dict | None:
    """将 asyncpg 返回的 response 字段统一转为 dict。

    asyncpg 的 JSONB 行为因 codec 版本而异：
    — 有 codec → 直接返回 dict
    — 无 codec → 返回 JSON 字符串
    — NULL → None

    这个函数确保 _format_progress_message 收到的永远是 dict | None，
    而不是 str。否则 .get() 会在字符串上触发 AttributeError。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _format_progress_message(
    agent: str,
    action: str,
    response: dict | None,
    duration_ms: float | None = None,
) -> str:
    """根据 agent + action + response 数据构造详细的中文进度消息。

    【设计思路】参考控制台日志的详细程度，将 response 中的结构化数据
    转为可读的用户消息。每条消息包含：动作摘要 + 关键数据指标 + 耗时。

    【前端展示效果】
      🟢 Collector 采集完成: 3竞品 15页面 42片段 · 3.2秒
      🟡 Analyzer 分析完成: 5维度(定价/功能/...) · 4.5秒
      🔵 Writer  报告生成: 3,200字符 · 1.2秒
      🟣 Quality 质检评分: 82.5分 ✅通过 · 0.8秒

    Args:
        agent: collector / analyzer / writer / quality / supervisor
        action: 具体 action 名
        response: Agent 响应 JSONB，包含该 Agent 执行结果的结构化数据
        duration_ms: 执行耗时（毫秒）

    Returns:
        详细的中文进度消息，如 "Collector 采集完成: 3竞品 15页面 42片段 · 3.2秒"
    """
    if response is None:
        response = {}

    # ── 耗时格式化 ──
    time_str = ""
    if duration_ms is not None:
        if duration_ms >= 1000:
            time_str = f" · {duration_ms / 1000:.1f}秒"
        else:
            time_str = f" · {duration_ms:.0f}毫秒"

    # ═══════════════════════════════════════════════════════════
    # Collector: 采集阶段
    # ═══════════════════════════════════════════════════════════
    if agent == "collector":
        if action == "collect_and_store":
            total_pages = response.get("total_pages", 0)
            total_chunks = response.get("total_chunks", 0)
            competitors_count = response.get("competitors_count", 0)
            # per-competitor details
            comp_details = response.get("per_competitor", {})
            comp_parts = []
            for comp_name, info in sorted(comp_details.items()):
                pages = info.get("pages", 0)
                chunks = info.get("chunks", 0)
                comp_parts.append(f"{comp_name}({pages}页/{chunks}段)")
            detail = "、".join(comp_parts) if comp_parts else f"{total_pages}页面 {total_chunks}片段"
            return f"✅ Collector 采集完成: {competitors_count}竞品 → {detail}{time_str}"
        return f"Collector: {action}{time_str}"

    # ═══════════════════════════════════════════════════════════
    # Analyzer: 分析阶段
    # ═══════════════════════════════════════════════════════════
    if agent == "analyzer":
        if action == "multi_dimension_analysis":
            dims = response.get("dimensions_analyzed", [])
            dim_count = response.get("dimensions_count", len(dims))
            dim_names = response.get("dimension_names", dims)
            dims_short = "、".join(dim_names[:5])
            if len(dim_names) > 5:
                dims_short += f"等{len(dim_names)}个"
            # per-dimension detail snippet
            dim_details = response.get("per_dimension", {})
            if dim_details:
                snippets = []
                for dname, info in list(dim_details.items())[:3]:
                    comps = info.get("competitors_analyzed", 0)
                    data_flag = info.get("has_data", True)
                    flag = "" if data_flag else " [数据不足]"
                    snippets.append(f"{dname}({comps}竞品{flag})")
                dims_short = "、".join(snippets)
            return f"✅ Analyzer 分析完成: {dim_count}维度 → {dims_short}{time_str}"
        return f"Analyzer: {action}{time_str}"

    # ═══════════════════════════════════════════════════════════
    # Writer: 撰写阶段
    # ═══════════════════════════════════════════════════════════
    if agent == "writer":
        if action == "generate_report":
            report_len = response.get("report_length", 0)
            is_rewrite = response.get("rewrite", False)
            prefix = "🔄 Writer 改写完成" if is_rewrite else "✅ Writer 报告生成"
            return f"{prefix}: {report_len:,}字符{time_str}"
        return f"Writer: {action}{time_str}"

    # ═══════════════════════════════════════════════════════════
    # Quality: 质检阶段
    # ═══════════════════════════════════════════════════════════
    if agent == "quality":
        if action == "judge_report":
            score = response.get("score", 0)
            passed = response.get("passed", False)
            dim_scores = response.get("dimension_scores", {})
            # 维度评分明细
            if dim_scores:
                score_parts = [f"{d}={s}分" for d, s in dim_scores.items()]
                score_detail = " | ".join(score_parts[:5])
            else:
                score_detail = ""
            status = "✅通过" if passed else "❌不通过(需重写)"
            msg = f"✅ Quality 质检评分: {score:.1f}分 {status}"
            if score_detail:
                msg += f" | {score_detail}"
            return f"{msg}{time_str}"
        return f"Quality: {action}{time_str}"

    # ═══════════════════════════════════════════════════════════
    # Supervisor: 调度阶段
    # ═══════════════════════════════════════════════════════════
    if agent == "supervisor":
        if action == "route":
            route_to = response.get("route", "?")
            reason = response.get("reason", "")
            detail = f" → {route_to}"
            if reason:
                detail += f" ({reason})"
            return f"🧠 Supervisor 路由决策{detail}{time_str}"
        return f"Supervisor: {action}{time_str}"

    # ── 通用兜底 ──
    return f"{agent}: {action}{time_str}"
