from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ============================================================
# 工具处理器类型签名
# 统一约定：async def handler(arguments: dict) -> dict | list
# 所有工具遵循同一协议，MCPServer 无需知晓每个工具内部参数
# ============================================================
ToolHandler = Callable[..., Any]


# ── 管控层的两个拒绝原因（与「工具内部执行失败」区分开）───────────────
class ToolNotFound(Exception):
    """工具名不在注册表里 = 白名单拒绝。"""


class ToolArgumentError(Exception):
    """参数不满足工具声明的 inputSchema = 参数校验拒绝。"""


# 参数类型 → 校验函数。
# ⚠️ bool 是 int 的子类，integer/number 必须显式排除 bool，
#    否则 max_results=True 会被当成合法整数放过去。
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, (list, tuple)),
    "object": lambda v: isinstance(v, dict),
    "boolean": lambda v: isinstance(v, bool),
}


def _validate_arguments(tool: ToolDef, arguments: dict) -> None:
    """轻量参数校验：required 键存在 + schema 已声明类型的类型匹配。

    只查「键丢了 / 类型明显不对」这类错误，不校验空值语义
    （空 list 对 embed_texts 是错的、对 rerank 可能是合法输入，交给工具自己判），
    也不做完整 JSON Schema —— 目的是兜住 LLM/调用方的手滑，不是替工具做业务校验。
    """
    if not isinstance(arguments, dict):
        raise ToolArgumentError(f"{tool.name}: arguments 必须是 dict，实际 {type(arguments).__name__}")

    params = tool.parameters or {}
    missing = [k for k in params.get("required", []) if k not in arguments]
    if missing:
        raise ToolArgumentError(f"{tool.name}: 缺少必填参数 {missing}")

    props = params.get("properties", {}) or {}
    for key, value in arguments.items():
        spec = props.get(key)
        if not spec:
            continue  # schema 未声明的额外参数放行（向前兼容老调用方）
        check = _TYPE_CHECKS.get(spec.get("type", ""))
        if check is not None and not check(value):
            raise ToolArgumentError(
                f"{tool.name}: 参数 {key!r} 期望 {spec.get('type')}，实际 {type(value).__name__}"
            )


def _digest_arguments(arguments: dict, max_str: int = 120) -> dict:
    """把工具参数压成可落库的摘要。

    必须压：rerank 的 documents 是几十段长文本、embed_texts 的 texts 是几百条
    chunk 正文，原样落库会让 agent_logs 一任务涨几十 MB。
    """
    if not isinstance(arguments, dict):
        return {"_raw_type": type(arguments).__name__}

    digest: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            digest[key] = value if len(value) <= max_str else f"{value[:max_str]}…(len={len(value)})"
        elif isinstance(value, (list, tuple)):
            first = value[0] if value else None
            # 只记长度，不记首个元素内容 —— rerank 的 documents 是整段正文，
            # 记「首元素前 60 字」等于把正文落库，摘要的意义就没了。
            digest[key] = {
                "list_len": len(value),
                "first_type": type(first).__name__ if value else None,
                "first_len": len(first) if isinstance(first, str) else None,
            }
        elif isinstance(value, dict):
            digest[key] = {"dict_len": len(value), "keys": list(value.keys())[:8]}
        else:
            digest[key] = value
    return digest


