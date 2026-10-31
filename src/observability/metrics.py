"""Prometheus 指标定义 — 竞品分析系统可观测性指标体系。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 面试 — 指标体系设计】
═══════════════════════════════════════════════════════════════════════════════

指标设计三原则：
  1. RED 模式（Rate, Errors, Duration）— 覆盖核心服务指标
  2. 每个 Counter 都有 status/type 标签 — 支持按维度下钻
  3. 指标采集在 HTTP 入口 + Harness 阻断点 — 不改动编排引擎

六项指标：
┌──────────────────────────────┬──────────┬─────────────────────────────────┐
│ 指标名                        │ 类型      │ 说明                             │
├──────────────────────────────┼──────────┼─────────────────────────────────┤
│ ca_tasks_total               │ Counter  │ 任务数按 status 分桶             │
│ ca_task_duration_seconds     │ Histogram│ 任务耗时按 route 分桶            │
│ ca_agent_calls_total         │ Counter  │ Agent 调用按 agent/action/status │
│ ca_rate_limit_hits           │ Counter  │ 限流触发按 layer 分              │
│ ca_harness_blocks            │ Counter  │ Harness 阻断按 check_type 分     │
│ ca_task_resumes_total        │ Counter  │ 续跑请求按 outcome 分（2026-10-31）│
└──────────────────────────────┴──────────┴─────────────────────────────────┘

⚠️ 2026-10-19 删除了 ca_quality_score / ca_rag_recall / ca_eval_* 两批指标：
   前者从未有调用点（质检分数、召回率都只落 DB/日志，没人 observe/set），
   后者是旧「评估结果落库 + 推 Prometheus」链路，ragas 重构后已改为文件归档、
   不再上报 Prometheus。Grafana 面板未引用它们，删除不影响既有看板。
"""

from __future__ import annotations

# prometheus_client 是 Prometheus 官方的 Python 客户端库，
# 用来在你的 Python 代码（FastAPI、Django、Agent、微服务）里埋点、定义监控指标，
# 启动一个 HTTP `/metrics` 端点，让 Prometheus 服务定期拉取指标（pull 模型）PyPI
#
# > 一句话：在 Python 应用内部生成监控指标，暴露给 Prometheus 采集


from prometheus_client import Counter, Histogram

# 核心 4 种指标类型（面试高频）
#
# 1. **Counter**：计数器，**只增不减**。接口总请求数、异常总数。`inc()` 递增，不能减少。
# 2. **Gauge**：仪表盘，**可增可减**。当前并发数、内存占用、队列长度。`set()` 直接赋值。
# 3. **Summary**：统计分位数（耗时分布），客户端本地计算。适合接口响应时间。
# 4. **Histogram**：直方图，桶（bucket）统计，推荐生产用，Prometheus 端算分位数。


# ═════════════════════════════════════════════════════════════════════════════
# §1 任务级指标
# ═════════════════════════════════════════════════════════════════════════════

ca_tasks_total = Counter(
    "ca_tasks_total",
    "竞品分析任务总数",
    ["status"],  # pending / running / completed / failed
)

# **直方图指标**，用来统计 Agent 任务执行耗时，
# 按 `route` 标签区分类型（`pipeline` / `supervisor`），
# 桶：`[1,5,10,30,60,120,300,600]` 秒，范围 1 秒～10 分钟。

ca_task_duration_seconds = Histogram(
    "ca_task_duration_seconds",
    "任务执行耗时（秒），按路由类型分桶",
    ["route"],  # pipeline / supervisor
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],  # 1s ~ 10min
)


# ═════════════════════════════════════════════════════════════════════════════
# §2 Agent 级指标
# ═════════════════════════════════════════════════════════════════════════════

ca_agent_calls_total = Counter(
    "ca_agent_calls_total",
    "Agent 调用次数",
    ["agent", "action", "status"],  # agent: collector/analyzer/writer/quality/supervisor
)                                   # status: success / error


# ═════════════════════════════════════════════════════════════════════════════
# §3 安全与限流指标（Phase 8 Harness + Phase 9 三层限流）
# ═════════════════════════════════════════════════════════════════════════════

ca_rate_limit_hits = Counter(
    "ca_rate_limit_hits",
    "限流触发次数",
    ["layer", "agent"],  # layer: global / agent / llm
)                        # agent: 空字符串表示全局层，否则为 agent 名

ca_harness_blocks = Counter(
    "ca_harness_blocks",
    "Harness 安全检查阻断次数",
    ["check_type"],  # whitelist / param_validation / rate_limit / pii
)


# ═════════════════════════════════════════════════════════════════════════════
# §3.5 续跑指标（2026-10-31）
# ═════════════════════════════════════════════════════════════════════════════

