"""MemoryConflictResolver — 三级冲突解决策略。

═══════════════════════════════════════════════════════════════════════════════
                          【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 语义冲突检测: embedding → 余弦相似度 → 阈值判定（≥0.85）
  §2 三级策略: OVERWRITE（事实更新）/ UPDATE（偏好变化）/ KEEP_BOTH（矛盾并存）
  §3 LLM-as-Arbitrator: LLM 判断冲突类型，代码执行策略


═══════════════════════════════════════════════════════════════════════════════
                     【L4 工程 — 三级策略的工程实现】
═══════════════════════════════════════════════════════════════════════════════

策略 1: OVERWRITE — 事实型更新（如价格变了）
  ────────────────────────────────────────
  触发: 新旧记忆语义相似 ≥ 0.85，且 LLM 判断为 fact 类型
  执行: 软删除所有冲突的旧记忆 → 插入新记忆（作为唯一权威版本）
  举例: "飞书企业版 200 元/人/月" → "飞书企业版 180 元/人/月" → OVERWRITE

策略 2: UPDATE — 偏好型变化（如用户换方案）
  ────────────────────────────────────
  触发: 新旧记忆语义相似 ≥ 0.85，且 LLM 判断为 preference 类型
  执行: 只软删除冲突中的 preference 类型旧记忆 → 插入新记忆
  举例: "用户更喜欢 Python 实现" → "用户要求 Go 高性能实现" → UPDATE
  保留旧偏好的原因: 偏好变化本身就是信息——"为什么从 Python 切 Go"

策略 3: KEEP_BOTH — 矛盾信息并存
  ─────────────────────────────
  触发: 新旧记忆语义相似 ≥ 0.85，但 LLM 判断为 contradictory
  执行: 两条都保留——标记冲突组 ID，供人类审查
  举例: 来源 A 说"竞品 X 日活 500 万" vs 来源 B 说"竞品 X 日活 200 万"
        两条都对（时间点不同）或至少有一条错——删除任何一条都丢信息

为什么用软删除而不是硬删除？
  — 审计追溯: 软删除保留 created_at / deleted_at 时间线
  — 可恢复: 操作失误可以 undo（un-soft-delete）
  — 信息价值: 旧记忆即使"过期"也是审计线索——"这个决策是什么时候做的"


═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — 冲突解决的两个时间点】
═══════════════════════════════════════════════════════════════════════════════

时间点 1: 写入时（当前实现）
  ─────────────────────
  add_memory() 被调用时 → detect_conflict() → resolve() → insert
  每次新记忆写入时做一次冲突检测。延迟小，但冲突仅与"同批次写入"比较。

时间点 2: 定时批处理（Phase 6 计划）
  ───────────────────────────────
  每天凌晨 3 点 cron → 扫描当天新记忆 → 两两 embedding 比较 → 冲突解决
  覆盖更全（跨批次冲突），但计算量大（N^2 级别）。

两者不互斥——写入时解决 80% 的常见冲突，批处理补上 20% 的跨批次冲突。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.db.dao import AgentMemoryDAO
from src.mcp.tools_rag import embed_query

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# LLM Prompt — 冲突仲裁
# ══════════════════════════════════════════════════════════════════════════════

_RESOLVE_PROMPT = """你是记忆冲突仲裁器。根据新信息和已有的冲突记忆，判断冲突类型并选择解决策略。

新信息: %s

冲突记忆:
%s

返回纯 JSON（不要其他文字）:
{
  "conflict_type": "fact|preference|contradictory",
  "reason": "简短理由",
  "action": "OVERWRITE|UPDATE|KEEP_BOTH"
}

