from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── 跨任务共享缓存（2026-10-31）──────────────────────────────
# 【为什么存在】Tavily 免费额度 1000 次/月，40 条任务按旧口径直接烧穿。
# 工具级幂等（idem_key）救不了它：指纹掺了 task_id，缓存按任务隔离，
# 两个任务搜同一个 query 仍是两次真实调用。共享缓存把键挪到「query 语义」上，
# 24h 内跨任务回放。
# 【为什么只对 web_search 开】web_fetch 已有按 URL 的跨任务去重（上游 seen_urls
# + 幂等键就是 URL）；embed/rerank 是纯计算无外部配额。搜索是唯一
# 「同参数重跑 = 纯浪费外部配额且现有层拦不住」的工具。
_SHARED_CACHE_TTL: dict[str, int] = {"web_search": 24 * 3600}

# query 归一化：LLM 每次生成的措辞会漂（"飞书 2025 价格" vs "2025 飞书价格"）。
# 完全字面匹配命中率太低；按「token 集」归一（大小写折叠 + 切词 + 排序），
# 语序/空格差异不 Miss，只有真实换词才 Miss。
# ⚠️ 先在中英/数字边界补空格再切：「2025飞书」必须切成「2025」+「飞书」，
#    否则它作为一个 token，和「2025 飞书」的 token 集合永不相等。
_SHARED_BOUNDARY_RE = re.compile(
    r"(?<=[0-9a-zA-Z])(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])(?=[0-9a-zA-Z])"
)
_SHARED_TOKEN_RE = re.compile(r"[0-9a-zA-Z\u4e00-\u9fff]+")


def _shared_cache_key(arguments: dict) -> str | None:
    """从工具参数提取归一化缓存键。非 web_search 形状的参数返回 None（不缓存）。"""
    if not isinstance(arguments, dict):
        return None
    q = arguments.get("query")
    if not isinstance(q, str) or not q.strip():
        return None
    normalized = _SHARED_BOUNDARY_RE.sub(" ", q.casefold())
    tokens = sorted(_SHARED_TOKEN_RE.findall(normalized))
    return " ".join(tokens) if tokens else None


def _shared_call_id(tool_name: str, norm_query: str) -> str:
    """共享缓存行键：**不含 task_id** —— 跨任务命中就是靠这一点。"""
    return hashlib.sha256(f"shared|{tool_name}|{norm_query}".encode("utf-8")).hexdigest()

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
        # 搜索结果的引擎来源分布（2026-10-30）。
        # 只记 n 的话，synthesize 兜底（不搜索、按 query 拼 URL）返回的非空列表
        # 和 Tavily 真搜到的结果长得一模一样 —— 下游算出的「搜索空结果率」恒为 0，
        # 假健康。记下来才能把「真搜到」和「拼出来的」分开。
        engines = Counter(
            item["engine"]
            for item in result
            if isinstance(item, dict) and item.get("engine")
        )
        if engines:
            summary["engines"] = dict(engines)
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
        # 渲染兜底标记（2026-10-30）：playwright = httpx 拿到骨架后用本地 chromium
        # 重新渲染取得的正文。不落这个键，抓取层「90% 靠渲染救回来」这件事
        # 在库里完全看不见，只能去翻日志文件。
        if result.get("fallback"):
            summary["fallback"] = result["fallback"]
        return summary
    if isinstance(result, str):
        return {"kind": "str", "chars": len(result)}
    return {"kind": type(result).__name__}


def _tool_call_id(task_id: str | None, name: str, idem_key: str) -> str:
    """把 (task, tool, 语义键) 压成确定性的幂等指纹。

    ★ 必须是确定性哈希而不是随机 UUID：中断续跑时「同一个逻辑调用」要算出
    同一个 id 才能查到首次结果 —— 随机 id 重启后对不上号，幂等整个失效。
    task_id 参与哈希 ⇒ 缓存天然按任务隔离，跨任务永不串。
    """
    return hashlib.sha256(f"{task_id or '-'}|{name}|{idem_key}".encode("utf-8")).hexdigest()


