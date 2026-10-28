"""FastAPI 路由 — 竞品分析系统 HTTP 服务层。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 架构 — HTTP 层定位】
═══════════════════════════════════════════════════════════════════════════════

routes.py 只做三件事：

  1. 接收 HTTP 请求 → 校验参数 → 调用已有模块
  2. 管理异步任务生命周期（创建/查询/SSE）
  3. 不写任何编排逻辑——路由决策在 router.py、引擎在 graph.py/supervisor.py

端点一览：
  ┌───────────────────────────┬──────┬────────────────────────────────┐
  │ 端点                       │ 方法  │ 功能                           │
  ├───────────────────────────┼──────┼────────────────────────────────┤
  │ /api/tasks                │ POST │ 创建竞品分析任务                │
  │ /api/tasks/{task_id}      │ GET  │ 查询任务状态                    │
  │ /api/tasks/{task_id}/stream│ GET  │ SSE 进度推送                    │
  │ /api/tasks/{task_id}/reports│ GET │ 获取报告列表                    │
  │ /health                   │ GET  │ 健康检查 + DB 连通              │
  │ /metrics                  │ GET  │ Prometheus 指标                 │
  └───────────────────────────┴──────┴────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import generate_latest
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.config import Settings
from src.db.connection import close_pool, create_pool
from src.db.dao import ConversationDAO, PreferenceDAO, ReportDAO, TaskDAO
from src.observability.logging import BoundLogger
from src.observability.metrics import (
    record_agent_call,
    record_harness_block,
    record_rate_limit_hit,
    record_task_completed,
    record_task_created,
    record_task_failed,
    record_task_started,
)
from src.api.rate_limit import AgentSemaphore, LLMRateLimiter, TokenBucket
from src.api.sse import event_generator
from src.api.auth_routes import router as auth_router
from src.auth.dependencies import CurrentUser, get_current_user

if TYPE_CHECKING:
    from asyncpg import Pool

logger = logging.getLogger("competitive_analysis")


# ═════════════════════════════════════════════════════════════════════════════
# §1 Pydantic 数据模型
# ═════════════════════════════════════════════════════════════════════════════

def _split_i18n_commas(items: list[str]) -> list[str]:
    """防御性拆分：中英文逗号都认，trim 空白，去空串。

    【L4 工程】前端已做 split(/[,，]/)，但后端加防御层：
      — 如果前端拼错（如"王者荣耀，英雄联盟"没拆开），后端自动拆分
      — 保证 competitors/dimensions 每个元素都是单个名称
    """
    result: list[str] = []
    for item in items:
        for part in item.replace("，", ",").split(","):
            cleaned = part.strip()
            if cleaned:
                result.append(cleaned)
    return result


def _find_duplicates(items: list[str] | None) -> list[str]:
    """返回重复出现的元素（保持首次出现顺序，每个重复值只列一次）。

    【为什么需要】analyzer 的内层循环是 `for competitor in competitors`，
    对重复项会**再跑一遍完整的「检索 + 精排 + LLM 分析」**（白花 token）；而
    结果写入 `analysis_results[dim][competitor]` 是 dict 赋值 → 后一次**静默覆盖**
    前一次，用户拿到一份"少了一格"却毫无报错的报告。维度列表同理。
    """
    if not items:
        return []
    seen: set[str] = set()
    dups: list[str] = []
    for x in items:
        if x in seen and x not in dups:
            dups.append(x)
        seen.add(x)
    return dups


def _dedupe(items: list[str]) -> list[str]:
    """保序去重（保留首次出现）。用于 LLM 抽取结果——用户没输入重复项，
    不该因模型的重复输出被 400，静默归一即可。"""
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _reject_duplicates(
    competitors: list[str] | None,
    dimensions: list[str] | None,
) -> None:
    """重复项校验：同任务内重复的竞品/维度 → HTTP 400（2026-10-13）。

    【L5 决策】为什么是「拒绝」而不是「静默去重」？
      — 用户列表里的重复项通常是**输入错误**（复制粘贴、中英文逗号混用导致），
        静默去重会让用户以为"我传了两个"而实际只分析了一个；
      — 显式 400 让用户当场发现并改正，避免拿到口径不符的报告。
      — LLM 抽取路径的重复属模型副作用，走 `_dedupe` 静默归一（见上）。
    """
    problems: list[str] = []
    dup_competitors = _find_duplicates(competitors)
    dup_dimensions = _find_duplicates(dimensions)
    if dup_competitors:
        problems.append(f"竞品重复：{'、'.join(dup_competitors)}")
    if dup_dimensions:
        problems.append(f"维度重复：{'、'.join(dup_dimensions)}")
    if problems:
        raise HTTPException(400, "；".join(problems) + "。请去掉重复项后重试。")


def _reject_over_limit(
    competitors: list[str] | None,
    dimensions: list[str] | None,
    settings: Settings,
) -> None:
    """规模上限校验：竞品或维度超过上限 → HTTP 400（2026-10-21 产品约束）。

    【为什么要有上限——不是成本，是质量会静默劣化】
      检索是**单任务混池**取 `vector_search_top_k`(=60) 条 chunk，这是个绝对值：
      竞品数一多，每个竞品分到的配额就被摊薄。实测 8 竞品 / 612 chunk 的任务里，
      某竞品在单一维度 **0/60** → 那格只能写 `[数据不足]`，
      而最终报告**看起来是完整的一份**。这种缺格比 400 难发现得多。

    【为什么是拒绝而不是截断】
      与 `_reject_duplicates` 同一口径：截断会静默改写用户要分析的范围，
      用户拿到少几格的报告却以为按自己说的跑了。显式 400 让用户自己决定砍哪个。

    【为什么显式提示数量】
      「最多 4 个」不告诉用户「你现在传了几个」，用户还得自己数一遍。
    """
    problems: list[str] = []
    if competitors and len(competitors) > settings.max_competitors:
        problems.append(
            f"竞品 {len(competitors)} 个，超过上限 {settings.max_competitors} 个"
            f"（{'、'.join(competitors)}）"
        )
    if dimensions and len(dimensions) > settings.max_dimensions:
        problems.append(
            f"维度 {len(dimensions)} 个，超过上限 {settings.max_dimensions} 个"
            f"（{'、'.join(dimensions)}）"
        )
    if problems:
        raise HTTPException(
            400,
            "；".join(problems)
            + f"。单次任务最多 {settings.max_competitors} 个竞品 × "
            f"{settings.max_dimensions} 个维度，请精简后重试"
            "（竞品过多会让每个竞品分到的资料变少，容易出现整块「数据不足」）。",
        )


def _reject_no_anchor(
    competitors: list[str] | None,
    dimensions: list[str] | None,
) -> None:
    """无锚点校验：竞品和维度**都为**空 → HTTP 400（2026-10-26）。

    【为什么"两个都空"要拒，"只缺一个"不拒】
      classify() 把"缺任一个"一律路由给 supervisor 探索，但两者性质不同：
      — 缺竞品、给了维度（"从定价角度看看这个市场"）→ 有分析角度，能反推该找谁 → 值得探索；
      — 缺维度、给了竞品（"分析下飞书"）→ 有对象，能推断该看什么 → 值得探索；
      — 两个都空（"帮我做个竞品分析"）→ **没有锚点**，ReAct 只能从零猜，
        跑出来往往是一堆不相关竞品，报告**看着完整、内容没用**。
      与 `_reject_over_limit` 同一判据：静默给出一份"能看但不对"的东西，比 400 难发现得多。

    【为什么必须在"抽取之后"调用】
      model_post_init 用 `is not None` 判结构化参数，所以 `competitors=[] + dimensions=[]`
      能通过 Pydantic（被当成"传了"）。若在入口就判，正常 query 模式（两个字段都没传）
      会被直接挡掉，连 LLM 抽取的机会都没有。所以判据要落在"参数定稿之后"——
      此时 competitors/dimensions = 显式参数 ∪ 抽取结果，两个都空才是真的没有锚点。

    【为什么是拒绝而不是交给探索】
      与 `_reject_duplicates` / `_reject_over_limit` 同口径：不静默替用户决定范围。
      提示里给两个正例，让用户知道"至少得给一个"。
    """
    if not competitors and not dimensions:
        raise HTTPException(
            400,
            "未能从描述中识别出竞品或维度，请至少指定其中一个再提交"
            "（例如「对比飞书和钉钉的定价」，或指定维度「从定价角度分析市场」）。",
        )


class TaskRequest(BaseModel):
    """创建任务请求体。

    query 和 (competitors + dimensions) 是互斥的两种模式：
      — 传 competitors + dimensions → Pipeline 模式（确定性分析）
      — 传 query → Supervisor 模式（自然语言入口，由 IntentRouter 决策）
      — 都不传 → 400 错误
    """
    title: str = Field(..., min_length=1, max_length=500, description="任务名称")
    competitors: list[str] | None = Field(None, description="竞品列表")
    dimensions: list[str] | None = Field(None, description="分析维度")
    query: str | None = Field(None, description="自然语言查询（Supervisor 模式入口）")

    def model_post_init(self, _context) -> None:
        """校验：至少要给一个锚点（query / competitors / dimensions 之一）。

        【L4 工程】关键区分：
          — competitors=[] + dimensions=["功能"] → Supervisor 探索（传了但空）
          — competitors=None → 字段未传（跟空列表语义不同）
          — 用 `is not None` 而非 falsy 判断，避免空列表被误判为"未传"

        【2026-10-27 M4 放宽：不再要求 competitors 与 dimensions 同时给】
        原来要求"两个都传"才放行，理由是"缺一个就跑不起来"。加了偏好之后
        这条前提不成立了：用户设过 `default_dimensions` 时，"只给竞品"是
        一种正常请求——缺的那半边由 `_create_task_impl` 的补缺逻辑填。
        真正该守的底线是"得有个锚点"：一个都没有就 400（_reject_no_anchor 也会再挡一次，
        但那是在 LLM 抽取与偏好补缺都试过之后，报错文案更准确）。

        【L4 工程】中英文逗号兼容：
          — 前端 split(/[,，]/) 已做，但后端加防御层
          — 如果前端没拆好（如"王者荣耀，英雄联盟"当成一个元素），后端自动拆
        """
        # ── 防御性拆分：中文逗号 → 英文逗号 ——
        if self.competitors:
            self.competitors = _split_i18n_commas(self.competitors)
        if self.dimensions:
            self.dimensions = _split_i18n_commas(self.dimensions)

        # 【2026-10-27 M4 放宽：不再要求 competitors 与 dimensions 同时给】
        #   原来判"两个都非 None"才放行，理由是缺一个就跑不起来。有了偏好之后
        #   这条前提不成立了：用户设过 default_dimensions 时，"只给竞品"
        #   是正常请求——缺的那半边由 _create_task_impl 的补缺逻辑填。
        #   判据因此改成"**至少给了一个字段**（哪怕是空列表）或给了 query"：
        #   空列表仍然放行，交给 _reject_no_anchor 报"竞品或维度"——
        #   那里的文案比 Pydantic 的 422 更能告诉用户该改什么。
        has_any_field = (self.competitors is not None) or (self.dimensions is not None)
        has_query = bool(self.query and self.query.strip())
        if not has_any_field and not has_query:
            raise ValueError("请提供 competitors / dimensions / query 至少一项")


class ChatRequest(BaseModel):
    """对话入口请求体（2026-10-27）。

    与 TaskRequest 的区别：TaskRequest 承诺"一定产出一份报告"，
    ChatRequest 只承诺"处理这一句话"——可能是建任务、可能是一次查询、
    也可能是一句澄清反问。所以 message 是自由文本，不预设结构。
    """

    message: str = Field(..., min_length=1, max_length=2000, description="一句话，可以是任意意图")
    conversation_id: str | None = Field(
        None,
        description="多轮会话标识；为空则服务端新建一个并在响应的 conversation_id 里回传",
    )


class TaskResponse(BaseModel):
    """任务查询响应。"""
    task_id: str
    status: str
    title: str
    competitors: list[str]
    dimensions: list[str]
    pipeline_mode: str
    created_at: str

    model_config = {"from_attributes": True}


class ReportResponse(BaseModel):
    """报告查询响应。

    is_latest=True 的是最终交付版本（版本号最高的那份）。
    前端可直接 filter(r => r.is_latest) 或取 reports[0]。
    """
    report_id: str
    task_id: str
    content: str | None
    quality_score: float | None
    quality_details: dict | None
    version: int
    is_latest: bool = False
    """最新版本标识。改写循环产生多版本，只有版本号最高的为 True。"""
    created_at: str


class HealthResponse(BaseModel):
    """健康检查响应。"""
    status: str
    db_connected: bool


def _parse_jsonb(value: object) -> dict | None:
    """解析 asyncpg JSONB 字段 — 兼容字符串和 dict 两种返回格式。

    【L4 工程】asyncpg 的 JSONB 行为与版本有关：
    — 旧版（带 codec）返回 dict
    — 新版直接返回 JSON 字符串
    — 如果是 None，返回 None（Pydantic Optional[dict] 接受）
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json
        return json.loads(value)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# §2 应用生命周期
