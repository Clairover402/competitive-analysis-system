"""MCP Server — Model Context Protocol 工具注册与调用。
============================================================

【L3 面试必问】MCP 核心理念：N×M → N+M
------------------------------------------------------------
传统 Agent 架构：每个 Agent 直接集成 M 个工具 → N 个 Agent
需要 N×M 次集成。新增一个工具，所有 Agent 都得改。

MCP 架构：工具集中管理在 MCPServer，所有 Agent 通过统一协议
(tools/list + tools/call) 按需调用。工具和 Agent 解耦——
N 个 Agent + M 个工具 = N+M 的关系，不是 N×M。

面试官追问："为什么不用 Agent 直接 import 工具函数？"
答：直接 import 有 3 个问题：
  1) 紧耦合——工具签名变了，所有 Agent 代码都得改
  2) 不安全——Agent 代码可以绕过参数校验直接调内部函数
  3) 不可观测——没有统一的调用日志、频控、权限检查点
MCP Server 作为统一网关：白名单校验 → 频控 → 审计日志 →
实际调用，全在 call_tool() 一个入口里完成（Harness Engineering）。

协议操作：
    tools/list: 声明所有可用工具及参数 schema（LLM 据此决策调哪个工具）
    tools/call: 执行指定工具并返回结果（统一入口，可挂载安全切面）

【L5 项目对标】
本项目 MCPServer 是 Supervisor 架构中所有 Agent 的"能力层"。
Collector → web_search/web_fetch
Analyzer → embed_texts/embed_query/rerank + web_search
Writer/Quality → 间接通过 Analyzer 的结果来"读"数据
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ============================================================
# 工具处理器类型签名
# 统一约定：async def handler(arguments: dict) -> dict | list
# 所有工具遵循同一协议，MCPServer 不需要知道每个工具的内部参数
# ============================================================
ToolHandler = Callable[..., Any]


class ToolDef:
    """工具定义——将"做什么"(handler)和"怎么描述"(schema)打包。

    【L3 面试追问】为什么 ToolDef 是独立类，不是 dict？
    ------------------------------------------------------------
    dict 可以存数据，但不能保证结构一致性。
    ToolDef 通过 __init__ 强制 4 个字段必须填，并通过
    input_schema 属性将内部存储的 dict 转换为 MCP 标准格式。
    这是"结构体 > 字典"原则——类型安全 + 自文档。

    面试官："inputSchema 为什么是 property 不是字段？"
    答：内部存 required/properties 分体（方便单独读取），
    property 拼成 MCP 协议要求的嵌套结构 {type, required, properties}，
    避免两处维护同一数据。
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: ToolHandler,
    ) -> None:
        self.name = name
        self.description = description
        # parameters = {"required": [...], "properties": {...}}
        # 存分体而非合并体，list_tools() 时通过 input_schema 拼接
        self.parameters = parameters
        self.handler = handler

    @property
    def input_schema(self) -> dict:
        """输出 MCP 标准的 inputSchema 格式。

        MCP 协议规定每个工具必须声明：
        {type: "object", required: [...], properties: {...}}
        LLM 根据这个 schema 决定：
          - 该调哪个工具（读 description）
          - 该传什么参数（读 required + properties）
          - 参数类型对不对（读 properties 里的 type）
        """
        return {
            "type": "object",
            "required": self.parameters.get("required", []),
            "properties": self.parameters.get("properties", {}),
        }


