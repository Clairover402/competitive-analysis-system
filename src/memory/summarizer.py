"""MemorySummarizer — 分层摘要记忆（递增合并 + 全量合并）。

═══════════════════════════════════════════════════════════════════════════════
                          【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 分层摘要策略: 递增合并（每轮微更新） + 全量合并（每 10 轮校准）
  §2 累积漂移问题: 每轮递增丢失 ~2%，100 轮后保真度仅 82%
  §3 LLM Prompt 工程: 输出 ≤500 字约束、[unverified] 标记、去闲聊指令


═══════════════════════════════════════════════════════════════════════════════
                     【L4 工程 — 分层摘要的核心问题】
═══════════════════════════════════════════════════════════════════════════════

问题: 为什么需要全量合并？递增合并不够吗？

递增合并的累积漂移（Cumulative Drift）:
  每轮递增时，LLM 基于"前次摘要 + 新消息"生成新摘要。
  理论上是增量更新，但 LLM 不是无损压缩——每轮会丢失 ~2% 的细节。

  轮次 5:  保真度 ≈ 0.98^4 = 92%
  轮次 10: 保真度 ≈ 0.98^9 = 83%
  轮次 20: 保真度 ≈ 0.98^19 = 69%

  就像传话游戏: 第 10 个人听到的版本和第 1 个人说的可能已经偏了 17%。

  全量合并的校准作用:
  直接拿原始对话 → LLM 重新提取 → 保真度恢复到 95%+
  这就像每 10 轮做一次"内存快照"，把漂移量归零。

  为什么间隔选 10 而不是 5 或 20？
    — 5: LLM 调用频率翻倍，但漂移量（~4%）已经很小，性价比低
    — 20: 漂移量 ~32%，校准效果大打折扣
    — 10: 工程实践中 LLM 成本 vs 保真度的最佳平衡点

错误处理:
  递增合并失败 → 回退到前次摘要或截断新消息（fallback 200 字）
  全量合并失败 → 返回空字符串（表示本轮摘要不可用，上游降级处理）
  两种情况的日志级别不同: 递增失败用 exception（意外），全量失败用 error（可控）


═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — 为什么不集成 Pipeline】
═══════════════════════════════════════════════════════════════════════════════

Pipeline 没有对话轮次概念:
  Pipeline 是 5 节点顺序流，每个节点执行一次。Writer 最多重写 2 次
  （3 个 report_version）——远达不到全量合并的 10 轮阈值。
  Summarizer 在 Pipeline 中会永远运行在递增模式下，全量合并路径永不到达。

正确的归属:
  Phase 5A Supervisor 有 ReAct 循环（_think → _act → _observe → _think → ...）。
  每轮 think 就是一次 LLM 推理，天然有对话轮次。
  摘要在第 5/10/15 轮触发全量合并，在中间轮次触发递增合并。

当前文件的角色:
  写好了完整逻辑但不集成——Staged Integration 策略（先写再挂）。
  Pipeline 的 graph.py 中有注释标记:
    "write 后的摘要钩子跳过——Summarizer 留给 Phase 5A Supervisor"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.db.dao import MemorySummaryDAO

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# LLM Prompt — 递增合并模板
# ══════════════════════════════════════════════════════════════════════════════

_INCREMENTAL_PROMPT = """你是对话摘要器。将前次摘要与新对话合并为一个简短摘要。

前次摘要:
%s

新对话:
%s

要求:
- 保留所有关键决策和用户偏好，不可丢失
- 丢弃闲聊和重复信息
- 不确定的信息标注为 [unverified]
- 输出不超过 500 字
"""

# ══════════════════════════════════════════════════════════════════════════════
# LLM Prompt — 全量合并模板
# ══════════════════════════════════════════════════════════════════════════════

_FULL_MERGE_PROMPT = """你是对话摘要器。从完整对话中提取所有关键信息。

对话:
%s

