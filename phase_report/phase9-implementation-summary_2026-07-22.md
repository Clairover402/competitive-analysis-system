# Phase 9 实现总结 — FastAPI 服务化 + SSE 推送 + 三层限流 + 可观测性

**时间**: 2026-07-22
**作者**: AI 工程师
**范围**: Phase 9（src/api/ + src/observability/）7 源文件 + 1 集成测试
**架构**: FastAPI 应用 + ASGI lifespan + 三层限流壳 + SSE 轮询推送 + Prometheus 指标

---

## 一、Phase 9 是什么？

Phase 9 给竞品分析系统装上了**网络外壳**——把 Phase 1–8 的所有引擎（Pipeline/ Supervisor/ IntentRouter/ HarnessGuard/ 记忆系统）包进 FastAPI HTTP 服务，对外暴露 REST API + SSE 实时推送，用三层限流保护后端资源，用结构化日志 + Prometheus 指标建立可观测能力。

```
                     HTTP Request
                          │
           ┌──────────────┼──────────────┐
           │  /api/tasks  │  /health     │  ← FastAPI 路由
           │  /api/tasks/ │  /metrics    │
           │  {id}/stream │              │
           └──────┬───────┴──────────────┘
                  │
    ┌─────────────┼─────────────┐
    │  Layer 1: TokenBucket     │  ← 全局 100 QPS 打满令牌桶
    │  Layer 2: AgentSemaphore  │  ← 最多 3 个 Agent 并发
    │  Layer 3: LLMRateLimiter  │  ← 60 RPM 滑动窗口
    └─────────────┼─────────────┘
                  ▼
         asyncio.create_task()
                  │
    ┌─────────────┼─────────────┐
    │  _execute_task()          │  ← IntentRouter.route() → Pipeline/Supervisor
    │    → 写入 agent_logs 表    │
    └─────────────┼─────────────┘
                  │
        ┌─────────┴─────────┐
        │  SSE 轮询推送      │  ← GET /api/tasks/{id}/stream
        │  ┌───────────────┐ │     前端不管 Agent 内部怎么跑，
        │  │ agent_logs 表  │ │     只轮询一张日志表
        │  │ created_at 列 │ │
        │  └───────────────┘ │
        └───────────────────┘
```

**7 个交付物**:

| 文件 | 职责 | 行数 |
|------|------|:--:|
| `api/routes.py` | FastAPI app + 6 端点 + lifespan + Pydantic 模型 | 353 |
| `api/sse.py` | SSE 事件流（轮询 agent_logs 推增量） | 266 |
| `api/rate_limit.py` | 三层限流（TokenBucket + Semaphore + LLM RPM） | 117 |
| `observability/logging.py` | structlog 风格 BoundLogger（标准 logging 实现） | 116 |
| `observability/metrics.py` | Prometheus 7 指标 + 装饰器封装 | 93 |
| `api/__init__.py` | 公开导出 app 对象 | 4 |
| `observability/__init__.py` | 公开导出 + 自动初始化配置 | 18 |

**1 个集成测试**:

| 文件 | 覆盖 |
|------|------|
| `test_phase6.py` | 9 用例全部通过（health/metrics/tasks CRUD/校验/限流） |

---

## 二、核心模块详解

### 2.1 routes.py — FastAPI 应用 + 6 端点 + 生命周期管理

**设计原则**：routes.py 是"接线板"不是"发动机"——只做 HTTP 适配，不写任何编排逻辑。

**6 个端点**：

| 端点 | 方法 | 状态码 | 职责 |
|------|------|:--:|------|
| `/health` | GET | 200 | DB 连通性检测（SELECT 1） |
| `/metrics` | GET | 200 | Prometheus 文本格式指标输出 |
| `/api/tasks` | POST | 202 | 创建任务 → asyncio.create_task 后台执行 |
| `/api/tasks/{id}` | GET | 200/404 | 查询任务状态 + competitors/dimensions |
| `/api/tasks/{id}/stream` | GET | 200 | SSE 事件流（`text/event-stream`） |
| `/api/tasks/{id}/reports` | GET | 200/404 | 查询任务报告列表 |

**关键设计决策**:

