# Phase 9 Codex 提示词 — FastAPI 服务化 + 可观测性

**生成时间**: 2026-07-21
**作者**: AI 工程师
**根提示词来源**: DEVELOPMENT_PLAN.md Phase 9
**基于实时代码审计**: router.py / graph.py / a2a.py / harness/ / schema.sql / dao.py / config.py / supervisor/

---

## 提示词（直接复制给 Codex）

````text
## 任务：竞品分析系统 Phase 9 — FastAPI 服务化 + SSE 进度推送 + 三层限流 + 可观测性

### 背景

竞品分析多Agent协作系统已完成以下模块：
- **路由+安全壳** (`src/supervisor/router.py`): `IntentRouter.route(task, llm_parsed)` 异步分流，返回 `{task_id, route, result, elapsed_ms}`
- **Pipeline** (`src/pipeline/graph.py`): `run_pipeline_task(task, mcp_server, pool)` 执行 LangGraph 采集→分析→撰写→质检
- **Supervisor** (`src/supervisor/supervisor.py`): `run_supervisor_task(task, mcp_server, pool, router, llm_supervisor)` 执行 ReAct 循环
- **A2A 通信** (`src/supervisor/a2a.py`): `A2ARouter` 注册表+send_task，已注入 HarnessGuard
- **Harness 五层检查** (`src/harness/guard.py`): `HarnessGuard.guard()` 白名单→参数→频控(TokenBucket)→PII→审计
- **审计日志** (`src/harness/audit.py`): `AuditLogger.log()` 写入 `agent_logs` 表
- **配置** (`src/config.py`): `Settings` pydantic-settings，包含 `token_bucket_capacity`, `llm_rpm_limit` 等
- **数据库** (`src/db/schema.sql`): 9 张表（tasks/reports/evidence_map/chunk_embeddings/agent_logs/memory_summaries/agent_memories/checkpoints/checkpoint_writes）
- **DAO** (`src/db/dao.py`): TaskDAO / AgentLogDAO / AgentMemoryDAO 等完整数据访问层

当前系统入口是 Python 脚本直接调用 `IntentRouter.route()`，**缺少 HTTP 服务封装**。Phase 9 目标是让系统对外暴露 REST API。

### 你的任务：创建 5 个文件

| # | 文件 | 职责 |
|---|------|------|
| 1 | `src/api/__init__.py` | 空文件 |
| 2 | `src/api/routes.py` | FastAPI 路由：任务创建/查询/SSE/报告/健康检查/指标 |
| 3 | `src/api/sse.py` | SSE 进度推送：从 agent_logs 读取并推流 |
| 4 | `src/api/rate_limit.py` | 三层限流：TokenBucket + AgentSemaphore + LLMRateLimiter |
| 5 | `src/observability/__init__.py` | 空文件 |
| 6 | `src/observability/logging.py` | 结构化日志配置（structlog） |
| 7 | `src/observability/metrics.py` | Prometheus 指标定义与暴露 |

---

#### 1. src/api/routes.py — FastAPI 应用与路由

**项目路径**: `D:\AAAagent\projects\competitive-analysis-system\`

```python
# routes.py 必须包含的内容：

# ===== 应用初始化 =====
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from contextlib import asynccontextmanager

# lifespan: 管理资源生命周期
#   启动时：调用 create_pool(settings) 创建全局连接池
#   关闭时：调用 close_pool() 关闭连接池
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: 初始化数据库连接池
    # shutdown: 关闭连接池
    yield

app = FastAPI(
    title="Competitive Analysis Agent System",
    version="1.0.0",
    lifespan=lifespan
)

# ===== 数据模型 =====
from pydantic import BaseModel

class TaskRequest(BaseModel):
    """创建任务请求体"""
    title: str                          # "协同办公软件竞品分析"
    competitors: list[str] | None = None # 竞品列表（可选，不填走 Supervisor）
    dimensions: list[str] | None = None  # 分析维度（可选）
    query: str | None = None             # 原始自然语言查询

class TaskResponse(BaseModel):
    task_id: str
    status: str
    title: str
    competitors: list[str]
    dimensions: list[str]
    pipeline_mode: str
    created_at: str

