"""Supervisor - ReAct 循环 + A2A 通信协议 + 代码路由。

Phase 5A: LangGraph StateGraph: think -> act -> observe -> route(条件边)。
Phase 5B: IntentRouter 代码路由（Pipeline vs Supervisor 分流）。
"""

from __future__ import annotations

from src.supervisor.a2a import AgentCard, A2ATask, TaskStatus, A2ARouter, create_agent_cards
from src.supervisor.state import SupervisorState
from src.supervisor.supervisor import (
    build_supervisor_graph,
    run_supervisor_task,
)
from src.supervisor.router import IntentRouter

__all__ = [
    "AgentCard",
    "A2ATask",
    "TaskStatus",
    "A2ARouter",
    "create_agent_cards",
    "SupervisorState",
    "build_supervisor_graph",
    "run_supervisor_task",
    "IntentRouter",
]