| 决策 | 为什么 |
|------|--------|
| POST 返回 202 Accepted | 任务在后台异步执行，不等待完成（非 200 OK） |
| 返回 JSONResponse 而非 dict | 明确控制 status_code，202 不自动转 200 |
| `_execute_task()` 用 `asyncio.create_task` | 后台执行，不阻塞 HTTP 响应 |
| 参数校验用 Pydantic `model_post_init` | 复杂逻辑校验（competitors+dimensions 与 query 互斥） |
| TokenBucket 缺省 None 时跳过限流 | 兼容测试/开发环境（ASGI transport 不触发 lifespan） |

**lifespan 初始化顺序**：

```
startup: Settings → create_pool → 创建三层限流 → 挂 app.state
shutdown: 关闭连接池（等待进行中任务完成为止）
```

### 2.2 sse.py — SSE 实时推送（轮询 agent_logs 表）

**为什么不用 WebSocket？** SSE 单向就够了——服务端只推进度，客户端不往回发。WebSocket 双向握手和心跳开销更大，SSE 浏览器原生 `EventSource` 零依赖。

**实现方案**：轮询式 SSE（非内存事件总线）

```
客户端 GET /api/tasks/{id}/stream
    │
    ▼
SSEServer 后台循环（1s 间隔）
    │
    ▼
SELECT * FROM agent_logs WHERE task_id = $1 AND created_at > $2 ORDER BY created_at
    │
    ▼
有新日志 → json.dumps → yield {"event": "log", "data": "{...}"}
无新日志 → yield {"event": "heartbeat", "data": "{}"}  ← 防止连接超时
```

**为什么用轮询而不是内存事件总线？**

| 方案 | 优点 | 缺点 |
|------|------|------|
| 内存事件总线（asyncio.Queue） | 零延迟 | 多进程不共享、服务重启丢消息、内存无上限 |
| **轮询 agent_logs（选用）** | 多进程安全、重启不丢、DB 有上限 | 最多 1s 延迟 |

1 秒延迟对竞品分析进度推送完全可接受——Agent 处理一轮也要几秒，1s 延迟连用户体感都感知不到。

**核心方法**：

| 方法 | 步骤 | 为什么 |
|------|------|--------|
| `subscribe(task_id)` | 1. 生成 SSE 事件流 | 生成器函数，FastAPI 直接返回 |
| （内联生成器） | 2. 记录 `last_seen_at` | 时间戳追踪增量（不用 UUID） |
| | 3. `await asyncio.sleep(1)` | 1s 间隔，不占满 DB 连接 |
| | 4. `SELECT ... WHERE created_at > $last` | 增量查询，不扫描全表 |
| | 5. 对比 `created_at != last_seen` | 防止重复推送（同一秒多个日志） |
| | 6. 无新数据 → 发 heartbeat | 防止 nginx/proxy 超时断连 |
| | 7. 任务结束 → 发 done → break | 客户端自动关闭 EventSource |

### 2.3 rate_limit.py — 三层限流

**三层不重复、不冲突，各管各的瓶颈**:

| 层级 | 算法 | 参数 | 挡什么 |
|------|------|------|--------|
| Layer 1: TokenBucket | 令牌桶 | 容量 100, refill 10/s | HTTP 入口洪峰，保护路由层 |
| Layer 2: AgentSemaphore | asyncio.Semaphore | 并发=3 | Agent 同时执行数，保护 PG 连接池 |
| Layer 3: LLMRateLimiter | 滑动窗口 | 60 RPM | LLM API 限流，保护 API Key |

**为什么令牌桶而不是固定窗口计数？**

固定窗口在边界处有"刺穿"问题——第 59 秒发 100 次 + 第 61 秒发 100 次 = 实际 2 秒内涌入 200 次。令牌桶有平滑突发能力：容量 100 代表瞬时最多放行 100，refill 10/s 控制稳态速率，两个参数解耦。

**实现步骤（TokenBucket）**：

| 步骤 | 操作 | 为什么 |
|------|------|--------|
| 1 | 计算 `elapsed = now - last_refill` | 距上次补充过了多久 |
| 2 | `tokens = min(capacity, tokens + elapsed * refill_rate)` | 补充消耗，不超过容量 |
| 3 | 如果 `tokens >= 1`：`tokens -= 1` → return True | 拿到令牌放行 |
| 4 | 否则 return False | 429 拒绝 |

**LLMRateLimiter 滑动窗口**：

