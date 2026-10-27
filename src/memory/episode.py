"""episodic 写入：任务收尾时留一条"这次做了什么、结论朝哪走"的记忆。

【职责】
  做：把任务结果压成一条 ≤200 字的 episode，写进 `agent_memories`（`memory_type='episode'`）；
      写失败只打日志，**绝不影响任务状态**。
  不做：不写报告里的具体数字（价格/用户数/评分会过期，属于 M3 带 TTL 的 semantic）；
        不给 episode 挂冲突检测（追加式历史，旧的不该被新的取代）；
        不决定"要不要检索它"（那是 `LongTermMemoryEngine.retrieve()` 的事）。

【输入 → 输出】
  task(dict: id/title/user_id/competitors/dimensions) + route + 报告正文 + 质量分
    → `build_episode_content()` → 一条定长文本
    → `write_task_episode()` → 写入的 memory_id；已存在/失败 → None

【关键决策】
  · 写在 `_execute_task` 收尾而不是 finalize 节点
      ← pipeline 与 supervisor 两条路径都经过这里，写两处必漏一处
  · 结论用一次 LLM 压成 ≤80 字，且**禁写数字**
      ← 报告首段通常是标题或表格，直接截断得到的是噪音；而写进数字就等于造一条
        90 天后必然过期的"事实"，那该由 semantic 管
  · `dedupe=True` + 部分唯一索引 ← 任务重跑不能留下两条几乎相同的 episode
  · 时间写绝对日期不写"3 天前" ← 相对时间一旦落库，第二天起就开始骗人

【读法/注意事项】
  先看 `EPISODE_TEMPLATE` 就知道记忆长什么样；`summary_of()` 是唯一会调 LLM 的地方，
  它失败时 `build_episode_content()` 仍会产出一条"无结论"的 episode——
  **宁可有骨架没结论，也不要因为一次 LLM 抖动而整条不写**（否则下次 analyze 依然从零开始）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.config import Settings
from src.memory.long_term import LongTermMemoryEngine

logger = logging.getLogger(__name__)

# 记忆正文模板。字段顺序 = analyze 读它时的阅读顺序：
# 先"什么时候、关于谁"，再"怎么做的"，最后"结论是什么"。
EPISODE_TEMPLATE = """[分析任务] {date} · {title}
竞品: {competitors} | 维度: {dimensions}
路径: {route} | 质量分: {score}
结论: {conclusion}
task: {task_id}"""

SUMMARY_PROMPT = """把下面这份竞品分析报告压成一句结论，不超过 80 字。

硬约束（违反任一条即为错误输出）：
1. 不写任何具体数字：价格、用户数、评分、百分比、排名一律不写。
2. 只写"结论朝哪边走"：谁在哪个维度占优、哪些格子数据不足、结论偏保守还是确定。
3. 不复述标题，不以"本报告/本次分析"开头，直接给结论。
4. 一句话。不要分点，不要换行。

报告正文：
%s