# 【为什么必须有这一项】「支持中断续跑」在没有计数时是一句无法证伪的话。
# 这个 Counter 让"续跑到底发生过几次、失败在哪一类"变成可查的数字：
#   accepted      → 真的从 checkpoint 接着跑了（配合 agent_logs 能验"没重复采集"）
#   no_checkpoint → 首个节点执行前就中断，压根没有断点（说明崩溃窗口在 collect 内）
#   finished      → 图已到终点，续跑无意义（多为前端重复点）
#   rejected      → 任务仍有日志写入，疑似还在跑（防双协程写同一 thread）
# ⚠️ no_checkpoint 占比高 = collect 节点的执行窗口太长，该往"节点内细分快照"走，
#   而不是继续加续跑重试。这是这个标签唯一真正的用法。
ca_task_resumes_total = Counter(
    "ca_task_resumes_total",
    "checkpoint 续跑请求次数",
    ["outcome"],
)


# ═════════════════════════════════════════════════════════════════════════════
# §3.6 工具超时对账（2026-10-31，亮点 A / A1）
# ═════════════════════════════════════════════════════════════════════════════

# 【为什么单独记】「超时后先对账再重试」这句话的价值全在 hit/miss 的比例上：
#   hit  → 工具有真实副作用（Tavily 已经扣了配额 / 页面已经抓过），回放省一次
#   miss → 超时的多半是"真没执行"，重试是对的
# 只有 miss 而没有 hit，说明超时点落在执行之前（比如连接阶段），
# 那时候对账这层代码是白跑的 —— 这个比例就是判断"值不值得留"的唯一依据。
ca_tool_reconcile_total = Counter(
    "ca_tool_reconcile_total",
    "工具调用超时后的对账结果",
    ["result"],  # hit / miss
)


# ═════════════════════════════════════════════════════════════════════════════
# §4 便捷采集函数（供 routes.py 和 harness/guard.py 调用）
# ═════════════════════════════════════════════════════════════════════════════

def record_task_created() -> None:
    """任务创建成功——POST /api/tasks 中调用。"""
    ca_tasks_total.labels(status="pending").inc()


def record_task_started() -> None:
    """任务开始执行——后台协程启动时调用。"""
    ca_tasks_total.labels(status="running").inc()


def record_task_completed(
    route: str, elapsed_seconds: float, *, observe_duration: bool = True
) -> None:
    """任务执行完成。

    Args:
        route: "pipeline" | "supervisor"
        elapsed_seconds: 从创建到完成的总耗时
        observe_duration: 是否把耗时记进直方图。
            ★ 续跑要传 False：续跑只跑了后半段，`elapsed_seconds` 是"剩余段耗时"
              而不是端到端耗时。混进同一个 Histogram 会让 ca_task_duration_seconds
              同时装着两种口径，P95 从此不可解释 —— 而曲线看不出自己脏了。
              完成计数照加（任务确实完成了），只是不参与耗时分布。
    """
    ca_tasks_total.labels(status="completed").inc()
    if observe_duration:
        ca_task_duration_seconds.labels(route=route).observe(elapsed_seconds)


def record_task_failed() -> None:
    """任务执行失败。"""
    ca_tasks_total.labels(status="failed").inc()


def record_agent_call(agent: str, action: str, success: bool) -> None:
    """记录一次 Agent 调用。

    Args:
        agent: collector / analyzer / writer / quality / supervisor
        action: web_search / web_fetch / embed_texts / grade_report 等
        success: True = success, False = error
    """
    status = "success" if success else "error"
    ca_agent_calls_total.labels(agent=agent, action=action, status=status).inc()


def record_rate_limit_hit(layer: str, agent: str = "") -> None:
    """记录一次限流触发。

    Args:
        layer: "global" / "agent" / "llm"
        agent: Agent 名（global 层为空）
    """
    ca_rate_limit_hits.labels(layer=layer, agent=agent).inc()


def record_harness_block(check_type: str) -> None:
    """记录一次 Harness 阻断。

    Args:
        check_type: whitelist / param_validation / rate_limit / pii
    """
    ca_harness_blocks.labels(check_type=check_type).inc()


def record_task_resumed(outcome: str) -> None:
    """记录一次续跑请求的结果。

    Args:
        outcome: completed / no_checkpoint / finished / rejected
    """
    ca_task_resumes_total.labels(outcome=outcome).inc()


def record_tool_reconcile(result: str) -> None:
    """记录一次工具超时对账的结果。

    Args:
        result: hit（查到成功结果，判定已执行并回放）/ miss（判定未执行，交回重试）
    """
    ca_tool_reconcile_total.labels(result=result).inc()

