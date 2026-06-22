"""PostgresSaver — LangGraph Checkpoint 的 PostgreSQL 持久化实现。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Checkpoint 是 LangGraph 的"断点续传"机制。每次图执行到某个节点后，
LangGraph 自动把当前 AgentState 存为 Checkpoint。如果执行中断（崩溃/超时），
下次用同一个 thread_id 调用 ainvoke()，LangGraph 从最近的 Checkpoint 恢复。

  graph.ainvoke(state, config)  ← config 中包含 thread_id
      │
      ├── [collect]  → PostgresSaver.aput()      写入 checkpoint
      │   [analyze]  → PostgresSaver.aput()      写入 checkpoint
      │   [write]    → PostgresSaver.aput()      写入 checkpoint
      │   [quality]  → PostgresSaver.aput()      写入 checkpoint
      │   [finalize] → PostgresSaver.aput()      写入 checkpoint
      │
      └── 中断 → graph.ainvoke(state, 同一个 thread_id) → aget_tuple() 恢复

PostgresSaver 继承 BaseCheckpointSaver，提供 PostgreSQL 持久化。
每个线程的状态存在 checkpoints 表中，pending writes 存在 checkpoint_writes 表中。


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 BaseCheckpointSaver 继承: 必须实现的 3 异步 + 3 同步方法
  §2 aget_tuple: LangGraph 恢复执行时的入口（按 thread_id 查最近 checkpoint）
  §3 aput: 每个节点执行后自动调用（保存 AgentState 快照）
  §4 aput_writes: 节点间 Pending Writes 的持久化
  §5 表结构设计: JSONB 列 + 复合主键 + ON CONFLICT upsert


═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — 持久化策略】
═══════════════════════════════════════════════════════════════════════════════

  操作              LangGraph 调用时机                PostgresSaver 方法
  ──────────────── ───────────────────────────────  ─────────────────────
  读 checkpoint    图恢复执行时，查找最近快照          aget_tuple()
  写 checkpoint    每个节点执行完毕后                  aput()
  写 pending writes 每次状态写入后                     aput_writes()
  建表              graph.compile(checkpointer=saver)  setup()

  【L4 工程】为什么不使用 LangGraph 自带的 SqliteSaver/PostgresSaver？
  ──────────────────────────────────────────────────────────────────
  LangGraph 官方提供的 AsyncPostgresSaver 依赖 psycopg（不是 asyncpg）。
  这个项目统一用 asyncpg（create_pool 返回 asyncpg.Pool），
  所以自建 PostgresSaver 适配统一连接池，避免引入第二个数据库驱动。

  【L4 工程】为什么 ON CONFLICT ... DO UPDATE 而不是先 DELETE 再 INSERT？
  ──────────────────────────────────────────────────────────────────────
  先 DELETE 再 INSERT = 两次数据库往返 + 锁竞争。
  ON CONFLICT upsert = 一次原子操作，如果冲突就更新，不冲突就插入。
  在 checkpoint 场景中，同一个 thread_id+checkpoint_id 可能被多次写入
  （如重写循环中 checkpointer 回到 write 节点再存），upsert 保证幂等。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

import asyncpg
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
)
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


class PostgresSaver(BaseCheckpointSaver):
    """PostgreSQL Checkpoint 存储 — 最小可行实现。

    【L3 核心考点】BaseCheckpointSaver 的继承契约
    ─────────────────────────────────────────────
    必须实现 6 个方法（3 异步 + 3 同步）：
      异步: aget_tuple / aput / aput_writes
      同步: get_tuple / put / put_writes（抛 NotImplementedError）
    
    LangGraph 1.0 使用的 async 调用路径，所以只实现异步方法就够。
    同步方法保留抛 NotImplementedError，代码意图明确：禁止同步调用。

    【L5 决策】为什么继承 BaseCheckpointSaver 而非从零写？
    ───────────────────────────────────────────────────
    LangGraph 的 graph.compile(checkpointer=saver) 要求 saver 必须是
    BaseCheckpointSaver 的子类实例。继承它 = 编译检查通过。
    从零写一个兼容的 Checkpointer 也行，但要多写适配层。

    用法:
        saver = PostgresSaver(pool)
        await saver.setup()           # 确保表存在
        graph = StateGraph(...).compile(checkpointer=saver)
        await graph.ainvoke(state, {"configurable": {"thread_id": "task-123"}})
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        """初始化 PostgresSaver。

        【L4 工程】为什么接收 pool 而不是创建自己的连接？
        ───────────────────────────────────────────────
        连接池是稀缺资源。如果 PostgresSaver 自己创建连接池，
        每个 saver 实例都有一个独立池 → 连接数膨胀 → 数据库压力。
        外部传入共享 pool → 所有模块复用同一个连接池 → 连接数可控。

        Args:
            pool: asyncpg 连接池（由 create_pool() 创建，外部传入）
        """
        super().__init__()
        self._pool = pool

    async def setup(self) -> None:
        """确保 checkpoints 和 checkpoint_writes 表存在（幂等）。

        【L4 工程】IF NOT EXISTS 的幂等保障
        ─────────────────────────────────
        多次调用 setup() 不会报错——表已存在就跳过。
        这意味着：
          — 服务重启时 setup() 可以安全地重复执行
          — 不依赖 DBA 手动建表（自包含部署）
          — schema.sql 和 setup() 双重保障（无论哪个先执行，最终表都存在）

        【L4 工程】表结构与 schema.sql 保持同步
        ──────────────────────────────────────
        两处定义相同表结构：
          — schema.sql：DBA 手动部署时使用
          — setup()：代码自动建表
        如果改表结构，两处必须同步更新。
        checkpoints 表含 type/metadata/checkpoint(JSONB) 列，
        checkpoint_writes 表含 channel/type/value 列。
        """
        async with self._pool.acquire() as conn:
            # checkpoints 表：每个线程的每次快照
            # 【L3 核心考点】复合主键 (thread_id, checkpoint_ns, checkpoint_id)
            # thread_id = 任务 ID（如 task-123）
            # checkpoint_ns = 命名空间（默认空字符串，多图场景用）
            # checkpoint_id = 快照 ID（LangGraph 自动生成的 UUID）
            # 三元组唯一确定一个 checkpoint
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id           TEXT NOT NULL,
                    checkpoint_ns       TEXT NOT NULL DEFAULT "",
                    checkpoint_id       TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    type                TEXT,
                    checkpoint          JSONB NOT NULL,
                    metadata            JSONB NOT NULL DEFAULT "{}",
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                )"""
            )
            # checkpoint_writes 表：节点的 Pending Writes
            # 【L3 核心考点】Pending Writes 是什么？
            # 节点执行期间的中间写入，在 checkpoint 确认前暂存。
            # 如果节点执行到一半崩溃了，pending writes 不会被合并到状态中，
            # 下次恢复会跳过这个不完整的节点。
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoint_writes (
                    thread_id       TEXT NOT NULL,
                    checkpoint_ns   TEXT NOT NULL DEFAULT "",
                    checkpoint_id   TEXT NOT NULL,
                    task_id         TEXT NOT NULL,
                    idx             INT NOT NULL,
                    channel         TEXT NOT NULL,
                    type            TEXT,
                    value           JSONB,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                )"""
            )

    # ══════════════════════════════════════════════════════════════════════
    # 异步 API（LangGraph 调用入口）
    # ══════════════════════════════════════════════════════════════════════

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """获取最近的 checkpoint + 关联的 pending writes。

        【L3 核心考点】LangGraph 何时调用 aget_tuple？
        ────────────────────────────────────────────
        graph.ainvoke(state, config) 执行前，LangGraph 先调 aget_tuple(config)，
        检查该 thread_id 是否有已保存的 checkpoint：
          — 有 → 从 checkpoint 恢复状态（断点续传）
          — 无 → 用传入的 state 作为初始状态（首次执行）

        【L3 核心考点】config 的结构
        ──────────────────────────
        config = {
            "configurable": {
                "thread_id": "task-uuid",          # 必填，任务唯一ID
                "checkpoint_ns": "",               # 命名空间（默认空）
                "checkpoint_id": "uuid-xxx"        # 可选，指定具体 checkpoint
            }
        }

        如果指定了 checkpoint_id → 查询精确匹配的快照
        如果没指定 → 查询该 thread 的最新快照（ORDER BY checkpoint_id DESC LIMIT 1）

        Args:
            config: LangGraph 的 RunnableConfig（含 thread_id）

        Returns:
            CheckpointTuple（含 checkpoint + metadata + pending_writes）
            或 None（首次执行，无历史 checkpoint）
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")

        async with self._pool.acquire() as conn:
            if checkpoint_id:
                # 【L4 工程】精确查询模式：上级传了具体的 checkpoint_id
                # 用于回退到特定快照（不是常见路径，但 LangGraph 协议支持）
                row = await conn.fetchrow(
                    """SELECT thread_id, checkpoint_ns, checkpoint_id,
                              parent_checkpoint_id, type, checkpoint, metadata
                       FROM checkpoints
                       WHERE thread_id = $1 AND checkpoint_ns = $2
                         AND checkpoint_id = $3""",
                    thread_id, checkpoint_ns, checkpoint_id,
                )
            else:
                # 【L4 工程】常见路径：查询最新 checkpoint
                # ORDER BY checkpoint_id DESC LIMIT 1 → 取最新的快照
                # 注意：checkpoint_id 是 LangGraph 生成的 UUID，按字典序排列，
                # 但不保证时间顺序。LangGraph 内部保证 checkpoint_id 的因果链。
                row = await conn.fetchrow(
                    """SELECT thread_id, checkpoint_ns, checkpoint_id,
                              parent_checkpoint_id, type, checkpoint, metadata
                       FROM checkpoints
                       WHERE thread_id = $1 AND checkpoint_ns = $2
                       ORDER BY checkpoint_id DESC LIMIT 1""",
                    thread_id, checkpoint_ns,
                )

            if row is None:
                return None  # 首次执行，无历史记录

            # JSONB → Python dict
            checkpoint = json.loads(row["checkpoint"])
            metadata = json.loads(row["metadata"])

            # 【L3 核心考点】parent_config：checkpoint 的链表指针
            # 每个 checkpoint 记录其父 checkpoint 的 config，
            # 形成 checkpoint_id 链表。LangGraph 可以沿着链表回溯历史状态。
            parent_config: RunnableConfig | None = None
            if row["parent_checkpoint_id"]:
                parent_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": row["parent_checkpoint_id"],
                    }
                }

            config_out: RunnableConfig = {
                "configurable": {
                    "thread_id": row["thread_id"],
                    "checkpoint_ns": row["checkpoint_ns"],
                    "checkpoint_id": row["checkpoint_id"],
                }
            }

            # ─── 查询 Pending Writes ───
            # 【L3 核心考点】pending writes 的作用
            # 节点执行完成后，LangGraph 先写 checkpoint_writes，
            # 确认后再写 checkpoints。如果节点中断（崩溃），
            # aget_tuple 会返回 pending_writes 但 checkpoint 是父节点的快照，
            # LangGraph 据此知道"上一个节点未完成"，可以重试。

            """
            # aput_writes: 节点执行中 LangGraph 调它暂存中间写入
            async def aput_writes(self, config, writes, task_id):
                
            
            # aput: 节点完全执行完毕后 LangGraph 调它写 checkpoint
            # 此时 writes 已经被 LangGraph 内部合并到 checkpoint 中
            async def aput(self, config, checkpoint, metadata, new_versions):
          
            # aget_tuple: 恢复执行时 LangGraph 调它查状态
            # 如果有 pending writes 但 checkpoint 是旧的 → LangGraph 知道节点未完成 → 重试
            async def aget_tuple(self, config):
              
            """

            writes_rows = await conn.fetch(
                """SELECT task_id, idx, channel, type, value
                   FROM checkpoint_writes
                   WHERE thread_id = $1 AND checkpoint_ns = $2
                     AND checkpoint_id = $3
                   ORDER BY task_id, idx""",
                thread_id, checkpoint_ns, row["checkpoint_id"],
            )

            pending_writes: list[tuple[str, str, Any]] = []
            for w in writes_rows:
                pending_writes.append((w["task_id"], w["channel"], w["value"]))

            return CheckpointTuple(
                config=config_out,
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=parent_config,
                pending_writes=pending_writes if pending_writes else None,
            )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """保存 checkpoint 快照。

        【L3 核心考点】LangGraph 何时调用 aput？
        ──────────────────────────────────────
        每个节点执行完毕后，LangGraph 自动调用 aput() 保存当前状态快照。
        调用顺序: 节点执行 → aput_writes(中间写入) → aput(快照) → 下一个节点

        【L4 工程】ON CONFLICT DO UPDATE — upsert 幂等保障
        ────────────────────────────────────────────────
        同一个 (thread_id, checkpoint_ns, checkpoint_id) 可能被多次写入：
          — 节点重试
          — 分布式环境下多个 worker 同时尝试写
        upsert 保证：如果存在就更新，不存在就插入。
        更新时用 EXCLUDED.xxx 引用 INSERT 语句中的新值。

        【L4 工程】json.dumps(default=str) 的作用
        ──────────────────────────────────────
        checkpoint 可能包含非 JSON 原生的对象（如 datetime、UUID）。
        default=str 不是最佳实践，而是兜底策略——
        正常情况不会走到这里，但如果某个值无法 JSON 序列化，
        至少不会让整个 checkpoint 写入失败（降级为字符串存储）。

        Args:
            config: 当前 config（含 thread_id/checkpoint_id）
            checkpoint: 状态快照（AgentState 的 dict 形式）
            metadata: checkpoint 元数据
            new_versions: Channel 版本号（由 LangGraph 管理，此实现暂不使用）

        Returns:
            包含 checkpoint_id 的 RunnableConfig
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")

        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO checkpoints
                       (thread_id, checkpoint_ns, checkpoint_id,
                        parent_checkpoint_id, type, checkpoint, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                   ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id)
                   DO UPDATE SET
                       parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
                       type = EXCLUDED.type,
                       checkpoint = EXCLUDED.checkpoint,
                       metadata = EXCLUDED.metadata""",
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                parent_checkpoint_id,
                "checkpoint",                #  type: #固定值标识这是 checkpoint 记录
                json.dumps(checkpoint, default=str),  # JSONB 列需要 json.dumps
                json.dumps(metadata, default=str),
            )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """保存 pending writes（节点执行期间的中间写入）。

        【L3 核心考点】LangGraph 何时调用 aput_writes？
        ─────────────────────────────────────────────
        节点执行期间，LangGraph 把每个中间写入（如状态更新）暂存为 pending write。
        节点执行完毕后，pending writes 被合并到状态中，然后写入 checkpoint。
        如果节点在中途崩溃，pending writes 不会被合并，下回恢复时跳过这个节点重试。

        【L4 工程】Prepared Statement（stmt.prepare）
        ────────────────────────────────────────────
        循环中多次执行相同的 SQL → 用 prepare 预编译一次 → 循环内复用。
        每次 execute 都需要 SQL 解析和计划生成，而 prepared statement 只做一次。
        在 writes 数量较大时（如 10+ 条），这能省掉多次解析开销。

        【L4 工程】ON CONFLICT upsert 的幂等性
        ──────────────────────────────────────
        同一个 (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        可能被多次写入 → upsert 覆盖旧值，保证只保留最新的 write。

        Args:
            config: 当前 config
            writes: [(channel_name, value), ...]，每个元素是一个中间写入
            task_id: 节点执行的 task_id（LangGraph 内部生成）
            task_path: 任务路径（通常为空字符串）
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        async with self._pool.acquire() as conn:
            # 预编译 INSERT 语句
            stmt = await conn.prepare(
                """INSERT INTO checkpoint_writes
                       (thread_id, checkpoint_ns, checkpoint_id,
                        task_id, idx, channel, type, value)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                   ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id,
                                task_id, idx)
                   DO UPDATE SET
                       channel = EXCLUDED.channel,
                       type = EXCLUDED.type,
                       value = EXCLUDED.value"""
            )

            # 【L4 工程】遍历 writes 并批量插入
            # enumerate 生成 idx（0, 1, 2, ...），表示写入顺序
            # channel: 状态字段名（如 "report_content"）
            # type: #value 的 Python 类型名（如 "str", "dict"），用于调试
            # value: JSON 序列化后的值
            for idx, (channel, value) in enumerate(writes):
                await stmt.fetch(
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    task_id,
                    idx,
                    channel,
                    type(value).__name__ if value is not None else None,
                    json.dumps(value, default=str) if value is not None else None,
                )

    # ══════════════════════════════════════════════════════════════════════
    # 同步包装（BaseCheckpointSaver 要求实现，但本项目只用异步路径）
    # ══════════════════════════════════════════════════════════════════════

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """同步版本 — 禁止调用。本系统使用异步路径 aget_tuple()。

        【L4 工程】为什么不实现同步版本？
        ──────────────────────────────
        整个系统都在 asyncio 事件循环中运行，所有数据库操作都是异步的。
        同步版本需要额外的线程池或 run_until_complete 包装，增加复杂度。
        直接抛 NotImplementedError 表明"这不是 bug，是设计约束"。
        """
        raise NotImplementedError("使用异步接口 aget_tuple")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """同步版本 — 禁止调用。"""
        raise NotImplementedError("使用异步接口 aput")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """同步版本 — 禁止调用。"""
        raise NotImplementedError("使用异步接口 aput_writes")