class ReportResponse(BaseModel):
    report_id: str
    task_id: str
    content: str | None
    quality_score: float | None
    version: int
    created_at: str

# ===== 路由 =====

# POST /api/tasks — 创建竞品分析任务
#   1. 生成 UUID task_id
#   2. 用 TaskDAO 写入 tasks 表（status=pending, pipeline_mode 先设 'pipeline'）
#   3. 从 request 构造 llm_parsed = {"competitors": ..., "dimensions": ..., "intent_is_clear": bool}
#      intent_is_clear = competitors 和 dimensions 都不为空
#   4. 构造 task dict = {"id": task_id, "title": ..., "user_id": "default"}
#   5. 用 asyncio.create_task() 在后台执行 IntentRouter.route(task, llm_parsed)
#      后台协程内：
#        a) TaskDAO.update_status(task_id, "running")
#        b) result = await router.route(task, llm_parsed)
#        c) TaskDAO.update_status(task_id, "completed" 或 "failed")
#   6. 立即返回 {"task_id": str(task_id), "status": "pending"}

# GET /api/tasks/{task_id} — 查询任务状态
#   1. TaskDAO.get_by_id(task_id)
#   2. 返回 TaskResponse

# GET /api/tasks/{task_id}/stream — SSE 进度推送
#   从 src.api.sse 导入 event_generator
#   返回 EventSourceResponse(event_generator(task_id, app.state.pool))

# GET /api/tasks/{task_id}/reports — 获取报告列表
#   1. 查询 reports 表 WHERE task_id = $1 ORDER BY version DESC
#   2. 返回 ReportResponse 数组

# GET /health — 健康检查
#   检查 DB 连接池是否可用（SELECT 1）

# GET /metrics — Prometheus 指标
#   从 src.observability.metrics 导入 generate_latest
#   返回 Response(content=..., media_type="text/plain")

# ===== 关键实现细节 =====

# 1. pool 挂载到 app.state.pool，所有路由通过 request.app.state.pool 访问
# 2. IntentRouter 在 lifespan 中创建一次，挂载到 app.state.router
# 3. SSE endpoint 需要设置响应头 Cache-Control: no-cache, X-Accel-Buffering: no
# 4. 所有路由都加 try/except，异常时返回 HTTPException(500, detail=str(e))
```

**⚠️ 关键集成点**：`src/api/routes.py` 不创建新的编排逻辑——它只是 `IntentRouter.route()` 的 HTTP 包装。路由决策（classify）、依赖创建（_setup_dependencies）、引擎分派（run_pipeline_task / run_supervisor_task）全部在 router.py 中完成。

---

#### 2. src/api/sse.py — SSE 进度推送

```python
# SSE 事件类型：
#   progress: {"agent": "collector", "message": "正在采集飞书数据...", "progress_pct": 0.25}
#   progress: {"agent": "analyzer", "message": "正在分析定价维度...", "progress_pct": 0.50}
#   progress: {"agent": "writer", "message": "正在生成报告...", "progress_pct": 0.75}
#   quality_result: {"score": 82, "passed": true, "dimensions": {...}}
#   complete: {"task_id": "xxx", "route": "pipeline", "elapsed_ms": 12345}
#   error: {"agent": "collector", "message": "搜索超时", "retry": true}

async def event_generator(task_id: str, pool: asyncpg.Pool):
    """从 agent_logs 表轮询读取进度并推送 SSE。

    实现步骤：
    1. 初始化 last_log_id = 0
    2. while True:
       a. SELECT id, agent_name, action, response, error, duration_ms, created_at
          FROM agent_logs WHERE task_id=$1 AND id > last_log_id ORDER BY id ASC
       b. 对每条新日志：
          - 根据 agent_name + action 构造消息文本
          - 根据 action 映射进度百分比（collect→25%, analyze→50%, write→75%, quality→90%）
          - yield {"event": "progress", "data": json.dumps({...})}
          - 更新 last_log_id
       c. 检查 tasks 表状态：SELECT status FROM tasks WHERE id=$1
       d. 如果 status == "completed" → yield complete 事件 + break
       e. 如果 status == "failed" → yield error 事件 + break
       f. await asyncio.sleep(1)  # 每秒轮询一次

    ⚠️ 超时保护：
    - 设置总超时 300 秒（5 分钟）
    - 超时后 yield error 事件 "任务执行超时" + break

    ⚠️ 断连处理：
    - 客户端断开 → asyncio.CancelledError → 清理并退出
    - 不在断开时关闭 task，task 继续在后台执行

    ⚠️ 进度百分比映射（agent_name → progress_pct）：
    - collector/collect → 0.25（采集完成占 25%）
    - analyzer/analyze → 0.50（分析完成占 50%）
    - writer/write → 0.75（撰写完成占 75%）
    - quality/grade → 0.95（质检完成占 95%）
    - 最终 complete 事件 → 1.0
    """