class MCPServer:
    """MCP 服务器——所有 Agent 的"能力网关"。

    【L4 工程考量】为什么用 dict[str, ToolDef] 不用 list？
    ------------------------------------------------------------
    list 查找是 O(n)，dict 是 O(1)。当工具数量增长到几十个时
    （如火山引擎客服 20+ 工具），O(n) 的 call_tool 会成为瓶颈。
    dict 的 key 是工具名，一次哈希查找完成路由。

    【L5 面试答题模板】
    面试官："MCP Server 在你们系统里起什么作用？"
    → "它是 Agent 和外部能力之间的抽象层。三个核心职责：
      1) 工具注册与发现（tools/list → LLM 决策依据）
      2) 统一调用入口（tools/call → 挂载安全切面）
      3) 错误隔离（工具抛异常不影响 Server 稳定性）
      未来可以在 call_tool 里加 Harness 五层检查：
      白名单 → 参数校验 → 频控 → PII阻断 → 审计日志"
    """

    def __init__(self, settings=None):
        # dict 存储：key=工具名, value=ToolDef
        # O(1) 查找，call_tool 热路径不收工具数量影响
        self._tools: dict[str, ToolDef] = {}
        # settings 透传给工具函数，让工具可以访问 DB 配置、API Key 等
        self._settings = settings

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: ToolHandler,
    ) -> None:
        """注册一个工具到 MCP Server。

        【L3 设计决策】register 为什么允许覆盖？
        ------------------------------------------------------------
        生产环境中热更新工具定义时，不需要重启服务。
        warning 日志告诉运维"这个工具被重新定义了"，
        但不阻断注册流程——优先可用性。

        面试官："覆盖安全吗？"
        → "对于工具定义覆盖是安全的（旧的 schema 被新的替换）。
          但如果 handler 有状态（如数据库连接），需要额外处理。
          我们当前的工具都是无状态的纯函数，所以安全。"
        """
        if name in self._tools:
            logger.warning("Tool %r is being overwritten", name)
        self._tools[name] = ToolDef(name, description, parameters, handler)
        logger.debug("Registered tool: %s", name)

    async def list_tools(self) -> list[dict]:
        """返回所有已注册工具的 schema（MCP tools/list 标准格式）。

        【L3 理论】LLM 如何使用这个列表？
        ------------------------------------------------------------
        1. System Prompt 注入 tools/list 结果
        2. LLM 根据 query 语义 + 工具描述匹配最合适的工具
        3. 返回 function_call: {name: "web_search", arguments: {query: "飞书定价"}}
        4. MCPServer.call_tool() 执行 → 结果注入下一轮对话

        这是 Function Calling 的标准流程——但 MCP 把工具定义
        和工具执行拆分到两个协议操作，支持异步、流式、批量。

        JSON Schema 为什么是必须的？
        → LLM 需要知道参数类型才能正确生成 structured output。
          没有 schema，LLM 可能传 string 给需要 int 的参数。
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """执行工具调用（MCP tools/call 标准格式）。

        【L4 工程考量】错误处理哲学：为什么 return isError 而不 raise？
        ------------------------------------------------------------
        1) MCP 协议规定：工具异常用 isError=true 标记，不断开连接
        2) 如果 raise 异常，上层 Agent 循环会中断——一个工具挂了，
           整个分析任务跟着挂，这不符合生产系统的鲁棒要求
        3) isError 让上层可以做降级：搜索挂了 → 用缓存结果 → 继续分析

        【L5 面试】"call_tool 可以加哪些安全层面？"
        1) 白名单校验：工具名不在白名单 → 拒绝调用
        2) 参数 schema 校验：arguments 不符合 inputSchema → 拒绝
        3) 频控：TokenBucket 限流，每工具每 Agent 独立计数
        4) PII 阻断：arguments 含身份证/手机号 → 脱敏或拒绝
        5) 审计日志：每次调用写入 agent_logs 表

        返回格式：
            成功 → {content: [{type: "text", text: "..."}], isError: false}
            失败 → {content: [{type: "text", text: "错误信息"}], isError: true}

        注意：content 是数组，每条 {type, text} ——
        MCP 协议支持一个工具调用返回多个内容块（文本+图片+表格）。
        本项目只用 text 类型，但结构预留了扩展空间。
        """
        # ---- 安全切面 1: 白名单（工具不存在 = 不在白名单） ----
        if name not in self._tools:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Tool not found: {name}",
                    }
                ],
                "isError": True,
            }

        tool = self._tools[name]
        try:
            # ---- 实际执行（未来可在此加参数校验/频控/PII阻断/审计） ----
            result = await tool.handler(arguments)

            # ---- 成功：JSON 序列化结果 ----
            # default=str 处理 datetime/UUID 等非 JSON 类型
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            result, ensure_ascii=False, default=str
                        ),
                    }
                ],
                "isError": False,
            }
        except Exception as e:
            # ---- 异常兜底：记录日志 + 返回错误 ----
            # logger.exception 自动附加 traceback，方便定位
            logger.exception("Tool %r failed", name)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": str(e),
                    }
                ],
                "isError": True,
            }


# ============================================================
# 工具处理器工厂（适配器模式）
# 当前 create_mcp_server() 用 lambda 直接适配，
# 此函数留给直接绑定 dict→args 的场景备用。
# ============================================================
def _make_handler(impl_fn: Callable) -> ToolHandler:
    """将原生函数适配为 MCP handler 签名。

    适配器模式：不改原有函数签名，在外面包一层。
    原生: async def fn(a: str, b: int) -> dict
    MCP:  async def handler(arguments: {"a": "...", "b": 1}) -> dict
    """
    async def handler(arguments: dict) -> Any:
        return await impl_fn(**arguments)

    return handler