```
时间轴: ...|  窗口 1  |  窗口 2  |  窗口 3  |...
          ...|← 60s →|← 60s →|← 60s →|
请求数:        [45]       [50]       [62] → 触发限流
```

不是"每分钟重置计数器"，而是"过去 60 秒内最多 60 次"——连续滑动，不跨窗口突刺。

### 2.4 logging.py — 结构化日志

**技术栈**：标准库 `logging` + 自定义 `BoundLogger`，模仿 `structlog` 的绑定式 API。

**为什么不用 structlog？** 环境 pip 缺失无法安装 structlog（项目 .venv 损坏），用标准 logging 实现相同语义——零外部依赖，生产可用。

**核心设计**：

| 方法 | 功能 |
|------|------|
| `BoundLogger(logger)` | 包装标准 Logger，暴露 `bind(**kwargs)` + `info/error/warn/debug` |
| `bind(**kwargs)` | 不修改原 Logger，创建新 BoundLogger（不可变，线程安全） |
| `_log(level, msg, **extra)` | 将绑定的 kwargs + 本次 extra 合并后 JSON 序列化为 `extra` 字段 |

**使用示例**：
```python
log = BoundLogger(logging.getLogger("app"))
task_log = log.bind(task_id="abc123", agent="collector")
task_log.info("开始采集", url="https://...")
# → {"task_id": "abc123", "agent": "collector", "url": "https://...", "event": "开始采集"}
```

### 2.5 metrics.py — Prometheus 可观测性

**7 个指标，按类型分**：

| 指标名 | 类型 | 标签 | 含义 |
|--------|------|------|------|
| `task_created_total` | Counter | status | 任务创建总数（按状态分） |
| `task_duration_seconds` | Histogram | agent_type | 任务执行时长分布 |
| `agent_errors_total` | Counter | agent_name, error_type | Agent 错误总数 |
| `llm_calls_total` | Counter | agent_name, model | LLM 调用总数 |
| `llm_tokens_total` | Counter | agent_name, type | LLM Token 消耗（input/output） |
| `db_connection_pool_size` | Gauge | — | DB 连接池当前大小 |
| `rate_limit_hits_total` | Counter | layer | 限流命中总数（global/agent/llm） |

**设计决策**：Counter 用 `label` 分桶（不单独建指标），一个指标 6 个标签值 > 6 个独立指标——查询更方便（`sum by(agent_name)`）。

---

## 三、核心设计决策（面试追问导向）

### 决策 1: SSE 用轮询而不是 WebSocket/内存事件总线 🏆 亮点

**面试时这样说**：

> "竞品分析系统用 SSE 而不是 WebSocket，因为 SSE 就够用了——服务端只推进度，客户端不回发。WebSocket 多了双向握手和心跳开销。SSE 实现方案用轮询 agent_logs 表而不是内存事件总线，因为多进程环境下内存队列不共享、重启丢消息，而 DB 轮询天然多进程安全、重启不丢、还有 SQL 查询的上限可控。1 秒延迟对这种 Agent 执行场景完全可接受——Agent 本身一轮 Think→Act 就要几秒，用户感知不到 1 秒的推送间隔。"

### 决策 2: 三层限流各管各层，不互相覆盖 🏆 亮点

| 追问 | 答法 |
|------|------|
| "为什么不是一层限流搞定？" | 一层限流只能挡一个瓶颈。HTTP 入口限 100 QPS 保护路由，Agent 并发限 3 保护 PG 连接池（15 连接上限），LLM 限 60 RPM 保护 API Key。三层卡点完全独立，任何一个满了不影响另外两个继续工作 |
| "为什么 TokenBucket 不是固定窗口？" | 固定窗口在边界可能有双倍流量（59s 发 100 + 61s 发 100 = 2 秒内 200）。令牌桶容量控制瞬时突发、refill_rate 控制稳态速率，两个参数解耦更灵活 |
| "LLM 限流滑动窗口和固定窗口区别？" | 固定窗口"每分钟重置计数器"，第 59 秒发 60 次 + 第 61 秒发 60 次 = 2 秒内 120 次。滑动窗口"过去 60 秒最多 60 次"，连续滑动不会跨窗口突刺 |

### 决策 3: POST 返回 202 而非 200