```

**agent_logs 表结构（已有）**：
```sql
agent_logs (
    id UUID, task_id UUID, agent_name VARCHAR(50),
    action VARCHAR(100), request JSONB, response JSONB,
    error TEXT, duration_ms FLOAT, created_at TIMESTAMPTZ
)
```

**关键设计决策**：SSE 通过轮询 agent_logs 表获取进度，而不是通过管道/队列。Harness 的 `AuditLogger.log()` 已经在每次 Agent 调用时写入 agent_logs，所以不需要额外的进度上报机制——SSE 直接读已有数据。

---

#### 3. src/api/rate_limit.py — 三层限流

```python
# 三层限流架构：
#   Layer 1: TokenBucket — 入口 QPS 限流（全局 100 QPS）
#   Layer 2: AgentSemaphore — Agent 并发控制（最多 3 个并发 Agent 调用）
#   Layer 3: LLMRateLimiter — LLM API 限流（60 RPM 滑动窗口）

# ===== 第一层: TokenBucket =====
class TokenBucket:
    """令牌桶入口限流器。

    算法：
    - 桶容量 = capacity（默认 100）
    - 每秒补充 refill_rate 个 token（默认 10 tokens/s）
    - 每次请求消耗 1 token
    - token 不足 → 请求被拒绝（429 Too Many Requests）

    ⚠️ 不使用 Redis——内存实现即可。
    ⚠️ asyncio.Lock 保证并发安全。
    """
    def __init__(self, capacity: int = 100, refill_rate: float = 10.0):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_rate
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """获取一个 token，成功返回 True。"""
        async with self.lock:
            # 1. 计算上次补充至今的时间间隔 → 补充 tokens
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            # 2. 尝试消耗 1 token
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

# ===== 第二层: AgentSemaphore =====
class AgentSemaphore:
    """Agent 并发控制。用 asyncio.Semaphore。"""
    def __init__(self, max_concurrent: int = 3):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        # tracked: 记录当前正在执行的 agent 调用（用于 /metrics）
        self.tracked: dict[str, int] = {}

    async def acquire(self, agent_name: str):
        await self.semaphore.acquire()
        self.tracked[agent_name] = self.tracked.get(agent_name, 0) + 1

    def release(self, agent_name: str):
        self.semaphore.release()
        self.tracked[agent_name] = max(0, self.tracked.get(agent_name, 0) - 1)

# ===== 第三层: LLMRateLimiter =====
class LLMRateLimiter:
    """LLM API 限流器——滑动窗口算法。

    ⚠️ 用滑动窗口不是固定窗口：
    固定窗口的问题：59秒发60个请求 + 下一秒再发60个 = 2秒内120个请求
    滑动窗口：检查过去60秒内的请求数，超过60就等待。
    """
    def __init__(self, max_rpm: int = 60):
        self.max_rpm = max_rpm
        self.requests: list[float] = []  # 请求时间戳列表
        self.lock = asyncio.Lock()

    async def wait_if_needed(self):
        """检查滑动窗口，必要时等待。"""
        async with self.lock:
            now = time.monotonic()
            window_start = now - 60.0
            # 清理窗口外的过期记录
            self.requests = [t for t in self.requests if t > window_start]
            if len(self.requests) >= self.max_rpm:
                # 最早请求过期的时间 = 最早请求 + 60 秒 - now
                wait_time = self.requests[0] + 60.0 - now
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    # 等完后再清理一次
                    self.requests = [t for t in self.requests if t > time.monotonic() - 60.0]
            self.requests.append(time.monotonic())


