"""SupervisorState — ReAct 探索模式共享状态定义。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

SupervisorState 是 Supervisor ReAct 循环的共享状态。2026-06-23 重构后，
由 LangGraph StateGraph 自动管理——不再依赖纯 Python while 循环手动操作。

  图结构: think → act → observe → route
                 ↑                    ↓
                 └──── continue ──────┘
                                      ↓
                                     END

  think()       ─读→ user_query, found_competitors, analysis_results
                ─写→ pending_decision, reasoning_trace, is_complete
                   (内部: memory_retrieval → LLM 决策 → JSON 解析)
  act()         ─读→ pending_decision
                ─写→ pending_task_result, pending_task_agent, pending_task_status
                   (内部: router.send_task)
  observe()     ─读→ pending_task_result/agent/status
                ─写→ found_competitors, analysis_results, report_content,
                     quality_*, current_round, messages_buffer
                   (内部: memory_summarizer, long_term_memory)
  route()       ─读→ is_complete, current_round, max_rounds
                → "continue" | "end"

StateGraph 自动在每个节点后调 PostgresSaver.aput() 保存 checkpoint。
中断后同一 thread_id 重调 graph.ainvoke() 自动恢复——不再需要手动 _save_checkpoint。


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 Annotated + operator.add: 列表累加 reducer
  §2 中间字段模式: pending_* 字段实现 StateGraph 节点间通信
  §3 条件路由: route_after_observe 的 is_complete + round 判断
  §4 状态溯源: 谁写、谁读、何时赋值（对照生命周期表）


═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — 状态生命周期全景】
═══════════════════════════════════════════════════════════════════════════════

  字段                    初始化    think    act    observe   说明
  ─────────────────────  ──────   ─────   ────   ───────   ──────
  task_id                ▲写      ●读                       任务唯一ID
  title                  ▲写      ●读                       报告标题
  user_id                ▲写      ●读                       用户ID
  user_query             ▲写      ●读                       原始问题
  found_competitors      ▲写(空)  ●读     ▲写    ●读         动态发现竞品
  collected_data         ▲写(空)  ●读     ▲写    ●读         采集结果
  analysis_results       ▲写(空)  ●读     ▲写    ●读         分析结果
  report_content         ▲写(空)  ●读     ▲写    ●读         Markdown报告
  quality_score          ▲写(0)   ●读     ▲写    ●读         加权均分
  quality_passed         ▲写(F)   ●读     ▲写    ●读         是否通过
  rewrite_suggestions    ▲写(空)  ●读     ▲写    ●读         改写建议
  current_round          ▲写(1)   ●读             ▲写(+1)    当前轮次
  max_rounds             ▲写(10)  ●读                       硬上限
  reasoning_trace        ▲写(空)  ▲写(R)          ●读         推理轨迹
  messages_buffer        ▲写(空)  ●读             ▲写         摘要缓冲
  final_output           ▲写(空)  ▲写              ▲写         最终输出
  is_complete            ▲写(F)   ▲写              ●读         完成标志

  【StateGraph 中间字段 — 不走 Checkpoint 持久化】
  pending_decision       ▲写(空)  ▲写      ●读                think→act 通信
  pending_task_result    ▲写(空)           ▲写     ●读         act→observe 通信
  pending_task_agent     ▲写(空)           ▲写     ●读         act→observe 通信
  pending_task_status    ▲写(空)           ▲写     ●读         act→observe 通信

  ●读 = 消费字段    ▲写 = 产出字段    R = 通过 operator.add reducer 追加

  【L4 工程】中间字段模式（pending_*）
  ───────────────────────────────────
  StateGraph 的节点之间没有直接函数调用——每个节点返回 dict，下个节点从 state 读。
  所以节点间传递数据必须通过 State 字段。pending_decision / pending_task_result
  就是"投递箱"：think 投进去 → act 取出来 → act 投进去 → observe 取出来。
  这种模式在 LangGraph 中被称为"中介状态（brokered state）"。

  Pipeline 不需要这种字段——因为 collect/analyze/write 的输出本身就是"业务字段"，
  不需要额外的中间层。但 Supervisor 的 decision 和 task_result 是流程中间产物，
  结束一轮后就没用了，所以加 pending_ 前缀标明"中间态"。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class SupervisorState(TypedDict):
    """Supervisor ReAct 探索模式共享状态 — 由 LangGraph StateGraph 管理。

    【L3 核心考点】三种字段类型的区别
    ──────────────────────────────
    — 普通字段（str/int/float/bool/dict）: 更新 = 覆盖（新值替换旧值）
    — Annotated[list, operator.add]: 更新 = 追加（列表拼接）
    — pending_* 中间字段: 节点间传递临时数据（think→act→observe），
      每轮被覆盖（下一轮 overwrite 上一轮的值）

    【L3 核心考点】operator.add vs add_messages
    ─────────────────────────────────────────
    Pipeline 的 messages 用 add_messages（LangGraph 内置）因为它需要 ID 去重。
    Supervisor 的 reasoning_trace 用 operator.add（Python 内置）因为
    每条 trace entry 没有 ID 且绝不会重复——不需要去重逻辑。

    operator.add([a], [b]) = [a, b]  → 节点返回 {"reasoning_trace": [entry]}
    时，LangGraph 自动拼接到现有列表末尾。
    """

    # ══════════════════════════════════════════════════════════════════════
    # §1 任务元信息 — run_supervisor_task 初始化，全链路只读
    # ══════════════════════════════════════════════════════════════════════

    task_id: str
    """任务唯一 ID（UUID）。全链路透传 + checkpoint thread_id。"""

    title: str
    """报告标题。Supervisor 模式下可从 user_query 提取后赋值。"""

    user_id: str
    """用户 ID。长期记忆按 user_id 分区，必填（initial_state 阶段传入）。"""

    user_query: str
    """用户原始开放性问题。_think 的 System Prompt 中注入此字段。"""

    # ══════════════════════════════════════════════════════════════════════
    # §2 探索结果 — _act + _observe 逐步填充
    # ══════════════════════════════════════════════════════════════════════

    found_competitors: list[str]
    """动态发现的竞品列表（逐轮追加，非初始化确定）。
    observe 收到 collector 的 result 后赋值。"""

    collected_data: dict
    """采集结果: {竞品名: {chunk_ids: [str], pages: [{url, title, text}]}}。"""

    analysis_results: dict
    """分析结果: {维度名: {竞品名: "分析结论"}}。"""

    report_content: str
    """Markdown 格式报告文本。Writer 产出 → Quality 读取。"""

    # ══════════════════════════════════════════════════════════════════════
    # §3 质量 — Quality Agent 产出
    # ══════════════════════════════════════════════════════════════════════

    quality_score: float
    """加权平均分（代码按权重重算，0-100）。"""

    quality_passed: bool
    """质量门禁是否通过。_think 读取判断是否回退重写。"""

    rewrite_suggestions: list[str]
    """Quality 不通过时的改写建议。"""

    # ══════════════════════════════════════════════════════════════════════
    # §4 控制 — ReAct 循环元信息
    # ══════════════════════════════════════════════════════════════════════

    current_round: int
    """当前轮次（1-based 递增）。observe 节点末尾 current_round += 1。
    初始化: 1。终止条件: current_round > max_rounds。"""

    max_rounds: int
    """硬上限 = 10。初始化一次不变。"""

    reasoning_trace: Annotated[list, operator.add]
    """推理轨迹（累加 reducer）。
    每项: {round, thought, action, agent, args, reason, observation}。
    think 节点追加 thought/action 部分，act 节点回填 task 状态。

    【L3 核心考点】operator.add 累加规则
    ──────────────────────────────────
    节点返回 {"reasoning_trace": [new_entry]} → LangGraph 自动:
      state["reasoning_trace"].extend([new_entry])
    不是覆盖，是拼接。每轮 think 追加一条，不会丢失历史。
    """

    messages_buffer: Annotated[list, operator.add]
    """摘要缓冲区（累加 reducer）。
    每项: {role, content}，observe 节点末尾追加。
    MemorySummarizer 逐轮读取做增量摘要。
    """

    # ══════════════════════════════════════════════════════════════════════
    # §5 终止
    # ══════════════════════════════════════════════════════════════════════

    final_output: str
    """最终输出。think 决策 finish 或达到 max_rounds 时赋值。"""

    is_complete: bool
    """是否完成。think 决策 finish → True，route 读取 → 路由到 END。"""

    # ══════════════════════════════════════════════════════════════════════
    # §6 StateGraph 中间字段 — 节点间通信（不走 Checkpoint 持久化）
    # ══════════════════════════════════════════════════════════════════════

    pending_decision: dict
    """think → act 通信: LLM 决策结果。
    think 写入 → act 读取。
    格式: {thought, action, agent, arguments, reason}。
    """

    pending_task_result: dict
    """act → observe 通信: A2ATask 执行结果。
    act 写入（router.send_task 返回的 result）→ observe 读取并映射到业务字段。
    """

    pending_task_agent: str
    """act → observe 通信: 执行的 Agent 名称（"collector"|"analyzer"|"writer"|"quality"）。
    observe 根据此值选择映射逻辑。
    """

    pending_task_status: str
    """act → observe 通信: 任务状态（"completed"|"failed"）。
    "failed" 时 observe 不更新业务字段（不 merge 失败的结果）。
    """