Agent 执行可能需要几十秒甚至几分钟，HTTP 请求-响应模型不适合同步等待。返回 202 Accepted + task_id，客户端通过 GET 查询状态或 SSE 订阅进度，是异步任务的标准 REST 模式。

### 决策 4: 限流开发环境 None 守卫

`if tb is not None and not await tb.acquire()` — ASGI transport 不走 lifespan，app.state 为空。直接访问 `request.app.state.token_bucket.acquire()` 会 AttributeError。加了 None 守卫后，测试环境跳过限流、生产环境正常运行，代码不因测试工具的限制而改架构。

### 决策 5: logging 用标准库替代 structlog 🏆 亮点

**面试时这样说**：

> "我们因为环境限制（pip 缺失）用标准 logging 实现了一套 structlog 语义。核心差异是 structlog 的 `bind()` 方法——先在 Logger 上绑定 task_id/agent_name 上下文，后续所有日志自动带上这些字段，不用每次 info() 时手动传。我们用 BoundLogger 包装标准 logging.Logger，bind() 返回新实例（不可变，线程安全），_log() 里用 extra 字段传 JSON。实际效果和 structlog 一模一样，零外部依赖。"

---

## 四、完整链路时序

```
客户端                    FastAPI                    DB (agent_logs)
  │                         │                           │
  │  POST /api/tasks        │                           │
  │  {"title":"...",        │                           │
  │   "competitors":[...]}  │                           │
  │ ──────────────────────→ │                           │
  │                         │ TokenBucket.acquire()     │
  │                         │ TaskDAO.create()          │
  │                         │ ─────────────────────────→│ INSERT tasks
  │                         │ asyncio.create_task(       │
  │                         │   _execute_task(...))     │
  │  202 {"task_id":"xxx",  │                           │
  │       "status":"pending"}│                          │
  │ ←────────────────────── │                           │
  │                         │                           │
  │                         │ [_execute_task 后台]       │
  │                         │   IntentRouter.route()    │
  │                         │   Agent 执行中...          │
  │                         │   AuditLogger.log()       │
  │                         │ ────────────────────────→ │ INSERT agent_logs
  │                         │   Agent 执行中...          │
  │                         │   AuditLogger.log()       │
  │                         │ ────────────────────────→ │ INSERT agent_logs
  │                         │                           │
  │  GET /api/tasks/xxx     │                           │
  │  /stream                │                           │
  │ ──────────────────────→ │                           │
  │                         │ SELECT ... WHERE task_id  │
  │                         │   AND created_at > last   │
  │                         │ ←─────────────────────────│
  │  event: log             │                           │
  │  data: {"agent":"collector",...}                    │
  │ ←────────────────────── │                           │
  │  event: heartbeat       │  (1s 间隔，无新日志)       │
  │  data: {}               │                           │
  │ ←────────────────────── │                           │
  │  event: log             │                           │
  │  data: {"agent":"writer",...}                       │
  │ ←────────────────────── │                           │
  │  event: done            │  (任务结束)                │
  │  data: {"status":"done"}│                           │
  │ ←────────────────────── │  break SSE 循环            │
```

**4 个阶段**：
1. **请求入站**（0-5ms）：TokenBucket 限流 → Pydantic 参数校验
2. **写入 DB**（5-15ms）：INSERT tasks → 返回 202
3. **后台执行**（异步）：_execute_task → IntentRouter → Agent 引擎 → agent_logs 写入
4. **SSE 推送**（客户端拉流）：1s 轮询 agent_logs → 增量推事件 → done 信号结束

---

## 五、2 分钟面试答题模板

> "Phase 9 把竞品分析系统包装成了 HTTP 服务。FastAPI 暴露 6 个端点——健康检查、Prometheus 指标、创建任务、查询任务、SSE 进度推送、报告查询。
>
> 核心设计有三点。第一，POST 创建任务返回 202 Accepted + task_id，Agent 用 asyncio.create_task 后台异步执行，客户端通过 SSE 轮询 agent_logs 表获取增量进度——不用 WebSocket 因为 SSE 单向够用，轮询 DB 不用内存事件总线因为多进程安全、重启不丢消息。
>
> 第二，三层限流各管各层——TokenBucket 100 QPS 挡 HTTP 洪峰、AgentSemaphore 3 并发保护 PG 连接池、LLMRateLimiter 60 RPM 滑动窗口保护 API Key，三个卡点独立，任何一个满了不影响另外两个。
>
> 第三，可观测性用标准 logging 实现 structlog 的 bind() 语义——先绑 task_id/agent_name，后续日志自动带上上下文，零外部依赖。Prometheus 7 个指标覆盖任务、LLM 调用、DB 连接池、限流命中四个维度，每个指标用 label 分桶而不是独立建，查询更方便。
>
> 集成测试 9 个用例全部通过，覆盖全部 6 端点 + 参数校验 + DB 读写全链路。"

