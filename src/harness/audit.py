"""AuditLogger — 结构化审计日志（Harness Engineering 审计层）。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

  A2ARouter.send_task() 第 ②.5 步
       │
       ├── HarnessGuard.guard() — 五层检查
       │       │
       │       ├── check_whitelist()         ──┐
       │       ├── validate_params()          │ AuditLogger.log() ←
       │       ├── check_rate_limit()         │ 通过和拦截都写
       │       ├── scan_for_pii()             │ fire-and-forget
       │       └── 都结束后                    │ ↓
       │              └── AuditLogger.log()   ──┘ INSERT INTO agent_logs
       │                                           (task_id, agent_name, action,
       │                                            request, response, error,
       │                                            duration_ms)
       │
       └── Phase 6 Dashboard 查询
              └── AuditLogger.get_task_trail(task_id)
                    └── SELECT * FROM agent_logs WHERE task_id=$1 ORDER BY created_at ASC
                          → 完整的 Agent 调用时间线（每一步谁做了什么、花了多久、出没出错）

  【L5 决策】AuditLogger 为什么是一层独立包装？
  DAO 只管 SQL INSERT，AuditLogger 管"什么时候记、记什么、失败怎么办"。
  未来可扩展（不改调用方）：
    — 采样策略（只记 10% 通过日志，拦截日志 100%）
    — 敏感字段脱敏（api_key → "***"）
    — 异步批量写入（攒 50 条一起 insert）


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 AuditLogger.__init__()  — DAO 注入 + 资源管理
  §2 AuditLogger.log()       — fire-and-forget 写入（try/except 兜底）
  §3 AuditLogger.get_task_trail() — 完整调用链查询（按时间升序）


═══════════════════════════════════════════════════════════════════════════════
                    【L4 工程 — 数据流向一览】
═══════════════════════════════════════════════════════════════════════════════

  数据                       来源                      存储                      查询
  ───────────────────────  ───────────────────────  ──────────────────────  ──────────
  task_id                   HarnessGuard.guard()     agent_logs.task_id      WHERE task_id=$1
  agent_name / action       HarnessGuard.guard()     agent_logs.agent_name   —
  request (JSONB)           HarnessGuard.guard()     agent_logs.request      审计回溯
  response (JSONB)          HarnessGuard.guard()     agent_logs.response     审计回溯
  error (TEXT)              HarnessGuard.guard()     agent_logs.error        WHERE error IS NOT NULL
  duration_ms (FLOAT)       HarnessGuard.guard()     agent_logs.duration_ms  ORDER BY duration_ms DESC
  created_at (TIMESTAMPTZ)  DEFAULT NOW()            agent_logs.created_at   ORDER BY created_at ASC
"""

from __future__ import annotations

import logging

from src.db.dao import AgentLogDAO

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# §1 AuditLogger — 审计日志记录器
# ═════════════════════════════════════════════════════════════════════════════