# ═════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理全局共享依赖的生命周期——启动时构建一次，所有任务复用。

    【L4 工程】为什么 build 在 lifespan 而非每次请求？
    ────────────────────────────────────────────────
    之前每次 POST /api/tasks 都会：
      — create_llm_client() × 9（每次建 HTTP Client）
      — build_pipeline_graph() → compile()（遍历图拓扑 + 绑定 checkpointer）
      — PostgresSaver.setup()（CREATE TABLE IF NOT EXISTS，幂等但每次查 PG）
      — A2ARouter.register() × 4（注册 AgentCard + handler）
      — _setup_dependencies() 重建整个依赖树

    根治后：所有一次性构建操作只在 lifespan 启动时执行一次。
    任务创建时只做 graph.ainvoke(initial_state, config)——纯执行，零初始化。

    共享安全：
      — LLM Client (ChatDeepSeek)：无状态 HTTP Client，实例级线程安全
      — MCP Server：只读工具能力，不持有任务状态
      — A2ARouter：注册表是启动时静态快照，运行时只读查询
      — CompiledStateGraph：任务隔离靠 config["thread_id"]，不是图实例
      — LongTermMemoryEngine：user_id 参数化，引擎本身是 DB+LLM 的无状态包装

    【L4 工程】lifespan 替代 @app.on_event("startup")
    — FastAPI 从 0.93 开始推荐 lifespan（支持 async context manager）
    — on_event 是同步的，不支持 async with 语义
    """
    # ═══ startup ═══
    settings = Settings()
    pool = await create_pool(settings)
    app.state.pool = pool
    app.state.settings = settings

    # ── 三层限流组件 ──
    app.state.token_bucket = TokenBucket(
        capacity=settings.token_bucket_capacity,
        refill_rate=10.0,
    )
    app.state.agent_semaphore = AgentSemaphore(max_concurrent=3)
    app.state.llm_limiter = LLMRateLimiter(max_rpm=60)

    # ── 自定义日志器 ──
    app.state.log = BoundLogger(logger)

    # ── MCP Server + 连接池（全局单例）──
    # audit 注入：让 5 个工具（含绕过 call_tool 的 3 个检索工具）的每次调用
    # 都落 agent_logs，action='tool_call' —— 轨迹评估的 L2 数据源。
    from src.mcp import create_mcp_server
    from src.db.dao import AgentLogDAO
    mcp_server = create_mcp_server(settings, audit=AgentLogDAO(pool))
    app.state.mcp_server = mcp_server

    # ── Pipeline 图（预编译，所有 Pipeline 任务复用）──
    from src.pipeline.graph import build_pipeline_graph
    logger.info("正在构建 Pipeline 图（LLM × 4 + PostgresSaver + 记忆引擎）...")
    pipeline_graph = await build_pipeline_graph(mcp_server, pool)
    app.state.pipeline_graph = pipeline_graph
    logger.info("Pipeline 图编译完成 ✅")

    # ── Supervisor 图（预编译，所有 Supervisor 任务复用）──
    from src.supervisor.supervisor import build_supervisor_graph
    from src.supervisor.a2a import A2ARouter, create_agent_cards
    from src.harness import HarnessGuard
    from src.agents import (
        collector_agent, analyzer_agent, writer_agent, quality_agent,
        create_llm_client,
    )

    # A2ARouter（注册 4 个 AgentCard + handler + 专属温度 LLM）
    guard = HarnessGuard(pool)
    a2a_router = A2ARouter(mcp_server, harness=guard)
    cards = create_agent_cards()

    llm_collector = create_llm_client(settings, temperature=0.3)
    llm_analyzer = create_llm_client(settings, temperature=0.1)
    llm_writer = create_llm_client(settings, temperature=0.3)
    llm_quality = create_llm_client(settings, temperature=0.0)

    handlers = {
        "collector": (collector_agent, llm_collector),
        "analyzer": (analyzer_agent, llm_analyzer),
        "writer": (writer_agent, llm_writer),
        "quality": (quality_agent, llm_quality),
    }
    for name, card in cards.items():
        handler, llm = handlers[name]
        a2a_router.register(card, handler, llm)

    llm_supervisor = create_llm_client(settings, temperature=0.3)

    logger.info("正在构建 Supervisor 图（think→act→observe→route 闭环）...")
    supervisor_graph = await build_supervisor_graph(
        mcp_server=mcp_server,
        pool=pool,
        router=a2a_router,
        llm_supervisor=llm_supervisor,
    )
    app.state.supervisor_graph = supervisor_graph
    logger.info("Supervisor 图编译完成 ✅")

    # ── IntentRouter（纯路由决策，不持有依赖）──
    from src.supervisor.router import IntentRouter
    app.state.router = IntentRouter(settings)

    # ── 长期记忆写入口（M3，2026-10-27）──
    # 只建一个实例供**写**用，和 pipeline 图内部那个读实例是两个对象，区别是：
    #   · 这里刻意**不传 conflict_resolver** —— 冲突检测（相似度≥0.85 → LLM 仲裁 →
    #     软删旧记忆）的语义是"新观测取代旧观测"，而 fact 的取代要更严：
    #     必须先同 subject 再比 0.90（见 `semantic.write_task_facts`），
    #     走 resolver 会把"支持多维表格"和"支持自动化流程"判成同一条而误删。
    #   · 读路径继续用 pipeline 图里那个带 resolver 的实例（它只读，resolver 不会触发）。
    from src.db.dao import AgentMemoryDAO as _AgentMemoryDAO
    from src.memory.long_term import LongTermMemoryEngine

    # model_version 传进去，是为了让**写入**打的标签与**检索**的过滤用同一个值：
    # 写的时候标 bge-m3、查的时候不按版本过滤，换模型后旧记忆会静默参与排序。
    app.state.memory_engine = LongTermMemoryEngine(
        llm_analyzer, _AgentMemoryDAO(pool), model_version=settings.embed_model_version
    )
    app.state.memory_llm = llm_analyzer

    logger.info(
        "竞品分析系统启动完成 | DB pool: min=2 max=10 | "
        "Pipeline ✅ | Supervisor ✅ | A2A 4Agent ✅"
    )

    yield

    # ═══ shutdown ═══
    await close_pool()
    logger.info("竞品分析系统已关闭")


# ═════════════════════════════════════════════════════════════════════════════
# §3 FastAPI 应用实例化
# ═════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="竞品分析多Agent协作系统",
    description="AI驱动的竞品分析系统 — Pipeline + Supervisor 双模引擎",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS 中间件（前后端分离 — 允许前端开发服务器跨域）──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 注册认证路由（公开，无需 JWT）──
app.include_router(auth_router)


# ═════════════════════════════════════════════════════════════════════════════
# §4 辅助函数
# ═════════════════════════════════════════════════════════════════════════════

def _get_pool(request: Request) -> Pool:
    """从请求上下文获取数据库连接池。

    Raises:
        HTTPException: 连接池未初始化（启动异常）。
    """
    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(500, "数据库连接池未初始化")
    return pool


# ── LLM 实体提取：从自然语言 query 提取 competitors + dimensions ──────────
# ★ 2026-10-27（D1）：prompt 与解析器改由 intent_funnel 提供，两个入口共用。
#   两份 prompt 必然漂移，同一个 query 在 /api/tasks 与 /api/chat 会抽出不同实体。
#   /api/tasks 只关心实体，所以只取 competitors/dimensions，忽略 intent/keyword。

async def _extract_entities_from_query(query: str, request: Request) -> dict:
    """用 LLM 从自然语言 query 中提取竞品名和维度。

    2026-07-29 修复：这里原先只有"交给 IntentRouter 的 LLM 实体提取"的注释、
    没有实现，导致纯 query 请求抽出空实体、被路由进 supervisor 空转。

    Returns:
        {"competitors": [str], "dimensions": [str]}；失败时两项皆空。
    """
    from src.agents import create_llm_client
    from src.supervisor.intent_funnel import (
        INTENT_PROMPT,
        normalize_entities,
        parse_intent_json,
    )

    try:
        settings = request.app.state.settings
        llm = create_llm_client(settings, temperature=0.0)
        resp = await llm.ainvoke(INTENT_PROMPT % query)
        data = parse_intent_json((resp.content or "").strip())
        competitors, dimensions = normalize_entities(data)

        logger.info(
            "LLM 实体提取: query=%r → competitors=%s, dimensions=%s",
            query[:60], competitors, dimensions,
        )
        return {"competitors": competitors, "dimensions": dimensions}

    except Exception:
        logger.exception("LLM 实体提取失败，将使用默认值")
        return {"competitors": [], "dimensions": []}


def _make_task_dict(
    task_id: UUID,
    request_body: TaskRequest,
    user_id: str,
) -> dict:
    """将 Pydantic 请求体转换为 task dict（兼容现有模块的 dict 接口）。

    user_id 由 JWT 鉴权注入，不再硬编码 "default"。
    """
    return {
        "id": str(task_id),
        "title": request_body.title,
        "competitors": request_body.competitors or [],
        "dimensions": request_body.dimensions or [],
        "user_id": user_id,
    }


async def _execute_task(
    task_id: UUID,
    task: dict,
    llm_parsed: dict,
    request: Request,
) -> None:
    """后台执行任务——从 app.state 复用预编译图，零初始化。

    【L4 工程】根治架构债务：
    之前每次都调 router.route() → _setup_dependencies() → build_*_graph()
    → 每任务创建 9 个 LLM Client + 编译 2 张 StateGraph。
    根治后：所有一次性构建在 lifespan 启动时完成，任务创建时
    直接从 app.state 拿预编译图 → graph.ainvoke()，纯执行。

    步骤：
    1. 更新任务状态 → running
    2. IntentRouter.classify() → 纯函数路由决策（零 LLM，0ms）
    3. 从 app.state 拿对应预编译图 → graph.ainvoke()
    4. 更新任务状态 → completed / failed
    5. 记录 Prometheus 指标

    【2026-10-15】这里不再自动触发评估。评估是上线前的回归测试
    （CLI / Golden 回归显式触发），不是生产运行时的一环。
    """
    pool = _get_pool(request)
    task_id_str = str(task_id)
    started_at = time.monotonic()

    # ── 路由决策（纯函数，零 LLM 调用）──
    from src.supervisor.router import IntentRouter
    route_type = IntentRouter.classify(llm_parsed)

    # ── 丰富 task ──
    enriched_task = {
        "id": task.get("id", task_id_str),
        "title": task.get("title", "竞品分析"),
        "user_id": task.get("user_id", "default"),
        "competitors": llm_parsed.get("competitors", []),
        "dimensions": llm_parsed.get("dimensions", []),
    }

    try:
        # ── 1. 标记 running + 回写实际路由模式 ──
        task_dao = TaskDAO(pool)
        await task_dao.update_status(task_id_str, "running")
        await task_dao.update_pipeline_mode(task_id_str, route_type)
        record_task_started()

        # ── 2. 从 app.state 拿预编译图 → 直接执行 ──
        if route_type == "pipeline":
            graph = request.app.state.pipeline_graph
            initial_state = {
                "task_id": enriched_task["id"],
                "title": enriched_task["title"],
                "user_id": enriched_task["user_id"],
                "competitors": enriched_task["competitors"],
                "dimensions": enriched_task["dimensions"],
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
            config = {"configurable": {"thread_id": f"pipeline-{enriched_task['id']}"}}
            final_state = await graph.ainvoke(initial_state, config)
            result = {
                "task_id": enriched_task["id"],
                "final_report": final_state.get("final_report", ""),
                "quality_score": final_state.get("quality_score", 0.0),
            }
        else:
            graph = request.app.state.supervisor_graph
            initial_state = {
                # §1 任务元信息
                "task_id": enriched_task["id"],
                "title": enriched_task["title"],
                "user_id": enriched_task["user_id"],
                "user_query": enriched_task["title"],
                # §2 探索结果（初始为空，ReAct 循环动态填充）
                "found_competitors": enriched_task["competitors"],
                "dimensions": enriched_task["dimensions"],
                "collected_data": {},
                "analysis_results": {},
                "report_content": "",
                # §3 质量（初始为 0/空）
                "quality_score": 0.0,
                "quality_passed": False,
                "quality_details": {},
                "rewrite_suggestions": [],
                # §4 控制
                "current_round": 1,
                "max_rounds": 10,
                "rewrite_count": 0,
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
            config = {"configurable": {"thread_id": f"supervisor-{enriched_task['id']}"}}
            final_state = await graph.ainvoke(initial_state, config)
            result = {
                "task_id": enriched_task["id"],
                "final_output": final_state.get("final_output", ""),
                "quality_score": final_state.get("quality_score", 0.0),
                "is_complete": final_state.get("is_complete", False),
            }

        # ── 3. 检查执行结果 ──
        # Pipeline 失败信号: final_report 为空（节点异常被 catch 后不赋值）
        # Supervisor 失败信号: !is_complete（ReAct 未正常终止，如 LLM 反复失败）
        pipeline_failed = (
            route_type == "pipeline"
            and not final_state.get("final_report", "")
        )
        supervisor_failed = (
            route_type == "supervisor"
            and not final_state.get("is_complete", False)
        )
        if pipeline_failed or supervisor_failed:
            await task_dao.update_status(task_id_str, "failed")
            elapsed = time.monotonic() - started_at
            record_task_failed()
            request.app.state.log.bind(
                task_id=task_id_str[:8]
            ).error("任务执行失败", extra={
                "route": route_type,
                "elapsed_ms": int(elapsed * 1000),
            })
            return

        # ── 4. 标记 completed ──
        await task_dao.update_status(task_id_str, "completed")
        elapsed = time.monotonic() - started_at
        record_task_completed(route_type, elapsed)

        request.app.state.log.bind(
            task_id=task_id_str[:8]
        ).info("任务完成", extra={
            "route": route_type,
            "elapsed_ms": int(elapsed * 1000),
        })

        # ── 4.6 从证据链提事实（M3，2026-10-27）──
        # 【为什么放在 completed 之后】记忆是任务完成后的附加动作：写失败只影响
        #   "下次核对少一条依据"，绝不能把已完成的任务改成 failed。
        # 【为什么只写 fact、不写 episode】一次任务的经过（分析了谁、什么时候、
        #   出了哪份报告、质量多少分）已经由 tasks + reports 承载——那才是情景记忆
        #   该在的地方，因为它要的是**结构化查询**（按用户/时间/竞品精确查），
        #   不是向量召回。再往 agent_memories 写一条摘要，是同一份事实的第二副本，
        #   且重跑走 DO NOTHING 不更新，会与 reports 的新版本静默分叉。
        # 【为什么事实来自 evidence_map 而不是报告正文】正文里的句子没有来源 URL；
        #   把"模型说过的话"升格成"观测到的事实"，之后就再也查不回来了。
        memory_engine = getattr(request.app.state, "memory_engine", None)
        if memory_engine is not None:
            try:
                from src.memory.semantic import write_task_facts

                await write_task_facts(
                    engine=memory_engine,
                    pool=pool,
                    task=enriched_task,
                    settings=request.app.state.settings,
                )
            except Exception:  # noqa: BLE001
                logger.warning("fact 写入异常（已忽略） task=%s", task_id_str[:8],
                               exc_info=True)

        # ── 5. 评估不再在此触发 ──
        # 【2026-10-15 设计修正】此前任务 completed 后会 fire-and-forget 自动跑
        # RAGAS/Judge 评估。该设计已移除：评估是**上线前的回归测试**，不是生产
        # 运行时的一环——真实用户任务没有标准答案，对它打分既无法判定本次交付
        # 好坏、也不指导用户决策，只是给每个生产任务平白加一笔 LLM 裁判开销，
        # 还会把两种口径的分数混进同一条 Grafana 趋势曲线。
        # 评估改由发版前显式触发（`python -m src.evaluation.ragas_core --golden`），
        # 生产链路零评估开销。

    except Exception as e:
        # ── 标记 failed ──
        try:
            task_dao = TaskDAO(pool)
            await task_dao.update_status(task_id_str, "failed")
        except Exception as db_err:
            logger.exception(
                "标记失败状态时 DB 写入也失败了 task_id=%s", task_id_str[:8],
                exc_info=True,
            )

        record_task_failed()
        elapsed = time.monotonic() - started_at
        logger.exception("任务失败 task_id=%s elapsed=%.0fms",
                          task_id_str[:8], elapsed * 1000)


# ═════════════════════════════════════════════════════════════════════════════
# §5 路由 — POST /api/tasks
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/api/tasks", status_code=202)
async def create_task(
    body: TaskRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> JSONResponse:
    """建任务入口 —— 只做限流，逻辑在 _create_task_impl。

    ★ 限流为什么留在这里、不进 impl（2026-10-27）：
      /api/chat 判出 analysis 后直接调 impl，而它自己已经 `tb.acquire()` 过一次。
      若 impl 也扣令牌，对话路径就**双扣**：高并发下走对话的用户先撞 429、
      走表单的不会。同一条链路两种限流待遇，是最难查的那类 bug。
    """
    tb = request.app.state.token_bucket
    if tb is not None and not await tb.acquire():
        record_rate_limit_hit("global")
        raise HTTPException(429, "请求过多，请稍后重试")
    return await _create_task_impl(body, request, current_user)


async def _create_task_impl(
    body: TaskRequest,
    request: Request,
    current_user: CurrentUser,
    prefill: dict | None = None,
) -> JSONResponse:
    """真正建任务：校验 → 实体定稿 → 写库 → 后台起任务。

    【L4 工程】返回 202 Accepted 而非 200：任务在后台异步跑，
      客户端拿 task_id 后走 GET /api/tasks/{id} 或 SSE 跟进。

    【L5 决策】校验顺序：参数校验 → 400 / 写库 → 202 / create_task() → 后台执行。

    prefill: 调用方已用 LLM 抽好的实体（/api/chat 的 L3 结果）。
      非空 ⇒ 不再抽第二次，只拿它补空缺字段。
      ⚠️ 缺了这一步，同一句话在对话入口要付两次抽取：L3 一次、
         "query 缺字段"分支再一次，还多烧 300~800ms。
    """
    # ── Layer 2: 参数校验 ──
    # Pydantic model_post_init 已保证 competitors+dimensions 或 query 至少传一组；
    # 这里追加**业务级**校验：
    #   ① 同任务内重复的竞品/维度直接 400（避免重复跑 + 静默覆盖）；
    #   ② 规模上限（2026-10-21）：超过 4×4 直接 400，不做静默截断。
    _reject_duplicates(body.competitors, body.dimensions)
    _reject_over_limit(body.competitors, body.dimensions, request.app.state.settings)

    # ── 构造 llm_parsed ──
    # 如果传了 competitors + dimensions → intent 明确
    # 如果只传了 query → LLM 从自然语言中提取实体
    llm_parsed = {
        "competitors": body.competitors or [],
        "dimensions": body.dimensions or [],
        "intent_is_clear": bool(body.competitors and body.dimensions),
    }
    if body.query:
        llm_parsed["query"] = body.query

    # ── 实体定稿：有 prefill 就用它补缺，否则才调 LLM 抽取 ──
    if prefill is not None:
        # chat 路径：L3 已经抽过，这里绝不再抽（也绝不用空值覆盖已有值）
        if not llm_parsed["competitors"]:
            llm_parsed["competitors"] = _dedupe(list(prefill.get("competitors") or []))
        if not llm_parsed["dimensions"]:
            llm_parsed["dimensions"] = _dedupe(list(prefill.get("dimensions") or []))
        llm_parsed["intent_is_clear"] = bool(
            llm_parsed["competitors"] and llm_parsed["dimensions"]
        )
        # prefill 也可能一次带来超上限的实体（"对比 A B C D E F G"），同样 400 不截断
        _reject_over_limit(
            llm_parsed["competitors"], llm_parsed["dimensions"],
            request.app.state.settings,
        )
    # 【2026-07-29 BUG修复】这段原先的注释写"交给 IntentRouter 的 LLM 实体提取"，
    # 但代码从没实现，导致 "对比王者荣耀和英雄联盟…" → 实体全空 → supervisor 空转。
    elif body.query and (not llm_parsed["competitors"] or not llm_parsed["dimensions"]):
        extracted = await _extract_entities_from_query(body.query, request)
        # 去重：LLM 抽取可能重复输出同一实体，静默归一，不让用户背锅
        if extracted["competitors"]:
            llm_parsed["competitors"] = _dedupe(extracted["competitors"])
        if extracted["dimensions"]:
            llm_parsed["dimensions"] = _dedupe(extracted["dimensions"])
        # 重新评估 intent_is_clear（提取后有值时可能改变）
        llm_parsed["intent_is_clear"] = bool(
            llm_parsed["competitors"] and llm_parsed["dimensions"]
        )
        # 规模上限：LLM 从一句话里可能抽出远超上限的实体（"对比 A B C D E F G"）。
        # 这里同样走 400 而不是截断——用户并不知道自己"说了 7 个"，
        # 静默只取前 4 个会让他以为 7 家都分析了。提示里带上限和实际数量，他才好改口径。
        _reject_over_limit(
            llm_parsed["competitors"], llm_parsed["dimensions"],
            request.app.state.settings,
        )

    # ── 偏好补缺（2026-10-27 M4：默认值不用每次说）──
    # 【位置很关键】必须在实体定稿之后、无锚点校验之前：
    #   定稿之后才知道用户真正缺什么（补的是空缺，不是覆盖用户明说的值）；
    #   校验之前补，才能让"只传了竞品、维度用默认值"的请求真的跑起来，
    #   而不是被 _reject_no_anchor 挡成 400。
    # ⚠️ 只吃 user_set / confirmed（DAO 在 SQL 层已过滤 proposed）——
    #    LLM 提取的提案在用户点确认前不许生效。
    if request.app.state.settings.memory_preferences_enabled and not (
        llm_parsed["competitors"] and llm_parsed["dimensions"]
    ):
        from src.memory.preferences import for_injection, prefill_for_task

        try:
            effective = await PreferenceDAO(_get_pool(request)).get_effective(
                current_user.user_id
            )
            usable, _suppressed = for_injection(effective)
            fill = prefill_for_task(usable)
            if not llm_parsed["competitors"] and fill.get("competitors"):
                llm_parsed["competitors"] = _dedupe(fill["competitors"])
            if not llm_parsed["dimensions"] and fill.get("dimensions"):
                llm_parsed["dimensions"] = _dedupe(fill["dimensions"])
            llm_parsed["intent_is_clear"] = bool(
                llm_parsed["competitors"] and llm_parsed["dimensions"]
            )
        except Exception:  # noqa: BLE001 — 偏好读失败最多是"回到没有默认值"，不该 500
            logger.warning("偏好补缺失败（忽略） user=%s", current_user.user_id,
                           exc_info=True)

    # ── 无锚点校验（2026-10-26）──
    # 此时 competitors/dimensions = 显式参数 ∪ LLM 抽取结果，是两条路径的定稿值。
    # 放在这里而不是入口：入口处 query 模式的两个字段还是空的，会被误挡。
    _reject_no_anchor(llm_parsed["competitors"], llm_parsed["dimensions"])

    # ── 写入 DB ──
    pool = _get_pool(request)
    task_dao = TaskDAO(pool)
    task_id = UUID(uuid4().hex)
    task = _make_task_dict(task_id, body, current_user.user_id)

    # ★ 写库用**定稿后**的实体，不是 body 原值（2026-10-27 修）。
    #   query 模式下 body.competitors 是 None，实体全是 LLM 抽的；
    #   以前这里写的是 body 副本 → tasks.competitors 恒为 [] →
    #   lookup 的"按竞品筛选"永远查不到（实测 huayue 30 个历史任务全是空数组）。
    #   执行侧不受影响（_execute_task 一直读 llm_parsed），
    #   所以这个 bug **只脏查询层**：报告是对的，查不回来而已。
    await task_dao.create(
        task_id=str(task_id),
        user_id=current_user.user_id,
        title=body.title,
        competitors=llm_parsed["competitors"],
        dimensions=llm_parsed["dimensions"],
        pipeline_mode="pipeline",  # 初始值，IntentRouter 可能改为 supervisor
    )
    record_task_created()

    # ── 后台异步执行 ──
    asyncio.create_task(_execute_task(task_id, task, llm_parsed, request))

    return JSONResponse(
        status_code=202,
        content={"task_id": str(task_id), "status": "pending"},
    )


# ═════════════════════════════════════════════════════════════════════════════
# §5.5 路由 — POST /api/chat（意图识别漏斗入口，2026-10-27）
# ═════════════════════════════════════════════════════════════════════════════

def _title_from_message(message: str, limit: int = 60) -> str:
    """从一句话里裁出任务标题。

    ⚠️ 只做截断不做摘要——标题是给列表看的，再调一次 LLM 起标题
    等于为一行文字付一次调用费。
    """
    text = " ".join((message or "").split())
    return text[:limit] or "竞品分析"


@app.post("/api/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> JSONResponse:
    """对话入口：一句话进来，按漏斗判意图后分派到四条链路。

    【与 POST /api/tasks 的分工】
      /api/tasks 只承诺"建一个分析任务"，参数不合规就 400；
      /api/chat 承诺"处理这句话"，所以它能把"查报告""查证据"这类
      **不产出报告**的请求也接住 —— 这是加对话入口的全部理由。

    【返回形态（前端按 type 分支渲染）】
      type=task     202  {task_id, entities}      → 跳任务详情页
      type=answer   200  {intent, summary, items} → 直接渲染气泡
      type=clarify  200  {question, options}      → 渲染四个选项按钮
      三者都带 conversation_id —— 前端存下来，下一轮带回即是多轮上下文。

    【会话记忆（M1）】
      写：每轮落两行（user + assistant），超过 6 轮后按 3 轮间隔刷新会话摘要；
      读：最近 6 行原文 + 摘要 → 只喂 L3（见 intent_funnel.l3_llm 注释）。
      写失败只记日志，不让对话 500。

    【L5 决策】analysis 直接调用 create_task()，不复制一份建任务逻辑：
    限流、去重、规模上限、无锚点校验、写库、后台起任务全在那一条链路里，
    复制一份 = 以后每改一次校验都要改两处（必漏）。
    """
    import json as _json

    tb = request.app.state.token_bucket
    if tb is not None and not await tb.acquire():
        record_rate_limit_hit("global")
        raise HTTPException(429, "请求过多，请稍后重试")

    settings = request.app.state.settings
    from src.supervisor.conversation import (
        ensure_conversation,
        format_history_block,
        load_context,
        remember_turn,
    )
    from src.supervisor.intent_funnel import (
        CLARIFY_CHOICES,
        FUNNEL_OFF_QUESTION,
        NO_ANCHOR_CHOICES,
        NO_ANCHOR_QUESTION,
        run_funnel,
    )
    from src.supervisor.query_handlers import (
        check_freshness,
        lookup_reports,
        trace_evidence,
    )

    # ── 会话记忆：先拿到会话，再把历史拼给 L3 ──
    #   没给 conversation_id → 新建；给了但查不到/不属于自己 → 统一 404
    #   （不区分"不存在"与"没有权限"，否则等于告诉别人"这个 id 是真的"）
    pool = _get_pool(request)
    conversation = await ensure_conversation(
        pool, current_user.user_id, body.conversation_id,
        title=_title_from_message(body.message),
    )
    if conversation is None:
        raise HTTPException(404, "会话不存在")

    context = await load_context(pool, conversation)
    history_block = format_history_block(context["summary"], context["turns"])

    funnel = await run_funnel(
        query=body.message, settings=settings, history_block=history_block
    )
    base = {"intent": funnel.intent, "layer": funnel.layer, "confidence": funnel.confidence}

    # ── 偏好提案识别（M4，2026-10-27）──
    # 【为什么放在漏斗之后而不是之前】一句话可能同时是命令与规则
    #   （"以后默认用功能和定价，先帮我分析飞书"）：判成 analysis 也不该丢掉提案。
    # 【为什么先过关键词门】判"这句话有没有在说偏好"本身就是分类任务，
    #   字符串命中能挡掉绝大多数轮次，剩下的才花一次 LLM（见 preferences.has_cue）。
    # 【为什么写进去的只是 proposed】LLM 可以提取，但不能替用户拍板；
    #   转正要等前端确认卡被点（POST /api/preferences/{key}/confirm）。
    pending_prefs: list[dict] = []
    if settings.memory_preferences_enabled:
        from src.memory.preferences import detect_proposals, pending_cards

        pref_dao = PreferenceDAO(pool)
        try:
            proposals = await detect_proposals(
                body.message, getattr(request.app.state, "memory_llm", None), settings
            )
            for p in proposals:
                await pref_dao.upsert(
                    current_user.user_id, p["key"], p["value"],
                    source="proposed", confidence=p["confidence"],
                )
            if proposals:
                # 提案落库后重新取一遍：确认卡要包含**之前没确认的**那些，
                # 不能只显示本轮新提的（否则上次的提案永远等不到确认）
                pending_prefs = pending_cards(await pref_dao.list(current_user.user_id))
        except Exception:  # noqa: BLE001 — 提案识别失败不影响这句对话
            logger.warning("偏好提案处理失败（忽略） user=%s", current_user.user_id,
                           exc_info=True)

    async def finish(
        status_code: int,
        content: dict,
        reply_text: str,
        ref_task_id: str | None = None,
    ) -> JSONResponse:
        """落两行会话流水后返回 —— 四个分支都走这里，避免漏写某一路。

        ⚠️ 落库在**返回之前**做、且失败只记日志（见 remember_turn），
           顺序反了的话响应一旦发出，进程被杀就丢这一轮。
        """
        await remember_turn(
            pool, conversation,
            user_message=body.message,
            intent=funnel.intent,
            reply_text=reply_text,
            ref_task_id=ref_task_id,
            settings=settings,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                **content,
                "conversation_id": conversation["id"],
                # 三个分支统一带：前端不必为每种 type 各写一遍确认卡逻辑。
                # 空列表也要带上（区分"没有提案"与"字段缺失"）。
                "pending_preferences": pending_prefs,
            },
        )

    # ── L4 澄清：不入库、不建任务，只回一句反问 ──
    if funnel.intent == "unknown":
        question = funnel.clarify or "能再说具体一点吗？"
        return await finish(200, {
            "type": "clarify",
            **base,
            "question": question,
            "options": list(CLARIFY_CHOICES),
            "echo": body.message,
        }, f"（反问）{question}")

    # ── analysis：复用 /api/tasks 的整条链路 ──
    if funnel.intent == "analysis":
        entities = funnel.entities or {}
        # LLM 可能重复输出同一实体，先归一 —— 否则 _reject_duplicates 会把它当用户输入报 400
        competitors = _dedupe(list(entities.get("competitors") or []))
        dimensions = _dedupe(list(entities.get("dimensions") or []))

        # ★ 判出"要分析"但一个锚点都没有：对话里该问一句，不该弹表单式 400。
        #   与上面的 L4 澄清不是一回事 —— 这里知道要什么，只是缺参数，文案与选项都不同。
        if not competitors and not dimensions:
            question = (
                FUNNEL_OFF_QUESTION if funnel.layer == "off" else NO_ANCHOR_QUESTION
            )
            return await finish(200, {
                "type": "clarify",
                **base,
                "question": question,
                "options": list(NO_ANCHOR_CHOICES),
                "echo": body.message,
            }, f"（反问）{question}")

        task_body = TaskRequest(
            title=_title_from_message(body.message),
            competitors=competitors or None,
            dimensions=dimensions or None,
            query=body.message,
        )
        # ★ 带 prefill 进 impl：L3 已经付过的那次抽取不许再付一次
        resp = await _create_task_impl(
            task_body, request, current_user,
            prefill={"competitors": competitors, "dimensions": dimensions},
        )
        payload = _json.loads(resp.body.decode())
        task_id = payload.get("task_id")
        return await finish(202, {
            "type": "task",
            **base,
            "task_id": task_id,
            "entities": {"competitors": competitors, "dimensions": dimensions},
            "echo": body.message,
        }, f"已开始分析：{'、'.join(competitors) or '（未指定竞品）'}", ref_task_id=task_id)

    # ── lookup / trace / verify：零 LLM 的 SQL 执行器 ──
    competitors = funnel.entities.get("competitors") or None

    if funnel.intent == "lookup":
        # dimensions 一路传下去：之前这里只给小了 competitors，"定价"这个词
        # 在调用处就丢了，SQL 再准也只剩「钉钉」这一个条件。
        data = await lookup_reports(
            pool,
            current_user.user_id,
            competitors,
            funnel.entities.get("dimensions"),
        )
    elif funnel.intent == "trace":
        data = await trace_evidence(pool, current_user.user_id, competitors, funnel.keyword)
    else:
        data = await check_freshness(pool, current_user.user_id, competitors, funnel.keyword)

    return await finish(200, {
        "type": "answer",
        **base,
        "summary": data["summary"],
        "items": data["items"],
        "echo": body.message,
    }, data["summary"])


# ═════════════════════════════════════════════════════════════════════════════
# §5.6 路由 — /api/preferences（程序性记忆，2026-10-27 M4）
# ═════════════════════════════════════════════════════════════════════════════

class PreferenceBody(BaseModel):
    """设置一条偏好。

    `source` 只开放 user_set / confirmed 两个值：proposed 是**内部状态**，
    由 LLM 提案那条链路写，客户端不该能直接把一条偏好标成"提案"——
    否则它会被当成"用户没确认过的东西"永久搁置。
    """
    value: Any = Field(..., description="偏好值，形状由 key 决定")
    source: str = Field("user_set", description="user_set | confirmed")


@app.get("/api/preferences")
async def list_preferences(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """列出该用户的全部偏好，并标出哪些**此刻真的会生效**。

    【为什么返回两个列表而不是一个】
    存的 ≠ 生效的。一条 `report_style=只要一行` 存得进去，但与 system 的
    "必须有总览表格 + 逐维度分析"冲突，注入时会被压制。
    前端要能显示"已保存，但不会生效（原因）"——静默忽略等于骗用户设置成功了。
    """
    from src.memory.preferences import for_injection

    dao = PreferenceDAO(_get_pool(request))
    rows = await dao.list(current_user.user_id)
    effective = await dao.get_effective(current_user.user_id)
    usable, suppressed = for_injection(effective)
    reasons = dict(suppressed)
    items = []
    for row in rows:
        value = row["value"]
        if isinstance(value, (str, bytes)):
            try:
                value = _parse_jsonb(value)
            except Exception:  # noqa: BLE001
                pass
        items.append({
            "key": row["key"],
            "value": value,
            "source": row["source"],
            "confidence": row["confidence"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "will_apply": row["key"] in usable,
            "suppressed_reason": reasons.get(row["key"]),
        })
    return {"items": items, "effective": usable}


@app.put("/api/preferences/{key}")
async def put_preference(
    key: str,
    body: PreferenceBody,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """写入一条偏好（UI 显式设置 → 直接生效）。

    【两层校验，各管一件事】
      这里做**结构校验**（类型/形状/条数）→ 不合法 400，当场告诉用户哪写错了；
      **语义压制**放在注入侧（`preferences.for_injection`）→ 因为"与 system 硬规则
      冲突"要看硬规则，而硬规则在 writer prompt 里，写入侧拿不到。
    返回值里的 `will_apply=false` 就是"存下了，但这次不会生效"的诚实回执。
    """
    from src.memory.preferences import for_injection, validate

    if body.source not in ("user_set", "confirmed"):
        raise HTTPException(400, "source 只能是 user_set 或 confirmed")
    reason = validate(key, body.value)
    if reason:
        raise HTTPException(400, reason)

    dao = PreferenceDAO(_get_pool(request))
    await dao.upsert(
        current_user.user_id, key, body.value,
        source=body.source, confidence=1.0 if body.source == "user_set" else 0.9,
    )
    usable, suppressed = for_injection({key: body.value})
    suppressed_reason = dict(suppressed).get(key)
    return {
        "key": key,
        "source": body.source,
        "will_apply": key in usable,
        "reason": suppressed_reason,
    }


@app.post("/api/preferences/{key}/confirm")
async def confirm_preference(
    key: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """把一条 proposed 转正 —— 确认卡的"以后都这样"按钮。"""
    dao = PreferenceDAO(_get_pool(request))
    if not await dao.confirm(current_user.user_id, key):
        raise HTTPException(404, "没有待确认的偏好")
    effective = await dao.get_effective(current_user.user_id)
    from src.memory.preferences import for_injection

    usable, _ = for_injection(effective)
    return {"key": key, "source": "confirmed", "will_apply": key in usable}


@app.delete("/api/preferences/{key}")
async def delete_preference(
    key: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删掉一条偏好（含"忽略"提案）。"""
    dao = PreferenceDAO(_get_pool(request))
    return {"key": key, "deleted": await dao.delete(current_user.user_id, key)}


