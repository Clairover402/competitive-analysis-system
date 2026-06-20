"""数据库模块 — asyncpg 连接池管理与数据访问层。
============================================================

【L4 工程架构】
src.db 是系统的"数据底座"——所有 Agent 通过 DAO 读写数据，
所有 Agent 共享同一个连接池（通过 create_pool() 创建的单例）。

连接池 → 7 个 DAO → 9 张表：
  TaskDAO    → tasks
  ReportDAO  → reports
  EvidenceDAO → evidence_map
  ChunkEmbeddingDAO → chunk_embeddings (向量检索)
  MemorySummaryDAO  → memory_summaries (短期摘要记忆)
  AgentMemoryDAO    → agent_memories (长期记忆+时间衰减)
  AgentLogDAO       → agent_logs (审计)

导出：
    连接池: create_pool / get_pool / close_pool
    DAO:    TaskDAO / ReportDAO / EvidenceDAO / ChunkEmbeddingDAO
            MemorySummaryDAO / AgentMemoryDAO / AgentLogDAO
"""

from src.db.connection import create_pool, get_pool, close_pool
from src.db.dao import (
    TaskDAO,
    ReportDAO,
    EvidenceDAO,
    ChunkEmbeddingDAO,
    MemorySummaryDAO,
    AgentMemoryDAO,
    AgentLogDAO,
)

__all__ = [
    "create_pool",
    "get_pool",
    "close_pool",
    "TaskDAO",
    "ReportDAO",
    "EvidenceDAO",
    "ChunkEmbeddingDAO",
    "MemorySummaryDAO",
    "AgentMemoryDAO",
    "AgentLogDAO",
]
