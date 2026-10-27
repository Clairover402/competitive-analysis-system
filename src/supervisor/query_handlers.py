"""三个查询类意图的 SQL 执行器 — lookup / trace / verify。

【职责】
  做：把"意图 + 实体"翻译成一条 SQL，返回可直接念给用户的结构化结果。
  不做：不做语义匹配（那是 L3 的事）、不写库、不调 Agent、不生成报告。

【输入 → 输出】
  lookup_reports(pool, user_id, competitors)              -> {"items": [...], "summary": str}
  trace_evidence(pool, user_id, competitors, keyword)     -> 同上
  check_freshness(pool, user_id, competitors, keyword)    -> 同上（附 fresh/stale 判定）

【关键决策】
  ★ 三跳全走 SQL，零 LLM ← 用户问的是**确定指代**（"上次""那份"），
    语义相似度会召回错任务，确定性查询只能用确定性工具
  ★ 排序一律 created_at DESC ← "上次"的字面含义就是时间最近，不需要模型理解
  ★ verify 借用 evidence_map.created_at 当 observed_at ← P3 的 observed_at/ttl_days
    列还没建，先不假造一张表；等 semantic 记忆落了再切过去
  ★ 竞品过滤用 jsonb `?|` 而不是 LIKE ← tasks.competitors 是 JSONB 数组，
    LIKE 会把"飞书"匹配进任何含该子串的句子
  ★ 查不到就如实说查不到 ← 空结果编一句"可能是网络问题"最伤可信度

【读法】
  ⚠️ 所有查询都带 user_id，多租户隔离在这一层做，别指望调用方记得加。
  ⚠️ 结果里的 datetime 已转 isoformat —— 这些 dict 会直接进 JSON 响应。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 事实类内容的默认保鲜期（天）。★ 按"变得多快"分档，不是拍一个数给所有事实。
DEFAULT_TTL_DAYS = 30
_TTL_BY_KEYWORD: tuple[tuple[tuple[str, ...], int], ...] = (
    (("定价", "价格", "收费", "套餐", "多少钱"), 30),
    (("融资", "估值", "轮次", "投资"), 90),
    (("功能", "版本", "更新", "发布"), 60),
    (("组织", "团队", "人数", "架构"), 180),
)


def _as_list(value: Any) -> list:
    """asyncpg 未注册 jsonb codec 时返回字符串，这里统一成 list。"""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return list(value) if isinstance(value, (list, tuple)) else []


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _short(text: Any, limit: int = 80) -> str:
    """把结论句压成一行短句，供聊天气泡用。

    ⚠️ claim 是"整段论断"（实测常见 200~400 字），原样拼进 summary 会变成
       一屏文字墙，用户看不到真正重要的"来源 + 时间"。截断是展示问题，
       不是数据丢失 —— items 里仍是全文。
    """
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


# 状态到中文的映射。★ summary 是**直接念给用户听的**，不能把库里的
#   running / pending 原样塞进去（实测气泡里出现过"状态 running"）。
_STATUS_CN = {
    "pending": "排队中",
    "collecting": "收集中",
    "analyzing": "分析中",
    "writing": "撰写中",
    "qa": "质检中",
    "completed": "已完成",
    "failed": "失败",
    "running": "运行中",
}


def _status_cn(status: Any) -> str:
    s = str(status or "").strip()
    return _STATUS_CN.get(s, s or "未知")


def _time_cn(iso: str | None) -> str:
    """ISO 时间 → "10-27 20:22"。

    ⚠️ 库里给的是 UTC ISO（2026-10-27T12:22:36+00:00），原样念出来
       既长又和用户本地时间差 8 小时。展示层转本地。
    """
    if not iso:
        return "未知时间"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.astimezone().strftime("%m-%d %H:%M")


def ttl_days_for(text: str | None) -> int:
    """按关键词给事实挑一个保鲜期档位；认不出来用默认值。"""
    blob = text or ""
    for keywords, days in _TTL_BY_KEYWORD:
        if any(k in blob for k in keywords):
            return days
    return DEFAULT_TTL_DAYS


# ── ① lookup：找历史任务 ────────────────────────────────────────────────

_LOOKUP_SQL = """
SELECT t.id::text        AS task_id,
       t.title           AS title,
       t.competitors     AS competitors,
       t.dimensions      AS dimensions,
       t.status          AS status,
       t.created_at      AS created_at,
       r.id::text        AS report_id,
       r.version         AS version,
       r.quality_score   AS quality_score
  FROM tasks t
  LEFT JOIN LATERAL (
        SELECT id, version, quality_score
          FROM reports
         WHERE task_id = t.id
         ORDER BY version DESC
         LIMIT 1
  ) r ON TRUE
 WHERE t.user_id = $1
   AND ($2::text[] IS NULL OR t.competitors ?| $2::text[])
 ORDER BY t.created_at DESC
 LIMIT $3