# ═════════════════════════════════════════════════════════════════════════════
# §5.5 路由 — 会话列表与回放（GET /api/conversations…）
# ═════════════════════════════════════════════════════════════════════════════
#
# 【为什么这组接口迟到了】
#   写入侧（POST /api/chat）一直在往 conversations / conversation_turns 里落，
#   conversations.title 的字段注释甚至写着「首句话裁出来的标题，仅给会话列表看」——
#   为列表准备好了数据，却没有任何接口把它读出来。
#   结果是：点「新对话」把本地指针清成 null 后，那段对话就再也找不回来，
#   而库里一条没少。**丢的是入口，不是数据。**
#
# 【为什么回放不做分页】
#   单个会话的轮数天然有上限（摘要机制就是为了不让它无限长），
#   200 行的兜底已经远超实际；加分页会让前端多一层状态，
#   而这次要解决的只是"找不回来"。

@app.get("/api/conversations")
async def list_conversations(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    limit: int = 20,
) -> dict:
    """列出该用户的历史会话（最近活跃的在前），供前端「历史对话」用。"""
    limit = max(1, min(int(limit), 100))
    dao = ConversationDAO(_get_pool(request))
    rows = await dao.list_for_user(current_user.user_id, limit=limit)

    items = []
    for r in rows:
        # 副标题取最后一句用户原话：标题是首句裁的，聊了七八轮之后
        # 首句和实际主题常常已经不是一回事了。
        last = " ".join((r.get("last_user_message") or "").split())
        items.append({
            "id": r["id"],
            "title": r["title"] or "（未命名会话）",
            "turn_count": r["turn_count"],
            "last_user_message": last[:120],
            "has_summary": bool(r.get("summary")),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return {"items": items, "count": len(items)}


@app.get("/api/conversations/{conversation_id}/turns")
async def get_conversation_turns(
    conversation_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    limit: int = 200,
) -> dict:
    """回放一个会话的完整流水（seq 正序），让前端把气泡重铺出来。

    【为什么 404 要靠 get() 判，不能靠「turns 为空」判】
      新建但还没说过话的会话，turns 也是空。拿空列表当 404，
      会把「空会话」误报成「不存在」—— 用户刚点开就被踢回列表，莫名其妙。
      所以先 get() 拿归属，再取流水。

    【回放拿不到 card，只能拿到文本】
      turns 里存的是当轮 assistant 的 reply_text，卡片的结构化结果没存。
      这是刻意的：卡片是「那次任务的产物」，产物在 tasks / reports 里，
      靠 ref_task_id 跳详情即可。再存一份等于给同一件事造第二个真相
      （与 episode 那次同构的坑）。
    """
    limit = max(1, min(int(limit), 500))
    pool = _get_pool(request)
    dao = ConversationDAO(pool)

    conv = await dao.get(conversation_id, current_user.user_id)
    if conv is None:
        # 与 /api/chat 一致：不区分「不存在」与「不是你的」，避免探测别人的会话
        raise HTTPException(404, "会话不存在")

    turns = await dao.all_turns(conversation_id, current_user.user_id, limit=limit)
    return {
        "conversation": {
            "id": conv["id"],
            "title": conv["title"] or "（未命名会话）",
            "summary": conv["summary"],
            "turn_count": conv["turn_count"],
            "updated_at": conv["updated_at"].isoformat() if conv["updated_at"] else None,
        },
        "items": [
            {
                "seq": t["seq"],
                "role": t["role"],
                "content": t["content"],
                "intent": t["intent"],
                "ref_task_id": t["ref_task_id"],
                "ref_report_id": t["ref_report_id"],
                "created_at": t["created_at"].isoformat() if t["created_at"] else None,
            }
            for t in turns
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
# §6 路由 — GET /api/tasks/{task_id}
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/tasks/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> TaskResponse:
    """查询任务状态。

    Returns:
        TaskResponse: 包含 status / competitors / dimensions / pipeline_mode 等
    Raises:
        404: 任务不存在
    """
    pool = _get_pool(request)
    task_dao = TaskDAO(pool)

    task = await task_dao.get(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")

    return TaskResponse(
        task_id=str(task["id"]),
        status=task["status"],
        title=task["title"],
        competitors=task.get("competitors", []),
        dimensions=task.get("dimensions", []),
        pipeline_mode=task.get("pipeline_mode", "pipeline"),
        created_at=str(task.get("created_at", "")),
    )


# ═════════════════════════════════════════════════════════════════════════════
# §7 路由 — GET /api/tasks/{task_id}/stream (SSE)
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/tasks/{task_id}/stream")
async def stream_task(
    task_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """SSE 进度推送。

    事件类型：
      — progress: {"agent": "collector", "message": "...", "progress_pct": 0.25}
      — complete: {"task_id": "...", "status": "completed", "report_id": "...", "quality_score": 82}
      — error: {"task_id": "...", "message": "..."}

    轮询：每秒从 agent_logs 表读取新增日志
    超时：600 秒后强制发送 error 事件
    断连：客户端断开时自动清理（不中断后台任务）
    """
    pool = _get_pool(request)

    # 验证任务存在
    task_dao = TaskDAO(pool)
    task = await task_dao.get(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")

    return EventSourceResponse(
        event_generator(task_id, pool),
    )


# ═════════════════════════════════════════════════════════════════════════════
# §8 路由 — GET /api/tasks/{task_id}/reports
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/tasks/{task_id}/reports")
async def get_reports(
    task_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ReportResponse]:
    """获取任务报告列表（按版本降序）。

    Returns:
        ReportResponse 数组 — 最新版本排在最前
    Raises:
        404: 任务不存在或无报告
    """
    pool = _get_pool(request)
    report_dao = ReportDAO(pool)

    reports = await report_dao.get_all_versions(task_id)
    if not reports:
        raise HTTPException(404, f"任务无报告: {task_id}")

    max_version = max((r.get("version", 1) for r in reports), default=1)
    return [
        ReportResponse(
            report_id=str(r["id"]),
            task_id=str(r["task_id"]),
            content=r.get("content"),
            quality_score=(
                float(r["quality_score"])
                if r.get("quality_score") is not None
                else None
            ),
            quality_details=_parse_jsonb(r.get("quality_details")),
            version=r.get("version", 1),
            is_latest=r.get("version", 1) == max_version,
            created_at=str(r.get("created_at", "")),
        )
        for r in reports
    ]


# ═════════════════════════════════════════════════════════════════════════════
# §9 路由 — GET /health
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check(request: Request) -> HealthResponse:
    """健康检查——返回服务状态 + 数据库连通性。

    Kubernetes livenessProbe / readinessProbe 直接打这个端点。
    """
    pool = _get_pool(request)
    db_ok = False

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 AS ok")
            db_ok = bool(row and row["ok"] == 1)
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning("健康检查 DB 连接失败: %s", e)
        db_ok = False

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db_connected=db_ok,
    )


# ═════════════════════════════════════════════════════════════════════════════
# §10 路由 — GET /metrics
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/metrics")
async def metrics():
    """Prometheus 指标暴露端点。

    返回 text/plain 格式的 Prometheus 指标数据。
    Kubernetes ServiceMonitor 或 Prometheus scrape 配置直接打这个端点。
    """
    return Response(
        content=generate_latest(),
        media_type="text/plain; charset=utf-8",
    )
