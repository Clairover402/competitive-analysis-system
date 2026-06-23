"""Supervisor 模块 — ReAct 循环 + A2A 通信协议。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Phase 5A 实现了竞品分析系统的"探索模式"控制器。当用户没说明竞品是谁时，
系统通过 ReAct 循环（think → act → observe → route）动态搜索、分析、写作。

  模块职责:
  ┌──────────────┬────────────────────────────────────────────────────────┐
  │ 文件         │ 职责                                                    │
  ├──────────────┼────────────────────────────────────────────────────────┤
  │ state.py     │ SupervisorState TypedDict（21 字段 + Annotated reducer）│
  │ a2a.py       │ AgentCard + A2ATask + A2ARouter + create_agent_cards   │
  │ supervisor.py│ 节点闭包工厂 + 条件路由 + build + run 入口              │
  │ __init__.py  │ 公开导出 8 个符号（本文件）                              │
  └──────────────┴────────────────────────────────────────────────────────┘

  外部使用示例:
  ────────────
  from src.supervisor import (
      build_supervisor_graph,    # 构建 + 编译 StateGraph
      run_supervisor_task,       # 一键执行入口
      A2ARouter,                 # 注册 Agent + 分发任务
      create_agent_cards,        # 预定义 4 张名片
  )

  # 1. 创建 A2A 路由器
  router = A2ARouter(mcp_server)
  cards = create_agent_cards()
  router.register(cards["collector"], collector_agent, llm_collector)
  # ...注册其余 3 个 Agent

  # 2. 运行 Supervisor
  result = await run_supervisor_task(
      task={"id": "uuid", "title": "AI 市场分析", "user_id": "u1"},
      mcp_server=mcp_server,
      pool=pool,
      router=router,
      llm_supervisor=llm_sup,
  )
  # result: {task_id, final_output, quality_score, is_complete}


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 __all__ 最小导出原则: 只导出 8 个符号，内部函数不暴露
  §2 模块级 docstring: 全景图 + 使用示例（让新开发者 30 秒看懂模块结构）
  §3 from ... import ... 集中管理: 本文件是所有 import 的"网关"
"""

from __future__ import annotations

from src.supervisor.a2a import (
    AgentCard,
    A2ATask,
    TaskStatus,
    A2ARouter,
    create_agent_cards,
)
from src.supervisor.state import SupervisorState
from src.supervisor.supervisor import build_supervisor_graph, run_supervisor_task

# ══════════════════════════════════════════════════════════════════════════════
# __all__ — 最小导出原则（只暴露外部使用符号）
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # A2A 协议 — 外部需要注册 Agent + 创建 Task
    "AgentCard",
    "A2ATask",
    "TaskStatus",
    "A2ARouter",
    "create_agent_cards",
    # Supervisor 状态 — 外部构造 initial_state 需要
    "SupervisorState",
    # Supervisor 引擎 — 外部入口
    "build_supervisor_graph",
    "run_supervisor_task",
]
"""不导出内部函数: _make_node_think, _make_node_act, _make_node_observe,
_extract_json, _extract_memory, route_after_observe —
这些是 Supervisor 实现细节，外部不应直接依赖。
"""
