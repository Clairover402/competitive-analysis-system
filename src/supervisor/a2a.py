"""A2A Protocol — Agent-to-Agent 通信协议（集中式拓扑 + 温度绑定注册表）。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

A2A（Agent-to-Agent）= AgentCard（名片） + A2ATask（任务单） + A2ARouter（调度台）

  通信关系:
  Supervisor ──→ A2ARouter ──→ Agent handler (collector/analyzer/writer/quality)
       │                            │
       │                            └──→ MCP Server ──→ 外部工具
       │                                                (web_search/web_fetch)
       └──── task.result ←──────────────────────────────┘

  A2A vs MCP 的区别（面试常问）:
  ─────────────────────────────
  MCP（Model Context Protocol）→ Agent ↔ 工具   → N Agents × M Tools = N+M 连接
  A2A（Agent-to-Agent）        → Agent ↔ Agent  → P2P 协议，本系统用集中式拓扑简化

  Supervisor 通过 A2A 调度 4 个 Agent，Agent 通过 MCP 调用外部工具。
  不是"二选一"，是"两个层次"的协议协同。

  生命周期:
  PENDING → (router.send_task) → RUNNING → (handler 执行) → COMPLETED
                                                              │
                                                      异常 ──→ FAILED

  A2ARouter 注册表三合一:
  ┌─────────────────────────────────────────────────────────────────┐
  │  register(card, handler, llm)  → 三字典同步写入               │
  │    _cards["collector"]   = AgentCard(...)                      │
  │    _handlers["collector"] = collector_agent                    │
  │    _llms["collector"]     = ChatDeepSeek(temperature=0.3)      │
  │                                                                │
  │  send_task(task) → 查表 → 构造 → 调用 → 更新 → 返回            │
  └─────────────────────────────────────────────────────────────────┘

  温度策略（按 Agent 角色语义选择，非随意设定）:
  ┌───────────┬─────────────┬───────────────────────────────────────┐
  │  Agent    │ temperature │  理由                                  │
  ├───────────┼─────────────┼───────────────────────────────────────┤
  │ collector │    0.3      │  搜索需要多样性（不同搜索词不同结果）    │
  │ analyzer  │    0.1      │  分析需要精确（一致性优先级）           │
  │ writer    │    0.3      │  写作需要多样性（但比搜索低）           │
  │ quality   │    0.0      │  评分需要确定性（同一份报告同一分数）    │
  │ supervisor│    0.3      │  调度器需探索性（决定下一步的能力）      │
  └───────────┴─────────────┴───────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 dataclass 模式: AgentCard / A2ATask 用 @dataclass 而非 class + __init__
  §2 Enum 状态机: TaskStatus 约束生命周期（PENDING→RUNNING→COMPLETED|FAILED）
  §3 注册表模式: A2ARouter 三字典 + register() 三合一绑定
  §4 统一签名: 所有 Agent handler = async def (task, mcp_server, llm) -> dict
  §5 工厂函数: create_agent_cards() 预定义 4 张卡片

═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — 关键工程决策】
═══════════════════════════════════════════════════════════════════════════════

  §A TYPE_CHECKING 隔离: ChatDeepSeek/MCPServer 仅类型标注时导入，运行时零开销
  §B 温度预绑定: register() 时三合一写入，运行时查表（不用闭包传温度）
  §C 集中式拓扑: Supervisor 作为所有 Agent 的中转站（不是 P2P）
  §D 统一 task_dict 构造: send_task 内将 A2ATask.arguments 转换为 8 字段 dict
  §E 失败不崩溃: handler 异常 → task.status=FAILED + task.error → 返回给调用方处理
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# §1 TaskStatus — 任务生命周期枚举
# ══════════════════════════════════════════════════════════════════════════════

class TaskStatus(Enum):
    """A2A 任务生命周期状态枚举。

    【L3 核心考点】Enum 状态机 vs 字符串常量
    ───────────────────────────────────────
    用 Enum 而非 "pending"/"running" 字符串:
    — IDE 自动补全（不会拼成 "runing"）
    — 类型检查: param: TaskStatus 比 param: str 安全
    — 集中定义: 新增状态不需要全局搜索字符串

    迁移路径（四态）:
      PENDING ──→ RUNNING ──→ COMPLETED
           │          │
           └──────────┴────→ FAILED（任何阶段异常）
    """
    PENDING = "pending"      # 任务已创建，等待调度
    RUNNING = "running"      # 任务正在执行（handler 调用中）
    COMPLETED = "completed"  # 任务执行成功，result 已赋值
    FAILED = "failed"        # 任务执行异常，error 已赋值


# ══════════════════════════════════════════════════════════════════════════════
# §2 AgentCard — Agent 能力声明（名片模式）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentCard:
    """A2A Agent Card — 声明 Agent 的能力、输入/输出 Schema。

    【L3 核心考点】@dataclass vs class + __init__
    ────────────────────────────────────────
    用 @dataclass 的原因:
    — 自动生成 __init__ / __repr__ / __eq__（样板代码为零）
    — field(default_factory=list) 避免可变默认值陷阱
    — 与 A2ATask 风格统一

    类比: 微服务中的 Service Registry — 每个 Agent 注册时提交名片，
    Supervisor 通过名片了解可调度哪些 Agent 及其能力边界。
    等价于 Google A2A 协议的 AgentCard 概念。

    Attributes:
        name: 唯一标识符（"collector"|"analyzer"|"writer"|"quality"）
        description: 人类可读的能力描述（注入 Supervisor Prompt）
        capabilities: 能力标签列表（用于 LLM 决策参考）
        input_schema: JSON Schema 格式的参数约束（required + properties）
        output_schema: JSON Schema 格式的输出约束
        endpoint: 路由名称
    """
    name: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    endpoint: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# §3 A2ATask — 任务单（一次 Agent 调用的完整生命周期）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class A2ATask:
    """A2A 任务单 — 封装一次 Agent 调用的参数、状态和结果。

    【L3 核心考点】A2ATask 是"任务单"，不是"任务执行器"
    ────────────────────────────────────────────
    A2ATask 本身不执行任何逻辑，它只是数据的载体。
    执行逻辑在 A2ARouter.send_task() 中——查表找到 handler 后调用。

    【L4 工程】field(default_factory=lambda: str(uuid4())) 的陷阱
    ───────────────────────────────────────────────────────
    不能写 default=str(uuid4())！Python 会在类定义时求值一次，导致所有实例共享
    同一个 UUID。用 default_factory=lambda: 保证每次实例化生成新 UUID。

    生命周期（4 态）:
      PENDING → RUNNING → COMPLETED | FAILED
      创建时    调度时     成功时      异常时

    Attributes:
        id: UUID 唯一标识（自动生成）
        agent_name: 目标 Agent 名称（对应 A2ARouter._cards 的 key）
        action: 动作名（通常与 agent_name 相同，但语义不同）
        arguments: 透传给 Agent handler 的参数字典
        status: 当前状态（PENDING → RUNNING → COMPLETED|FAILED）
        result: 执行成功时的返回结果（dict）
        error: 执行失败时的错误信息（str）
        created_at: 任务创建时间戳（time.time()）
        completed_at: 任务完成时间戳（COMPLETED 或 FAILED 时赋值）
    """
    id: str = field(default_factory=lambda: str(uuid4()))
    agent_name: str = ""
    action: str = ""
    arguments: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: dict | None = None
    error: str | None = None
    created_at: float = 0.0
    completed_at: float | None = None


# ══════════════════════════════════════════════════════════════════════════════
# §4 AgentHandler — 统一函数签名类型
# ══════════════════════════════════════════════════════════════════════════════

# 所有 Agent handler 的签名: async def handler(task: dict, mcp_server, llm) -> dict
AgentHandler = Callable[..., Any]


# ══════════════════════════════════════════════════════════════════════════════
# §5 A2ARouter — 注册表 + 任务调度（核心组件）
# ══════════════════════════════════════════════════════════════════════════════

class A2ARouter:
    """A2A 路由器 — Agent 注册表 + 任务分发。

    【L5 架构】集中式拓扑设计
    ────────────────────────
    本系统不采用 Google A2A 原生的 P2P 架构，而是集中式拓扑——所有 Agent 调用
    全部经过 Supervisor 中转:

      Supervisor ──→ A2ARouter ──→ collector/analyzer/writer/quality
           ↑                                          │
           └──────────── task.result ─────────────────┘

    选择集中式而非 P2P 的原因:
    — 简化调试: 所有调用路径经过同一个 send_task()，日志集中
    — 统一温控: register() 三合一绑定，不用在各 Agent 间传递温度参数
    — 安全可控: 路由表完全掌握所有 Agent 接口，不需要 Agent 间直接发现

    【L3 核心考点】注册表模式
    ────────────────────────
    三字典并行存储:
      _cards:    dict[str, AgentCard]      # 名片 → LLM 决策参考
      _handlers: dict[str, AgentHandler]   # 函数 → 调用入口
      _llms:     dict[str, ChatDeepSeek]   # LLM  → 温度绑定

    三本字典用同一个 key（agent_name）索引，保证一致性。
    为什么不合并到一个对象？因为三者的生命周期和使用者不同:
    — _cards 供 Supervisor prompt 使用（只需要 name + capabilities）
    — _handlers + _llms 供 send_task 使用（需要可调用对象）
    — 分离存储 → 查询时不需要从不完整的对象中拆字段

    Attributes:
        _cards: Agent 名 → AgentCard 映射
        _handlers: Agent 名 → handler 函数映射
        _llms: Agent 名 → ChatDeepSeek 实例映射（各 Agent 温度不同）
        _mcp_server: MCP 工具服务器（透传给 handler）
    """

    def __init__(self, mcp_server: MCPServer, harness: "HarnessGuard | None" = None) -> None:
        """初始化空注册表，绑定 MCP Server。

        MCP Server 在整个 A2A 链路中只创建一次，通过 A2ARouter 透传给所有 Agent。
        而不是每个 Agent 各自创建一份——避免工具列表不一致。
        """
        self._cards: dict[str, AgentCard] = {}
        self._handlers: dict[str, AgentHandler] = {}
        self._llms: dict[str, ChatDeepSeek] = {}
        self._mcp_server = mcp_server
        self._harness = harness

    # ────────────────────────────────────────────────────────────────────────
    # 公开方法: register — 三合一绑定（卡片 + 函数 + LLM）
    # ────────────────────────────────────────────────────────────────────────

    def register(
        self,
        card: AgentCard,
        handler: AgentHandler,
        llm: ChatDeepSeek,
    ) -> None:
        """注册一个 Agent——卡片、函数、LLM 三合一绑定。

        【L5 决策】温度在注册时绑定，不在调用时传递
        ─────────────────────────────────────────
        淘汰的设计:
          async def handler(task, mcp_server, llm, temperature):
              ...  # temperature 由调用方传入，每次调用都要传

        当前设计:
          router.register(card, handler, ChatDeepSeek(temperature=0.3))
          # 温度在注册时就已绑定到 llm 实例，handler 内部直接用 llm 不管温度

        优势: 同一 handler 函数可注册两次绑定不同温度——
        register(card_v1, handler, ChatDeepSeek(t=0.1))   # 精确模式
        register(card_v2, handler, ChatDeepSeek(t=0.7))   # 探索模式
        handler 代码一行不改。

        类比 Spring @Autowired——Controller 不关心 Service 的构造参数。
        Constructor Injection 把依赖初始化从使用者中解耦。

        Args:
            card: AgentCard 名片（含 name/capabilities/input_schema）
            handler: async def (task, mcp_server, llm) -> dict
            llm: 已配置好温度的 ChatDeepSeek 实例
        """
        self._cards[card.name] = card
        self._handlers[card.name] = handler
        self._llms[card.name] = llm
        logger.info("Registered agent: %s (capabilities: %s)", card.name, card.capabilities)

    # ────────────────────────────────────────────────────────────────────────
    # 公开方法: get_card — 按名称查名片
    # ────────────────────────────────────────────────────────────────────────

    def get_card(self, agent_name: str) -> AgentCard | None:
        """按名称查询 AgentCard。

        【L4 工程】为什么返回 AgentCard | None 而不是抛异常？
        ──────────────────────────────────────────────
        Supervisor 可能在 LLM 幻觉出了不存在的 agent_name 时调用此方法。
        返回 None 让调用方自行判断（重试/fallback），比抛异常更优雅。
        send_task 内部会检查 None 并标记 FAILED。

        Args:
            agent_name: Agent 名称（"collector"|"analyzer"|"writer"|"quality"）

        Returns:
            对应的 AgentCard，未注册返回 None
        """
        return self._cards.get(agent_name)

    # ────────────────────────────────────────────────────────────────────────
    # 公开方法: list_agents — 列所有注册的 Agent 名片
    # ────────────────────────────────────────────────────────────────────────

    def list_agents(self) -> list[AgentCard]:
        """列出所有已注册的 AgentCard 列表。

        用途: 外部调试/显示，不建议用于 Supervisor Prompt（用 list_capabilities）。
        """
        return list(self._cards.values())

    # ────────────────────────────────────────────────────────────────────────
    # 公开方法: list_capabilities — 格式化给 Supervisor Prompt 的能力清单
    # ────────────────────────────────────────────────────────────────────────

    def list_capabilities(self) -> str:
        """格式化 Agent 能力清单——供 Supervisor System Prompt 的 {agent_list} 插值。

        【L5 决策】用 chr(10) 而非 "\\n"
        ────────────────────────────────
        避免 f-string 中 "\\n" 被当作字面量而非换行符，用 chr(10) 明确意图。

        输出示例:
          - collector: Search and collect competitor information (capabilities: collect, web_search, web_fetch)
          - analyzer: Multi-dimensional competitive analysis (capabilities: analyze, embed, rerank)
          ...

        Returns:
            每行一条的格式化 Agent 能力描述文本
        """
        lines = []
        for card in self._cards.values():
            caps = ", ".join(card.capabilities)
            lines.append(f"- {card.name}: {card.description} (capabilities: {caps})")
        return chr(10).join(lines)

    # ────────────────────────────────────────────────────────────────────────
    # 公开方法: send_task — 任务分发核心（查表 → 构造 → 调用 → 返回）
    # ────────────────────────────────────────────────────────────────────────

    async def send_task(self, task: A2ATask) -> A2ATask:
        """向目标 Agent 发送任务并等待结果。

        【L4 工程】send_task 的 6 步执行流程
        ──────────────────────────────────

        Step  Action                      说明
        ────  ─────────────────────────   ─────────────────────────
        ①    查 _cards[agent_name]       验证 Agent 是否注册
        ②    查 _handlers + _llms        确认 handler 和 LLM 都已绑定
        ③    更新 status = RUNNING       标记开始执行
        ④    构造 task_dict（8 字段）    统一所有 Agent 的入参格式
        ⑤    await handler(task_dict,    调用 Agent 函数
                mcp_server, llm)
        ⑥    更新 status + result/error  根据返回值/异常设置 COMPLETED/FAILED

        【L3 核心考点】task_dict 为什么是 8 字段统一格式？
        ─────────────────────────────────────────────
        所有 4 个 Agent 接收相同的 dict 结构，但各取所需:
        — collector 只用 competitors + dimensions
        — analyzer  只用 competitors + dimensions + analysis_results (已有结果)
        — writer    只用 title + analysis_results
        — quality   只用 report_markdown

        统一格式的好处: Agent handler 签名完全一致，新增 Agent 不需要改 A2ARouter。
        新 Agent 在 task_dict 中忽略不用的字段即可。

        Args:
            task: 待执行的 A2ATask（至少需 agent_name 和 arguments）

        Returns:
            更新后的 A2ATask（含 result 或 error，status 为 COMPLETED 或 FAILED）
        """
        # Step ①: 查名片 — 验证 Agent 是否注册
        card = self._cards.get(task.agent_name)
        if card is None:
            task.status = TaskStatus.FAILED
            task.error = f"Unknown agent: {task.agent_name}"
            task.completed_at = time.time()
            logger.error("A2A send_task 失败: 未知 agent=%s", task.agent_name)
            return task

        # Step ②: 查 handler + LLM — 确认完整注册
        handler = self._handlers.get(task.agent_name)
        llm = self._llms.get(task.agent_name)
        if handler is None or llm is None:
            task.status = TaskStatus.FAILED
            task.error = f"Agent {task.agent_name} not fully registered (missing handler or LLM)"
            task.completed_at = time.time()
            logger.error("A2A send_task 失败: agent=%s 注册不完整", task.agent_name)
            return task

        # Step ②.5: Harness 五层安全检查（Phase 5B 集成）
        if self._harness is not None:
            guard_result = await self._harness.guard(
                agent_name=task.agent_name,
                action=task.action,
                arguments=task.arguments,
                schema=card.input_schema,
                task_id=task.id,
            )
            if not guard_result["passed"]:
                task.status = TaskStatus.FAILED
                task.error = guard_result.get("error", "HARNESS_BLOCKED")
                task.completed_at = time.time()
                logger.warning("Harness ??: agent=%s, action=%s, error=%s",
                               task.agent_name, task.action, task.error)
                return task
        # Step ③: 标记执行中
        task.status = TaskStatus.RUNNING
        task.created_at = time.time()

        # Step ④-⑥: 构造 → 调用 → 更新
        try:
            # 构造统一 task_dict（8 字段，所有 Agent 通用）
            task_dict = {
                "id": task.id,
                "title": task.arguments.get("title", ""),
                "competitors": task.arguments.get("competitors", []),
                "dimensions": task.arguments.get("dimensions", []),
                "analysis_results": task.arguments.get("analysis_results", {}),
                "report_markdown": task.arguments.get("report_markdown", ""),
                "rewrite_suggestions": task.arguments.get("rewrite_suggestions"),
                "memory_context": task.arguments.get("memory_context", ""),
            }
            # 调用 Agent handler（统一签名）
            result = await handler(task_dict, self._mcp_server, llm)
            task.result = result
            task.status = TaskStatus.COMPLETED
            logger.info("A2A send_task 完成: agent=%s, action=%s, duration=%.2fs",
                        task.agent_name, task.action, time.time() - task.created_at)
        except Exception as e:
            # Step ⑥-异常路径: 记录错误但不崩溃
            logger.exception("Task %s failed for agent %s", task.id, task.agent_name)
            task.error = str(e)
            task.status = TaskStatus.FAILED

        task.completed_at = time.time()
        return task


# ══════════════════════════════════════════════════════════════════════════════
# §6 create_agent_cards — 预定义 4 张 Agent 名片的工厂函数
# ══════════════════════════════════════════════════════════════════════════════

def create_agent_cards() -> dict[str, AgentCard]:
    """工厂函数: 创建 4 张标准 Agent 名片。

    【L3 核心考点】工厂函数 vs 硬编码
    ──────────────────────────────
    为什么不直接在 A2ARouter 初始化时创建 4 张卡片？

    用工厂函数而非硬编码在 class __init__ 中的原因:
    — 测试隔离: 单元测试可只注册 1 个 Agent（不引入未测试的 Agent）
    — 扩展性: 以后加 Agent 只需加一行 create_agent_cards 工厂，不改 A2ARouter 代码
    — 可替换: 如果以后要动态从配置文件加载 Agent 定义，只需换工厂函数实现

    【L5 决策】input_schema 的 required 字段
    ──────────────────────────────────────
    collector/analyzer: ["competitors", "dimensions"]  — 运行时由 Supervisor 的
      think 决策动态注入（因为是探索模式，竞品和维度可能还不确定）
    writer: ["title", "analysis_results"]              — 写作前必须完成分析
    quality: ["report_markdown"]                       — 评分前必须有报告

    Returns:
        {agent_name: AgentCard} 字典，包含 collector/analyzer/writer/quality 四张名片
    """
    return {
        # ── Collector: 数据采集 ──
        "collector": AgentCard(
            name="collector",
            description="Search and collect competitor information from the web",
            capabilities=["collect", "web_search", "web_fetch"],
            input_schema={
                "type": "object",
                "required": ["competitors", "dimensions"],
                "properties": {
                    "competitors": {"type": "array", "items": {"type": "string"}},
                    "dimensions": {"type": "array", "items": {"type": "string"}},
                },
            },
            output_schema={"type": "object"},
            endpoint="collector",
        ),
        # ── Analyzer: 多维分析 + RAG ──
        "analyzer": AgentCard(
            name="analyzer",
            description="Multi-dimensional competitive analysis with RAG retrieval",
            capabilities=["analyze", "embed", "rerank"],
            input_schema={
                "type": "object",
                "required": ["competitors", "dimensions"],
                "properties": {
                    "competitors": {"type": "array", "items": {"type": "string"}},
                    "dimensions": {"type": "array", "items": {"type": "string"}},
                },
            },
            output_schema={"type": "object"},
            endpoint="analyzer",
        ),
        # ── Writer: Markdown 报告生成 ──
        "writer": AgentCard(
            name="writer",
            description="Compose structured Markdown competitive analysis report",
            capabilities=["write", "compose_report"],
            input_schema={
                "type": "object",
                "required": ["title", "analysis_results"],
                "properties": {
                    "title": {"type": "string"},
                    "analysis_results": {"type": "object"},
                },
            },
            output_schema={"type": "object"},
            endpoint="writer",
        ),
        # ── Quality: LLM-as-Judge 质量门禁 ──
        "quality": AgentCard(
            name="quality",
            description="Evaluate report quality with 5-dimension scoring",
            capabilities=["evaluate", "score_report"],
            input_schema={
                "type": "object",
                "required": ["report_markdown"],
                "properties": {
                    "report_markdown": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            endpoint="quality",
        ),
    }