# ── 工具级超时（2026-10-31，亮点 A / A1）──────────────────────────────
# 【为什么必须在这一层设】此前一个工具**没有任何超时上限**：httpx 的超时只护
# 住 HTTP 读写，playwright 卡在 page.goto 之外的等待（站点弹窗、JS 死循环）
# 会把 collect 节点挂住直到进程被杀 —— 而任务状态停在 running，
# 用户看到的是"一直在跑"，日志里一行错误都没有。
# 【为什么这些数字能这么宽】它们是 watch-dog，不是预算：每个值都 ≥ 该工具内部
# 各段 httpx 超时之和（web_search 三层 20+15+15、web_fetch 20+30+20），
# 正常路径永远碰不到，只有"真卡死"才会触发。
# 【为什么 embed/rerank 也给】它们虽然是纯计算，但都经 httpx 打外部推理服务；
# 留一个上限是为了让"卡住"以异常形式暴露，而不是拖成永不结束的协程。
_TOOL_TIMEOUT_SECONDS: dict[str, float] = {
    "web_search": 90.0,
    "web_fetch": 120.0,
    "embed_texts": 300.0,   # 大批量分多次请求，单个请求 60s
    "embed_query": 60.0,
    "rerank": 180.0,
}
DEFAULT_TOOL_TIMEOUT_SECONDS = 300.0


def _tool_timeout(name: str) -> float:
    return _TOOL_TIMEOUT_SECONDS.get(name, DEFAULT_TOOL_TIMEOUT_SECONDS)


# 对账的三值结果：命中（已执行）/ 未命中（判定未执行）/ 不适用。
# ⚠️ 不能用 None 表示"未命中" —— 工具真的可以合法返回 None，
# 那样"查到了 None"和"没查到"就混成一个值，恰好是这类代码最典型的静默缺陷。
_MISS = object()