class AuditLogger:
    """审计日志记录器 — 包装 AgentLogDAO，提供业务语义。

    ▎【L4 工程】为什么需要 AuditLogger 而不是直接在 HarnessGuard 里调 DAO？

    ┌──────────────────┬─────────────────────────────────────┐
    │ 直接调 DAO          │ 用 AuditLogger 包装                  │
    ├──────────────────┼─────────────────────────────────────┤
    │ harness 知道 SQL   │ harness 只关心"记录这个事件"        │
    │ harness 管 try/catch│ AuditLogger 统一管容错              │
    │ 改表名要改 harness  │ DAO 内部改，AuditLogger 接口不变   │
    │ 加脱敏要改 harness  │ AuditLogger.log() 内部加脱敏       │
    └──────────────────┴─────────────────────────────────────┘

    类比：DAO = JDBC Connection → AuditLogger = SLF4J。
    业务代码不该知道怎么写 SQL，只该声明"我要记日志"。

    ▎方法：

    | 方法 | 功能 | 容错 |
    |------|------|------|
    | `log(event)` | 写入一条审计日志 | try/except，失败不抛异常 |
    | `get_task_trail(task_id)` | 查询完整调用链 | try/except，失败返回空列表 |
    """

    def __init__(self, pool) -> None:
        """注入连接池。

        【L4 工程】pool 由 HarnessGuard 在初始化时传入。
        AuditLogger 自己创建 AgentLogDAO —— HarnessGuard 不知道 DAO 的存在。

        Args:
            pool: asyncpg.Pool（由 HarnessGuard.__init__ 传入）
        """
        # 【L4 工程】DAO 持有 pool，每次调用 acquire() → 用完 release()
        # asyncpg 连接池自动管理连接生命周期
        self._dao = AgentLogDAO(pool)

    async def log(self, event: dict) -> None:
        """写入一条审计日志到 agent_logs 表。

        【L5 决策】fire-and-forget：不返回值，不抛异常。

        为什么？审计日志是辅助链路，不能因为日志写入失败而阻断主业务。
        try/except 捕获所有异常后只打一句 logger.exception——
        类比飞机的黑匣子：记录一切但不影响飞行。

        ▎执行步骤：

        | 步骤 | 做什么 | 说明 |
        |:--:|------|------|
        | 1 | 从 event 提取所有字段 | 缺字段用默认值兜底 |
        | 2 | `await self._dao.log(...)` | asyncpg INSERT |
        | 3 | `except Exception` → logger.exception | 不抛异常 |

        Args:
            event: {
                task_id: str,           # 关联任务 ID
                agent_name: str,        # 被调用的 Agent 名称
                action: str,            # 执行的 action
                request: dict | None,   # 调用参数（JSONB）
                response: dict | None,  # 返回结果（JSONB）
                error: str | None,      # 错误信息（拦截时为 "WHITELIST_DENIED" 等）
                duration_ms: float | None, # 检查耗时（毫秒）
            }
        """
        try:
            # 【L4 工程】对缺失字段做默认值处理
            # agent_name 默认为 "harness"（非 Agent 调用的系统级日志）
            # action 默认为 "guard"（标注来自安全检查中间件）
            await self._dao.log(
                task_id=event.get("task_id", ""),
                agent_name=event.get("agent_name", "harness"),
                action=event.get("action", "guard"),
                request=event.get("request"),
                response=event.get("response"),
                error=event.get("error"),
                duration_ms=event.get("duration_ms"),
            )
        except Exception:
            # 【L5 决策】记自己的失败日志，但不往上抛
            # 如果连 logger.exception 也失败了（几乎不可能），静默吞掉
            logger.exception("审计日志写入失败")

    async def get_task_trail(self, task_id: str) -> list[dict]:
        """查询完整调用链日志（按时间升序）。

        【L4 工程】Phase 6 可观测性展示：
        — Dashboard 用此方法画 Agent 调用时间线
        — 质检 Agent 复盘："哪一步花了最长时间？哪一步出错？"
        — 故障排查："collector 为什么被拦截了？" → 查 agent_logs.error

        ▎执行步骤：

        | 步骤 | 做什么 | 说明 |
        |:--:|------|------|
        | 1 | `self._dao.get_by_task(task_id)` | SELECT * FROM agent_logs ORDER BY created_at ASC |
        | 2 | 返回结果列表 | Python 原生 dict（asyncpg 已转换） |
        | 3 | 数据库异常 → 返回空列表 | 不抛异常，调用方检查空列表即可 |

        Args:
            task_id: 任务 UUID（如 "abc-123-def"）

        Returns:
            [
                {
                    task_id, agent_name, action,
                    request: dict,   # JSONB → Python dict
                    response: dict,  # JSONB → Python dict
                    error: str | None,
                    duration_ms: float,
                    created_at: datetime,
                },
                ...
            ]
            按 created_at 升序排列 → 直接就是"时间线"
            失败时返回空列表 []
        """
        try:
            return await self._dao.get_by_task(task_id)
        except Exception:
            logger.exception("查询调用链失败: task_id=%s", task_id)
            return []
