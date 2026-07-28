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
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.config import Settings
from src.db.connection import close_pool, create_pool
from src.db.dao import AgentLogDAO, ReportDAO, TaskDAO
from src.observability.logging import BoundLogger
from src.observability.metrics import (
    generate_latest,
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
        """校验：competitors+dimensions 和 query 至少传一组。

        【L4 工程】关键区分：
          — competitors=[] + dimensions=["功能"] → Supervisor 探索（传了但空）
          — competitors=None → 字段未传（跟空列表语义不同）
          — 用 `is not None` 而非 falsy 判断，避免空列表被误判为"未传"
        """
        has_structured = (self.competitors is not None and self.dimensions is not None)
        has_query = bool(self.query and self.query.strip())
        if not has_structured and not has_query:
            raise ValueError("请提供 competitors+dimensions 或 query")


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
    from src.mcp import create_mcp_server
    mcp_server = create_mcp_server(settings)
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
        # ── 1. 标记 running ──
        task_dao = TaskDAO(pool)
        await task_dao.update_status(task_id_str, "running")
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
                "collected_data": {},
                "analysis_results": {},
                "report_content": "",
                # §3 质量（初始为 0/空）
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
    """创建竞品分析任务（异步后台执行）。

    【L4 工程】POST 返回 202 Accepted 而非 200 OK：
      — 任务在后台异步执行，不等待完成
      — 客户端拿到 task_id 后通过 GET /api/tasks/{id} 或 SSE 跟进

    【L5 决策】限流顺序：
      1. TokenBucket（入口 QPS）→ 329
      2. 参数校验 → 400
      3. 写入 DB → 202
      4. asyncio.create_task() → 后台执行
    """
    # ── Layer 1: 入口限流（token_bucket=None 时跳过，测试/开发环境兼容）──
    tb = request.app.state.token_bucket
    if tb is not None and not await tb.acquire():
        record_rate_limit_hit("global")
        raise HTTPException(429, "请求过多，请稍后重试")

    # ── 参数校验（Pydantic model_post_init 已自动执行，不需要手动调用）──

    # ── 构造 llm_parsed ──
    # 如果传了 competitors + dimensions → intent 明确
    # 如果只传了 query → 交给 IntentRouter 的 LLM 实体提取
    llm_parsed = {
        "competitors": body.competitors or [],
        "dimensions": body.dimensions or [],
        "intent_is_clear": bool(body.competitors and body.dimensions),
    }
    if body.query:
        llm_parsed["query"] = body.query

    # ── 写入 DB ──
    pool = _get_pool(request)
    task_dao = TaskDAO(pool)
    task_id = UUID(uuid4().hex)
    task = _make_task_dict(task_id, body, current_user.user_id)

    await task_dao.create(
        task_id=str(task_id),
        user_id=current_user.user_id,
        title=body.title,
        competitors=task["competitors"],
        dimensions=task["dimensions"],
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
    超时：300 秒后强制发送 error 事件
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