# ===== 组合使用（在 routes.py 的依赖注入中） =====
# 创建 POST /api/tasks 的 FastAPI Depends：
# async def check_rate_limit():
#     if not await token_bucket.acquire():
#         raise HTTPException(429, "请求过多，请稍后重试")
```

**⚠️ 注意事项**：
- `TokenBucket` 和 `LLMRateLimiter` 不要和 `src/harness/guard.py` 中已有的 `TokenBucket` 冲突——harness 中的 TokenBucket 用于 Agent 间调用的频控（单 Agent 级别），api 中的 TokenBucket 用于 HTTP 入口的全局 QPS。它们职责不同，可以共存。
- 如果 harness/guard.py 中的 TokenBucket 实现更好，可以直接从 harness 导入复用它，不重复造轮子。api/rate_limit.py 专注于 HTTP 层限流，不重复 harness 层。

---

#### 4. src/observability/logging.py — 结构化日志

```python
import structlog
import logging

def setup_logging():
    """配置结构化日志。

    日志键：timestamp, level, task_id, agent, action, message

    格式示例：
    [2026-07-21T19:30:00+08:00] [INFO] [task_abc] [collector] [web_search] 搜索完成, 返回 15 条结果
    """
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

# 绑定上下文的辅助函数：
# logger = structlog.get_logger().bind(task_id="xxx", agent="collector")
# await logger.info("web_search", query="飞书 定价", results=15)

# 同时配置标准 logging（作为 fallback）：
# 让 structlog 和标准 logging 共存——第三方库（asyncpg, httpx）用标准 logging，
# 我们自己的代码用 structlog
```

**⚠️ 注意**：`structlog` 可能不在 pyproject.toml 中——直接在 terminal 用 pip 安装即可，不需要改 pyproject.toml。如果 structlog 安装不了，降级为标准 logging + 自定义 Formatter 输出 JSON 格式日志。

---

#### 5. src/observability/metrics.py — Prometheus 指标

```python
# 使用 prometheus_client 库（已在 pyproject.toml 中）

from prometheus_client import Counter, Histogram, Gauge, generate_latest, REGISTRY

# ===== 指标定义 =====

# 1. 任务计数（按状态）
ca_tasks_total = Counter(
    "ca_tasks_total",
    "竞品分析任务总数",
    ["status"]  # pending, running, completed, failed
)

# 2. 任务耗时（按路由类型）
ca_task_duration_seconds = Histogram(
    "ca_task_duration_seconds",
    "任务执行耗时（秒）",
    ["route"],  # pipeline, supervisor
    buckets=[1, 5, 10, 30, 60, 120, 300, 600]  # 1s ~ 10min
)

# 3. Agent 调用次数（按 agent + action + status）
ca_agent_calls_total = Counter(
    "ca_agent_calls_total",
    "Agent 调用次数",
    ["agent", "action", "status"]  # success, error
)

# 4. 质检分数分布
ca_quality_score = Histogram(
    "ca_quality_score",
    "质检分数分布",
    buckets=[0, 50, 60, 70, 75, 80, 85, 90, 95, 100]
)

# 5. RAG 召回率（按维度）
ca_rag_recall = Gauge(
    "ca_rag_recall",
    "RAG 检索召回率（当前值）",
    ["dimension"]
)

# 6. 限流触发次数（按层级 + agent）
ca_rate_limit_hits = Counter(
    "ca_rate_limit_hits",
    "限流触发次数",
    ["layer", "agent"]  # global, agent, llm
)

# 7. Harness 阻断次数（按检查类型）
ca_harness_blocks = Counter(
    "ca_harness_blocks",
    "Harness 安全检查阻断次数",
    ["check_type"]  # whitelist, param_validation, rate_limit, pii
)

# ===== 指标采集（在 routes.py 中集成） =====
# POST /api/tasks 创建成功 → ca_tasks_total.labels(status="pending").inc()
# 后台任务开始 → ca_tasks_total.labels(status="running").inc()
# 任务完成 → ca_tasks_total.labels(status="completed").inc() + ca_task_duration_seconds.labels(route=...).observe(...)
# HarnessGuard.guard() 返回 blocked → ca_harness_blocks.labels(check_type=...).inc()

