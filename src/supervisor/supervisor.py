"""Supervisor Graph — LangGraph StateGraph 编排引擎。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Supervisor 是竞品分析系统的"探索模式控制器"。
纯 Python while 循环重构为 LangGraph StateGraph，与 Pipeline（Phase 5）技术栈统一。

  图结构:  think ──→ act ──→ observe ──→ route(条件边)
             ^                              |
             |──── "continue" ──────────────|
                         |
                     "end" → END

  4 个节点职责:
  ┌──────────┬──────────────────────────────────────────────────────────┐
  │ 节点     │ 做什么                                                    │
  ├──────────┼──────────────────────────────────────────────────────────┤
  │ think    │ 读 state → 检索长期记忆 → 拼 Prompt → LLM 决策 → 写      │
  │          │ pending_decision / reasoning_trace / is_complete         │
  │          │ 【L3】闭包工厂注入 LLM + router + 记忆组件                 │
  ├──────────┼──────────────────────────────────────────────────────────┤
  │ act      │ 读 pending_decision → 构造 A2ATask → router.send_task    │
  │          │ → 写 pending_task_result/agent/status                    │
  │          │ 【L3】闭包工厂注入 router（不持有 LLM）                    │
  ├──────────┼──────────────────────────────────────────────────────────┤
  │ observe  │ 读 pending_* → 映射 result 到业务字段 → 提取长期记忆     │
  │          │ → 增量摘要 → 每10轮全量合并 → current_round += 1          │
  │          │ 【L3】闭包工厂注入 summarizer + memory_engine             │
  ├──────────┼──────────────────────────────────────────────────────────┤
  │ route    │ 读 is_complete + current_round vs max_rounds              │
  │          │ → "continue"(回到think) 或 "end"(到END)                   │
  │          │ 【L3】add_conditional_edges 条件路由                      │
  └──────────┴──────────────────────────────────────────────────────────┘

  与 Pipeline（Phase 5）的技术栈统一:
  ┌──────────────────┬─────────────────────┬─────────────────────┐
  │ 维度             │ Pipeline            │ Supervisor          │
  ├──────────────────┼─────────────────────┼─────────────────────┤
  │ 编排引擎         │ StateGraph          │ StateGraph          │
  │ Checkpoint       │ PostgresSaver       │ PostgresSaver       │
  │ 状态定义         │ AgentState(TypedDict)│ SupervisorState     │
  │ 图结构           │ 直线(串行6节点)      │ 闭环(think↺route)  │
  │ thread_id 前缀   │ pipeline-{task_id}  │ supervisor-{task_id}│
  └──────────────────┴─────────────────────┴─────────────────────┘

  【L5 架构】闭包工厂 vs 类方法 — 为什么不写成 class Supervisor？
  ─────────────────────────────────────────────────────────
  淘汰设计:   class Supervisor { self.llm, self.router, ... }
              async def run(self): while(...) ...
  当前设计:   _make_node_think(llm, router, ...) → node_think
              _make_node_act(router)             → node_act
              _make_node_observe(summarizer, ...) → node_observe

  闭包工厂的优势:
  — 依赖精确到节点: think 持有 LLM+router+retrieval+memory，act 只持有 router
  — 最小权限: 节点函数看不到不需要的依赖（act 不需要 retrieval_strategy）
  — 可测试: 单元测试只想测 think → 只创建 node_think，不构建完整图


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 闭包工厂: _make_node_think/act/observe 创建节点函数 + DI 注入
  §2 StateGraph 5 步构建: graph → add_node ×3 → add_edge ×2 → add_conditional_edges → compile
  §3 条件路由: route_after_observe 的双重终止条件
  §4 JSON 解析: _extract_json 纯函数 + re.DOTALL + 2 次 LLM 重试
  §5 operator.add reducer: reasoning_trace + messages_buffer 列表累加
  §6 checkpoint 自动持久化: graph.compile(checkpointer=saver) 每个节点后自动 aput()

═══════════════════════════════════════════════════════════════════════════════
                        【L5 决策 — 关键工程选择】
═══════════════════════════════════════════════════════════════════════════════

  决策 1: while 循环 → StateGraph
    原 while 需手动 _save_checkpoint + 手动恢复。StateGraph 绑定 PostgresSaver
    后每个节点自动持久化 + 同一 thread_id 自动断点续传。

  决策 2: 闭包工厂 → DI 注入
    每节点只持有必要的依赖，act 不需要 retrieval_strategy，observe 不需要 router。

  决策 3: 长期记忆永不阻塞
    think 前检索（同步，必须等结果）→ observe 后写入（异步，不阻塞下一轮）。

  决策 4: 假阴代价 > 假阳 → 每次 think 都注入 Top 5 记忆
    不让 LLM 决定"要不要查记忆"，每次都传，LLM 自己判断相关性。

  决策 5: JSON 解析 2 次失败 → 安全退出（finish）
    不无限重试消耗 token，2 次足以覆盖 LLM 的随机波动。

  决策 6: thread_id 前缀隔离
    Pipeline 用 pipeline-{id}，Supervisor 用 supervisor-{id}，同表不同前缀。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from langgraph.graph import StateGraph, END

from src.supervisor.state import SupervisorState
from src.supervisor.a2a import A2ATask, A2ARouter, TaskStatus
from src.pipeline.checkpoint import PostgresSaver

if TYPE_CHECKING:
    from langgraph.graph import CompiledStateGraph
    from asyncpg import Pool
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer
    from src.memory.summarizer import MemorySummarizer
    from src.memory.retrieval import MemoryRetrievalStrategy
    from src.memory.long_term import LongTermMemoryEngine

logger = logging.getLogger(__name__)

# ============================================================
# 【L4 工程】Prompt 常量 — 固定骨架，运行时动态插值
# ============================================================

_SUPERVISOR_SKELETON = (
    "你是竞品分析系统的 Supervisor（调度器）。\n\n"

    "## 可用 Agent\n"
    "{agent_list}\n\n"                    # ← router.list_capabilities() 动态注入

    "## 当前任务\n"
    "- 任务ID: {task_id}\n"
    "- 标题: {title}\n"
    "- 用户查询: {user_query}\n"
    "- 分析维度: {dimensions}\n"
    "- 指定竞品: {competitors_list}\n\n"

    "## 当前进度\n"
    "{progress}\n\n"                      # ← 根据 state 各字段动态构造

    "## 历史推理轨迹（最近3轮）\n"
    "{reasoning_trace}\n\n"               # ← state.reasoning_trace[-3:]

    "## 长期记忆上下文\n"
    "{memory_context}\n\n"                # ← MemoryRetrievalStrategy Top 5

    "## 输出格式\n"
    "请以 JSON 格式输出下一步决策：\n"
    "```json\n"
    "{{\n"
    '    "thought": "分析当前状态，解释为什么选择这个行动（中文）",\n'
    '    "action": "<agent_name 或 finish>",\n'
    '    "arguments": {{}},\n'
    '    "reason": "选择该行动的原因（一句话，中文）"\n'
    "}}\n```\n\n"

    "## 规则\n"
    "0. 如果竞品列表包含「待探索」或为空，禁止调用 collector！应先用内部知识推理可能的竞品名称，\n"
    "   然后更新 found_competitors，再进入正常流程。\n"
    "1. 如果所有必要数据已收集、分析完成、报告已生成、质量已通过 -> action=\"finish\"\n"
    "2. 如果需要收集竞品数据 -> action=\"collector\"，arguments 必须包含:\n"
    "   - competitors: [竞品A, 竞品B, ...]  — 使用上方「指定竞品」列表（不能是「待探索」）\n"
    "   - dimensions: [维度1, 维度2, ...]     — 使用上方「分析维度」列表\n"
    "3. 如果已收集数据但未分析 -> action=\"analyzer\"，arguments 同样包含 competitors + dimensions\n"
    "4. 如果分析完成但未写报告 -> action=\"writer\"，arguments 包含 title + analysis_results\n"
    "5. 如果报告已生成但未评分 -> action=\"quality\"，arguments 包含 report_markdown\n"
    "6. 如果质量未通过且还有剩余轮次 -> action=\"writer\"（重写）\n"
    "7. 如果质量已通过 -> action=\"finish\"\n"
    "8. 每轮只能选择一个 agent 执行\n"
    "9. 输出必须是合法 JSON，不要有任何额外文本\n"
    "10. 如果同一 agent 连续调用 2 次都返回空结果（0页面/0分析），代表该 agent 无法完成任务，\n"
    "    应输出 action=\"finish\"，并在 reason 中说明「因数据不足，需用户补充信息」"
)
"""Supervisor System Prompt 骨架。

