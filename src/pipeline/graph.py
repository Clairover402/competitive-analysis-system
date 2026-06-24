"""Pipeline Graph — LangGraph StateGraph 编排引擎。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

graph.py 是竞品分析系统的"指挥中心"。它不采集数据、不做分析——
它只负责把 Phase 3 的四个 Agent 串成一条自动化流水线。

  用户 task
      │
      ▼
  ┌───────────┐    ┌───────────┐    ┌───────────┐    ┌───────────┐    ┌───────────┐
  │  collect   │───→│  analyze  │───→│   write   │───→│  quality  │───→│ finalize  │
  │  (采集)    │    │  (分析)   │    │  (撰写)   │    │  (评分)   │    │  (完成)   │
  │  t=0.3     │    │  t=0.1    │    │  t=0.3    │    │  t=0.0    │    │           │
  └───────────┘    └───────────┘    └───────────┘    └────┬──────┘    └───────────┘
                                                          │
                                                  passed? ──是──→ finalize
                                                          │
                                                         否 (且 steps>0)
                                                          │
                                                          ▼
                                                     write (重写)
                                                     remaining_steps -= 1

【L5 决策】为什么选 Pipeline 模式而不是 Supervisor 模式？
─────────────────────────────────────────────────────
竞品分析是一个确定性流程：
  采集 → 分析 → 撰写 → 评分 → 完成
每一步的输入输出明确，不存在"可能需要跳到其他步骤"的歧义。
确定性流程 = Pipeline，开放性流程 = Supervisor。
Pipeline 的优势：
  — 可预测：每次执行路径一致
  — 可优化：每步的 resource/cost 可预估
  — 可调试：沿着边就能追踪状态变化
如果将来需要"动态选择分析策略"或"根据中间结果分流"，再升级为 Supervisor。


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 StateGraph 构建: add_node + add_edge + add_conditional_edges + compile
  §2 条件边: route_after_quality 路由逻辑（过→finalize，不过→write循环）
  §3 remaining_steps 循环控制: 在哪里递减、为什么是3、防死循环原理
  §4 Checkpoint 集成: checkpointer=saver + thread_id 断点续传
  §5 闭包工厂模式: _make_node_*() 注入不同温度的 LLM 实例
  §6 ainvoke 执行: 初始状态 + config(thread_id) → 最终状态


═══════════════════════════════════════════════════════════════════════════════
                    【L4 工程 — 图拓扑一览】
═══════════════════════════════════════════════════════════════════════════════

  节点              类型         入口                        出口
  ──────────────── ─────────── ────────────────────────   ────────────
  collect          ENTRY       用户 task dict              always → analyze
  analyze          中间         状态中的 task 元信息        always → write
  write            中间+循环    分析结果 + rewrite_suggest  always → quality
  quality          中间+条件    报告正文                    passed? → finalize
                                                           否 → write
  finalize         TERMINAL    最终报告 + 评分             (图结束)

  所有边:
    collect  → analyze           (add_edge，确定性边)
    analyze  → write             (add_edge，确定性边)
    write    → quality           (add_edge，确定性边)
    quality  → finalize / write  (add_conditional_edges，条件边)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langgraph.graph import StateGraph

from src.agents import (
    collector_agent,
    analyzer_agent,
    writer_agent,
    quality_agent,
    create_llm_client,
)
from src.pipeline.state import AgentState
from src.pipeline.checkpoint import PostgresSaver
from src.memory.long_term import LongTermMemoryEngine
from src.memory.conflict import MemoryConflictResolver
from src.db.dao import AgentMemoryDAO
from src.db.dao import TaskDAO
from src.db.connection import create_pool
from src.mcp import create_mcp_server
from src.config import Settings

if TYPE_CHECKING:
    from langgraph.graph import CompiledStateGraph
    from asyncpg import Pool
    from src.mcp.server import MCPServer
    from langchain_deepseek import ChatDeepSeek

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# §1 闭包工厂：节点函数工厂
# ═════════════════════════════════════════════════════════════════════════════

"""
【L4 工程】为什么用闭包工厂（_make_node_*）而不是直接写节点函数？