def _summarize_result(result: Any) -> dict:
    """结果摘要 —— 只记体积与分布，不记原文。

    为什么连分数分布都要记：`rerank` 返回 `[{index, text, score}]`，
    `top1 - median` 是判断「精排到底有没有区分度」的唯一廉价信号。
    2026-10-17 实测过导航条打 0.916、真证据打 0.854（精排把噪声排在证据前面），
    当时靠人工翻日志才发现 —— 落成指标后它就是可回归的数字。
    """
    if isinstance(result, (list, tuple)):
        summary: dict[str, Any] = {"kind": "list", "n": len(result)}
        scores = [
            item["score"]
            for item in result
            if isinstance(item, dict) and isinstance(item.get("score"), (int, float))
        ]
        if scores:
            ordered = sorted(scores, reverse=True)
            summary["score_top1"] = round(ordered[0], 4)
            summary["score_median"] = round(ordered[len(ordered) // 2], 4)
            summary["score_min"] = round(ordered[-1], 4)
        return summary
    if isinstance(result, dict):
        summary = {"kind": "dict", "n": len(result)}
        for key in ("text_content", "text", "content"):
            value = result.get(key)
            if isinstance(value, str):
                summary["chars"] = len(value)
                break
        return summary
    if isinstance(result, str):
        return {"kind": "str", "chars": len(result)}
    return {"kind": type(result).__name__}


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

    def __init__(self, settings=None, audit=None):
        # dict 存储，key=工具名 value=ToolDef
        # O(1) 查询，call_tool 热路径不受工具数量影响
        self._tools: dict[str, ToolDef] = {}
        # settings 透传给工具函数，让工具可以访问DB配置、API Key 等
        self._settings = settings
        # 审计出口：任何带 `async log(**kwargs)` 的对象（项目里是 AgentLogDAO）。
        # 这里刻意不 import AgentLogDAO —— mcp 层不该反向依赖 db 层，
        # 鸭子类型足够，测试时塞个假对象就能验审计。
        self._audit = audit

    def attach_audit(self, audit) -> None:
        """后置挂载审计出口。

        【为什么需要后置挂载】`create_mcp_server` 在 lifespan 里调用时，
        连接池可能还没建好；先建 server、拿到 pool 再 attach，
        避免为了构造函数多传一层依赖。
        """
        self._audit = audit

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
        """
        if name in self._tools:
            logger.warning("Tool %r 该工具被重新定义", name)
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

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        task_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict:
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
        # 协议入口只做编解码 —— 管控全部落在 invoke，保证「一份管控实现」：
        # 外部 MCP 调用和 Agent 进程内直调走的是同一套白名单/校验/审计。
        try:
            result = await self.invoke(
                name, arguments, task_id=task_id, agent_name=agent_name
            )
        except ToolNotFound:
            return {
                "content": [{"type": "text", "text": f"Tool not found: {name}"}],
                "isError": True,
            }
        except ToolArgumentError as exc:
            return {
                "content": [{"type": "text", "text": f"Invalid arguments: {exc}"}],
                "isError": True,
            }
        except Exception as exc:
            # 工具内部异常：MCP 协议规定用 isError 标记，不中断连接
            logger.exception("Tool %r failed", name)
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }

        return {
            "content": [
                {
                    "type": "text",
                    # default=str 处理 datetime/UUID 等非 JSON 类型
                    "text": json.dumps(result, ensure_ascii=False, default=str),
                }
            ],
            "isError": False,
        }

    async def invoke(
        self,
        name: str,
        arguments: dict,
        *,
        task_id: str | None = None,
        agent_name: str | None = None,
    ) -> Any:
        """统一执行层 —— 所有工具调用的唯一实现路径。

        【职责】白名单 → 参数校验 → 执行 → 审计，返回工具原生 dict/list。
        【与 call_tool 的分工】
            invoke    = 管控 + 原生结果（进程内 Agent 走这里）
            call_tool = MCP 协议编解码（content 数组 + JSON 字符串），内部调 invoke
        拆开的理由有两条：
          ① 管控必须只有一份实现。2026-10-19 之前 call_tool 号称「唯一入口」，
             但 embed_texts / embed_query / rerank 被 Agent 直接 import 调函数绕过，
             白名单/参数校验/审计实际只覆盖 2/5 的工具。
          ② 检索工具返回 1024 维向量、几十条长文本，为套 MCP 协议先 json.dumps
             再 json.loads 是纯浪费；invoke 直接给原生对象。

        【审计口径】只记参数摘要不记原文；失败必记、成功也记 ——
        「调了但没成功」和「根本没调」必须能区分，否则轨迹评估无从谈起。
        """
        tool = self._tools.get(name)
        if tool is None:
            # 白名单拒绝要留痕：这是安全事件，不是普通业务失败
            await self._record(name, arguments, task_id, agent_name, 0.0, f"ToolNotFound: {name}")
            raise ToolNotFound(name)

        started = time.perf_counter()
        try:
            _validate_arguments(tool, arguments)
        except ToolArgumentError as exc:
            await self._record(
                name, arguments, task_id, agent_name,
                round((time.perf_counter() - started) * 1000, 1),
                f"ToolArgumentError: {exc}",
            )
            raise

        error: str | None = None
        result_summary: dict | None = None
        try:
            result = await tool.handler(arguments)
            # 2026-10-19：结果摘要入审计 —— 轨迹评估要靠它算空结果率、
            # 采集漏斗、rerank 分数区分度；不记原文，只记体积/分布。
            result_summary = _summarize_result(result)
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self._record(
                name, arguments, task_id, agent_name,
                round((time.perf_counter() - started) * 1000, 1),
                error, result_summary,
            )

    async def _record(
        self,
        name: str,
        arguments: dict,
        task_id: str | None,
        agent_name: str | None,
        duration_ms: float,
        error: str | None,
        result_summary: dict | None = None,
    ) -> None:
        """写一条工具调用审计。审计永远不能影响主流程。"""
        if self._audit is None or not task_id:
            return
        try:
            await self._audit.log(
                task_id=task_id,
                agent_name=agent_name or "unknown",
                action="tool_call",
                request={"tool": name, "args": _digest_arguments(arguments)},
                response={"ok": error is None, **(result_summary or {})},
                error=error,
                duration_ms=duration_ms,
            )
        except Exception:
            # 与 AgentLogDAO.log 的 fire-and-forget 口径一致：记自己的失败，不往上抛
            logger.exception("工具调用审计写入失败 tool=%r", name)

