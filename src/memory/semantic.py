"""semantic 写入：把证据链里的结论提成"带保鲜期的事实"，并处理新旧取代。

【职责】
  做：从 `evidence_map`（有 claim、有 source_url、有原文）提炼一条条 fact 记忆，
      每条带 `observed_at` / `ttl_days` / `subject` / `evidence_url`；
      新观测与同 subject 的旧观测高度相似时，把旧的标成 `superseded`。
  不做：不写报告里没有的东西（无来源的结论一律跳过）；
        不做事实校验（"这条还对不对"要重新抓页面，不在记忆层假装能做）；
        不物理删除旧观测（取代 ≠ 删除，见下）；
        不给 episode 判取代（追加式历史，见 M2 取舍）。

【输入 → 输出】
  report_id + evidence_map 行
    → `build_fact_content()` 一条定长正文
    → `write_task_facts()` 写入 N 条 fact，并把 M 条旧观测标为 superseded

【关键决策】
  · 事实只能来自 evidence_map ← 报告正文里的句子没有来源 URL，写进记忆等于
    把"模型说过的话"升格成"观测到的事实"，之后谁都查不回来
  · observed_at 用证据抓取时间，不用入库时间 ← verify 问"多久没核对"，
    起点是抓到它的那一刻；用 created_at 会把"任务跑完才写记忆"的延迟算进陈旧度
  · 取代 = 先同 subject，再比相似度（≥0.90）← 纯相似度会把"支持多维表格"和
    "支持自动化流程"判成同一条，然后删掉一条真的信息
  · ttl_days 复用 `query_handlers.ttl_days_for()` ← 不新造第二套档位表，
    否则 verify 判"过期"和写入侧记的保鲜期会对不上
  · 落库的 ttl_days 只是**当时的默认档位**，不是最终判定值 ← 用户改了
    `ttl_overrides` 偏好之后，verify 读取时会重算覆盖它（见 check_freshness）。
    这样改配置不需要回填 248 条历史 fact
  · 写失败只记日志 ← 记忆是任务完成后的附加动作，不能把 completed 改成 failed

【读法/注意事项】
  ⚠️ `status='expired'` 当前**不落库**：过期是随时间变化的判断，写进列就要有
    定时任务去刷，而本系统不引调度器（见实施计划 §5 不做清单）。
     读取侧用 `observed_at + ttl_days` 现算，见 `query_handlers.check_freshness`。
  ⚠️ 没有向量就**不判取代**（不误取代优先于不漏取代）：判"是不是同一件事"
    必须靠语义，关键词重合度不足以承担这个判断。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.config import Settings
from src.mcp.tools_rag import embed_texts
from src.memory.long_term import LongTermMemoryEngine, importance_for
from src.memory.preferences import load_overrides
from src.supervisor.query_handlers import ttl_days_for

logger = logging.getLogger(__name__)

# 事实正文模板。字段顺序 = 读它时的顺序：先"关于谁、哪一维、什么时候看的"，
# 再结论，最后来源。前三行是元信息，最后一行是给人点回去的。
FACT_TEMPLATE = """[事实] {competitor}·{dimension}（观测 {date}）
{claim}
来源: {url}"""

# 太短的 claim 多是"数据不足""待验证"这类占位串，提成事实只会污染检索。
_MIN_CLAIM_CHARS = 12
# 单条 claim 上限：evidence_map 里有 400 字以上的整段论断，直接写进记忆会让
# 一条记忆吃掉注入预算的一大截（analyze 注入 5 条 × 每条截 200 字）。
_MAX_CLAIM_CHARS = 220


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def pick_competitor(claim: str, competitors: list[str] | None) -> str:
    """从 claim 里认出说的是哪个竞品；认不出返回空串。

    ⚠️ 按**出现位置最早**的那个判，不按列表顺序：句子里先提谁，主语多半就是谁
      （"与飞书相比，钉钉的审批更轻"——先说飞书，但主语是钉钉，
       所以这条会归错）。这是已知误差，接受它的理由是：
       ① evidence_map 没有 competitor 列，只能从文本里认；
       ② 归错只影响 subject（取代范围），不影响记忆本身可被检索。
       要根治得让 quality 节点在抽证据时一并落 competitor，属于下次改 DDL 的事。
    """
    text = claim or ""
    best, best_pos = "", len(text) + 1
    for name in competitors or []:
        name = str(name).strip()
        if not name:
            continue
        pos = text.find(name)
        if pos != -1 and pos < best_pos:
            best, best_pos = name, pos
    return best


def build_subject(competitor: str, dimension: str | None) -> str:
    """取代范围的锚点：`飞书|定价`。认不出竞品时用 `未知竞品|综合`。

    为什么保留"未知"而不是跳过：这类事实仍可被检索与展示，
    只是取代范围放宽到"同维度的未知竞品"——宁可多留一条，不要丢一条真信息。

    ⚠️ 维度必须先 strip 再判空：`"  "` 是真值，`or` 兜不住它，
       会拼出 `飞书|` 这种"看起来像空维度其实不是"的锚点——下次同样的空维度
       只要多一个空格，就成了另一个 subject，取代关系从此断掉。
    """
    dim = (dimension or "").strip() or "综合"
    return f"{competitor or '未知竞品'}|{dim}"


def build_fact_content(
    *,
    competitor: str,
    dimension: str | None,
    claim: str,
    source_url: str,
    observed_at: datetime | None,
) -> str:
    """拼 fact 正文（纯函数，不调 LLM、不查库）。"""
    claim_text = _one_line(claim)[:_MAX_CLAIM_CHARS]
    return FACT_TEMPLATE.format(
        competitor=competitor or "未知竞品",
        # 同 build_subject：先 strip 再兜底，否则纯空白维度会渲染成"·（观测 …）"
        dimension=(dimension or "").strip() or "综合",
        # 相对时间（"3 天前"）一旦落库，第二天起就开始骗人，所以写绝对日期
        date=(observed_at or datetime.now()).strftime("%Y-%m-%d"),
        claim=claim_text,
        url=(source_url or "").strip() or "—",
    )


async def _latest_report(pool, task_id: str) -> dict | None:
    """取任务的最新报告（改写循环会产生多版本，只有最高版本是交付稿）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id::text AS id, created_at
               FROM reports WHERE task_id = $1
               ORDER BY version DESC LIMIT 1""",
            task_id,
        )
        return dict(row) if row else None


async def _evidence_rows(pool, report_id: str, limit: int) -> list[dict]:
    """取该报告的证据行（有新有旧按写入顺序，取前 limit 条）。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT claim, source_url, dimension, created_at
               FROM evidence_map WHERE report_id = $1::uuid
               ORDER BY id LIMIT $2""",
            report_id, limit,
        )
        return [dict(r) for r in rows]


async def write_task_facts(
    *,
    engine: LongTermMemoryEngine,
    pool,
    task: dict,
    settings: Settings | None = None,
) -> dict:
    """任务收尾把证据链提成交付事实；任何失败都只记日志。

    返回统计 dict（written / superseded / skipped / evidence），供调用方打日志。
    这个返回值不是给业务用的，是给"记忆到底写了什么"这件事留证据用的——
    M2 的教训是：没有计数就只能靠翻日志猜。
    """
    cfg = settings or Settings()
    stats = {"evidence": 0, "written": 0, "superseded": 0, "skipped": 0, "report_id": None}
    if not cfg.memory_semantic_enabled:
        return stats

    task_id = task.get("id")
    user_id = task.get("user_id") or "default"
    if not task_id:
        logger.warning("任务缺少 id，跳过 fact 写入")
        return stats

    try:
        report = await _latest_report(pool, task_id)
        if not report:
            logger.debug("任务无报告，跳过 fact 写入 task=%s", str(task_id)[:8])
            return stats
        stats["report_id"] = report["id"]

        rows = await _evidence_rows(pool, report["id"], cfg.memory_semantic_max_per_task)
        stats["evidence"] = len(rows)
        if not rows:
            return stats

        competitors = list(task.get("competitors") or [])
        importance, half_life = importance_for("fact")
        dao = engine.dao

        # 用户在 `ttl_overrides` 里改过的档位，写入时就按它定；读不到偏好不影响写入
        overrides = await load_overrides(pool, user_id)

        # ── 先把要写的都算好，一次批量 embed ──
        # 逐条 embed_query 是 N 次网络往返；20 条证据 = 20 次 × ~100ms，
        # 而 embed_texts 内部按 batch_size=12 打包，20 条只要 2 次往返。
        pending: list[dict] = []
        for row in rows:
            claim = _one_line(row.get("claim"))
            url = (row.get("source_url") or "").strip()
            if len(claim) < _MIN_CLAIM_CHARS or not url:
                stats["skipped"] += 1
                continue
            competitor = pick_competitor(claim, competitors)
            subject = build_subject(competitor, row.get("dimension"))
            content = build_fact_content(
                competitor=competitor,
                dimension=row.get("dimension"),
                claim=claim,
                source_url=url,
                observed_at=row.get("created_at"),
            )
            if await dao.fact_exists(user_id, task_id, subject, content):
                stats["skipped"] += 1       # 任务重跑：这条事实已经写过了
                continue
            pending.append({
                "content": content,
                "subject": subject,
                "claim": claim,
                "dimension": row.get("dimension"),
                "url": url,
                "observed_at": row.get("created_at") or report["created_at"],
                "ttl_days": ttl_days_for(claim, row.get("dimension"), overrides),
            })

        if not pending:
            return stats

        vecs: list[list[float]] = []
        try:
            vecs = await embed_texts([p["content"] for p in pending])
        except Exception as exc:  # noqa: BLE001
            # 向量失败不阻断写入（"写入允许无向量"），代价是这批不判取代
            logger.warning("fact 批量嵌入失败，本次不判取代: %s", exc)
            vecs = []
        if len(vecs) != len(pending):
            vecs = []

        for idx, item in enumerate(pending):
            vec = vecs[idx] if vecs else []
            # 先找旧观测再插入：插入之后新行自己也会命中相似（相似度 1.0）
            olds = []
            if vec:
                olds = await dao.find_active_similar(
                    user_id, vec, item["subject"],
                    threshold=cfg.memory_semantic_supersede_threshold,
                )
            new_id = await dao.insert(
                user_id=user_id,
                memory_type="fact",
                content=item["content"],
                importance=importance,
                embedding=vec or None,
                source_task_id=task_id,
                half_life_days=half_life,
                observed_at=item["observed_at"],
                ttl_days=item["ttl_days"],
                evidence_url=item["url"],
                subject=item["subject"],
                model_version=engine.model_version or "bge-m3",
            )
            stats["written"] += 1
            for old in olds:
                await dao.supersede(old["id"], new_id)
                stats["superseded"] += 1

        logger.info(
            "fact 写入完成 task=%s 证据=%d 写入=%d 取代=%d 跳过=%d",
            str(task_id)[:8], stats["evidence"], stats["written"],
            stats["superseded"], stats["skipped"],
        )
        return stats
    except Exception as exc:  # noqa: BLE001 — 记忆写入不许把任务链路带崩
        logger.warning(
            "fact 写入失败（不影响任务） task=%s: %s", str(task_id)[:8], exc,
            exc_info=True,
        )
        return stats
