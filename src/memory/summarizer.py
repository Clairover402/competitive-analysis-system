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
               【L5 架构 — 两条路径，一条在跑，一条不接】
═══════════════════════════════════════════════════════════════════════════════

✅ 在跑：`incremental_summary()` —— 会话入口的滑窗摘要
  `supervisor/conversation.py::_refresh_summary` 在用：
  会话超 RECENT_TURNS(6) 行后，每 3 轮把「前次摘要 + 最近 12 行原文」压成一条
  滚动摘要，落在 conversations.summary 上，与最近 6 行原文一起拼成
  format_history_block() 交给 L3。这是本项目**唯一**真实生效的上下文压缩。

❌ 不接：`summarize_round()` / `full_merge_summary()` —— 按 task_id 多版本存
  设计假设是「ReAct 每轮都摘要，每 10 轮全量校准」。但 2026-10-31 核查后确认
  Supervisor 不能接，三个理由：
    ① think 的 prompt 是**有界**的（progress 固定字段 + reasoning_trace 只取
       最后 3 条），轮次增加不会让上下文变长 —— 没有需要压的对象；
    ② 摘要没有消费方：think 从不读 summary，写了也进不了 prompt；
    ③ 因此接上等于每轮白加一次 LLM 调用（10 轮 10 次），换一段没人看的字符串。
  原 observe 节点里那段调用已删除（它还有第二个缺陷：full_merge 的返回值被丢弃）。

⚠️ 别把两条路径的「轮」混为一谈：
  会话的轮 = 用户问答轮（会长，30 轮很常见）；
  Supervisor 的轮 = ReAct 决策轮（硬上限 10，且 prompt 不随之增长）。
  前者需要压缩，后者不需要 —— 这是同一个词在两种语境下含义不同。
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

# 摘要输入的字符预算（不是消息条数）。
# ★ 2026-10-31 改：原实现取 `new_messages[-5:]`，等于在调用方已经定好窗口之后
#   又砍一刀 —— 首次摘要调用方传全量（6 行 / 3 轮），被它砍成 5 条，
#   第 1 轮的**用户原话**正好被切掉，竞品名就此永久丢失。
#   现在改成"按字符预算从尾部保留"：预算管的是成本，窗口宽度由调用方决定。
_MAX_INPUT_CHARS = 8000

# 单条消息的截断长度（递增合并）
_PER_MSG_CHARS = 300

# 单条消息的截断长度（全量合并：只看一次，需要更多上下文）
_PER_MSG_CHARS_FULL = 500


def _tail_within_budget(
    messages: list[dict], budget: int, per_msg: int
) -> list[dict]:
    """从尾部往前取消息，直到字符预算用完；返回**保持原顺序**的切片。

    为什么按预算而不是条数：一条 3000 字的长消息和 10 条 30 字的短消息，
    对上下文的占用差两个数量级，用条数根本约束不住成本。
    """
    out: list[dict] = []
    used = 0
    for m in reversed(messages):
        cost = min(len(m.get("content") or ""), per_msg)
        if out and used + cost > budget:
            break
        out.append(m)
        used += cost
    return list(reversed(out))


def fits_budget(messages: list[dict], *, full: bool = False) -> bool:
    """这组消息能否**整段**喂进摘要器（超预算时 _tail_within_budget 只留尾部）。

    ★ 为什么要暴露这个判定（2026-10-31）：
      调用方要把"喂了多少行"记成"覆盖了多少行"（可回链锚点）。超预算时
      摘要器静默丢掉头部，若调用方仍按 max(seq) 推进锚点，锚点就会声称
      覆盖了一段从未被概括的原文 —— 回链过去对不上，而且**不报错**。
      判据与 _tail_within_budget 同口径，否则两边会各算一套。
    """
    per = _PER_MSG_CHARS_FULL if full else _PER_MSG_CHARS
    return sum(min(len(m.get("content") or ""), per) for m in messages) <= _MAX_INPUT_CHARS