一句话结论："""

# 报告正文送进 LLM 的上限。报告通常 2000~6000 字，
# 结论方向在开头几节就定了，尾部多是附录与引用清单，截掉不影响结论。
_REPORT_HEAD_CHARS = 3000


def _cn_list(items: list[str] | None) -> str:
    """用顿号连接，空列表给"—"而不是空字符串（空串会让模板出现"维度: "）。"""
    return "、".join(items) if items else "—"


def build_episode_content(
    *,
    task: dict,
    route_type: str,
    conclusion: str,
    quality_score: float | None,
    max_chars: int = 200,
) -> str:
    """拼出 episode 正文（纯函数，不调 LLM、不查库）。

    `conclusion` 为空时照常拼，只是这一行写「(未压缩)」——
    调用方据此仍能写入一条有骨架的记忆。
    """
    score = "—"
    if quality_score is not None:
        # ⚠️ 2026-10-27 实测踩到：state 里的 quality_score 是 **0~100**（本次写入拿到 86.0），
        #    与 `reports.quality_score` 的 DDL 注释（"质检分数 0-100"）一致，
        #    而 Settings/文档里又常写成 0.0~1.0 的比例。两种量纲混在同一个字段上，
        #    直接 f"{score:.2f}" 会把 86 分写成"86.00"，看起来像 0.86 的两位小数值，
        #    历史数据里两种写法并存就无法比较。
        #    ⇒ >1 视为百分制取整，≤1 视为比例保留两位，宁可在记忆里统一成人能读的形式。
        score = f"{quality_score:.0f}" if quality_score > 1 else f"{quality_score:.2f}"
    text = EPISODE_TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d"),
        title=(task.get("title") or "竞品分析").strip()[:40],
        competitors=_cn_list(task.get("competitors")),
        dimensions=_cn_list(task.get("dimensions")),
        route=route_type,
        score=score,
        conclusion=(conclusion or "").strip() or "(未压缩)",
        task_id=task.get("id") or "—",
    )
    # ⚠️ 截断只可能发生在"结论写太长"时（模板其余部分长度可控），
    #    所以从头截不会切掉 task_id 之外的关键信息；task_id 放最后正因为它最不能丢，
    #    真被截了也说明 LLM 没按 80 字约束办事，属于要修 prompt 的信号。
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


async def summary_of(report_text: str, llm: Any, *, limit: int = 80) -> str:
    """用一次 LLM 把报告压成结论方向；失败/空报告 → 返回空串（不抛）。

    为什么值得花这一次调用：episode 的唯一消费端是 analyze 的 `[历史记忆]` 段，
    下一轮分析要靠它判断"这个竞品上次结论是什么"。若把报告开头原样截 80 字放进去，
    拿到的多半是「# 竞品分析报告」这类标题噪音，等于记忆写了但没用。

    temperature 由调用方传入的 llm 决定（生产用 0.1 的 analyzer 实例即可）。
    """
    text = (report_text or "").strip()
    if not text:
        return ""
    try:
        resp = await llm.ainvoke(SUMMARY_PROMPT % text[:_REPORT_HEAD_CHARS])
        out = (getattr(resp, "content", "") or "").strip()
        # 模型偶尔仍会分点/换行，压成一行再截断
        out = " ".join(out.split())
        return out[:limit]
    except Exception as exc:  # noqa: BLE001 — 记忆写入不允许把异常抛给任务链路
        logger.warning("episode 结论压缩失败，将写入无结论骨架: %s", exc)
        return ""


async def write_task_episode(
    *,
    engine: LongTermMemoryEngine,
    llm: Any,
    task: dict,
    route_type: str,
    report_text: str,
    quality_score: float | None = None,
    settings: Settings | None = None,
) -> str | None:
    """任务收尾写一条 episode；任何失败都只记日志，返回 None。

    调用约定：由 `_execute_task` 在任务被标记 completed **之后**调用，
    外面已有一层 try/except，但本函数自己也兜一层——
    因为"记忆没写进去"和"任务失败"是两件事，前者不该让后者发生。
    """
    cfg = settings or Settings()
    if not cfg.memory_episodic_enabled:
        logger.debug("memory_episodic_enabled=false，跳过 episode 写入")
        return None

    task_id = task.get("id")
    if not task_id:
        # 没有 task_id 就无从幂等（唯一索引建在 source_task_id 上），直接放弃
        logger.warning("任务缺少 id，跳过 episode 写入")
        return None

    try:
        conclusion = await summary_of(report_text, llm)
        content = build_episode_content(
            task=task,
            route_type=route_type,
            conclusion=conclusion,
            quality_score=quality_score,
            max_chars=cfg.memory_episodic_max_chars,
        )
        memory_id = await engine.add_memory(
            user_id=task.get("user_id") or "default",
            content=content,
            memory_type="episode",
            source_task_id=task_id,
            dedupe=True,
        )
        if memory_id:
            logger.info("episode 已写入 task=%s memory=%s", str(task_id)[:8], memory_id[:8])
        return memory_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "episode 写入失败（不影响任务） task=%s: %s", str(task_id)[:8], exc,
            exc_info=True,
        )
        return None
