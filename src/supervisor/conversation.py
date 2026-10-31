"""会话记忆的读写编排——把 conversations / conversation_turns 接进对话入口。

【职责】
  做：resolve 会话（没有就建、有就校验归属）、取上下文、落两行流水、超轮次时刷新摘要。
  不做：判意图（那是 intent/classifier）、执行意图（那是 query_handlers / create_task）、
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

# ★ 窗口折算成「轮」——2026-10-31 修。单位必须统一，否则差一倍。
# 每轮问答在 conversation_turns 里是 **2 行**（user + assistant），
# 所以 6 行 = 3 轮。此前 `should_summarize` 直接拿轮数（3、4、5…）跟
# RECENT_TURNS（6，行数）比，等价于要等到第 7 轮才第一次生成摘要；
# 而第 4 轮时第 1 轮就已经滑出内容窗口了 —— 第 4~6 轮存在**上下文真空**：
# 既不在滑窗里，也还没有摘要。早期约束（竞品名、口径、未完成的动作）就在这段丢的。
# 实测见 evaluation/memory/retention.py。
RECENT_ROUNDS = RECENT_TURNS // 2

# 超过窗口后，每 3 轮刷新一次摘要。
# 为什么不每轮刷：摘要是一次 LLM 调用，逐轮刷等于对话成本翻倍，而摘要漂移是慢变量。
SUMMARIZE_EVERY = 3

# ★ 每 12 轮做一次「全量重建」（2026-10-31 接回）：用**原始消息**重新生成摘要，
#   而不是在前次摘要上再叠加。递增合并的误差只增不减：A→A'→A''→A'''，
#   每一步都拿"已经概括过的概括"作输入，几轮之后摘要描述的是摘要、不是对话。
#   全量重建是快照重建（对话 → 摘要），误差归零。
#
# ⚠️ **必须是 SUMMARIZE_EVERY 的倍数**，否则永远不会触发（这是第一版踩的坑）：
#   刷新只发生在 turn_count = 3, 6, 9, 12…（SUMMARIZE_EVERY 的倍数），
#   取 10 时 `10 % 10 == 0` 看着没问题，但第 10 轮**根本不是刷新轮**
#   （10-3=7，7%3≠0），于是校准一次都没跑过 —— 报表里只有首次全量。
#   12 = 4 次刷新一次重建，且 12 的倍数必然也是 3 的倍数。
#   为什么是 12 轮：12 轮 = 24 行 ≈ 2400~4800 字，稳在摘要器 8000 字预算内；
#   再长就要靠 _tail_within_budget 砍头部，重建本身就不完整了。
FULL_MERGE_ROUNDS = 12

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
            #   维度槽位从抽不出来恢复成「结论证据」（意图本身两版都是 trace）。
            #   助手回复最长、也最像"用户在说报告"，占篇幅会把抽取拉偏。
            cut = 200 if t.get("role") == "user" else 80
            lines.append(f"{role}: {raw[:cut]}")
    lines.append("【本轮】")
    return "\n".join(lines) + "\n"


def should_summarize(turn_count: int) -> bool:
    """本轮结束后要不要刷新摘要（turn_count 是含本轮的用户轮数）。

    ★ 触发点必须**早于**内容滑出窗口（2026-10-31 修）。
      窗口 6 行 = 3 轮：第 4 轮开始时，第 1 轮就已经装不下了。
      所以第一次摘要在第 3 轮末生成（此刻窗口刚好填满、内容尚未滑出），
      第 4 轮就能用上。此前等到第 7 轮才生成，第 4~6 轮是真空期。

      判据用 `>= RECENT_ROUNDS` 而不是 `>`：前者是「填满了就压」，
      后者是「溢出后才压」—— 溢出后压缩，丢掉的那一轮已经补不回来了。

    之后每 SUMMARIZE_EVERY 轮刷一次：turn_count = 3, 6, 9, 12, ...
    """
    overflow = turn_count - RECENT_ROUNDS
    return overflow >= 0 and overflow % SUMMARIZE_EVERY == 0


def summary_plan(turn_count: int, has_prev: bool) -> tuple[str, int | None]:
    """本次刷新用哪种方式、取多少行原文 —— 生产与评估共用的**唯一判据**。

    返回 `(mode, limit)`：
      · `mode="full"` → 用原始消息全量重建，**不把前次摘要当输入**，`limit` 为 None
      · `mode="incremental"` → 前次摘要 + 新行，`limit=RECENT_TURNS*2`

    ★ 为什么返回 mode 而不是一个布尔「是否全量」（2026-10-31）：
      调用方要据此决定**用哪个 prompt**、**传不传 prev**。只返回行数的话，
      "取全量行但仍在前次摘要上叠加"照样会漂移——那种实现看起来更省，
      但误差链没断，且从行为上看不出差别（摘要照样又长又像）。
      ⚠️ 评估侧（evaluation/memory/retention.py）必须调这一个函数，
         自己写一套判据的话，测的就是评估而不是线上。

    ★ 三种情况必须全量，理由各不相同：
      · 首次（has_prev=False）：没有前次摘要，窗口外的原文是它们**唯一**的存在形式。
        只取尾部等于主动扔掉开头，而且扔掉的这部分之后再也进不了任何摘要
        （后续都是增量合并）。实测：8 轮会话里第 1 轮的四家竞品名全丢，
        正是首次只取了后 6 轮。
      · 周期校准（`turn_count % FULL_MERGE_ROUNDS == 0`）：切断增量累积的误差链。
    """
    if not has_prev:
        return "full", None
    if turn_count % FULL_MERGE_ROUNDS == 0:
        return "full", None
    return "incremental", RECENT_TURNS * 2


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
        summary, upto_seq = await _refresh_summary(pool, conversation, settings, turn_count)
        if summary:
            await dao.update_summary(conversation["id"], summary, upto_seq)
            logger.info(
                "会话摘要已刷新 conversation=%s turns=%d 覆盖至 seq=%s",
                conversation["id"], turn_count, upto_seq,
            )
    except Exception:
        logger.exception("会话摘要刷新失败 conversation=%s", conversation.get("id"))


async def _refresh_summary(
    pool: Any, conversation: dict, settings: Any, turn_count: int | None = None
) -> tuple[str, int | None]:
    """刷新会话摘要，返回 **(摘要, 覆盖到的最大 seq)**。

    ⚠️ 只用 MemorySummarizer 的两个纯函数（文本进、文本出）：
       `incremental_summary()` / `full_merge_summary()`。
       不调 `summarize_round()`：那个方法按 task_id 存多版本、还要回查
       memory_summaries 拿历史消息，而会话摘要只是一条滚动值。

    ★ 两种模式，判据全在 `summary_plan()`（评估侧调同一个函数）：
      · full        —— 原始消息全量重建，**不传 prev**，切断增量误差链；
                       同时在 memory_summaries 留一条快照（round_range="1-N"）
      · incremental —— 前次摘要 + 最近两窗口

    ★ 返回 upto_seq 而不是只返回摘要（2026-10-31）：
      它是"到这行为止都已被概括"的锚点，可回链要靠它。
      ⚠️ 别用 `turn_count * 2` 推算：assistant 行可能因异常少写一条，
         推算值会比真实值大，回链就指向一段并不在摘要里的原文。
    """
    from src.agents import create_llm_client
    from src.config import Settings
    from src.memory.summarizer import MemorySummarizer, fits_budget

    rounds = turn_count if turn_count is not None else int(conversation.get("turn_count") or 0) + 1
    mode, limit = summary_plan(rounds, bool(conversation.get("summary")))
    dao = ConversationDAO(pool)
    turns = await dao.recent_turns(conversation["id"], limit=limit)

    # ★ 缺口检出：窗口只有 RECENT_TURNS*2 行、每 3 轮刷一次，所以能容忍**一次**
    #   刷新失败。连续失败两次后，窗口起点会跨过一段从未进过摘要的行 ——
    #   它们既不在 summary 里，也即将滑出 6 行滑窗，等于永久丢失。
    #   检出后**升级为全量重建**（不是"取全量行再做增量"）：洞要补上，
    #   误差链也要一起断掉，否则补进去的新内容和 prev 里的旧内容会互相重复。
    prev_upto = conversation.get("summary_upto_seq")
    widened = False
    if mode == "incremental" and prev_upto is not None and turns and int(turns[0]["seq"]) > int(prev_upto) + 1:
        widened = True
        mode, limit = "full", None
        logger.warning(
            "摘要窗口与前次覆盖位置之间有缺口 conversation=%s prev_upto=%s 窗口起点=%s，升级为全量重建",
            conversation.get("id"), prev_upto, turns[0]["seq"],
        )
        turns = await dao.recent_turns(conversation["id"], limit=None)

    messages = [{"role": t["role"], "content": t["content"]} for t in turns]
    summarizer = MemorySummarizer(
        create_llm_client(settings or Settings(), temperature=0.3),
        MemorySummaryDAO(pool),  # 摘要落库由本模块决定，不经 DAO 的 task 维度接口
    )
    if mode == "full":
        summary = await summarizer.full_merge_summary(messages)
    else:
        summary = await summarizer.incremental_summary(conversation.get("summary"), messages)

    upto_seq = max((int(t["seq"]) for t in turns), default=None)

    # ★ 全量重建超预算 ⇒ 摘要只含尾部，前面的行其实没进去。
    #   此时锚点**不许前进**（返回 None，update_summary 的 COALESCE 会保留旧值）：
    #   宁可少报覆盖，也不能让"可回链"指向一段没被概括的原文。
    if mode == "full" and not fits_budget(messages, full=True):
        logger.warning(
            "全量重建超出摘要预算 conversation=%s 行数=%d，锚点保持不动",
            conversation.get("id"), len(turns),
        )
        upto_seq = None

    # ★ 每次全量重建留一条快照（2026-10-31）：这是"漂移可观测"的数据来源——
    #   对比第 10/20/30 轮的快照，能直接看出哪些约束在合并中被概括掉了。
    #   round_range 标 "1-N"（覆盖全史），不标 "11-20"：内容是全史重建，
    #   标成本段会让人以为它只概括了本段。
    #   ⚠️ 留档失败不影响本次摘要（日志即可）：它是观测数据，不是注入源。
    if mode == "full" and summary:
        try:
            await MemorySummaryDAO(pool).save_for_conversation(
                conversation["id"], f"1-{rounds}", summary, "full_merge",
            )
        except Exception:
            logger.exception("摘要快照留档失败 conversation=%s", conversation.get("id"))

    return summary, upto_seq
