"""Pipeline AgentState — LangGraph StateGraph 共享状态定义。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

AgentState 是整个 Pipeline 编排层的"共享记忆"——四个 Agent 通过它传递数据。

  Collector ─写→ collected_data ─读→ Analyzer
  Analyzer  ─写→ analysis_results ─读→ Writer
  Writer    ─写→ report_content ─读→ Quality
  Quality   ─写→ quality_score/passed/suggestions ─读→ Finalize / Writer(改写)

每个节点返回 dict 部分更新，LangGraph 自动合并到 AgentState。
就像四个工位在流水线上传递同一个工单，每个工位填自己那栏。

【L5 决策】为什么用 TypedDict 而不是 Pydantic BaseModel？
─────────────────────────────────────────────────────
  — TypedDict 是 LangGraph 的"一等公民"，StateGraph(AgentState) 直接接受
  — Pydantic 也可以但 LangGraph 对 TypedDict 的 Annotated reducer 支持更原生
  — TypedDict 零运行时开销（纯类型标注），序列化到 checkpoint 时也是纯 dict
  — 这个项目不需要 Pydantic 的验证能力——字段值由 Agent 产出，不需要校验层

【L5 决策】为什么用单一 StateGraph 而不是每个 Agent 各自维护状态？
─────────────────────────────────────────────────────────────
  这是 Pipeline 模式 vs Supervisor 模式的关键区别。
  Pipeline 中的 Agent 是顺序执行的，状态天然共享。
  各自维护状态 → 需要额外的状态同步层 → 增加复杂度。
  共享 AgentState → 每个 Agent 只需要知道"我读哪些字段、我写哪些字段"。


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 TypedDict + Annotated: LangGraph 状态定义的核心模式
  §2 Reducer 机制: add_messages 累加 vs 普通字段覆盖
  §3 字段生命周期: 谁写、谁读、何时赋值
  §4 remaining_steps: 防死循环的计数器（Pipeline 特有）


═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — 状态管理全景】
═══════════════════════════════════════════════════════════════════════════════

  字段                    Collector  Analyzer  Writer  Quality  Finalize   说明
  ─────────────────────  ─────────  ────────  ──────  ───────  ────────   ──────
  task_id                ●读        ●读       ●读     ●读      ●读       任务唯一ID
  title                  ●读        ●读       ●读     ●读               报告标题
  competitors            ●读        ●读       ●读     ●读               竞品列表
  dimensions             ●读        ●读       ●读     ●读               分析维度
  pipeline_mode          ●读                                             流水线标识
  collected_data         ▲写        ●读                                  采集结果
  analysis_results                  ▲写       ●读                        分析结果
  report_content                              ▲写     ●读      ●读       报告正文
  report_version                              ▲写                        重写版本号
  quality_score                                         ▲写              加权平均分
  quality_details                                       ▲写              五维详情
  quality_passed                                        ▲写      ●读     是否通过
  rewrite_suggestions                                   ▲写      ●读     改写建议
  messages               ●读/写     ●读/写    ●读/写  ●读/写            消息历史
  remaining_steps                              ●写     ●读     ●读       防死循环
  final_report                                                    ▲写     最终输出

  ●读 = 消费字段    ▲写 = 产出字段

  【L4 工程】这个表的实战价值
  ────────────────────────────
  排查问题时对照这张表：如果 Writer 拿到空的 analysis_results，
  不急着改 Writer——先看 Analyzer 是不是没写。
  这就是"状态溯源"——比 print(f"here {x}") 更高效。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Pipeline 全链路 AgentState — 四个 Agent 共享的工作状态。

    【L3 核心考点】TypedDict 的工作方式
    ────────────────────────────────
    TypedDict 不是类，是类型标注。运行时它就是一个普通 dict。
    但 LangGraph 在编译期读取 TypedDict 的字段定义，生成对应的 Channel。
    每个字段在 StateGraph 内部对应一个 Channel，节点写入 → Channel 更新 → 下个节点读取。

    【L3 核心考点】Annotated[list, add_messages] 的 reducer 机制
    ──────────────────────────────────────────────────────────
    普通字段（如 str、dict）的更新方式是"覆盖"：
      state["report_content"] = "新报告"  → 旧值被覆盖

    Annotated[list, add_messages] 的方式是"累加"：
      state["messages"]  = [msg1]         → messages = [msg1]
      state["messages"] += [msg2]         → messages = [msg1, msg2]
      不是覆盖，是追加。

    `add_messages` 是 LangGraph 内置的 reducer 函数，还支持去重：
      如果两条消息的 ID 相同，后者覆盖前者（防止重复插入）。

    为什么 messages 用累加而不是覆盖？
      — LLM 对话需要完整的历史上下文
      — 每条消息可能是 tool_call / tool_result，不能丢
      — 覆盖 = 只保留最后一条消息，LLM 会丢失上下文

    为什么其他字段用覆盖而不是累加？
      — collected_data 更新时只需要最新的采集结果
      — report_content 重写时旧版本直接丢弃
      — 覆盖语义 = "以最新状态为准"
    """

    # ══════════════════════════════════════════════════════════════════════
    # 任务元信息 — 由 run_pipeline_task 初始化，全链路只读
    # ══════════════════════════════════════════════════════════════════════

    task_id: str
    """任务唯一 ID（UUID）。全链路透传，用于日志关联 + DB 查询筛选 + Checkpoint thread_id。
    
    【L4 工程】task_id 的三重身份
      — 业务身份: TaskDAO 的 WHERE task_id = $1
      — 编排身份: Checkpoint 的 thread_id
      — 日志身份: AgentLogDAO 的 task_id 关联
    一个 ID 串联三个系统，不需要做 ID 映射。
    """

    title: str
    """报告标题（用户输入）。如 "2025年企业协作工具竞品分析"。

    仅在 Writer 构造时使用（拼入报告标题和 prompt）。
    """

    competitors: list[str]
    """竞品名称列表（用户输入）。如 ["飞书", "钉钉", "企业微信", "Teams", "Slack"]。

    Collector 用这个列表遍历生成搜索词；
    Analyzer 用这个列表遍历做维度分析；
    Writer 用这个列表拼报告表格。
    """

    dimensions: list[str]
    """分析维度列表（用户输入）。如 ["定价策略", "功能对比", "技术架构"]。

    Analyzer 的外层循环变量——每个维度一次 RAG 检索 + LLM 分析。
    维度的数量和内容由用户决定，系统不硬编码任何维度。
    """

    pipeline_mode: str  # "pipeline"
    """流水线模式标识。当前固定为 "pipeline"。

    【L5 决策】预留扩展点
    将来如果加入 Supervisor 模式（动态路由），这个字段区分：
      "pipeline"  → 走当前的确定性流水线
      "supervisor" → 走 LLM 动态决策路由
    目前只有一个值，但预留字段避免将来改 State 定义（向后兼容）。
    """

    # ══════════════════════════════════════════════════════════════════════
    # Collector 产出 — node_collect 写入
    # ══════════════════════════════════════════════════════════════════════

    collected_data: dict
    """采集结果，格式: {竞品名: {chunk_ids: [str], pages: [{url, title, text}]}}。

    Collector 写入 → 当前阶段 Analyzer 不直接消费（用 DB 检索替代），
    但保留在 State 中用于可观测性（Supervisor 监控采集了多少数据）。
    """

    # ══════════════════════════════════════════════════════════════════════
    # Analyzer 产出 — node_analyze 写入
    # ══════════════════════════════════════════════════════════════════════

    analysis_results: dict
    """分析结果，格式: {维度名: {竞品名: "分析结论（含 source_url）"}}。

    示例:
      {
        "定价策略": {
          "飞书": "企业版¥200/人/月，2025Q1降至¥180（来源: feishu.cn/pricing）",
          "钉钉": "专业版¥180/人/年（来源: dingtalk.com/price）"
        }
      }

    Analyzer 写入 → Writer 消费 → 拼入报告。
    """

    # ══════════════════════════════════════════════════════════════════════
    # Writer 产出 — node_write 写入
    # ══════════════════════════════════════════════════════════════════════

    report_content: str
    """Markdown 格式的完整报告文本（Writer 产出）。

    Quality 读取此字段进行评分。
    如果改写（Quality 不通过），Writer 会被再次调用，覆盖此字段。
    """

    report_version: int
    """报告版本号，从 1 开始。

    【L4 工程】为什么需要版本号？
      — 日志中区分 "初稿 v1" 和 "改写 v2"
      — 调试时知道报告被改写了几次
      — Quality→Writer 循环中判断是否达到最大改写次数
    初始值 0 → Writer 第一次执行后设为 1 → 每次改写 +1。
    """

    # ══════════════════════════════════════════════════════════════════════
    # Quality 产出 — node_quality 写入
    # ══════════════════════════════════════════════════════════════════════

    quality_score: float
    """加权平均分（代码重算，范围 0-100）。

    Quality Agent 给五维评分，但 overall_score 由代码按权重计算后覆盖。
    详见 quality.py 中 _WEIGHTS 和代码重算逻辑。
    """

    quality_details: dict
    """五维评分详情，格式:
      {"完整性": {"score": 85, "comment": "5个维度全部覆盖"}, ...}

    当前仅用于可观测性（日志记录），未来可用于报告质量 dashboard。
    """

    quality_passed: bool
    """质量门禁是否通过（overall_score >= 70）。

    route_after_quality 条件边的核心判断依据：
      True  → 路由到 finalize（结束）
      False → 路由到 write（重写循环）
    """

    rewrite_suggestions: list[str]
    """Quality 不通过时的改写建议列表。如 ["补source_url", "概述太长"]。

    【L4 工程】这是 Quality→Writer 回退循环的数据载体。
    Quality 产出 → route_after_quality 判断不通过 →
    将 suggestions 注入 task["rewrite_suggestions"] →
    Writer 重新生成时在 prompt 中注入修改要求。
    """

    # ══════════════════════════════════════════════════════════════════════
    # 控制字段
    # ══════════════════════════════════════════════════════════════════════

    messages: Annotated[list, add_messages]
    """LangGraph 消息历史（累加 reducer）。

    【L3 核心考点】add_messages 累加规则
      — 同名同ID消息 → 覆盖（防止重复）
      — 不同消息 → 追加到列表末尾
      — 这个 reducer 保证 LLM 看到完整对话上下文

    当前 Pipeline 模式下消息主要用于可观测性（记录每一步做了什么），
    不是 LLM 对话上下文（因为每个 Agent 各自调用 llm.ainvoke，不走 messages 历史）。
    Supervisor 模式下 messages 会更重要（LLM 需要看到前一步的决策结果）。
    """

    remaining_steps: int
    """剩余循环步数，初始值 = 3，防死循环。

    【L3 核心考点】remaining_steps 的递减时机
    ────────────────────────────────────────
    只在 Writer 节点递减（graph.py node_write 中 remaining_steps -= 1）。
    为什么不放在 route_after_quality 里减？
      — 条件边函数只能读状态，不能写状态（LangGraph 设计约束）
      — 所以递减逻辑必须在节点函数内完成

    流向:
      initial = 3 → write(v1) 后 = 2 → quality 不通过 → write(v2) 后 = 1 →
      quality 不通过 → write(v3) 后 = 0 → quality 不通过 →
      route_after_quality 发现 remaining_steps <= 0 → 强制 finalize

    【L4 工程】为什么是 3 而不是 2 或 5？
      — 第一次写: 初稿（0 次改写）
      — 第二次写: Quality 建议修改（1 次改写）
      — 第三次写: 再次修改（2 次改写）
      — ≤ 0 → 强制终止（最多 2 次改写机会）
      — 如果设成 5 → 最多 4 次改写 → token 消耗 ×2，但改进递减
    """

    # ══════════════════════════════════════════════════════════════════════
    # 最终输出 — node_finalize 写入
    # ══════════════════════════════════════════════════════════════════════

    final_report: str
    """最终报告文本（通过质量门禁或强制终止后的版本）。

    node_finalize 写入 → run_pipeline_task 读取 → 返回给调用方。
    如果 Quality 从未通过（所有改写都失败），最终报告是最后一次改写的版本。
    """
