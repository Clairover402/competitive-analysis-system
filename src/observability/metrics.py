"""Prometheus 指标定义 — 竞品分析系统可观测性指标体系。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 面试 — 指标体系设计】
═══════════════════════════════════════════════════════════════════════════════

指标设计三原则：
  1. RED 模式（Rate, Errors, Duration）— 覆盖核心服务指标
  2. 每个 Counter 都有 status/type 标签 — 支持按维度下钻
  3. 指标采集在 HTTP 入口 + Harness 阻断点 — 不改动编排引擎

七项指标：
┌──────────────────────────────┬──────────┬─────────────────────────────────┐
│ 指标名                        │ 类型      │ 说明                             │
├──────────────────────────────┼──────────┼─────────────────────────────────┤
│ ca_tasks_total               │ Counter  │ 任务数按 status 分桶             │
│ ca_task_duration_seconds     │ Histogram│ 任务耗时按 route 分桶            │
│ ca_agent_calls_total         │ Counter  │ Agent 调用按 agent/action/status │
│ ca_quality_score             │ Histogram│ 质检分数分布                     │
│ ca_rag_recall                │ Gauge    │ RAG 召回率（当前值）             │
│ ca_rate_limit_hits           │ Counter  │ 限流触发按 layer 分              │
│ ca_harness_blocks            │ Counter  │ Harness 阻断按 check_type 分     │
└──────────────────────────────┴──────────┴─────────────────────────────────┘
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY


# ═════════════════════════════════════════════════════════════════════════════
# §1 任务级指标
# ═════════════════════════════════════════════════════════════════════════════

ca_tasks_total = Counter(
    "ca_tasks_total",
    "竞品分析任务总数",
    ["status"],  # pending / running / completed / failed
)

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

ca_quality_score = Histogram(
    "ca_quality_score",
    "质检分数分布",
    buckets=[0, 50, 60, 70, 75, 80, 85, 90, 95, 100],
)

ca_rag_recall = Gauge(
    "ca_rag_recall",
    "RAG 检索召回率（当前任务值）",
    ["dimension"],
)


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
# §4 便捷采集函数（供 routes.py 和 harness/guard.py 调用）
# ═════════════════════════════════════════════════════════════════════════════

def record_task_created() -> None:
    """任务创建成功——POST /api/tasks 中调用。"""
    ca_tasks_total.labels(status="pending").inc()


def record_task_started() -> None:
    """任务开始执行——后台协程启动时调用。"""
    ca_tasks_total.labels(status="running").inc()


def record_task_completed(route: str, elapsed_seconds: float) -> None:
    """任务执行完成。

    Args:
        route: "pipeline" | "supervisor"
        elapsed_seconds: 从创建到完成的总耗时
    """
    ca_tasks_total.labels(status="completed").inc()
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


def record_quality_score(score: float) -> None:
    """记录质检分数。"""
    ca_quality_score.observe(score)


def record_rag_recall(dimension: str, recall: float) -> None:
    """记录某维度的 RAG 召回率。

    ▸ 注意：Gauge 是"当前值"语义，每次 set 会覆盖。
    """
    ca_rag_recall.labels(dimension=dimension).set(recall)


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