【L4 工程】固定骨架 + 动态插值 两种构造方式的对比
───────────────────────────────────────────────
✗ 全部动态生成: 每次 think 重新拼字符串 → 难以 diff 调试
✓ 固定骨架 + .format(): 骨架不动，只换插值变量 → git diff 友好、Prompt 版本可管理

【L5 决策】为什么不把 9 条规则放在 LLM 的 system message 里？
─────────────────────────────────────────────────────────
放在 user message（这里）比 system message 更有效——DeepSeek 对 user 中的指令
服从度更高。system message 容易被长上下文冲淡。

9 条规则对应 ReAct 循环的全部可能路径:
  Rule 1/7 → finish（退出）
  Rule 2   → collector（启动）
  Rule 3   → analyzer（分析）
  Rule 4   → writer（写作）
  Rule 5   → quality（评分）
  Rule 6   → writer retry（质量不通过重写）
  Rule 8   → 单 Agent 执行（防止笛卡尔爆炸）
  Rule 9   → 纯 JSON 输出（解析要求）
"""


# ══════════════════════════════════════════════════════════════════════════════
# §1 _extract_json — LLM JSON 解析纯函数
# ══════════════════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> dict | None:
    """从 LLM 响应文本中提取第一个完整 JSON 对象。

    【L3 核心考点】JSON 解析的鲁棒模式
    ────────────────────────────────
    为什么用 re.search 而非直接 json.loads？

    LLM 输出不可控——可能包裹 markdown 代码块、可能加注释、可能多对象：
      坏 case 1: ```json\n{"action":"collector"}\n```     → json.loads 直接报错
      坏 case 2: 先说一段中文，再给 {"action":"collector"} → 同上
      坏 case 3: {"a":1}\n{"b":2}                         → 取第一个即可

    防御策略（三层）:
    ┌─────────────┬─────────────────────────────────────────────┐
    │ 层          │ 机制                                        │
    ├─────────────┼─────────────────────────────────────────────┤
    │ L1 正则提取 │ re.search(r"\\{.*\\}", text, re.DOTALL)      │
    │             │ re.DOTALL 让 . 匹配换行 → 跨行 JSON 不走丢   │
    │ L2 json解析 │ json.loads(match.group())                   │
    │             │ 解析失败 → 返回 None（调用方决定是否重试）     │
    │ L3 调用重试 │ _make_node_think 内 for attempt in range(2)  │
    │             │ 2 次 LLM 调用后仍失败 → 安全退出              │
    └─────────────┴─────────────────────────────────────────────┘

    【L4 工程】为什么是独立纯函数？
    ────────────────────────────
    — 无副作用: 只读 text，不写任何外部状态
    — 可单独测试: 不需要 Supervisor 实例、不需要 LLM
    — 可复用: Pipeline 的 Prompt 输出解析也可用同一个函数

    Args:
        text: LLM 原始响应文本（可能含 markdown 标记）

    Returns:
        解析成功的 dict；解析失败返回 None
    """
    text = text.strip()
    # L1: 正则匹配第一个 { 到最后一个 }（跨行）
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            # L2: JSON 解析
            return json.loads(match.group())
        except json.JSONDecodeError as e:
            # JSON 格式错误 → 记录警告，返回 None 由调用方重试
            logger = logging.getLogger(__name__)
            logger.warning("LLM 响应 JSON 解析失败，等待重试。原始响应前100字符: %s",
                           text[:100])
            pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# §2 长期记忆写入已禁用
# ══════════════════════════════════════════════════════════════════════════════
# 【L5 决策】_extract_memory 函数已删除
# 原因：自动从 Agent 执行结果提取"决策/偏好/事实"写入 agent_memories 表
# 会把任务临时数据（如"Google市场份额23%"）当作用户画像，严重污染长期记忆
# 长期记忆改为手动写入路径（仅用户明确表达的偏好/决策驱动）
# 参见 MEMORY.md 2026-07-28 教训记录


# ══════════════════════════════════════════════════════════════════════════════
# §3 _make_node_think — think 节点闭包工厂
# ══════════════════════════════════════════════════════════════════════════════

def _make_node_think(
    llm: ChatDeepSeek,
    router: A2ARouter,
    retrieval_strategy: MemoryRetrievalStrategy | None = None,
    memory_engine: LongTermMemoryEngine | None = None,
):
    """创建 think 节点函数——注入 LLM + router + 记忆组件。

    【L3 核心考点】闭包工厂 = DI 容器
    ────────────────────────────────
    外层函数 _make_node_think 是"工厂"，接收依赖 → 内层函数 node_think 是"产品"，
    持有工厂注入的依赖。LangGraph 调 node_think(state) 时，内部直接用闭包捕获的
    llm/router/retrieval/memory_engine。

    调用链: build_supervisor_graph(顶层) → _make_node_think(llm, router, ...)
            → 返回 node_think(state) → LangGraph 每轮调 node_think(state)

    think 节点的 4 步执行流程:
    ┌─────┬────────────────────────────┬──────────────────────────────────┐
    │ 步骤 │ 做什么                     │ 产出                             │
    ├─────┼────────────────────────────┼──────────────────────────────────┤
    │  ①  │ 构建进度描述 + 读轨迹      │ progress 文本 + trace_text        │
    │  ②  │ 长期记忆检索               │ memory_context（Top 5 条记忆）     │
    │  ③  │ 拼装 Prompt + LLM 调用     │ LLM 响应文本                      │
    │  ④  │ JSON 解析（2 次重试）      │ pending_decision / reasoning_trace │
    │     │                            │ is_complete / final_output        │
    └─────┴────────────────────────────┴──────────────────────────────────┘

    Args:
        llm: Supervisor 专用 ChatDeepSeek（temperature=0.3）
        router: A2ARouter（提供 list_capabilities() 给 Prompt）
        retrieval_strategy: 可选长期记忆检索策略
        memory_engine: 可选长期记忆引擎

    Returns:
        async def node_think(state: SupervisorState) -> dict: 满足 LangGraph
        节点签名的异步函数，返回部分 state 更新
    """
    async def node_think(state: SupervisorState) -> dict:
        """think 节点: 分析当前状态，决策下一步行动。

        ├─ 读: user_query, found_competitors, collected_data, analysis_results,
        │      report_content, quality_score/passed, reasoning_trace, user_id
        └─ 写: pending_decision, reasoning_trace(append), is_complete, final_output
        """
        # ─── 步骤①: 构建进度描述 ───
        progress_parts = []
        if state.get("found_competitors"):
            progress_parts.append(
                f"已发现竞品: {', '.join(state['found_competitors'])}"
            )
        if state.get("collected_data"):
            progress_parts.append(
                f"已收集数据: {len(state['collected_data'])} 个竞品"
            )
        if state.get("analysis_results"):
            progress_parts.append(
                f"已完成分析: {len(state['analysis_results'])} 个维度"
            )
        if state.get("report_content"):
            progress_parts.append("报告已生成")
        if state.get("quality_score") is not None and state["quality_score"] > 0:
            progress_parts.append(
                f"质量评分: {state['quality_score']}"
                f" (通过={'是' if state['quality_passed'] else '否'})"
            )
        progress = "\n".join(progress_parts) if progress_parts else "尚未开始"

        # ─── 步骤①续: 读取推理轨迹（最近 3 条） ───
        trace = state.get("reasoning_trace", [])
        trace_text = ""
        if trace:
            for t in trace[-3:]:
                trace_text += (
                    f"- 第{t.get('round', '?')}轮: "
                    f"{t.get('thought', '')} -> {t.get('action', '')}"
                    f" ({t.get('reason', '')})\n"
                )
        if not trace_text:
            trace_text = "（尚无推理记录）"

        # ─── 步骤②: 长期记忆检索 ───
        memory_context = ""
        if retrieval_strategy is not None and memory_engine is not None:
            try:
                user_query = state.get("user_query", "")
                memories = await retrieval_strategy.retrieve_if_needed(
                    user_id=state["user_id"],
                    message=user_query,
                    engine=memory_engine,
                )
                if memories:
                    memory_parts = []
                    for m in memories[:5]:
                        memory_parts.append(
                            f"- [{m.get('type', '?')}] {m.get('content', '')}"
                        )
                    memory_context = "\n".join(memory_parts)
                    logger.info("长期记忆检索: %d 条", len(memory_parts))
            except Exception:
                logger.exception("长期记忆检索失败，继续执行")
        if not memory_context:
            memory_context = "（无相关长期记忆）"

        # ─── 步骤③: 拼装 Prompt + LLM 调用 ───
        dimensions = state.get("dimensions", [])
        competitors = state.get("found_competitors", [])
        prompt = _SUPERVISOR_SKELETON.format(
            agent_list=router.list_capabilities(),
            task_id=state.get("task_id", ""),
            title=state.get("title", ""),
            user_query=state.get("user_query", ""),
            dimensions=", ".join(dimensions) if dimensions else "（待探索）",
            competitors_list=", ".join(competitors) if competitors else "（待探索）",
            progress=progress,
            reasoning_trace=trace_text,
            memory_context=memory_context,
        )

        # ─── 步骤④: LLM 调用 + JSON 解析（最多 2 次） ───
        for attempt in range(2):
            try:
                response = await llm.ainvoke(prompt)
                text = (
                    response.content
                    if hasattr(response, "content")
                    else str(response)
                )

                decision = _extract_json(text)
                if decision:
                    action = decision.get("action", "finish")
                    logger.info(
                        "Supervisor 决策(attempt=%d): action=%s, thought=%.60s",
                        attempt + 1, action, decision.get("thought", "")
                    )
                    return {
                        # pending_decision → act 节点消费
                        "pending_decision": {
                            "thought": decision.get("thought", ""),
                            "action": action,
                            "agent": decision.get("action", ""),
                            "arguments": decision.get("arguments", {}),
                            "reason": decision.get("reason", ""),
                        },
                        # reasoning_trace → operator.add 追加
                        "reasoning_trace": [{
                            "round": state.get("current_round", 1),
                            "thought": decision.get("thought", ""),
                            "action": action,
                            "agent": decision.get("action", ""),
                            "args": decision.get("arguments", {}),
                            "reason": decision.get("reason", ""),
                        }],
                        # is_complete → route 节点判断终点
                        "is_complete": action == "finish",
                        # final_output → action=finish 时赋值
                        "final_output": (
                            decision.get("thought", "分析完成")
                            if action == "finish"
                            else ""
                        ),
                    }
                else:
                    # L2 失败（正则匹配到但 JSON 解析失败）→ 重试
                    logger.warning(
                        "Supervisor JSON 解析失败(attempt=%d)，原始响应: %.200s...",
                        attempt + 1, text
                    )
            except Exception as e:
                # LLM 调用本身失败 → 重试
                logger.exception(
                    "Supervisor LLM 调用失败(attempt=%d): %s", attempt + 1, e
                )

        # 2 次都失败 → 安全退出（防死循环）
        logger.error("Supervisor 2 次 JSON 解析均失败，强制 finish")
        return {
            "pending_decision": {
                "thought": "JSON 解析失败，安全退出",
                "action": "finish",
                "agent": "finish",
                "arguments": {},
                "reason": "两次 JSON 解析失败，安全退出",
            },
            "reasoning_trace": [{
                "round": state.get("current_round", 1),
                "thought": "JSON 解析失败",
                "action": "finish",
                "agent": "finish",
                "args": {},
                "reason": "fallback",
            }],
            "is_complete": True,
            "final_output": "分析因技术问题终止",
        }

    return node_think


# ══════════════════════════════════════════════════════════════════════════════
# §4 _make_node_act — act 节点闭包工厂
# ══════════════════════════════════════════════════════════════════════════════

def _make_node_act(router: A2ARouter):
    """创建 act 节点函数——注入 A2ARouter（仅此一个依赖）。

    【L3 核心考点】act 为什么不需要 LLM？
    ───────────────────────────────────
    act 的职责是"执行 think 已经做好的决策"，不涉及推理——它只需:
    1. 读 pending_decision 中的 agent_name + action + arguments
    2. 构造 A2ATask
    3. 调 router.send_task(task)
    4. 返回结果

    LLM 调用在 Agent handler 内部完成（think 只做"选谁"，handler 做"怎么执行"）。
    act 节点本身是纯调度，不需要独立的 LLM 实例。

    【L4 工程】finish 时的短路逻辑
    ─────────────────────────────
    action=="finish" → 返回占位结果，不调用 router.send_task（没必要）。
    这不是"跳过"——是"think 已经决策了结束，act 负责传递这个状态给 observe"。

    Args:
        router: 已注册 4 个 Agent 的 A2ARouter

    Returns:
        async def node_act(state: SupervisorState) -> dict
    """
    async def node_act(state: SupervisorState) -> dict:
        """act 节点: 执行 LLM 决策，调用对应 Agent。

        ├─ 读: pending_decision
        └─ 写: pending_task_result, pending_task_agent, pending_task_status
        """
        decision = state.get("pending_decision", {})
        agent_name = decision.get("agent", "")
        action = decision.get("action", "")
        arguments = decision.get("arguments", {})

        # ─── 安全网：自动补全必填字段 ───
        # 【2026-07-29 BUG修复】LLM 可能忘记在 arguments 里填充 competitors/dimensions，
        # 但 AgentCard.input_schema.required 强制要求这些字段。
        # 在调度前用 state 中的值自动补全——Prompt 负责"教 LLM 怎么做"，
        # 代码负责"确保 LLM 做错也不会崩"。
        if action in ("collector", "analyzer"):
            if "competitors" not in arguments or not arguments["competitors"]:
                arguments["competitors"] = state.get("found_competitors", [])
            if "dimensions" not in arguments or not arguments["dimensions"]:
                arguments["dimensions"] = state.get("dimensions", [])

        # ─── 安全网：拦截"待探索"占位符 ───
        # 【2026-07-29 BUG修复】"待探索"是语义占位符，不是真实竞品名。
        # Collector 的搜索过滤器依赖标题匹配真实竞品名——"待探索"永远不会命中。
        # 传占位符到 Collector → 100% 返回 0结果 → Supervisor 重复重试 → 轮次耗尽。
        # 拦截策略：标记为 failed 而非 silently 替换，让 observe 的失败路径处理。
        _PLACEHOLDER_COMPETITORS = {"待探索"}
        if action == "collector" and arguments.get("competitors"):
            actual = set(arguments["competitors"])
            if actual.issubset(_PLACEHOLDER_COMPETITORS) or actual == set():
                logger.warning(
                    "【安全网拦截】collector 收到占位符竞品: %s，阻止调度",
                    arguments["competitors"],
                )
                return {
                    "pending_task_agent": "collector",
                    "pending_task_status": "failed",
                    "pending_task_result": {
                        "error": "竞品列表仅为占位符「待探索」，无法执行采集",
                        "suggestion": "请用户补充具体竞品名称，例如「飞书」「钉钉」「企业微信」",
                    },
                }

        # finish 短路: think 决策了结束 → 不调用任何 Agent
        if action == "finish":
            logger.info("Supervisor act 跳过: 决策为 finish")
            return {
                "pending_task_agent": "finish",
                "pending_task_status": "completed",
                "pending_task_result": {"message": "任务已完成"},
            }

        # 构造任务单（一行构造）
        # 【2026-07-29 BUG修复】task.id 必须使用真实的 task_id（来自 tasks 表），
        # 而非 A2ATask 默认生成的随机 UUID。
        # 否则 HarnessGuard 的审计日志写入 agent_logs 表时，外键约束报错：
        # "键值对(task_id)=(随机UUID)没有在表 tasks 中出现"
        task = A2ATask(
            agent_name=agent_name,
            action=action,
            arguments=arguments,  # 可能已被安全网自动补全了 competitors/dimensions
        )
        # 覆盖为真实 task_id（来自 SupervisorState，对应 tasks 表中的主键）
        task.id = state["task_id"]
        logger.info("Supervisor act: agent=%s, action=%s", agent_name, action)

        # 路由分发（查表 → 构造 → 调用 → 返回）
        try:
            result_task = await router.send_task(task)
            return {
                "pending_task_result": result_task.result or {},
                "pending_task_agent": agent_name,
                # .value 取 Enum 的字符串值（"completed"/"failed"）
                "pending_task_status": result_task.status.value,
            }
        except Exception as e:
            # router.send_task 内部已 try/except，这里兜底更极端的异常
            logger.exception("act 执行失败: %s", e)
            return {
                "pending_task_result": {"error": str(e)},
                "pending_task_agent": agent_name,
                "pending_task_status": "failed",
            }

    return node_act


# ══════════════════════════════════════════════════════════════════════════════
# §5 _make_node_observe — observe 节点闭包工厂
# ══════════════════════════════════════════════════════════════════════════════

def _make_node_observe(
    summarizer: MemorySummarizer | None = None,
    memory_engine: LongTermMemoryEngine | None = None,
):
    """创建 observe 节点函数——注入 MemorySummarizer + LongTermMemoryEngine。

    【L3 核心考点】observe 是 ReAct 循环中最重的节点
    ──────────────────────────────────────────────
    大部分状态写操作都在 observe 完成:

    observe 节点的 9 步执行流程:
    ┌─────┬─────────────────────────────┬───────────────────────────────────┐
    │ 步骤 │ 做什么                      │ 写入字段                          │
    ├─────┼─────────────────────────────┼───────────────────────────────────┤
    │  ①  │ 读 pending_task_agent/      │ —（仅读取）                       │
    │     │ status/result + decision    │                                   │
    │  ②  │ status=="failed" → 记日志   │ reasoning_trace(observation)      │
    │  ③  │ collector completed →       │ found_competitors, collected_data │
    │     │ 映射 result 到业务字段       │                                   │
    │  ④  │ analyzer completed →        │ analysis_results                  │
    │     │ 映射 result 到业务字段       │                                   │
    │  ⑤  │ writer completed →          │ report_content                    │
    │     │ 映射 report_markdown         │                                   │
    │  ⑥  │ quality completed →         │ quality_score, quality_passed,    │
    │     │ 映射评分 + 门禁              │ rewrite_suggestions               │
    │  ⑦  │ 长期记忆提取                │ (LongTermMemoryEngine.add_memory) │
    │  ⑧  │ 追加 messages_buffer        │ messages_buffer(operator.add)     │
    │  ⑨  │ 摘要记忆(增减+全量合并)      │ (MemorySummarizer)                │
    │     │ + current_round += 1         │ current_round                     │
    └─────┴─────────────────────────────┴───────────────────────────────────┘

    【L5 决策】为什么 status=="failed" 不更新业务字段？
    ──────────────────────────────────────────────
    失败的 Agent 调用可能返回不完整/错误的 result。把失败的 result 合并进
    analysis_results 或 report_content 会污染后续推理——宁可缺失一轮数据，
    也不能让错误数据误导 LLM 的下一轮决策。

    但失败会在 reasoning_trace 中记录，LLM 能看到"上轮失败了"→ 可自行决定重试。

    【L5 决策】摘要记忆时序
    ────────────────────
    增量摘要每轮执行（轻量），全量合并每 10 轮执行（重量）。
    两种摘要结束后才 current_round += 1——保证摘要中 round_num 与实际一致。

    Args:
        summarizer: 可选 MemorySummarizer（若 None 则跳过摘要）
        memory_engine: 可选 LongTermMemoryEngine（若 None 则跳过长期记忆写入）

    Returns:
        async def node_observe(state: SupervisorState) -> dict
    """
    async def node_observe(state: SupervisorState) -> dict:
        """observe 节点: 观察 Agent 执行结果，更新业务状态。

        ├─ 读: pending_task_agent, pending_task_status, pending_task_result,
        │      pending_decision, current_round, task_id, user_id
        └─ 写: found_competitors, collected_data, analysis_results,
                report_content, quality_score/passed, rewrite_suggestions,
                reasoning_trace(observation), messages_buffer(append),
                current_round(+1)
        """
        # ─── 步骤①: 读取 pending_* 中间字段 ───
        agent_name = state.get("pending_task_agent", "")
        status = state.get("pending_task_status", "")
        result = state.get("pending_task_result", {}) or {}
        decision = state.get("pending_decision", {})
        current_round = state.get("current_round", 1)

        updates: dict = {}
        trace_obs = {}

        # ─── 步骤②: 失败路径 — 只记日志，不更新业务字段 ───
        if status == "failed":
            logger.warning("Agent %s 执行失败（业务字段不更新）", agent_name)
            trace_obs = {
                "round": current_round,
                "observation": f"{agent_name} failed",
            }

        # ─── 步骤③-⑥: 成功路径 — 按 Agent 名称映射 result 到业务字段 ───
        elif status == "completed" and agent_name != "finish":

            if agent_name == "collector":
                # result 格式: {竞品名: {chunk_ids: [str], pages: [{url,title,text}]}}
                found = list(result.keys()) if result else []
                # ── 检查是否有有效素材 ──
                # 【2026-07-29 BUG修复】collector "成功完成"但返回 0 chunk = 事实上的失败。
                # 需要在 reasoning_trace 中明确标记，让 LLM 看到"上轮collector无有效数据"。
                total_chunks = sum(len(result.get(c, {}).get("chunk_ids", [])) for c in found)
                total_pages = sum(len(result.get(c, {}).get("pages", [])) for c in found)
                if total_chunks == 0 and total_pages == 0:
                    logger.warning(
                        "⚠️ Collector 返回空数据: %d竞品 0chunk — 视为采集失败",
                        len(found),
                    )
                    trace_obs = {
                        "round": current_round,
                        "observation": (
                            f"collector returned 0 pages/chunks for {found} — "
                            f"竞品名称可能是占位符或搜索无匹配结果，需更换竞品或终止"
                        ),
                    }
                    # 不更新 business fields（避免覆盖正确的 found_competitors）
                    # 但追踪已记录，LLM 下一轮 think 会看到这条
                else:
                    updates["found_competitors"] = found
                    updates["collected_data"] = result
                    logger.info("收集完成: %d 个竞品, %d 页面, %d chunk", len(found), total_pages, total_chunks)
                    trace_obs = {
                        "round": current_round,
                        "observation": f"collected {len(found)} competitors, {total_pages} pages",
                    }

            elif agent_name == "analyzer":
                # result 格式: {维度名: {竞品名: "分析结论"}}
                updates["analysis_results"] = result
                dims = list(result.keys()) if result else []
                logger.info("分析完成: %d 个维度", len(dims))
                trace_obs = {
                    "round": current_round,
                    "observation": f"analyzed {len(dims)} dimensions",
                }

            elif agent_name == "writer":
                # result 格式: {report_markdown: str}
                updates["report_content"] = result.get("report_markdown", "")
                logger.info("报告生成完成")
                trace_obs = {
                    "round": current_round,
                    "observation": "report written",
                }

            elif agent_name == "quality":
                # result 格式: {overall_score: float, passed: bool, rewrite_suggestions: [str]}
                updates["quality_score"] = result.get("overall_score", 0)
                updates["quality_passed"] = result.get("passed", False)
                updates["rewrite_suggestions"] = result.get("rewrite_suggestions", [])
                logger.info(
                    "质量评分: %.1f, 通过=%s",
                    updates["quality_score"],
                    updates["quality_passed"],
                )
                trace_obs = {
                    "round": current_round,
                    "observation": f"quality score: {updates['quality_score']}",
                }

            # ─── 步骤⑦: 长期记忆提取 —— 已禁用
            # 自动提取会把"Google市场份额23%"这类临时数据当作用户画像
            # 长期记忆改为手动写入路径，详见 MEMORY.md

        # ─── 步骤⑧: 追加 messages_buffer ───
        reason = decision.get("reason", "")
        msg_entry = {"role": "assistant", "content": f"[{agent_name}] {reason}"}

        # ─── 步骤⑨: 摘要记忆 ───
        if summarizer is not None:
            try:
                task_id = state.get("task_id", "")
                # messages_buffer 是 Annotated[list, operator.add] —
                # 节点返回 {"messages_buffer": [entry]} 时 LangGraph 自动拼接
                # 但完整历史列表需要手动构造
                all_msgs = list(state.get("messages_buffer", [])) + [msg_entry]

                # 增量摘要: 每轮执行（前轮摘要 + 本轮消息 → 新摘要）
                await summarizer.summarize_round(
                    task_id=task_id,
                    messages=all_msgs,
                    round_num=current_round,
                )

                # 全量合并: 每 10 轮校准语义漂移
                # 增量摘要累积 10 轮后可能出现主题偏移，全量合并用原始消息
                # 全部重新生成一次摘要，校准偏差
                if current_round % 10 == 0:
                    await summarizer.full_merge_summary(messages=all_msgs)
                    logger.info("全量摘要合并完成: round=%d", current_round)
            except Exception:
                # 摘要失败不阻塞主循环
                logger.exception("摘要记忆失败，继续执行")

        # ─── 写入 updates ───
        updates["messages_buffer"] = [msg_entry]
        updates["current_round"] = current_round + 1

        # reasoning_trace 追加本轮 observation（operator.add 自动拼接）
        if trace_obs:
            updates["reasoning_trace"] = [trace_obs]

        return updates

    return node_observe


# ══════════════════════════════════════════════════════════════════════════════
# §6 route_after_observe — 条件路由（双重终止 + 自循环）
# ══════════════════════════════════════════════════════════════════════════════

def route_after_observe(state: SupervisorState) -> str:
    """observe → route 的条件路由函数。

    【L3 核心考点】add_conditional_edges 的条件路由
    ────────────────────────────────────────────
    条件路由 = 根据 state 的当前值决定下一个节点（而非固定边）:
      graph.add_conditional_edges(
          "observe",           # 从 observe 节点出发
          route_after_observe, # 用此函数判断下一步
          {                    # 返回值 → 目标节点映射
              "continue": "think",  # 继续 → 回到 think（循环）
              "end": END,          # 结束 → END（终止）
          }
      )

    【L5 决策】双重终止条件（任一满足即退出）
    ───────────────────────────────────────
    条件 1: is_complete == True  → think 决策了 finish（正常完成）
    条件 2: current_round > max_rounds → 轮次耗尽（硬上限，防死循环）

    哪个先行？is_complete 先于 current_round 判断——正常完成优先于超时退出。
    current_round > max_rounds 时打 warning 日志（区别于正常 finish 的 info）。

    Returns:
        "end"      → 结束（LangGraph 路由到 END）
        "continue" → 继续（LangGraph 路由回 think 节点）
    """
    # 条件 1: 正常完成（think 决策了 finish）
    if state.get("is_complete", False):
        logger.info("Supervisor 路由: 任务完成 → END")
        return "end"

    # 条件 2: 轮次耗尽
    if state.get("current_round", 1) > state.get("max_rounds", 10):
        logger.warning(
            "Supervisor 路由: 轮次耗尽(current=%d, max=%d) → END",
            state.get("current_round"),
            state.get("max_rounds"),
        )
        return "end"

    # 继续循环
    logger.info(
        "Supervisor 路由: 继续 → think (round=%d)",
        state.get("current_round", 1),
    )
    return "continue"


# ══════════════════════════════════════════════════════════════════════════════
# §7 build_supervisor_graph — 图构建 + PostgresSaver 编译
# ══════════════════════════════════════════════════════════════════════════════

async def build_supervisor_graph(
    mcp_server: MCPServer,
    pool: Pool,
    router: A2ARouter,
    llm_supervisor: ChatDeepSeek,
    summarizer: MemorySummarizer | None = None,
    retrieval_strategy: MemoryRetrievalStrategy | None = None,
    memory_engine: LongTermMemoryEngine | None = None,
) -> CompiledStateGraph:
    """构建并编译 Supervisor StateGraph — 外部调用此函数获取可执行图。

    【L3 核心考点】StateGraph 构建的 5 步法
    ─────────────────────────────────────
    ┌─────┬────────────────────┬──────────────────────────────────────┐
    │ 步骤 │ 代码               │ 说明                                 │
    ├─────┼────────────────────┼──────────────────────────────────────┤
    │  ①  │ StateGraph(State)  │ 创建图，绑定 TypedDict 状态类型       │
    │  ②  │ add_node("X", fn)  │ 注册节点函数（注意是 fn 不是 fn()）   │
    │  ③  │ add_edge("A","B")  │ 固定边（A→B 无条件）                 │
    │     │ add_conditional_   │ 条件边（根据 state 动态选目标）       │
    │     │ edges("C", fn,     │                                      │
    │     │ {"yes":"D","no":E})│                                      │
    │  ④  │ set_entry_point()  │ 指定起始节点（必须，否则图不合法）     │
    │  ⑤  │ compile(checkpt)   │ 绑定 Checkpoint + 编译 → 可执行图     │
    └─────┴────────────────────┴──────────────────────────────────────┘

    【L4 工程】PostgresSaver 自动持久化
    ─────────────────────────────────
    compile(checkpointer=saver) 后，LangGraph 在每个节点执行完成后自动调
    saver.aput() 保存 checkpoint。不需要在节点函数内手动调用任何 persist 逻辑。
    中断后同一 thread_id 重调 graph.ainvoke() → 从最后的 checkpoint 恢复。

    【L5 决策】与 Pipeline 统一 PostgresSaver
    ──────────────────────────────────────
    Pipeline 和 Supervisor 两张图共用同一个 asyncpg Pool → 同一个 PostgresSaver
    实例（或两个独立实例连同一数据库）。通过 thread_id 前缀自然分区:
    — Pipeline:    pipeline-{task_id}
    — Supervisor:  supervisor-{task_id}

    同一张 checkpoints 表，不同前缀，查询时加 WHERE thread_id LIKE 'supervisor-%'
    即可只查 Supervisor 的历史。

    Args:
        mcp_server: MCP 工具服务器（透传给 Agent handler）
        pool: asyncpg 连接池（供 PostgresSaver 使用）
        router: A2A 路由器（已注册 4 个 Agent 的卡片+handler+LLM）
        llm_supervisor: Supervisor 专用 LLM（temperature=0.3，决策探索性）
        summarizer: 可选摘要记忆器
        retrieval_strategy: 可选长期记忆检索策略
        memory_engine: 可选长期记忆引擎

    Returns:
        编译后的 CompiledStateGraph（调用 ainvoke(state, config) 执行）
    """
    # ─── Checkpoint 持久化（建表 + 绑定） ───
    saver = PostgresSaver(pool)
    await saver.setup()
    logger.info("PostgresSaver 已就绪")

    # ─── 闭包工厂创建 3 个节点函数（DI 注入） ───
    node_think = _make_node_think(
        llm_supervisor, router, retrieval_strategy, memory_engine
    )
    node_act = _make_node_act(router)
    node_observe = _make_node_observe(summarizer, memory_engine)

    # ─── 步骤①: 创建 StateGraph ───
    graph = StateGraph(SupervisorState)

    # ─── 步骤②: 注册节点 ───
    graph.add_node("think", node_think)
    graph.add_node("act", node_act)
    graph.add_node("observe", node_observe)

    # ─── 步骤③-固定边: think → act → observe ───
    graph.add_edge("think", "act")
    graph.add_edge("act", "observe")

    # ─── 步骤③-条件边: observe → think(continue) 或 END ───
    graph.add_conditional_edges(
        "observe",
        route_after_observe,
        {
            "continue": "think",
            "end": END,
        },
    )

    # ─── 步骤④: 起始节点 ───
    graph.set_entry_point("think")

    # ─── 步骤⑤: 编译 + 绑定 Checkpoint ───
    compiled = graph.compile(checkpointer=saver)
    logger.info("Supervisor 图编译完成")
    return compiled


# ══════════════════════════════════════════════════════════════════════════════
# §8 run_supervisor_task — 外部调用入口
# ══════════════════════════════════════════════════════════════════════════════

async def run_supervisor_task(
    task: dict,
    mcp_server: MCPServer,
    pool: Pool,
    router: A2ARouter,
    llm_supervisor: ChatDeepSeek,
    summarizer: MemorySummarizer | None = None,
    retrieval_strategy: MemoryRetrievalStrategy | None = None,
    memory_engine: LongTermMemoryEngine | None = None,
) -> dict:
    """运行 Supervisor 模式竞品分析——外部调用的一键入口。

    【L5 架构】这是 Supervisor 的"一键执行"入口。
    调用方（IntentRouter / FastAPI endpoint）只需传 task dict，内部完成:
    1. build_supervisor_graph → 构建 + 编译图
    2. 初始化 SupervisorState（21 字段显式赋值）
    3. config = {"configurable": {"thread_id": f"supervisor-{task['id']}"}}
    4. graph.ainvoke(initial_state, config) → 执行 ReAct 循环
    5. 返回精简结果 {task_id, final_output, quality_score, is_complete}

    【L3 核心考点】initial_state 必须 21 个字段全部显式初始化
    ──────────────────────────────────────────────────────
    LangGraph 的 TypedDict StateGraph 要求所有字段在 initial_state 中有值。
    不能依赖"默认值"——TypedDict 没有运行时默认值机制。

    为什么 current_round 从 1 开始（而非 0）？
    → 1-based: 第 1 轮、第 2 轮...更符合人类心智模型。
    → 判断 condition: current_round > max_rounds（而非 >=）更自然。
      例如 max_rounds=10，第 10 轮执行完后 current_round 变为 11，
      11 > 10 → 终止。用 1-based 比 0-based 少一次 +1 转换。

    Args:
        task: 任务字典 {
            id: str,              # UUID，任务唯一ID
            title: str,           # 报告标题
            competitors: [str],   # 初始竞品列表（可能为空，动态发现）
            dimensions: [str],    # 分析维度
            user_id: str,         # 用户ID（多用户隔离 + 长期记忆分区）
        }
        mcp_server: MCP 工具服务器
        pool: asyncpg 连接池
        router: A2A 路由器（已注册 4 Agent）
        llm_supervisor: Supervisor 专用 LLM
        summarizer: 可选
        retrieval_strategy: 可选
        memory_engine: 可选

    Returns:
        {
            task_id: str,
            final_output: str,       # 最终报告文本
            quality_score: float,    # 质量评分（0-100）
            is_complete: bool,       # 是否正常完成
        }
    """
    # 构建图
    graph = await build_supervisor_graph(
        mcp_server=mcp_server,
        pool=pool,
        router=router,
        llm_supervisor=llm_supervisor,
        summarizer=summarizer,
        retrieval_strategy=retrieval_strategy,
        memory_engine=memory_engine,
    )

    # 初始状态 — 显式初始化全部 21 个字段（TypedDict 没有运行时默认值）
    initial_state: SupervisorState = {
        # §1 任务元信息
        "task_id": task["id"],
        "title": task.get("title", ""),
        "user_id": task.get("user_id", "default"),
        "user_query": task.get("title", ""),
        # §2 探索结果（初始为空，ReAct 循环动态填充）
        "dimensions": task.get("dimensions", []),
        "found_competitors": task.get("competitors", []),
        "collected_data": {},
        "analysis_results": {},
        "report_content": "",
        # §3 质量（初始为0/空）
        "quality_score": 0.0,
        "quality_passed": False,
        "rewrite_suggestions": [],
        # §4 控制
        "current_round": 1,
        "max_rounds": 10,
        "reasoning_trace": [],
        "messages_buffer": [],
        # §5 终止
        "final_output": "",
        "is_complete": False,
        # §6 中间字段（初始为空）
        "pending_decision": {},
        "pending_task_result": {},
        "pending_task_agent": "",
        "pending_task_status": "",
    }

    # Checkpoint 配置: thread_id 前缀隔离
    config = {
        "configurable": {
            "thread_id": f"supervisor-{task['id']}",
        },
    }

    logger.info(
        "Supervisor 启动: task_id=%s, competitors=%s, dimensions=%s",
        task["id"],
        task.get("competitors", []),
        task.get("dimensions", []),
    )

    # 执行 ReAct 循环（LangGraph 自动管理 checkpoint 持久化 + 条件路由）
    final_state = await graph.ainvoke(initial_state, config)

    logger.info(
        "Supervisor 完成: task_id=%s, rounds=%d, quality=%.1f",
        task["id"],
        final_state.get("current_round", 0),
        final_state.get("quality_score", 0.0),
    )

    return {
        "task_id": task["id"],
        "final_output": final_state.get("final_output", ""),
        "quality_score": final_state.get("quality_score", 0.0),
        "is_complete": final_state.get("is_complete", True),
    }