def _count_reconcile(result: str) -> None:
    """对账结果计数。延迟 import + 吞异常：埋点不能影响主流程，
    mcp 层也不该在模块级反向依赖 observability。"""
    try:
        from src.observability.metrics import record_tool_reconcile

        record_tool_reconcile(result)
    except Exception:  # noqa: BLE001 — 计数器坏了不该让工具调用失败
        logger.debug("对账指标写入失败 result=%s", result, exc_info=True)


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
        # 幂等存储：任何带 `async get(id) -> Any|None` / `async save(...) -> bool`
        # 的对象（项目里是 ToolCallResultDAO）。同 audit 一样鸭子类型后置挂载。
        self._idem = None
        # 进程内同 key 在跑的执行任务：并发/重试同 key 共享一次执行，
        # 不只是查表 —— 表只在执行**完成后**才有记录，in-flight 期查表是查空的。
        self._inflight: dict[str, asyncio.Task] = {}

    def attach_audit(self, audit) -> None:
        """后置挂载审计出口。

        【为什么需要后置挂载】`create_mcp_server` 在 lifespan 里调用时，
        连接池可能还没建好；先建 server、拿到 pool 再 attach，
        避免为了构造函数多传一层依赖。
        """
        self._audit = audit

    def attach_idempotency(self, store) -> None:
        """后置挂载幂等存储（项目里是 ToolCallResultDAO，同 attach_audit 理由）。

        不挂 = invoke 的 idem_key 参数被忽略、行为与改动前完全一致 ——
        幂等是**逐调用点选择**的（只有副作用工具传 idem_key），不是全局开关。
        """
        self._idem = store

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
        idem_key: str | None = None,
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
                name, arguments, task_id=task_id, agent_name=agent_name,
                idem_key=idem_key,
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
        idem_key: str | None = None,
    ) -> Any:
        """统一执行层 —— 所有工具调用的唯一实现路径。

        【职责】白名单 → 参数校验 → （幂等回放）→ 执行 → 落结果 → 审计，
        返回工具原生 dict/list。
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

        【幂等（2026-10-30）】idem_key 是调用方给的「逻辑调用」语义键
        （如 f"search|{competitor}|{query}"），与 (task_id, tool_name) 一起
        哈希成 tool_call_id：
          命中结果日志表 / 进程内 in-flight → 回放首次结果，不重复副作用；
          未命中 → 执行，成功后落表。
        为什么只有副作用工具传 idem_key：embed/rerank 是纯计算，重跑无副作用，
        传了反而把 MB 级向量塞进缓存表。⚠️ 只缓存成功结果 —— 失败可重试，
        缓存失败等于把偶发故障钉死。⚠️ 调用方拿到回放结果时审计里
        replayed=true，轨迹评估据此区分「真执行」与「回放」。
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

        # ── 幂等层：不传 idem_key 或未挂存储 → 走原路径，行为零变化 ──
        if idem_key is not None and self._idem is not None:
            return await self._invoke_idempotent(
                tool, arguments, idem_key, task_id, agent_name, started
            )

        error: str | None = None
        result_summary: dict | None = None
        try:
            # 超时上限见 _TOOL_TIMEOUT_SECONDS。无 idem_key 的调用没有对账依据
            # （不知道"这个逻辑调用"是谁），超时只能判定为未完成、交回上层降级。
            result = await asyncio.wait_for(tool.handler(arguments), _tool_timeout(name))
            # 2026-10-19：结果摘要入审计 —— 轨迹评估要靠它算空结果率、
            # 采集漏斗、rerank 分数区分度；不记原文，只记体积/分布。
            result_summary = _summarize_result(result)
            return result
        except asyncio.TimeoutError:
            error = f"TimeoutError: {name} 超过 {_tool_timeout(name):.0f}s 未返回"
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self._record(
                name, arguments, task_id, agent_name,
                round((time.perf_counter() - started) * 1000, 1),
                error, result_summary,
            )

    async def _invoke_idempotent(
        self,
        tool: ToolDef,
        arguments: dict,
        idem_key: str,
        task_id: str | None,
        agent_name: str | None,
        started: float,
    ) -> Any:
        """带幂等的执行：查表回放 → 共享 in-flight → shield 执行 + 落结果。

        ★ 执行体必须套 asyncio.shield：调用方超时 wait_for 掐断的是 shield 外层，
          执行体继续跑完并落表 —— 于是「超时重试返回首次结果」成立：
          重试同 key 查表即命中。不 shield 的话超时会连执行体一起取消，
          表里永远没有记录，重试只会再次重复副作用。
        ★ in-flight 表的清理在执行体 finally 里做，不在等待方做 ——
          等待方超时先走，若由它清理，后来的同 key 调用查不到 in-flight
          又查不到表（还没落库），会再起一次重复执行。
        """
        tc_id = _tool_call_id(task_id, tool.name, idem_key)
        name = tool.name

        # 1) 首次结果已落表 → 直接回放（覆盖：上次正常执行完、中断后续跑重放）
        try:
            cached = await self._idem.get(tc_id)
        except Exception:
            # 存储挂了不能阻断主流程：退化为直接执行（幂等失效一次，可用性优先）
            logger.exception("幂等查询失败，退化为直接执行 tool=%r", name)
            cached = None
        if cached is not None:
            await self._record(
                name, arguments, task_id, agent_name,
                round((time.perf_counter() - started) * 1000, 1),
                None, {"replayed": True, **_summarize_result(cached)},
            )
            return cached

        # 1.5) 跨任务共享缓存（24h）：别的任务已经搜过同一 query → 直接回放。
        #   放在 per-task 回放之后、in-flight 之前：本任务自己搜过的优先级最高
        #   （语义完全一致），共享缓存兜跨任务，in-flight 只兜本进程并发。
        shared_ttl = _SHARED_CACHE_TTL.get(name)
        if shared_ttl is not None:
            norm = _shared_cache_key(arguments)
            if norm:
                shared_id = _shared_call_id(name, norm)
                try:
                    shared_cached = await self._idem.get_fresh(shared_id, shared_ttl)
                except Exception:
                    # 同幂等层的纪律：存储挂了退化为直接执行，不阻断主流程
                    logger.exception("共享缓存查询失败，跳过 tool=%r", name)
                    shared_cached = None
                if shared_cached is not None:
                    await self._record(
                        name, arguments, task_id, agent_name,
                        round((time.perf_counter() - started) * 1000, 1),
                        None,
                        {"replayed": True, "cache": "shared", **_summarize_result(shared_cached)},
                    )
                    return shared_cached

        # 2) 进程内已有同 key 执行在跑（并发 / 快速重试）→ 共享它的结果
        existing = self._inflight.get(tc_id)
        if existing is not None:
            return await asyncio.shield(existing)

        # 3) 新执行：成功落表，异常不落表（可重试）
        exec_task = asyncio.create_task(
            self._exec_and_cache(tool, arguments, tc_id, task_id)
        )
        self._inflight[tc_id] = exec_task
        error: str | None = None
        result_summary: dict | None = None
        try:
            result = await asyncio.shield(exec_task)
            result_summary = _summarize_result(result)
            return result
        except asyncio.TimeoutError as exc:
            # ── 超时对账（2026-10-31）──
            # 超时只能说明"没等到结果"，不能说明"没执行"：执行体可能已经打完
            # 外部 API（配额已扣）却没能在期限内返回。此时先查结果日志：
            #   命中 → 判定**已执行**，回放首次结果（省掉一次重复副作用）
            #   未命中 → 判定**未执行**，抛回去交上层重试
            # ⚠️ 查不到时无法区分"没执行"和"执行了但失败"，但对重试决策而言
            #    两者一样（都该重试），所以表里不需要 status 列 —— 只缓存成功结果
            #    本身就是正确设计。
            hit = await self._reconcile(tc_id, name)
            if hit is _MISS:
                _count_reconcile("miss")
                error = (
                    f"TimeoutError: {name} 超过 {_tool_timeout(name):.0f}s 未返回，"
                    f"对账未命中（判定未执行）"
                )
                raise
            _count_reconcile("hit")
            result_summary = {
                "replayed": True, "reconcile": "timeout",
                **_summarize_result(hit),
            }
            return hit
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self._record(
                name, arguments, task_id, agent_name,
                round((time.perf_counter() - started) * 1000, 1),
                error, result_summary,
            )

    async def _reconcile(self, tc_id: str, name: str) -> Any:
        """超时后对账：查结果日志，返回首次结果或 `_MISS`。

        只查一次、不等待：执行体在超时那一刻已被取消，不会再落表；
        真正可能命中的是「另一个进程/上一次尝试已经成功并落了表」。
        存储故障按"查不到"处理（退化为重试），与幂等层的既有纪律一致。
        """
        try:
            rec = await self._idem.get_record(tc_id)
        except Exception:  # noqa: BLE001 — 对账失败不能盖住原始超时
            logger.exception("超时对账查询失败，按未执行处理 tool=%r", name)
            return _MISS
        if rec is None:
            logger.info("超时对账未命中，判定未执行 tool=%s id=%s", name, tc_id[:8])
            return _MISS
        logger.info(
            "超时对账命中，判定已执行并回放 tool=%s id=%s 落库于 %s",
            name, tc_id[:8], rec.get("created_at"),
        )
        return rec["result"]

    async def _exec_and_cache(
        self,
        tool: ToolDef,
        arguments: dict,
        tc_id: str,
        task_id: str | None,
    ) -> Any:
        """执行工具并把**成功**结果落幂等表。执行失败只上抛、不落表。"""
        try:
            # 超时上限是 watch-dog：一旦触发，wait_for 会取消 handler —— 卡死的
            # 执行体不能留在后台继续跑（它可能正在写库/占连接），
            # 取消后由 _invoke_idempotent 走对账分支决定"已执行/未执行"。
            result = await asyncio.wait_for(
                tool.handler(arguments), _tool_timeout(tool.name)
            )
        finally:
            self._inflight.pop(tc_id, None)
        try:
            await self._idem.save(
                tc_id, tool.name, task_id, _digest_arguments(arguments), result
            )
        except Exception:
            # 落表失败 ≈ 这次执行白缓存（重试会重跑一次），但不能让它炸掉主流程
            logger.exception("幂等结果落库失败 tool=%r tc_id=%s…%s",
                             tool.name, tc_id[:8], tc_id[-8:])
        # 共享缓存：只在幂等行落成功后写。upsert 覆盖旧值 —— 缓存语义允许
        # 新换旧，与幂等行的 DO NOTHING（首次结果不可变）刻意相反。
        shared_ttl = _SHARED_CACHE_TTL.get(tool.name)
        if shared_ttl is not None:
            norm = _shared_cache_key(arguments)
            if norm:
                try:
                    await self._idem.upsert_shared(
                        _shared_call_id(tool.name, norm),
                        tool.name, _digest_arguments(arguments), result,
                    )
                except Exception:
                    logger.exception("共享缓存落库失败 tool=%r（不影响本次结果）", tool.name)
        return result

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