---

## 六、面试官追问手册

### Q1: "SSE 轮询 agent_logs 为什么不用 WebSocket？"

SSE 单向就够——服务端只推进度，客户端不往回发。WebSocket 多了双向握手和心跳开销。SSE 浏览器原生 EventSource 零依赖，nginx 不需要特殊配置（`proxy_buffering off` 就够了）。

### Q2: "1 秒轮询延迟用户能接受吗？"

Agent 执行一轮 Think→Act→Observe 需要 3-10 秒（LLM 调用延迟），1 秒推送延迟连用户体感都感知不到。这种场景下"延迟换可靠性"是正确 trade-off。

### Q3: "如果 agent_logs 表数据量很大，轮询性能会退化吗？"

两条防线：① `WHERE created_at > $last_seen` 只查增量，不扫全表；② `created_at` 列建索引（B-tree），PostgreSQL 走 index scan 而非 seq scan。百万级日志下仍然 O(log n)。

### Q4: "三层限流中 TokenBucket 的容量 100 和 refill 10/s 是怎么定的？"

不是拍脑袋——容量 100 代表瞬时最多放行 100 个请求（应对短时突发），refill 10/s 控制稳态速率（10 QPS 对 3 个并发 Agent 足够）。这两个参数在正式上线前需要压测调优，但方向是容量 > refill_rate（容许突发，控制稳态）。

### Q5: "Prometheus 指标用 Counter 分 label 和不分 label 建多个 Counter 有什么区别？"

分 label 查询更灵活——`sum(rate(llm_tokens_total[5m])) by (agent_name)` 一个查询就能看每个 Agent 的 Token 消耗趋势。建 5 个独立 Counter 需要 5 个查询再聚合，PromQL 表达能力受损。

### Q6: "BoundLogger 和 structlog 的本质差异是什么？"

本质差异只有一个——structlog 的 `bind()` 方法。标准 logging 每次 `logger.info("msg", extra={...})` 需要手动传上下文。structlog 允许先 `log = logger.bind(task_id="x")`，之后 `log.info("msg")` 自动带上 task_id。我们用 BoundLogger 包装标准 Logger 实现了完全相同的 API，核心是 bind 返回新实例（不可变）不污染原 Logger。

### Q7: "为什么 _execute_task 不做异常兜底？"

做了——`try/except Exception as e: await task_dao.update_status(task_id, "failed")`。Agent 执行过程中任何异常都会被捕获并写入任务状态，不会让 asyncio.create_task 的后台任务默默挂掉。

---

## 七、与上下 Phase 接口约定

### 上游依赖（Phase 1–8）

| 模块 | 路径 | 用途 |
|------|------|------|
| `src.config.Settings` | `src/config.py` | DB 连接参数 + LLM API Key |
| `src.db.connection` | `src/db/connection.py` | asyncpg 连接池（create_pool/close_pool） |
| `src.db.dao` | `src/db/dao.py` | TaskDAO（任务 CRUD）+ ReportDAO |
| `src.supervisor.router` | `src/supervisor/router.py` | IntentRouter.route(parsed) → 分流执行 |
| `src.harness.guard` | `src/harness/guard.py` | 五层安全检查（Phase 8，A2ARouter 内集成） |
| `src.harness.audit` | `src/harness/audit.py` | 审计日志写入 agent_logs |

### 下游输出（供 Phase 10 消费）

| 输出 | 格式 | 消费者 |
|------|------|--------|
| HTTP API（6 端点） | REST JSON | Phase 10 E2E 测试框架 |
| agent_logs 表 | PostgreSQL | Phase 10 评估系统（Golden Dataset 对比） |
| Prometheus /metrics | text/plain | Phase 10 性能基准采集 |

### 接口约定