# ===== 指标暴露 =====
# GET /metrics 路由: return Response(content=generate_latest(REGISTRY), media_type="text/plain")
```

**⚠️ 指标采集集成策略**：在 `routes.py` 的任务执行流程中嵌入 `inc()` / `observe()` 调用。不修改现有的 `router.py` 或 `graph.py`——指标采集集中在 HTTP 层和 harness 的 guard 层。如果 harness/guard.py 需要改动，只加 `ca_harness_blocks.inc()` 一行在 `guard()` 的阻断逻辑分支中。

---

### 验收标准

1. ✅ `POST /api/tasks` 返回 `{"task_id": "...", "status": "pending"}`，任务在后台异步执行
2. ✅ `GET /api/tasks/{id}` 返回任务状态（pending/running/completed/failed）
3. ✅ `GET /api/tasks/{id}/stream` 推送 SSE 事件（progress + quality_result + complete/error）
4. ✅ `GET /api/tasks/{id}/reports` 返回报告列表
5. ✅ `GET /health` 返回 `{"status": "ok"}`
6. ✅ `GET /metrics` 输出 Prometheus 格式指标
7. ✅ TokenBucket 正确实现 100 QPS 入口限流
8. ✅ 结构化日志包含 task_id 和 agent 字段
9. ✅ 服务可以 `uvicorn src.api.routes:app --reload` 启动

---

### 注意事项

1. **不修改已有模块**：router.py / graph.py / a2a.py / harness/ / dao.py 保持不动。Phase 9 只是新增 HTTP 层包装。
2. **数据库连接池**：通过 `app.state.pool` 管理，在 lifespan 中创建。routes.py 通过 `request.app.state.pool` 获取。
3. **IntentRouter 实例**：在 lifespan 中创建一次（`Settings()` → `IntentRouter(settings)`），挂载到 `app.state.router`。
4. **SSE 超时**：轮询最多 300 秒，超时发送 error 事件并断开。
5. **异常处理**：所有路由都 try/except，异常时返回 HTTPException(500, detail=str(e))，同时 log.exception。
6. **Python 3.13 兼容**：当前环境是 Python 3.13，fastapi + uvicorn + sse-starlette 兼容。
7. **不需要 .env 变更**：Settings 已有所有必需字段（token_bucket_capacity=100, llm_rpm_limit=60）。

### 相关文件速查

| 需要 import 的文件 | 导入内容 | 用途 |
|---|---|---|
| `src.config` | `Settings` | 读取配置 |
| `src.db.connection` | `create_pool`, `close_pool` | 连接池生命周期 |
| `src.db.dao` | `TaskDAO`, `AgentLogDAO` | 任务 CRUD + 日志读取 |
| `src.supervisor.router` | `IntentRouter` | 路由决策 + 引擎分派 |
| `src.harness.guard` | `HarnessGuard` | 五层检查（如需在 API 层复用频控） |
| `src.observability.metrics` | 各 Counter/Histogram | 指标采集 |
````

---

## 补充说明（给花月看，不给 Codex）

### 为什么这个提示词比 DEVELOPMENT_PLAN 中的版本更长？

1. **DEVELOPMENT_PLAN 写于项目早期**（6/14），当时还没写任何代码。现在 Phase 1–8 代码已存在，提示词必须反映真实代码结构。
2. **明确"不修改已有模块"**——这是 Codex 最容易踩的坑。它可能试图重构 router.py 或 graph.py，必须明确制止。
3. **SSE 从 agent_logs 表轮询**——这是利用已有审计基础设施，不需要额外开发进度上报机制。
4. **Layer 1 限流和 Harness 限流共存**——harness/guard.py 已有 TokenBucket（Agent 级），api/rate_limit.py 做 HTTP 入口级。两个不冲突，但要说明清楚。

### 预估工作量

1 个 Codex 会话，约 30-60 分钟。5 个文件体量适中，最大的是 routes.py 和 sse.py。

### 是否现在发送给 Codex？

已保存到 `D:\AAAagent\projects\competitive-analysis-system\phase_report\phase9-codex-prompt_2026-07-21.md`。你可以直接复制提示词发给 Codex。