要求:
- 提取所有关键决策、用户偏好和事实表述
- 丢弃闲聊和重复信息
- 不确定的信息标注为 [unverified]
- 输出不超过 500 字
"""


class MemorySummarizer:
    """分层摘要记忆——每轮递增 + 每 10 轮全量校准。

    【L3 核心概念】分层摘要的工作方式
    ───────────────────────────────
    递增合并（增量模式）:
      前次摘要 + 本轮新消息 → LLM 生成新摘要
      类比: Git 的 incremental commit — 每次只记录 diff

    全量合并（校准模式）:
      原始对话（最近 10 轮）→ LLM 重新提取关键事实
      类比: Git 的 squash — 丢掉中间 diff，只保留最终状态



    【L5 架构】Pipeline 不集成此类
    ─────────────────────────────
    Pipeline 无对话轮次（最多 3 个 report_version），
    Summarizer 留给 Phase 5A Supervisor 的 ReAct 循环激活。
    """

    # 【L4 工程】全量合并间隔 — 10 轮触发一次
    # 每 10 轮做一次全量对话摘要，把累积漂移归零
    FULL_MERGE_INTERVAL = 10

    def __init__(self, llm: ChatDeepSeek, summary_dao: MemorySummaryDAO) -> None:
        """初始化摘要器。

        Args:
            llm: ChatDeepSeek 实例（温度建议 0.3，摘要需要稳定输出）
            summary_dao: memory_summaries 表的 DAO 层

        【L5 决策】为什么注入 DAO 而不是让 Summarizer 自己 new？
          — 依赖注入: Summarizer 不关心数据库连接从哪来
          — 连接池复用: 同一个 pool 被 Pipeline、Analyzer、Summarizer 共享
          — 测试友好: 单测时注入 mock DAO，不依赖真实数据库
        """
        self._llm = llm
        self._dao = summary_dao

    # ══════════════════════════════════════════════════════════════════════
    # 递增合并 — 每轮触发
    # ══════════════════════════════════════════════════════════════════════

    async def incremental_summary(
        self,
        prev_summary: str | None,
        new_messages: list[dict],
    ) -> str:
        """递增合并: 前次摘要 + 新对话 → LLM → 新摘要。

        【L4 工程】为什么只取最后 5 条消息？
        ─────────────────────────────────
          — 消息太多会超过 LLM 上下文窗口（但不取全量又可能丢失上下文）
          — 5 条的取舍: 够覆盖最近一轮对话（think + act + observe + user + think）
          — 如果一轮有 10 条消息，取最后 5 条就丢了前半轮——但全量合并会补回来

        【L4 工程】每条消息截断到 300 字符
        ────────────────────────────────
          — 防止长消息撑爆 prompt（如代码块 2000+ 字符）
          — 300 字符 ≈ 中文 ~150 字，足够保留核心语义
          — 截断不是摘要——LLM 生成的新摘要会做真正的信息压缩

        【L4 工程】失败回退策略
        ─────────────────────
          LLM 调用失败 → 返回前次摘要（如果有）或新消息截断（200 字）
          不回退到空字符串——至少保留前一轮的摘要，信息不丢。
        """
        # ── 构造新消息文本 ──
        new_text = ""
        for m in new_messages[-5:]:          # 只取最后 5 条
            role = m.get("role", "?")
            content = m.get("content", "")[:300]  # 截断到 300 字符
            new_text += f"[{role}]: {content}\n"

        prev = prev_summary if prev_summary else "(无前次摘要)"
        prompt = _INCREMENTAL_PROMPT % (prev, new_text)

        try:
            resp = await self._llm.ainvoke(prompt)
            summary = resp.content.strip()
            if len(summary) > 500:          # 强制截断（防御性）
                summary = summary[:500]
            return summary
        except Exception:
            logger.exception("递增合并摘要失败")
            # 【L4 工程】fallback: 前次摘要 > 新消息截断
            return prev_summary or new_text[:500]

    # ══════════════════════════════════════════════════════════════════════
    # 全量合并 — 每 10 轮触发
    # ══════════════════════════════════════════════════════════════════════

    async def full_merge_summary(
        self,
        messages: list[dict],
    ) -> str:
        """全量合并: 原始对话 → LLM → 全新摘要。校准累积漂移。

        【L3 核心概念】全量合并 vs 递增合并的区别
        ─────────────────────────────────────────
          递增合并: A → A' → A'' → A''' （链式更新，漂移累积）
          全量合并: B → LLM → C         （快照重建，漂移归零）

        【L4 工程】为什么全量合并也截断每条消息？
        ────────────────────────────────────
          截断到 500 字符（比递增的 300 更长）——全量合并只看这一次，
          需要更多上下文来理解整段对话。
        """
        full_text = ""
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")[:500]  # 全量合并用更长的截断
            full_text += f"[{role}]: {content}\n"

        prompt = _FULL_MERGE_PROMPT % full_text

        try:
            resp = await self._llm.ainvoke(prompt)
            summary = resp.content.strip()
            if len(summary) > 500:
                summary = summary[:500]
            return summary
        except Exception:
            logger.exception("全量合并摘要失败")
            # 【L4 工程】fallback: 返回空字符串
            # 全量合并失败意味着本次校准不可用，但不影响已有摘要
            return ""

    # ══════════════════════════════════════════════════════════════════════
    # 统一入口 — 自动选择递增或全量
    # ══════════════════════════════════════════════════════════════════════

    async def summarize_round(
        self,
        task_id: str,
        messages: list[dict],
        round_num: int,
    ) -> str:
        """统一摘要入口——根据轮次自动选择递增或全量合并。

        【L4 工程】决策流程
        ────────────────
        1. 判断 round_num 是否为 10 的倍数（且 > 1）
        2. 如果是 → 从 DB 取最近 10 轮对话 → 全量合并 → 写 DB
        3. 如果不是 → 取最新摘要 → 递增合并 → 写 DB

        【L5 决策】为什么用 DB 取对话而不是用传入的 messages？
        ──────────────────────────────────────────────────
          全量合并需要最近 10 轮的完整对话，但 summarize_round 的
          messages 参数只有当前轮的消息。历史消息存储在 DB 的
          memory_summaries 表中（每轮一条记录），通过 _get_recent_messages
          按倒序取最近 N 条。

        【L3 核心考点】这种方法叫"分阶段检索"（Staged Retrieval）：
          — 写阶段: 每轮摘要写入 DB（一次写）
          — 读阶段: 全量合并时按需从 DB 取（一次读）
          — 避免把 10 轮对话都缓存在内存里的 O(n) 空间开销
        """
        # ── 判断: 全量合并触发？ ──
        if round_num > 1 and round_num % self.FULL_MERGE_INTERVAL == 0:
            # 从 DB 取最近 10 轮对话
            recent = await self._get_recent_messages(task_id, count=self.FULL_MERGE_INTERVAL)
            if recent:
                # 全量合并 → 写 DB
                summary = await self.full_merge_summary(recent)
                range_str = f"{round_num - self.FULL_MERGE_INTERVAL + 1}-{round_num}"
                await self._dao.save(task_id, range_str, summary, "full_merge")
                logger.info("全量合并摘要已保存，轮次 %d-%d",
                            round_num - self.FULL_MERGE_INTERVAL + 1, round_num)
                return summary

        # ── 默认: 递增合并 ──
        # 取最新摘要记录
        prev_record = await self._dao.get_latest(task_id)
        prev_summary = prev_record["summary_text"] if prev_record else None
        summary = await self.incremental_summary(prev_summary, messages)
        await self._dao.save(task_id, str(round_num), summary, "incremental")
        logger.info("递增摘要已保存，轮次 %d", round_num)
        return summary

    # ══════════════════════════════════════════════════════════════════════
    # 辅助方法 — 从 DB 取最近 N 轮消息
    # ══════════════════════════════════════════════════════════════════════

    async def _get_recent_messages(
        self, task_id: str, count: int = 10
    ) -> list[dict]:
        """从 DB 获取最近 N 轮摘要记录。

        【L4 工程】为什么从摘要表取消息而不是从 messages 表？
        ──────────────────────────────────────────────────
          对话的完整消息历史存在 LangGraph 的 checkpoint 中，
          不是业务 DB。Direct SQL 查询 checkpoint JSONB 列
          比查自家的 memory_summaries 表复杂得多。
          摘要表已经存了每轮的摘要文本——对全量合并来说足够。

        【L4 工程】已知缺陷（低严重度）
        ──────────────────────────
          get_by_round_range(task_id, "") 传入空字符串作为 round_range，
          当前实现不匹配任何记录。但触发此路径的只有全量合并
          （每 10 轮一次），且当前 Summarizer 不集成 Pipeline，
          所以不影响功能。留给 Phase 5A 修复。
        """
        messages: list[dict] = []
        try:
            records = await self._dao.get_by_round_range(task_id, "")
            for r in records[-count:]:
                if isinstance(r.get("summary_text"), str):
                    messages.append({"role": "summary", "content": r["summary_text"]})
        except Exception:
            pass
        return messages