直接写:
  async def node_collect(state: AgentState) -> dict:
      llm = create_llm_client(settings, temperature=0.3)  # 每次调用都要创建？
      mcp_server = ...  # 从哪获取？
      ...

闭包工厂:
  def _make_node_collect(mcp_server, llm):
      async def node_collect(state: AgentState) -> dict:
          # llm 和 mcp_server 已经在闭包中，不需要从 state 获取
          result = await collector_agent(task, mcp_server, llm)
          ...
      return node_collect

闭包工厂的三个优势：
  ① LLM 实例在 build_pipeline_graph 时创建一次，不在每次节点执行时重复创建
     （每个 LLM 实例 ≈ 底层 HTTP Client，重复创建浪费资源）
  ② LLM 的 temperature 在构建时绑定，不进 State（不进 Checkpoint）
     → Checkpoint 只存 AgentState 字段，不存 ChatDeepSeek 实例 → 可序列化
  ③ mcp_server 和 llm 通过闭包注入，节点函数签名干净（只接受 state）
     → 满足 LangGraph 节点函数要求: async def node_xxx(state: AgentState) -> dict

【L5 决策】为什么 4 个 LLM 实例用闭包而不存进 AgentState？
─────────────────────────────────────────────────────
AgentState 会被序列化存入 Checkpoint（JSONB）。
ChatDeepSeek 实例包含 HTTP Client、api_key、连接池 → 不可序列化。
如果把 llm 存入 AgentState，写入 Checkpoint 时会报错。

闭包的关键洞察：
  温度是编译期决定的，不是运行期状态 → 不需要进 State/Checkpoint
  进了 State 反而污染 Checkpoint（把不可序列化对象写入 JSONB → 崩溃）