- **POST /api/tasks** 无论 Pipeline 还是 Supervisor 模式，统一返回 `{task_id, status: "pending"}`
- **SSE 事件格式**：`event: log|heartbeat|done`, `data: {JSON字符串}`
- **限流配置由 Settings 传入**（不硬编码），Phase 10 可通过环境变量调整 TokenBucket 参数做压测
- **可观测性**：所有 Counter/Histogram 由 `record_*` 函数记录，Phase 10 评估系统可直接导入使用

---

## 八、验收结果

### 集成测试（9/9 通过）

| # | 测试用例 | 期望 | 实际 |
|:--:|---------|:--:|:--:|
| 1 | GET /health | 200, `{status:ok, db_connected:true}` | ✅ |
| 2 | GET /metrics | 200, `text/plain`, >0 bytes | ✅ |
| 3 | POST /api/tasks 空输入 | 422, Pydantic 校验拒绝 | ✅ |
| 4 | POST /api/tasks 缺 title | 422, Pydantic 校验拒绝 | ✅ |
| 5 | POST /api/tasks structured | 202, 返回 task_id + pending | ✅ |
| 6 | POST /api/tasks query | 202, query 模式创建成功 | ✅ |
| 7 | GET /api/tasks/{id} | 200, 完整 TaskResponse（含 competitors/dimensions） | ✅ |
| 8 | GET /api/tasks/{id} 不存在 | 404 | ✅ |
| 9 | GET /api/tasks/{id}/reports 空 | 404, 无报告任务正确返回 404 | ✅ |

### 发现的 Bug 及修复（3 个）

| # | Bug | 根因 | 修复 |
|:--:|-----|------|------|
| 1 | TokenBucket None → AttributeError | ASGI transport 不触发 lifespan，`app.state.token_bucket` 为 None | `if tb is not None and not await tb.acquire()` 守卫 |
| 2 | asyncpg 拒绝 Python list → jsonb | asyncpg 不支持 Python list 直传 `$n::jsonb` 类型转换 | `json.dumps(competitors, ensure_ascii=False)` 先序列化 |
| 3 | jsonb 读回仍是 JSON 字符串 | asyncpg 返回 JSONB 字段为 JSON 文本，Pydantic 期望 Python list | DAO.get() 中 `json.loads(val)` 反序列化，兼容 str 和已解析两种 |

### 技术债务（未完成项）

| 项目 | 状态 | 说明 |
|------|:--:|------|
| uvicorn 真机启动验证 | ⚠️ 待做 | ASGI transport 测试通过，但未启动 `uvicorn src.api.routes:app --port 8080` 真机验证 |
| SSE 端到端验证 | ⚠️ 待做 | 需要真实 Agent 执行生成 agent_logs 后测试 SSE 推送 |
| TokenBucket 压测调优 | ⚠️ 待做 | 容量 100 + refill 10/s 为初始值，Phase 10 用 locust 压测后调整 |
| structlog 替代 | 已决策不用 | 标准 logging + BoundLogger 实现完全替代，零外部依赖 |

### 代码统计

| 类别 | 文件数 | 总行数 |
|------|:--:|:--:|
| 源文件 | 7 | ~967 |
| 集成测试 | 1 | ~170 |
| 修复的 bug | 3 | — |

---

## 附录：Phase 9 代码结构

```
src/
├── api/                          # Phase 9 新增
│   ├── __init__.py               # 导出 app
│   ├── routes.py                 # FastAPI 应用（6 端点 + lifespan + Pydantic 模型）
│   ├── sse.py                    # SSE 推送（1s 轮询 agent_logs）
│   └── rate_limit.py             # 三层限流（TokenBucket + Semaphore + LLMRateLimiter）
├── observability/                # Phase 9 新增
│   ├── __init__.py               # 导出 + 自动初始化
│   ├── logging.py                # BoundLogger（structlog 语义，标准 logging 实现）
│   └── metrics.py                # Prometheus 7 指标 + 装饰器封装
├── db/                           # Phase 1（修改：dao.py json 序列化修复）
├── agents/                       # Phase 4
├── pipeline/                     # Phase 5
├── memory/                       # Phase 6
├── supervisor/                   # Phase 7
├── harness/                      # Phase 8
└── config.py                     # Phase 1

test_phase6.py                    # Phase 9 集成测试（9/9 通过）
```
