"""Pipeline 模块 — LangGraph 编排层。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Pipeline 层是竞品分析系统的"指挥中心"——它不采集数据、不做分析，
只负责把 Phase 3 的四个 Agent 串成一条自动化流水线。

                      ┌─────── 调用方 ───────┐
                      │ run_pipeline_task()   │
                      └──────────┬───────────┘
                                 │
                      ┌──────────▼───────────┐
                      │ build_pipeline_graph()│  ← 编译一次，invoke 多次
                      └──────────┬───────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
    ┌─────────▼─────┐  ┌────────▼──────┐  ┌────────▼─────────┐
    │  AgentState   │  │  PostgresSaver │  │  CompiledGraph   │
    │  (共享状态)    │  │  (断点续传)    │  │  (可执行图)       │
    └───────────────┘  └───────────────┘  └──────────────────┘

三个模块的职责：
  state.py      → 定义"管道中流动什么数据"（AgentState TypedDict）
  graph.py      → 定义"数据怎么流动"（StateGraph 节点+边+条件边）
  checkpoint.py → 定义"管道中断后怎么恢复"（PostgreSQL 持久化）

【L5 决策】为什么将 Pipeline 和 Agent 拆为两个层？
────────────────────────────────────────────────
  Agent 层（Phase 3）: 四个专精 Agent，各自独立，只关心"我的输入→我的输出"
  Pipeline 层（Phase 4）: 编排引擎，只关心"谁先谁后、条件怎么判断"

  分离的好处：
    ① 测试隔离：可以单独测试 collector_agent（mock task + mcp_server），
       不需要启动整个 Pipeline。
    ② 策略切换：如果将来要换成 Supervisor 模式，只需改 graph.py 的编排逻辑，
       Agent 层代码零修改。
    ③ 职责单一：Agent 不关心"下一步是什么"，Pipeline 不关心"Agent 内部怎么做"。

用法:
    from src.pipeline import run_pipeline_task

    result = await run_pipeline_task({
        "id": "uuid",
        "title": "飞书 vs 钉钉 竞品分析",
        "competitors": ["飞书", "钉钉"],
        "dimensions": ["定价", "功能", "用户体验"],
    })
    # result: {task_id, final_report, quality_score}


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 AgentState: TypedDict + Annotated reducer → 共享状态定义
  §2 StateGraph: add_node + add_edge + add_conditional_edges + compile
  §3 Checkpoint: BaseCheckpointSaver 继承 → aget_tuple / aput / aput_writes
  §4 条件边: route_after_quality 路由（过→finalize，不过→write循环）
  §5 remaining_steps: 循环防死锁（write 节点递减）


═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — 模块依赖图】
═══════════════════════════════════════════════════════════════════════════════

  run_pipeline_task()  ← 外部调用入口
      │
      ├── Settings()                    (config.py)
      ├── create_mcp_server(settings)   (mcp/__init__.py)
      ├── create_pool(settings)         (db/connection.py)
      │
      └── build_pipeline_graph(mcp, pool)
              │
              ├── create_llm_client() ×4  (agents/__init__.py)
              ├── PostgresSaver(pool)      (pipeline/checkpoint.py)
              │
              ├── AgentState              (pipeline/state.py)
              │
              ├── _make_node_collect()    → collector_agent  (agents/collector.py)
              ├── _make_node_analyze()    → analyzer_agent   (agents/analyzer.py)
              ├── _make_node_write()      → writer_agent     (agents/writer.py)
              ├── _make_node_quality()    → quality_agent    (agents/quality.py)
              ├── _make_node_finalize()   → TaskDAO           (db/dao.py)
              │
              └── route_after_quality()  (条件路由)
"""

from __future__ import annotations

from src.pipeline.state import AgentState
from src.pipeline.graph import build_pipeline_graph, run_pipeline_task
from src.pipeline.checkpoint import PostgresSaver

# 【L4 工程】__all__ 导出——模块的"路由表"
# 显式声明公开接口，IDE 可以自动补全，import * 不会漏导。
# 外部只需要这 4 个符号，不需要知道内部实现细节。
__all__ = [
    "AgentState",
    "build_pipeline_graph",
    "run_pipeline_task",
    "PostgresSaver",
]
