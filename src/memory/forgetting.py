"""MemoryForgetting — 三层遗忘策略。

═══════════════════════════════════════════════════════════════════════════════
                          【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 三层遗忘: 自然衰减 / 定期归档 / 显式删除
  §2 JVM GC 类比: Minor GC（衰减）/ Survivor→Old Gen（归档）/ Full GC（删除）
  §3 软删除设计: is_active=false 代替物理 DELETE


═══════════════════════════════════════════════════════════════════════════════
                     【L4 工程 — 三层策略详解】
═══════════════════════════════════════════════════════════════════════════════

策略 1: 自然衰减 — SQL 层自动处理（无需代码触发）
  ───────────────────────────────────────────
  衰减公式已嵌入 AgentMemoryDAO.similarity_search 的 ORDER BY:
    ORDER BY (
      similarity * 0.6
      + importance * 0.3
      + POWER(0.5, days / half_life_days) * 0.1  ← 时间衰减项
    ) DESC

  POWER(0.5, days / half_life_days) 的行为（等价于 e^(-λ·days)，λ=ln2/half_life）:
    第 0 天:  权重 = 1.0（新记忆全权重）
    第 t 天:  权重 = e^(-t/half_life)（指数衰减）
    第 2t 天: 权重 = e^(-2) ≈ 0.135（权重降到 13.5%）
    第 3t 天: 权重 = e^(-3) ≈ 0.05（几乎贡献为零）

  关键点: 不主动删除——只是搜索时旧记忆排序下沉。
  类比: 搜索引擎的旧网页不是删了，只是排在后面。

  为什么用 EXP 衰减而不是线性衰减？
    — 线性（0.5^(days/half_life)）在第 3 个半衰期就衰减到 12.5%
    — EXP 在第 3 个半衰期才到 5%——给重要记忆更长的"存活期"
    — EXP 曲线在前期更平滑（第 1 个半衰期仅衰减到 37%，线性是 50%）

策略 2: 定期归档 — 180 天未访问记忆标记为不活跃
  ─────────────────────────────────────────
  触发: cron 调度（Phase 6 实现），每天凌晨 3 点执行
  执行: `UPDATE agent_memories SET is_active = false WHERE created_at < NOW() - 180 days`
  保护: `WHERE memory_type != 'decision'` — 关键决策永不过期

  为什么归档而不删除？
    — 审计: 旧记忆是决策历史的证据
    — 恢复: 发现归档错了可以 undo（SET is_active = true）
    — 合规: 某些行业要求保留 2 年以上业务决策记录

  180 天的依据:
    — 产品迭代通常在半年内有明显变化（定价/功能/策略没变那就没分析价值）
    — 超过半年没被检索的记忆几乎不会再被检索
    — 不删除 decision 类是因为关键决策的"背景信息"有长期参考价值

策略 3: 显式删除 — 用户主动触发（软删除）
  ─────────────────────────────────────
  触发: 用户操作（"忘记这条信息"）
  执行: `UPDATE agent_memories SET is_active = false WHERE id = $1`
  保护: 不物理删除数据——软删除 = 可恢复的删除

  软删除 vs 硬删除:
    硬删除: DELETE FROM agent_memories WHERE id = $1  → 数据永远消失
    软删除: UPDATE agent_memories SET is_active = false → 数据还在，查不到而已
    工程价值: 用户说"忘不掉"时，undelete 只需 SET is_active = true


═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — 为什么不内置 cron 调度】
═══════════════════════════════════════════════════════════════════════════════

Phase 4.5 只写逻辑，不写调度。原因:
  1. 调度器选型不是记忆模块的事: APScheduler vs Celery vs cron 取决于整体架构
  2. 调度频率不硬编码: 归档 180 天、衰减 SQL 实时生效、删除人工触发——
     这三个时间维度完全不同，统一调度器反而复杂
  3. 职责分离: MemoryForgetting 负责"怎么忘"→ Phase 6 服务层负责"什么时候忘"

Phase 6 的集成方案（计划）:
  — APScheduler 每天凌晨 3 点调 archive_old_memories()
  — 自然衰减不需要调度（SQL ORDER BY 自动生效）
  — explicit_forget 走 HTTP DELETE /api/memories/{id} 手动触发

JVM GC 类比说明:
  ┌──────────────────────────────────────────────────────────────────┐
  │ JVM 堆            │ 对应记忆系统的操作             │ 说明          │
  ├──────────────────────────────────────────────────────────────────┤
  │ Minor GC          │ natural_decay() — 自动清除 Eden 区    │ 高频，无代码  │
  │ Survivor→Old Gen  │ archive_old_memories() — 归档到旧代   │ 低频，cron   │
  │ Full GC           │ explicit_forget() — 用户手动触发      │ 极低频，手动  │
  └──────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from uuid import UUID

from src.db.dao import AgentMemoryDAO

logger = logging.getLogger(__name__)


class MemoryForgetting:
    """遗忘策略管理器——三种策略，均软删除，不物理破坏数据。

    【L3 核心概念】遗忘的三种时间维度
    ────────────────────────────
      — natural_decay: 自动的、持续发生的、无需代码触发的（SQL 层实时衰减）
      — archive_old_memories: 定期的、批量的、由调度器触发的（每天凌晨打扫一次）
      — explicit_forget: 手动的、精确的、由用户触发的（删除这一条，不是一批）

    【L4 工程】三种策略的关系
    ─────────────────────
      自然衰减是最底层机制——所有记忆都在 SQL 层自动衰减。
      归档是中层清理——把已经衰减到几乎检索不到的旧记忆标记为不活跃。
      显式删除是最上层——用户指定删除某一条。

      三者不互相依赖，各自独立演化。

    用法:
        forgetter = MemoryForgetting(dao)
        archived = await forgetter.archive_old_memories(days=180)  # cron 触发
        await forgetter.explicit_forget(memory_id)                 # 用户触发
    """

    def __init__(self, dao: AgentMemoryDAO) -> None:
        """初始化遗忘管理器。

        Args:
            dao: agent_memories 表的 DAO 层

        【L5 决策】为什么只注入 DAO 而不注入 LLM？
        ───────────────────────────────────
          遗忘判断是确定性的——超期天数、memory_type 匹配——
          不需要 LLM 判断。这是纯规则逻辑，没有语义歧义。
          DAO 的 archive_old() 是纯 SQL（WHERE created_at < NOW() - $1），
          比 LLM 判断"这条该不该删"快 1000 倍且不会出错。
        """
        self._dao = dao

    async def natural_decay(self) -> None:
        """策略1: 自然衰减——SQL 层自动处理，无需代码操作。

        【L3 核心概念】为什么这个方法是空函数？
        ──────────────────────────────────
          衰减公式已嵌入 AgentMemoryDAO.similarity_search 的 ORDER BY 子句:
            加权分 = (1-cosine)*0.6 + importance*0.3 + POWER(0.5,days/half_life)*0.1
          每次检索时 SQL 自动按衰减后的权重排序——不需要额外代码触发。

          空函数 ≠ 没逻辑。逻辑在 SQL 里，不在 Python 里。
          保留空函数是为了 API 完整性: 三种策略在代码层都有对应方法，
          即使其中一种是 no-op（空操作）。

        【L4 工程】SQL 层衰减的优势
        ────────────────────────
          — 零延迟: 不依赖 cron 调度，每次查询时实时计算衰减
          — 零额外存储: 不存 last_decay_at / next_decay_at 字段
          — 零遗漏: 不会出现"有记忆在两次调度之间漏衰减"
        """
        pass  # 衰减由 SQL ORDER BY 自动完成

    async def archive_old_memories(self, days: int = 180) -> int:
        """策略2: 定期归档——超期记忆标记为不活跃。

        【L4 工程】执行流程
        ────────────────
        1. DAO.archive_old(days) → SQL: UPDATE ... SET is_active=false
        2. SQL 内部排除 decision 类型: WHERE memory_type != 'decision'
        3. 返回 affected_rows → 记录日志 → 返回归档条数

        Args:
            days: 超期天数，默认 180（半年）

        Returns:
            本次归档的记忆条数

        为什么用 affected_rows 作为返回值？
          — 调用方（Phase 6 的 cron handler）需要知道"清理了 324 条记忆"
          — 如果某天只有 2 条 → 日志正常（2 条被归档）
          — 如果某天 0 条 → 日志正常（没有需要归档的记忆）
          — 如果某天 5000 条 → 日志告警（可能有历史数据一次性大量过期）

        【L4 工程】为什么不在此方法内嵌入 decision 保护？
        ──────────────────────────────────────────
          保护逻辑在 DAO 层的 SQL 中: WHERE memory_type != 'decision'
          这是数据层的职责——Forgetting 只需要说"归档"，
          至于哪些记忆不能归档，由 DAO 决定。
          如果 Forgetting 层自己再做一遍过滤 → 代码臃肿 + 两处逻辑要同步。
        """
        count = await self._dao.archive_old(days=days)
        logger.info("日志：已归档 %d 条超期记忆（> %d 天）", count, days)
        return count

    async def explicit_forget(self, memory_id: str) -> None:
        """策略3: 显式删除——用户主动删除某条记忆（软删除）。

        【L4 工程】不物理删除的工程价值
        ────────────────────────────
        — 恢复: 用户误删 → UPDATE is_active=true 即可恢复
        — 审计: 保留 created_at + deleted_at 时间线 → 谁什么时候删了什么
        — 合规: 某些行业不允许硬删除数据

        Args:
            memory_id: 记忆 ID（UUID 字符串）
        """
        await self._dao.soft_delete(memory_id)
        logger.info("软删除记忆: %s", memory_id)

    async def run_maintenance(self, days: int = 180) -> dict:
        """定期维护入口（Phase 6 的 cron 可触发此方法）。

        【L5 架构】这是 Phase 4.5 和 Phase 6 的接口约定
        ─────────────────────────────────────────────
        Phase 4.5: 定义 run_maintenance() 和 archive_old_memories()
        Phase 6: 注册 APScheduler 任务 →
          @scheduler.scheduled_job('cron', hour=3)
          async def nightly_forgetting():
              result = await forgetting.run_maintenance(days=180)
              metrics.gauge('memories.archived', result['archived_count'])

        当前只做归档，未来可能扩展:
          — 重复记忆去重（找到 KEEP_BOTH 标记的冲突组，人工审查后合并且删除）
          — 零引用记忆清理（没有被任何任务引用的孤立 memory）

        Returns:
            {"archived_count": 324} — 结构化返回值，方便 metrics 采集
        """
        count = await self.archive_old_memories(days=days)
        return {"archived_count": count}
