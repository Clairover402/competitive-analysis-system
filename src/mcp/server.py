from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ============================================================
# 工具处理器类型签名
# 统一约定：async def handler(arguments: dict) -> dict | list
# 所有工具遵循同一协议，MCPServer 无需知晓每个工具内部参数
# ============================================================
ToolHandler = Callable[..., Any]


class ToolDef:
    """工具定义 —— 将"做什么(handler)"和"如何描述"(schema)封装。

    面试追问：为什么ToolDef是独立类，不是dict？
    ------------------------------------------------------------
    dict可以存储数据，但无法保证结构一致性。
    ToolDef 通过 __init__ 强制 4 个字段必填，并通过
    input_schema 属性将内部存储的 dict 转换为 MCP 标准格式。
    这是"结构化对象 > 字典"原则 —— 类型安全 + 自文档。

    面试官提问："inputSchema 为什么是 property 不是字段？"
    回答：内部拆分required/properties（方便单独读取），
    property 组装成 MCP 协议要求的嵌套结构 {type, required, properties}，
    避免两处维护同一套数据。
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
        # 存储拆分而非合并对象，list_tools() 时通过 input_schema 拼接
        self.parameters = parameters
        self.handler = handler

    @property
    def input_schema(self) -> dict:
        """输出 MCP 标准的 inputSchema 格式。

        MCP 协议规定每个工具必须声明：
        {type: "object", required: [...], properties: {...}}
        LLM 根据这套 schema 决定：
          - 该调用哪个工具（读 description）
          - 该传什么参数（看 required + properties）
          - 参数类型是否合法（看 properties 内的 type）
        """
        return {
            "type": "object",
            "required": self.parameters.get("required", []),
            "properties": self.parameters.get("properties", {}),
        }


class MCPServer:
    """MCP 服务端 —— 所有 Agent 的能力网关。

    工程考察：为什么用 dict[str, ToolDef] 不用 list？
    ------------------------------------------------------------
    list 查询是 O(n)，dict 是 O(1)。当工具数量增长到几十个时
    （例如爬虫业务后台 20+ 工具），O(n) 的 call_tool 会成为瓶颈。
    dict 的 key 为工具名，一次哈希查找完成路由。

    面试答题模板：
    面试官提问："MCP Server 在你们系统里起到什么作用？"
    回答："它是 Agent 和外部能力之间的隔离层。三大核心职责：
      1) 工具注册与发现（tools/list → LLM 决策依据）
      2) 统一调用入口（tools/call → 挂载安全切面）
      3) 故障隔离（工具异常不影响 Server 稳定性）
      未来可在 call_tool 里增加五层校验流水线：
      白名单校验 → 参数校验 → 限流 → PII拦截 → 审计日志"
    """

    def __init__(self, settings=None):
        # dict 存储，key=工具名 value=ToolDef
        # O(1) 查询，call_tool 热路径不受工具数量影响
        self._tools: dict[str, ToolDef] = {}
        # settings 透传给工具函数，让工具可以访问DB配置、API Key 等
        self._settings = settings

    @property
    def settings(self):
        """Public accessor for Settings held by this server."""
        return self._settings

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: ToolHandler,
    ) -> None:
        """注册一个工具到 MCP Server。

        设计决策：register 为什么允许覆盖？
        ------------------------------------------------------------
        生产环境热更新工具定义时，不需要重启服务。
        warning 日志告知运维"该工具被重新定义"，
        但不阻断注册流程 —— 优先保证可用性。

        面试官提问："覆盖安全吗？"
        回答："对于工具定义覆盖是安全的（旧schema被新的替换）。
          但如果handler带有状态（如数据库连接），需要额外处理。
          我们当前所有工具都是无状态纯函数，因此安全。"
        """
        if name in self._tools:
            logger.warning("Tool %r is being overwritten", name)
        self._tools[name] = ToolDef(name, description, parameters, handler)
        logger.debug("Registered tool: %s", name)

    async def list_tools(self) -> list[dict]:
        """返回所有已注册工具的schema（MCP tools/list 标准格式）。

        理论题：LLM 如何使用这个列表？
        ------------------------------------------------------------
        1. System Prompt 注入 tools/list 返回结果
        2. LLM 根据 query 语义 + 工具描述匹配最合适的工具
        3. 返回 function_call: {name: "web_search", arguments: {query: "机票定价"}}
        4. MCPServer.call_tool() 执行 → 结果注入下一轮对话

        这是 Function Calling 标准流程 —— 但 MCP 把工具定义
        和工具执行拆分成两个协议操作，支持异步、流式、权限隔离。

        JSON Schema 为什么是必需的？
        回答：LLM 需要知晓参数类型才能生成结构化输出。
          没有schema，LLM可能用string传给需要int的参数。
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

        工程考察：异常处理策略 —— 为什么return isError 而不是 raise？
        ------------------------------------------------------------
        1) MCP 协议规定：工具异常用 isError=true 标记，不中断连接
        2) 如果直接抛出异常，上层Agent循环会中断；一个工具挂了，
           整个分析任务跟着卡死，不符合生产系统容错要求
        3) isError 让上层可以降级处理：搜索超时 → 使用缓存结果 → 继续分析

        面试提问：call_tool 可以增加哪些安全切面？
        1) 白名单校验：工具名不在白名单 → 拒绝调用
        2) 参数schema校验：arguments不符合inputSchema → 拒绝
        3) 限流：TokenBucket 限流，每个Agent独立计数
        4) PII 拦截：arguments包含身份证/手机号 → 脱敏或拒绝
        5) 审计日志：每次调用写入agent_logs表

        返回格式：
            成功 → {content: [{type: "text", text: "..."}], isError: false}
            失败 → {content: [{type: "text", text: "错误信息"}], isError: true}

        注意：content 是数组，每条 {type, text} ——
        MCP 协议支持单次工具调用返回多个内容块（文本+图片+表格）。
        本项目仅使用 text 类型，但结构预留扩展空间。
        """
        # ---- 安全切面 1: 白名单校验(工具不存在=不在白名单) ----
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
            # ---- 实际执行(未来可在此增加参数校验/限流/PII拦截/审计) ----
            result = await tool.handler(arguments)

            # ---- 成功，JSON序列化结果 ----
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
            # ---- 异常捕获：记录日志 + 返回错误结构 ----
            # logger.exception 自动附加堆栈，方便定位
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
# 工具处理器工厂(适配器模式)
# 当前 create_mcp_server() 使用 lambda 直接适配，
# 此函数留给直接绑定 dict→args 的场景备用
# ============================================================
def _make_handler(impl_fn: Callable) -> ToolHandler:
    """将原生函数适配为 MCP handler 签名。

    适配器模式：不修改原有函数签名，外层包装一层。
    原生: async def fn(a: str, b: int) -> dict
    MCP:  async def handler(arguments: {"a": "...", "b": 1}) -> dict
    """
    async def handler(arguments: dict) -> Any:
        return await impl_fn(**arguments)

    return handler