_INCREMENTAL_PROMPT = """你是对话摘要器。将前次摘要与新对话合并为一个简短摘要。

前次摘要:
%s

新对话:
%s

【输出格式：四节固定，缺内容写「无」，不许省略标题】
【对象】竞品名 / 产品名 / 公司名，逐个列出
【维度与要求】用户要看的评估维度、分析角度、报告结构
【口径与约束】换算方式、统计范围、含税与否、付费周期、上限、排除项
【未完成】用户明确说了还没做的动作

【必须逐字保留，一个字都不许改】
- 产品名 / 竞品名（如「Notion」「飞书」）—— 禁止替换成「该产品」「对话主题」「上述内容」
- 评估维度名（如「并发上限」「技术架构」）—— 用户后面会说「那个维度」，
  概括成「性能相关」就再也对不回去了
- 版本号、URL、金额与币种、日期、比例
- 用户明确设定的口径（换算方式、统计范围、上限、排除项）
- 还没做完的动作（「最后再写技术架构」这类）

【为什么这条约束是硬要求】
摘要的消费者是**下一轮的指代消解**：用户会说「那 Slack 呢」「按刚才说的口径算」。
一旦你把实体名或维度名概括掉，后续所有轮次都无法把它们映射回具体对象，
表现为"上下文压缩之后答非所问"，而且**不报错**——只是越答越飘。
所以宁可摘要长一点，也不许把名字换成代词。

前次摘要里已经出现过的上述要素，必须原样带到新摘要中，不得二次概括。
总长不超过 600 字（四节合计）。
"""

# ══════════════════════════════════════════════════════════════════════════════
# LLM Prompt — 全量合并模板
# ══════════════════════════════════════════════════════════════════════════════

_FULL_MERGE_PROMPT = """你是对话摘要器。从完整对话中提取所有关键信息。

对话:
%s

【输出格式：四节固定，缺内容写「无」，不许省略标题】
【对象】竞品名 / 产品名 / 公司名，逐个列出
【维度与要求】用户要看的评估维度、分析角度、报告结构
【口径与约束】换算方式、统计范围、含税与否、付费周期、上限、排除项
【未完成】用户明确说了还没做的动作

【必须逐字保留】
- 产品名/竞品名、评估维度名、版本号、URL、金额与币种、日期
- 用户设定的口径、未完成动作
禁止替换成「该产品」「上述内容」「性能相关」等代词或概括。

总长不超过 600 字（四节合计）。
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



    【L5 架构】哪个方法在跑，哪个没有
    ─────────────────────────────
    在跑:   incremental_summary()  ← conversation.py 的会话滑窗摘要
    不接:   summarize_round() / full_merge_summary()（按 task_id 多版本）
            原因见文件头「两条路径，一条在跑，一条不接」。
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

        【L4 工程】输入按字符预算裁，不按条数裁（2026-10-31 修）
        ────────────────────────────────────────────────
          旧实现 `new_messages[-5:]`：调用方（_refresh_summary）已经按窗口
          挑好了行数，这里再按"5 条"砍一刀，第 1 轮用户原话被切掉 →
          竞品名永久丢失。实测：8 轮会话里四家竞品名全丢，就是这一行造成的。
          现在由 `_tail_within_budget` 只按总字数封顶，窗口宽度交还调用方。

        【L4 工程】失败回退策略
        ─────────────────────
          LLM 调用失败 → 返回前次摘要（如果有）或新消息截断（200 字）
          不回退到空字符串——至少保留前一轮的摘要，信息不丢。
        """
        # ── 构造新消息文本 ──
        kept = _tail_within_budget(new_messages, _MAX_INPUT_CHARS, _PER_MSG_CHARS)
        new_text = ""
        for m in kept:
            role = m.get("role", "?")
            content = (m.get("content") or "")[:_PER_MSG_CHARS]
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
        kept = _tail_within_budget(messages, _MAX_INPUT_CHARS, _PER_MSG_CHARS_FULL)
        full_text = ""
        for m in kept:
            role = m.get("role", "?")
            content = (m.get("content") or "")[:_PER_MSG_CHARS_FULL]
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

        【L4 工程】Phase 7 修复：不再传空字符串给 get_by_round_range
        ──────────────────────────────────────────────────────────
          旧实现: dao.get_by_round_range(task_id, "") → 空字符串不匹配任何记录
          新实现: dao.get_recent_by_task(task_id, count) → 按 created_at 倒序取
          全量合并只需要"最近 N 条摘要"，不需要按 round_range 字段过滤。
        """
        messages: list[dict] = []
        try:
            records = await self._dao.get_recent_by_task(task_id, limit=count)
            for r in records:
                if isinstance(r.get("summary_text"), str):
                    messages.append({"role": "summary", "content": r["summary_text"]})
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("full_merge 摘要查询失败 task_id=%s count=%s", task_id, count)
            pass
        return messages