"""


async def lookup_reports(
    pool,
    user_id: str,
    competitors: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """按用户（可选再按竞品）取最近的任务与报告。"""
    competitors = [c for c in (competitors or []) if str(c).strip()] or None
    rows = await pool.fetch(_LOOKUP_SQL, user_id, competitors, limit)

    items = []
    for row in rows:
        items.append({
            "task_id": row["task_id"],
            "title": row["title"],
            "competitors": _as_list(row["competitors"]),
            "dimensions": _as_list(row["dimensions"]),
            "status": row["status"],
            "created_at": _iso(row["created_at"]),
            "report_id": row["report_id"],
            "version": row["version"],
            "quality_score": row["quality_score"],
        })

    if not items:
        summary = "没有找到匹配的历史任务。" + ("（按竞品筛选后为空）" if competitors else "")
    else:
        head = items[0]
        # ⚠️ report 是 LEFT JOIN：跑完没出报告时 version 也是 None，
        #   直接 `v{version}` 会念成"报告版本 vNone"（实测踩过）。
        report_desc = (
            f"报告版本 v{head['version']}（{head['report_id']}）"
            if head["version"] is not None
            else "尚无报告"
        )
        summary = (
            f"最近一次是「{head['title']}」，竞品 {'/'.join(head['competitors']) or '未记录'}，"
            f"{_status_cn(head['status'])}，创建于 {_time_cn(head['created_at'])}；{report_desc}。"
        )
        if len(items) > 1:
            summary += f"另有 {len(items) - 1} 条更早的记录。"
    return {"items": items, "summary": summary}


# ── ② trace：按 report_id 取证据链 ──────────────────────────────────────

_TRACE_SQL = """
SELECT e.claim        AS claim,
       e.source_url   AS source_url,
       e.source_text  AS source_text,
       e.dimension    AS dimension,
       e.created_at   AS created_at,
       r.id::text     AS report_id,
       r.version      AS version,
       t.id::text     AS task_id,
       t.title        AS title,
       t.competitors  AS competitors
  FROM evidence_map e
  JOIN reports r ON r.id = e.report_id
  JOIN tasks   t ON t.id = r.task_id
 WHERE t.user_id = $1
   AND ($2::text[] IS NULL OR t.competitors ?| $2::text[])
   AND ($3::text   IS NULL OR e.claim ILIKE '%' || $3 || '%')
 ORDER BY t.created_at DESC, e.created_at DESC
 LIMIT $4
"""


async def trace_evidence(
    pool,
    user_id: str,
    competitors: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> dict:
    """按竞品 / 关键词取证据行（claim + 来源 URL + 原文片段）。"""
    competitors = [c for c in (competitors or []) if str(c).strip()] or None
    keyword = (keyword or "").strip() or None
    rows = await pool.fetch(_TRACE_SQL, user_id, competitors, keyword, limit)

    items = [{
        "claim": row["claim"],
        "source_url": row["source_url"],
        "source_text": row["source_text"],
        "dimension": row["dimension"],
        "observed_at": _iso(row["created_at"]),
        "report_id": row["report_id"],
        "task_id": row["task_id"],
        "title": row["title"],
        "competitors": _as_list(row["competitors"]),
    } for row in rows]

    if not items:
        summary = "没有找到匹配的证据记录。"
        if keyword:
            summary += f"（结论里没有出现「{keyword}」）"
    else:
        head = items[0]
        summary = (
            f"「{_short(head['claim'])}」\n"
            f"来源：{head['source_url']}\n"
            f"原文：{_short(head['source_text'], 120)}\n"
            f"抓取于 {_time_cn(head['observed_at'])}（报告「{head['title']}」）"
        )
        if len(items) > 1:
            summary += f"\n另有 {len(items) - 1} 条相关证据。"
    return {"items": items, "summary": summary}


# ── ③ verify：核对事实是否过期 ──────────────────────────────────────────

async def check_freshness(
    pool,
    user_id: str,
    competitors: list[str] | None = None,
    keyword: str | None = None,
    ttl_days: int | None = None,
    limit: int = 10,
) -> dict:
    """按证据的抓取时间判 fresh / stale，不做重新采集。

    ⚠️ 这不是"校验事实真伪"，只是"这条结论多久没被核对过"。
    真伪要重新抓页面比原文，属于 P3 的范围，别在这里冒充。
    """
    rows = await trace_evidence(pool, user_id, competitors, keyword, limit)
    items = rows["items"]
    if not items:
        return {"items": [], "summary": "没有可核对的事实记录。"}

    requested_ttl = ttl_days or ttl_days_for(keyword or " ".join(competitors or []))
    now = datetime.now(timezone.utc)

    fresh, stale = [], []
    for item in items:
        item["ttl_days"] = ttl_days_for(item["claim"] + " " + (item["dimension"] or ""))
        observed = item["observed_at"]
        age_days = None
        if observed:
            observed_dt = datetime.fromisoformat(observed)
            age_days = (now - observed_dt).days
        item["age_days"] = age_days
        item["status"] = "unknown" if age_days is None else ("stale" if age_days >= item["ttl_days"] else "fresh")
        (stale if item["status"] == "stale" else fresh).append(item)

    parts = []
    if stale:
        head = stale[0]
        # ⚠️ 措辞守死 Q7：说"超过保鲜期"，不说"已过期"。
        #   保鲜期是我们自己设的阈值，事实有没有变我们并不知道 ——
        #   一没重新抓页面、二没比对原文，说"已过期"就是拿阈值冒充事实。
        parts.append(
            f"{len(stale)} 条已超过保鲜期。最久的是「{_short(head['claim'], 60)}」，"
            f"{head['age_days']} 天前抓的，保鲜期 {head['ttl_days']} 天。"
        )
    if fresh:
        head = fresh[0]
        parts.append(
            f"{len(fresh)} 条仍在保鲜期内。最近的是「{_short(head['claim'], 60)}」，"
            f"{head['age_days']} 天前抓的。"
        )
    parts.append("以上只判断了「多久没核对」，不代表事实已变；要核实需重新抓取原文。")
    return {"items": items, "summary": "\n".join(parts), "requested_ttl_days": requested_ttl}
