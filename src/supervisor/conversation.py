"""会话记忆的读写编排——把 conversations / conversation_turns 接进对话入口。

【职责】
  做：resolve 会话（没有就建、有就校验归属）、取上下文、落两行流水、超轮次时刷新摘要。
  不做：判意图（那是 intent_funnel）、执行意图（那是 query_handlers / create_task）、
        向量化（会话原话按 seq 顺序取，见 schema.sql §7.5 注释）。

【输入 → 输出】
  输入：`pool`（asyncpg）+ `user_id` + 一句话
  输出：`conversation` dict（id/summary/turn_count）+ `format_history_block()` 拼出来的提示块

【关键决策】
  · 不改 thread_id ← checkpointer 是任务级执行快照，混用会同时弄坏任务恢复与对话上下文。
  · 只把历史喂给 L3（LLM），不喂 L1/L2 ← 规则与向量匹配的是"这一句"，
    掺进上文会让"那两家呢"这类指代句在 L2 上拿到一个假的相似度。
  · 摘要只在刚超轮次时做、之后每 3 轮刷一次 ← 每轮都调 LLM 等于给对话加一倍成本。
  · 落库失败只记日志、绝不让对话 500 ← 记忆是增强项，不能成为可用性的单点。

【读法】
  `ensure_conversation()` 返回 None 表示"给了 id 但不是你的" —— 上层统一按 404 处理，
  不区分"不存在"和"不是你的"，避免泄露别人的会话是否存在。
"""

from __future__ import annotations

import logging
from typing import Any

from src.db.dao import ConversationDAO, MemorySummaryDAO

logger = logging.getLogger(__name__)

# 保留多少行原文进 prompt —— 6 行 ≈ 3 轮问答。
# 再多会挤占 L3 那次 LLM 的注意力，而更早的内容本来就该在 summary 里。
RECENT_TURNS = 6

# 超过 RECENT_TURNS 后，每 3 轮刷新一次摘要（turn_count = 7, 10, 13, ...）。
# 为什么不每轮刷：摘要是一次 LLM 调用，逐轮刷等于对话成本翻倍，而摘要漂移是慢变量。
SUMMARIZE_EVERY = 3

_ROLE_CN = {"user": "用户", "assistant": "助手"}


async def ensure_conversation(
    pool: Any,
    user_id: str,
    conversation_id: str | None,
    title: str | None = None,
) -> dict | None:
    """拿到本次对话的会话；没给 id 就新建，给了但查不到/不属于自己返回 None。"""
    dao = ConversationDAO(pool)
    if conversation_id:
        return await dao.get(conversation_id, user_id)
    new_id = await dao.create(user_id=user_id, title=title)
    return {
        "id": new_id,
        "user_id": user_id,
        "title": title,
        "summary": None,
        "turn_count": 0,
    }


async def load_context(pool: Any, conversation: dict) -> dict:
    """取拼 prompt 需要的两部分：摘要 + 最近 N 行原文。"""
    turns = await ConversationDAO(pool).recent_turns(conversation["id"], limit=RECENT_TURNS)
    return {"summary": conversation.get("summary"), "turns": turns}


def format_history_block(summary: str | None, turns: list[dict]) -> str:
    """把上下文拼成一段给 L3 看的文本；没有历史时返回空串（等价于单轮）。"""
    if not summary and not turns:
        return ""

    lines = ["【会话上下文（仅供理解指代，不要回答这一段）】"]
    if summary:
        lines.append(f"[已压缩摘要] {summary}")
    if turns:
        lines.append("[最近对话]")
        for t in turns:
            role = _ROLE_CN.get(t.get("role", ""), t.get("role", "?"))
            raw = " ".join((t.get("content") or "").split())
            # 用户原话是指代的锚，留 200 字；助手回复只是背景，压到 80 字。
            # ← 实测：同一段历史、同一个问题，助手回复从 200 字压到 80 字后，
            #   keyword 从 None 恢复成「结论证据」（意图本身两版都是 trace）。
            #   助手回复最长、也最像"用户在说报告"，占篇幅会把抽取拉偏。
            cut = 200 if t.get("role") == "user" else 80
            lines.append(f"{role}: {raw[:cut]}")
    lines.append("【本轮】")
    return "\n".join(lines) + "\n"


def should_summarize(turn_count: int) -> bool:
    """本轮结束后要不要刷新摘要（turn_count 是含本轮的用户轮数）。"""
    overflow = turn_count - RECENT_TURNS
    return overflow >= 1 and (overflow - 1) % SUMMARIZE_EVERY == 0


async def remember_turn(
    pool: Any,
    conversation: dict,
    *,
    user_message: str,
    intent: str,
    reply_text: str,
    ref_task_id: str | None = None,
    ref_report_id: str | None = None,
    settings: Any = None,
) -> None:
    """落两行流水（user + assistant），必要时刷新摘要。

    整段都包在 try 里：会话记忆写失败不该让用户看到 500，
    但必须打日志——否则又会变成"表建了没人写、还没人知道"的老问题。
    """
    dao = ConversationDAO(pool)
    try:
        await dao.add_turn(
            conversation["id"], "user", user_message,
            intent=intent, ref_task_id=ref_task_id, ref_report_id=ref_report_id,
        )
        await dao.add_turn(
            conversation["id"], "assistant", reply_text,
            intent=intent, ref_task_id=ref_task_id, ref_report_id=ref_report_id,
        )
    except Exception:
        logger.exception("会话流水写入失败 conversation=%s", conversation.get("id"))
        return

    turn_count = int(conversation.get("turn_count") or 0) + 1
    if not should_summarize(turn_count):
        return

    try:
        summary = await _refresh_summary(pool, conversation, settings)
        if summary:
            await dao.update_summary(conversation["id"], summary)
            logger.info("会话摘要已刷新 conversation=%s turns=%d", conversation["id"], turn_count)
    except Exception:
        logger.exception("会话摘要刷新失败 conversation=%s", conversation.get("id"))


async def _refresh_summary(pool: Any, conversation: dict, settings: Any) -> str:
    """用现成的 MemorySummarizer 做递增摘要。

    ⚠️ 只用它的 `incremental_summary()`（纯函数：prev + messages → 文本），
       不调 `summarize_round()`：后者的"分阶段检索"假设摘要按 task_id 存多版本，
       而会话摘要只需要一条滚动值，落在 conversations.summary 上。
    """
    from src.agents import create_llm_client
    from src.config import Settings
    from src.memory.summarizer import MemorySummarizer

    turns = await ConversationDAO(pool).recent_turns(
        conversation["id"], limit=RECENT_TURNS * 2
    )
    messages = [{"role": t["role"], "content": t["content"]} for t in turns]
    summarizer = MemorySummarizer(
        create_llm_client(settings or Settings(), temperature=0.3),
        MemorySummaryDAO(pool),  # 本路径不落 memory_summaries，仅为满足构造签名
    )
    return await summarizer.incremental_summary(conversation.get("summary"), messages)