"""


def _make_node_collect(mcp_server: MCPServer, llm: ChatDeepSeek):
    """创建 collect 节点函数（闭包注入 LLM t=0.3）。

    【L5 架构】节点函数签名
    async def node_xxx(state: AgentState) -> dict
    这是 LangGraph 节点的标准签名：
      — state: 当前 AgentState（只读）
      — return: dict，局部更新的字段，LangGraph 自动 merge

    节点函数不应该直接修改 state，而是返回 dict。
    LangGraph 把返回的 dict merge 到当前状态中：
      state["collected_data"] = result  # 由 LangGraph 完成，不是 node 自己做
    """
    async def node_collect(state: AgentState) -> dict:
        """Collector 节点：调用 collector_agent，写入 collected_data。

        【L3 核心考点】节点内部做了什么？
        ─────────────────────────────
        1. 从 state 中提取 task 元信息（id, title, competitors, dimensions）
        2. 传给 collector_agent(task, mcp_server, llm)
        3. collector_agent 内部执行 6 步流水线：生成→搜索→抓取→分块→嵌入→存储
        4. 返回 {"collected_data": result}
        5. LangGraph 自动将该 dict merge 到 AgentState

        节点不关心 downstream 是什么——它只负责自己这步。
        """
        task = {
            "id": state["task_id"],
            "title": state["title"],
            "competitors": state["competitors"],
            "dimensions": state["dimensions"],
        }
        logger.info("【Pipeline】collect 开始, task=%s", state["task_id"])
        result = await collector_agent(task, mcp_server, llm)
        return {
            "collected_data": result,
        }
    return node_collect


def _make_node_analyze(mcp_server: MCPServer, llm: ChatDeepSeek, engine: LongTermMemoryEngine):
    """创建 analyze 节点函数（闭包注入 LLM t=0.1）。

    读取 collected_data 中的 task 元信息，调用 analyzer_agent。
    analyzer_agent 内部：每个维度并行 RAG 检索 + LLM 分析。
    """
    async def node_analyze(state: AgentState) -> dict:
        """Analyzer 节点：RAG 检索 + LLM 分析，写入 analysis_results。

        【L3 核心考点】Analyzer 不消费 collected_data 中的 chunk_ids
        ──────────────────────────────────────────────────────────
        它用 task_id 直接从 chunk_embeddings 表检索 chunk，
        不走 collected_data → 减少 State 中传递大数据。
        这是一种"State 瘦身"策略——只传控制信息（task_id），
        大体积数据（chunk 文本）留在 DB 中按需检索。
        """
        user_id = state["user_id"]
        query = f"{state['title']} {', '.join(state['dimensions'])}"
        memories = await engine.retrieve(user_id, query)

        memory_context = ""
        if memories:
            memory_context = "\n[历史记忆]\n"
            for i, m in enumerate(memories[:5]):
                memory_context += f"{i+1}. [{m.get('memory_type','?')}] {m.get('content','')[:200]}\n"

        task = {
            "id": state["task_id"],
            "title": state["title"],
            "competitors": state["competitors"],
            "dimensions": state["dimensions"],
            "memory_context": memory_context,
        }
        logger.info("【Pipeline】analyze 开始, task=%s, memories=%d", state["task_id"], len(memories))
        result = await analyzer_agent(task, mcp_server, llm)
        return {"analysis_results": result, "retrieved_memories": memories}
    return node_analyze


def _make_node_write(mcp_server: MCPServer, llm: ChatDeepSeek):
    """创建 write 节点函数（闭包注入 LLM t=0.3）。

    【L5 决策】这是 Pipeline 中唯一的循环节点——可能被执行多次。
    首次执行：写初稿。
    再次执行：Quality 不通过 → route → 回到 write → 改写。

    【L4 工程】remaining_steps 递减点
    ────────────────────────────────
    remaining_steps 只在 write 节点递减（不在 collect/analyze/quality），
    因为 write 是循环的起点——每次经过 write 意味着"又多了一轮改写尝试"。

    如果放在 quality 节点递减：quality 不通过 → 递减 → route →
    write → quality → 递减 → route → ... → 步数耗尽 → 强制 finalize。
    这样也可以，但语义不够清晰（"质量节点扣步数"不如"写作节点扣步数"直观）。

    如果放在 route_after_quality 函数里递减：
    条件边函数只能读 state，不能写 state（LangGraph 设计约束）。
    所以递减只能在节点函数内完成。
    """
    async def node_write(state: AgentState) -> dict:
        """Writer 节点：组装 Markdown 报告。

        首次写入或 Quality 回退重写时调用。
        回退时 task 中附带 rewrite_suggestions，Writer 会在 prompt 中注入修改要求。

        【L3 核心考点】version 递增
        report_version 从 0 开始，每次 write 执行后 +1。
        这个值不进 Checkpoint 的因果链——它是业务字段，不是 Checkpoint 元数据。
        """
        task = {
            "id": state["task_id"],
            "title": state["title"],
            "competitors": state["competitors"],
            "dimensions": state["dimensions"],
            "analysis_results": state.get("analysis_results", {}),
            "rewrite_suggestions": state.get("rewrite_suggestions"),
        }
        logger.info("【Pipeline】write 开始, task=%s version=%d",
                    state["task_id"], state.get("report_version", 0) + 1)
        result = await writer_agent(task, mcp_server, llm)
        return {
            "report_content": result["report_markdown"],
            "report_version": state.get("report_version", 0) + 1,
            # 【L4 工程】remaining_steps -= 1：在循环入口点递减
            # 初始值=3 → 第1次write后=2 → 第2次=1 → 第3次=0 → 强制finalize
            # 这意味着：1次初稿 + 最多2次改写
            "remaining_steps": state["remaining_steps"] - 1,
        }
    return node_write


def _make_node_quality(mcp_server: MCPServer, llm: ChatDeepSeek):
    """创建 quality 节点函数（闭包注入 LLM t=0.0）。

    temperature=0.0 保证评分可复现——同一份报告每次评分一致。
    """
    async def node_quality(state: AgentState) -> dict:
        """Quality 节点：五维 LLM-as-Judge 打分。

        【L3 核心考点】quality_agent 产出的 5 个字段的后续流向
        ───────────────────────────────────────────────────
        quality_score   → Finalize 节点日志 + run_pipeline_task 返回值
        quality_details → 仅用于日志（当前未串联到前端）
        quality_passed  → route_after_quality 的条件判断 ← 最关键
        rewrite_suggestions → 不通过时回传 Writer，作为改写依据
        issues          → 当前未存入 AgentState（只在 quality_agent 内部日志用）
        """
        task = {
            "id": state["task_id"],
            "title": state["title"],
            "competitors": state["competitors"],
            "dimensions": state["dimensions"],
            "report_markdown": state["report_content"],
        }
        logger.info("【Pipeline】quality 开始, task=%s", state["task_id"])
        result = await quality_agent(task, mcp_server, llm)
        return {
            "quality_score": result["overall_score"],
            "quality_details": result["dimensions"],
            "quality_passed": result["passed"],
            "rewrite_suggestions": result["rewrite_suggestions"],
        }
    return node_quality


def _make_node_finalize(pool: Pool, engine: LongTermMemoryEngine | None = None, llm_extract: ChatDeepSeek | None = None):
    """创建 finalize 节点函数（注入 Pool，不是 LLM）。

    【L5 决策】finalize 为什么需要 pool 而不是 llm？
    ──────────────────────────────────────────────
    finalize 不调用任何 LLM——它只做两件事：
      ① 更新 task 的数据库状态（需要 pool）
      ② 返回最终报告（只是把 state 中的 report_content 提升为 final_report）
    不需要 LLM → 不注入 LLM → 只注入 Pool。

    这也是"按需注入"原则的体现——不为了统一签名而引入不必要依赖。
    """
    async def node_finalize(state: AgentState) -> dict:
        """Finalize 节点：设置 final_report，更新 task 的数据库状态。

        【L4 工程】为什么 finalize 只负责"标记完成"而不生成新内容？
        ──────────────────────────────────────────────────────
        职责分离：内容生成是 Writer 的事，状态管理是 Finalize 的事。
        Finalize 不触摸报告内容——只是"盖章确认"并写入 DB。

        这也意味着：
          — 如果 Quality 不通过且 remaining_steps 耗尽，强制 finalize 时
            final_report 可能是低分报告 ← 调用方需要检查 quality_score
          — 不能假设 final_report 一定是一份通过的报告
        """
        logger.info("【Pipeline】finalize 开始, task=%s score=%.0f",
                    state["task_id"], state.get("quality_score", 0))

        task_dao = TaskDAO(pool)
        await task_dao.update_status(state["task_id"], "completed")

        # Persist 3-5 key decisions to long-term memory
        if engine and state.get("report_content"):
            try:
                extract_prompt = (
                    "Extract 3-5 key decisions, user preferences, or important facts from the competitive analysis report below. "
                    "One per line, format: {type}|{content} "
                    "type must be one of: decision/preference/fact. "
                    "No other text. "
                    "\n\nReport:\n" + state["report_content"][:3000]
                )
                llm_result = await llm_extract.ainvoke(extract_prompt)
                lines = llm_result.content.strip().split("\n")
                for line in lines:
                    line = line.strip()
                    if "|" in line:
                        parts = line.split("|", 1)
                        mem_type = parts[0].strip()
                        mem_content = parts[1].strip()
                        if mem_type in ("decision", "preference", "fact") and len(mem_content) > 5:
                            await engine.add_memory(
                            user_id=state["user_id"],
                                content=mem_content,
                                memory_type=mem_type,
                                source_task_id=state["task_id"],
                            )
            except Exception:
                logger.exception("Failed to persist memories from report")

        return {
            "final_report": state["report_content"],
        }
    return node_finalize


# ═════════════════════════════════════════════════════════════════════════════
# §2 条件路由
# ═════════════════════════════════════════════════════════════════════════════

def route_after_quality(state: AgentState) -> str:
    """条件边：Quality 通过 → finalize，不通过 → 回退 write（如 steps 还有剩余）。

    【L3 核心考点】条件边函数的工作机制
    ──────────────────────────────────
    add_conditional_edges("quality", route_after_quality, {
        "finalize": "finalize",   # 返回值 "finalize" → 路由到 finalize 节点
        "write": "write",         # 返回值 "write"   → 路由到 write 节点
    })

    LangGraph 执行完 quality 节点后，调用 route_after_quality(state)。
    函数返回一个字符串 → LangGraph 查字典 → 跳转到对应节点。

    【L3 核心考点】条件边函数约束
    ──────────────────────────
    条件边函数签名: def route(state: AgentState) -> str
    — 只能读 state，不能写 state（没有 return dict 的出口）
    — 必须返回一个字符串（匹配字典中的 key）
    — 函数应该是纯函数（无副作用），不调用外部服务

    【L4 工程】三条分支的优先级
    ─────────────────────────
    ① passed=True          → finalize（正常通过，最优路径）
    ② remaining_steps ≤ 0  → finalize（步数耗尽，强制终止）
    ③ passed=False + steps > 0 → write（还有改写机会）

    注意顺序：先判断 passed，再判断 steps。如果 passed=True 但 steps=0，
    仍然走 finalize（因为已经通过了，不需要多余判断）。

    Args:
        state: 当前 AgentState

    Returns:
        "finalize" — 质量通过或步数耗尽，结束流程
        "write"   — 需要重写，还有剩余步数
    """
    if state.get("quality_passed"):
        logger.info("【Pipeline】质量通过 (score=%.0f)，路由到 finalize",
                    state.get("quality_score", 0))
        return "finalize"

    if state.get("remaining_steps", 0) <= 0:
        # 【L4 工程】步数耗尽的日志级别是 WARNING 而不是 ERROR
        # 这不是系统错误（ERROR），而是业务逻辑的正常分支（所有改写都没通过）
        # 在监控告警中，WARNING 不会触发 on-call，但会出现在 dashboards 中
        logger.warning("【Pipeline】剩余步数耗尽 (steps=%d)，强制 finalize",
                       state.get("remaining_steps", 0))
        return "finalize"

    logger.info("【Pipeline】质量不通过 (score=%.0f, steps_left=%d)，路由到 write 重写",
                state.get("quality_score", 0), state.get("remaining_steps", 0))
    return "write"


# ═════════════════════════════════════════════════════════════════════════════
# §3 Graph 构建
# ═════════════════════════════════════════════════════════════════════════════

async def build_pipeline_graph(
    mcp_server: MCPServer,
    pool: Pool,
    user_id: str = "default",
) -> CompiledStateGraph:
    """构建并编译 Pipeline StateGraph。

    【L3 核心考点】StateGraph 构建的 5 步法
    ────────────────────────────────────
    ① graph = StateGraph(AgentState)        — 定义状态类型
    ② graph.add_node("name", node_func)     — 注册节点（每个节点一个名字+函数）
    ③ graph.add_edge("A", "B")              — 确定性边（A → B 永远走这条）
    ④ graph.add_conditional_edges(...)      — 条件边（根据状态动态选择）
    ⑤ graph.set_entry_point("collect")       — 设置入口节点
    ⑥ compiled = graph.compile(checkpointer) — 编译为可执行图

    编译后得到 CompiledStateGraph，可以调 ainvoke() 执行。

    【L4 工程】4 个不同温度的 LLM 在 build 时创建，不在 invoke 时创建
    ────────────────────────────────────────────────────────────────
    build 阶段: llm_collector = create_llm_client(settings, temperature=0.3)  ✅
    invoke 阶段: 直接使用闭包中的 llm 实例                                       ✅
    如果在 invoke 阶段创建 → 每个 task 创建 4 个新 LLM 实例 → 浪费 HTTP Client 开销

    【L5 决策】为什么 build 和 invoke 分开？
    ──────────────────────────────────────
    build 是一次性操作（应用启动时执行一次），invoke 是每次请求执行。
    分开的好处：
      — build 慢但只做一次（创建 4 个 LLM 实例 + 编译图）
      — invoke 快（直接执行编译好的图，不需要重新创建 LLM）
      — 多个并发请求可以复用同一个 compiled graph（只有 checkpointer 隔离）

    Args:
        mcp_server: MCP 工具服务器（含 settings + 工具能力）
        pool: asyncpg 连接池

    Returns:
        编译后的 CompiledStateGraph，可调用 graph.ainvoke(state, config)
    """
    settings = mcp_server.settings

    # ─── 创建 4 个不同温度的 LLM 客户端 ───
    # 【L5 决策】4 个实例 4 种温度——每个 Agent 的确定性需求不同
    # Collector: 搜索需要多样性 → 0.3
    # Analyzer:  分析需要稳定   → 0.1
    # Writer:    撰写可微调措辞 → 0.3
    # Quality:   评分必须可复现 → 0.0
    llm_collector = create_llm_client(settings, temperature=0.3)
    llm_analyzer = create_llm_client(settings, temperature=0.1)
    llm_writer = create_llm_client(settings, temperature=0.3)
    llm_quality = create_llm_client(settings, temperature=0.0)

    # ─── Checkpoint 持久化 ───
    # 【L3 核心考点】checkpointer 的生命周期
    # saver 在 graph.compile() 时绑定到 CompiledStateGraph。
    # 每次 ainvoke() 时，LangGraph 自动调用 saver.aput() 写 checkpoint。
    # thread_id 作为隔离键——不同 task 的 checkpoint 不会混淆。
    saver = PostgresSaver(pool)
    await saver.setup()  # 确保表存在（幂等）

    # ─── 长期记忆引擎 ───
    agent_memory_dao = AgentMemoryDAO(pool)
    conflict_resolver = MemoryConflictResolver(llm_analyzer, agent_memory_dao)
    ltm_engine = LongTermMemoryEngine(llm_analyzer, agent_memory_dao, conflict_resolver)

    # ─── 构建图 ───
    graph = StateGraph(AgentState)

    # 【L3 核心考点】add_node 的参数
    # 第一个参数是节点名（字符串，用于边引用）
    # 第二个参数是节点函数（async def node_xxx(state) -> dict）
    # 节点名和节点函数分离：同一个函数可以用不同名字注册（虽然这里没用到）
    graph.add_node("collect", _make_node_collect(mcp_server, llm_collector))
    graph.add_node("analyze", _make_node_analyze(mcp_server, llm_analyzer, ltm_engine))
    graph.add_node("write", _make_node_write(mcp_server, llm_writer))
    graph.add_node("quality", _make_node_quality(mcp_server, llm_quality))
    graph.add_node("finalize", _make_node_finalize(pool, ltm_engine, llm_analyzer))

    # ─── 确定性边 ───
    # 【L3 核心考点】add_edge("A", "B")
    # 执行完节点 A 后，无条件跳到节点 B。
    # 不需要判断任何条件——这条边永远存在。
    # Pipeline 主线：collect → analyze → write → quality
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "write")
    graph.add_edge("write", "quality")

    # ─── 条件边 ───
    # 【L3 核心考点】add_conditional_edges 的三要素
    # ① 源节点: "quality"
    # ② 路由函数: route_after_quality → 返回 "finalize" 或 "write"
    # ③ 路由字典: {"finalize": "finalize", "write": "write"}
    #    — key 是路由函数的返回值
    #    — value 是目标节点名
    #    — 如果路由函数返回的值不在字典中 → LangGraph 报错
    graph.add_conditional_edges(
        "quality",
        route_after_quality,
        {
            "finalize": "finalize",
            "write": "write",
        },
    )

    # ─── 入口 ───
    # 【L3 核心考点】set_entry_point — 图执行的起点
    # 调用 graph.ainvoke(state, config) 时，从入口节点开始执行。
    # 一个图只有一个入口点（StateGraph 设计约束）。
    graph.set_entry_point("collect")

    # ─── 编译 ───
    # 【L3 核心考点】compile(checkpointer=saver)
    # 编译为 CompiledStateGraph。checkpointer 传入后：
    #   — 每次 ainvoke 从 checkpoint 恢复（如果有历史）
    #   — 每个节点执行后自动写 checkpoint
    #   — 中断后可重试（同一个 thread_id）
    compiled = graph.compile(checkpointer=saver)
    logger.info("【Pipeline】图编译完成，已启用 PostgreSQL Checkpoint")
    return compiled


# ═════════════════════════════════════════════════════════════════════════════
# §4 入口函数
# ═════════════════════════════════════════════════════════════════════════════

async def run_pipeline_task(
    task: dict,
    mcp_server: "MCPServer | None" = None,
    pool: "Pool | None" = None,
) -> dict:
    """运行 Pipeline 模式竞品分析——外部调用入口。

    【L5 架构】这是系统的"一键执行"入口。
    调用方只需要传 task dict，不需要知道内部有 5 个节点 4 个 Agent。

    【L3 核心考点】ainvoke 的执行流程
    ──────────────────────────────
    graph.ainvoke(initial_state, config) 的执行过程：
      ① LangGraph 调 aget_tuple(config) 查有没有历史 checkpoint
      ② 有 → 从 checkpoint 恢复；无 → 用 initial_state
      ③ 从入口节点 collect 开始执行
      ④ collect → (写 checkpoint) → analyze → (写 checkpoint) → ...
      ⑤ 到达 finalize（或图无更多出边）→ 返回最终状态

    【L4 工程】initial_state 需要提供所有 AgentState 字段的初始值
    ──────────────────────────────────────────────────────────────
    虽然很多字段在初始时是空的（如 collected_data={}），但必须显式给出。
    否则 LangGraph 在第一个节点尝试读字段时会 KeyError。
    所有字段初始值汇总：
      — 元信息: 由 task dict 传入
      — Agent 产出: 空 {} / "" / 0 / 0.0 / False / []
      — 控制字段: messages=[], remaining_steps=3, pipeline_mode="pipeline"

    【L4 工程】config["configurable"]["thread_id"]
    ─────────────────────────────────────────────
    thread_id 是 Checkpoint 的隔离键。必须唯一（每个任务一个 thread_id）。
    同一个 thread_id 的多次 ainvoke 会从上次 checkpoint 恢复。
    如果两个不同任务用了同一个 thread_id → 状态混淆 → 严重 bug。
    所以 project_id 用 task["id"]（UUID）作为 thread_id——保证唯一。

    Args:
        task: {
            id: str,              # UUID，任务唯一ID
            title: str,           # 报告标题
            competitors: [str],   # 竞品列表
            dimensions: [str]     # 分析维度
        }

    Returns:
        {task_id, final_report, quality_score}
    """
    if mcp_server is None or pool is None:
        settings = Settings()
        mcp_server = create_mcp_server(settings)
        pool = await create_pool(settings)

    try:
        graph = await build_pipeline_graph(mcp_server, pool)

        # ─── 初始状态 ───
        # 【L4 工程】所有字段必须显式初始化
        initial_state: AgentState = {
            "task_id": task["id"],
            "title": task["title"],
            "user_id": task.get("user_id", "default"),
            "competitors": task["competitors"],
            "dimensions": task["dimensions"],
            "pipeline_mode": "pipeline",
            "collected_data": {},
            "analysis_results": {},
            "report_content": "",
            "report_version": 0,
            "quality_score": 0.0,
            "quality_details": {},
            "quality_passed": False,
            "rewrite_suggestions": [],
            "messages": [],
            "remaining_steps": 3,
            "final_report": "",
        }

        # ─── config 配置 ───
        # 【L4 工程】thread_id 用 task["id"]
        # 保证每个任务唯一隔离，同时支持断点续传
        config = {
            "configurable": {
                "thread_id": task["id"],
            }
        }

        # ─── 执行图 ───
        final_state = await graph.ainvoke(initial_state, config)

        return {
            "task_id": task["id"],
            "final_report": final_state.get("final_report", ""),
            "quality_score": final_state.get("quality_score", 0),
        }
    finally:
        # 【L4 工程】连接池不在此关闭
        # pool 是全局单例，多个请求复用同一个池。
        # 在 finally 中关闭池会导致后续请求拿不到连接。
        # 池的生命周期由应用层管理（如 FastAPI lifespan）。
        pass