策略指南:
- fact（事实更新）: 新事实替换旧事实 → OVERWRITE（软删除旧的，插入新的）
- preference（偏好变化）: 用户偏好已改变，旧偏好有历史参考价值 → UPDATE（标记旧偏好过期，插入新偏好）
- contradictory（矛盾信息）: 两条语义相似但相互矛盾的陈述 → KEEP_BOTH（两条都保留，标记 conflict_id）
"""


class MemoryConflictResolver:
    """三级冲突解决——按冲突类型选择策略。

    【L3 核心概念】冲突检测 + 解决两步走
    ─────────────────────────────────
      第一步 detect_conflict:
        新记忆 → embed → 查询 DB 中语义相似的旧记忆（≥0.85）
        返回冲突记忆列表（可能为空）

      第二步 resolve:
        新记忆 + 冲突记忆 → LLM 判断类型 → 执行对应策略
        返回 {action, conflicts, soft_delete_ids}

    【L4 工程】为什么冲突解决需要 LLM 而不是纯规则？
    ──────────────────────────────────────────
      纯规则（如"相似 ≥ 0.85 就覆盖"）太粗糙:
        0.86 可能是两个不同的消息（误覆盖）
        0.84 可能是同一条消息的微调（未检测到冲突）
      LLM 读两条消息的文本内容 → 语义层面判断"是更新还是矛盾"——
      比纯向量相似度判断准确得多。

    用法:
        resolver = MemoryConflictResolver(llm, dao)
        conflicts = await resolver.detect_conflict(user_id, content)
        if conflicts:
            action = await resolver.resolve(content, conflicts)
    """

    def __init__(self, llm: ChatDeepSeek, dao: AgentMemoryDAO) -> None:
        """初始化冲突解决器。

        Args:
            llm: ChatDeepSeek 实例（温度建议 0.0，仲裁需要高度一致）
            dao: agent_memories 表的 DAO 层
        """
        self._llm = llm
        self._dao = dao

    async def detect_conflict(
        self, user_id: str, content: str, threshold: float = 0.85
    ) -> list[dict]:
        """检测语义冲突——返回相似度 ≥ threshold 的已有记忆。

        【L4 工程】阈值 0.85 的选择
        ───────────────────────
          0.80 → 太宽: 把"飞书定价 200" 和 "飞书功能强" 判为相似 → 误覆盖
          0.85 → 合适: 区分"实质内容相同" vs "主题相同但内容不同"
          0.90 → 太严: "飞书 200 元" 和 "飞书版费 200" 被判为不相似 → 漏检测

          threshold 是可配置的——构造函数不锁定，调用方可以传:
            conflicts = await resolver.detect_conflict(user_id, content, threshold=0.90)

        【L4 工程】为什么用 embed_query 而不是另起一个 embedding 模型？
        ──────────────────────────────────────────────────────────
          BGE-M3 已经在 RAG 检索中被加载——共用同一个模型避免:
            — 内存翻倍（两个 embedding 模型各占 ~2.5GB）
            — 向量空间不兼容（两个模型对同一文本的 embedding 不可比较）
          embed_query() 来自 tools_rag.py，和 Collector/Analyzer 用的是同一个函数。
        """
        embedding = await embed_query(content)
        if not embedding:
            return []
        # 【L3】get_conflicts 在 SQL 层用 <=> 余弦距离算子查相似记忆
        return await self._dao.get_conflicts(user_id, embedding, threshold)

    async def resolve(
        self, new_content: str, conflicts: list[dict]
    ) -> dict:
        """根据冲突类型选择策略（LLM 仲裁 + 代码执行）。

        Returns:
            {
                "action": "OVERWRITE|UPDATE|KEEP_BOTH",  # 选定的策略
                "conflicts": [...],                       # 冲突记忆列表
                "soft_delete_ids": [...]                  # 被软删除的 ID
            }

        【L4 工程】为什么最多选前 5 条冲突记忆给 LLM？
        ───────────────────────────────────────────
          — 控制 prompt 长度: 5 条 × 200 字 = 1000 字，不会超过上下文窗口
          — 5 条之后的冲突记忆优先级低（相似度最低的那批），
            先解决最相似的冲突，剩余的留给下次写入或批处理
          — 不是扔掉——只是不传给 LLM，conflicts 列表的原长度不受影响

        【L4 工程】OVERWRITE vs UPDATE 的软删除差异
        ────────────────────────────────────────
          OVERWRITE: 删除所有冲突记忆（新旧信息互相矛盾，旧的不再有价值）
          UPDATE: 只删除 preference 类型的旧记忆（fact 类旧记忆仍有价值）
        """
        if not conflicts:
            return {"action": "INSERT", "conflicts": [], "soft_delete_ids": []}

        # ── 构造冲突文本 ──
        conflicts_text = ""
        for i, c in enumerate(conflicts[:5]):
            conflicts_text += f"[{i+1}] (类型={c.get('memory_type', '?')}) {c.get('content', '')[:200]}\n"

        # ── LLM 仲裁 ──
        prompt = _RESOLVE_PROMPT % (new_content, conflicts_text)
        try:
            resp = await self._llm.ainvoke(prompt)
            text = resp.content.strip()

            # 【L4 工程】防御性清理: 去掉 LLM 包裹的 ```json 代码块
            if text.startswith("```"):
                text = text.split("```", 2)[1].strip()

            result = json.loads(text)

            action = result.get("action", "KEEP_BOTH")
            soft_delete_ids = []

            # ── 执行策略 ──
            if action == "OVERWRITE":
                # 事实更新: 删除所有冲突旧记忆
                for c in conflicts:
                    await self._dao.soft_delete(c["id"])
                    soft_delete_ids.append(c["id"])
            elif action == "UPDATE":
                # 偏好变化: 只删除 preference 类型的旧记忆
                for c in conflicts:
                    if c.get("memory_type") == "preference":
                        await self._dao.soft_delete(c["id"])
                        soft_delete_ids.append(c["id"])

            return {
                "action": action,
                "conflicts": conflicts,
                "soft_delete_ids": soft_delete_ids,
            }
        except Exception:
            # 【L4 工程】LLM 仲裁失败 → 默认 KEEP_BOTH
            # KEEP_BOTH 是最安全的 fallback：不丢任何信息
            logger.exception("冲突解决失败，默认 KEEP_BOTH")
            return {"action": "KEEP_BOTH", "conflicts": conflicts, "soft_delete_ids": []}
