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
        """校验：competitors+dimensions 和 query 至少传一组。"""
        has_structured = self.competitors and self.dimensions
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

    class Config:
        from_attributes = True


class ReportResponse(BaseModel):
    """报告查询响应。"""
    report_id: str
    task_id: str
    content: str | None
    quality_score: float | None
    quality_details: dict | None
    version: int
    created_at: str


class HealthResponse(BaseModel):
    """健康检查响应。"""
    status: str
    db_connected: bool


# ═════════════════════════════════════════════════════════════════════════════
# §2 应用生命周期
# ═════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理连接池和路由器的生命周期。

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

    # ── IntentRouter 延迟导入（避免循环引用） ──
    # 只在 lifespan 中导入一次，不在模块顶层导入
    from src.supervisor.router import IntentRouter
    app.state.router = IntentRouter(settings)

    logger.info("竞品分析系统启动完成, DB pool: min=2, max=10")

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

# ── 注册认证路由（公开，无需 JWT）──
app.include_router(auth_router)

# ── 静态文件（登录页）──
from fastapi.staticfiles import StaticFiles
import os
_static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


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


def _get_router(request: Request):
    """从请求上下文获取 IntentRouter 实例。"""
    from src.supervisor.router import IntentRouter

    router = request.app.state.router
    if router is None:
        raise HTTPException(500, "路由器未初始化")
    return router


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
    """后台执行任务——IntentRouter 全链路。

    【L4 工程】步骤：
    1. 更新任务状态 → running
    2. 调用 IntentRouter.route(task, llm_parsed)
    3. 根据路由结果 → 分发到 Pipeline 或 Supervisor 引擎
    4. 更新任务状态 → completed / failed
    5. 记录 Prometheus 指标

    ⚠️ 不修改现有模块——只包装调用 + 指标采集。
    """
    pool = _get_pool(request)
    router = _get_router(request)
    task_id_str = str(task_id)
    started_at = time.monotonic()

    try:
        # ── 1. 标记 running ──
        task_dao = TaskDAO(pool)
        await task_dao.update_status(task_id_str, "running")
        record_task_started()

        # ── 2. 执行路由 + 引擎 ──
        result = await router.route(task, llm_parsed)

        # ── 3. 标记 completed ──
        await task_dao.update_status(task_id_str, "completed")
        elapsed = time.monotonic() - started_at
        route = result.get("route", "pipeline")
        record_task_completed(route, elapsed)

        request.app.state.log.bind(
            task_id=task_id_str[:8]
        ).info("任务完成", extra={
            "route": route,
            "elapsed_ms": int(elapsed * 1000),
        })

    except Exception as e:
        # ── 4. 标记 failed ──
        try:
            task_dao = TaskDAO(pool)
            await task_dao.update_status(task_id_str, "failed")
        except Exception:
            pass  # 连 DB 更新都失败则不记录到 tasks 表

        record_task_failed()
        elapsed = time.monotonic() - started_at
        request.app.state.log.bind(
            task_id=task_id_str[:8]
        ).error("任务失败", extra={
            "error": str(e),
            "elapsed_ms": int(elapsed * 1000),
        })


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
            quality_details=r.get("quality_details"),
            version=r.get("version", 1),
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
    except Exception:
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
