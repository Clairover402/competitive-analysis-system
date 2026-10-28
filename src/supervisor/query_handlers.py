"""三个查询类意图的 SQL 执行器 — lookup / trace / verify。

【职责】
  做：把"意图 + 实体"翻译成一条 SQL，返回可直接念给用户的结构化结果。
  不做：不做语义匹配（那是 L3 的事）、不写库、不调 Agent、不生成报告。

【输入 → 输出】
  lookup_reports(pool, user_id, competitors, dimensions)  -> {"items": [...], "summary": str}
  trace_evidence(pool, user_id, competitors, keyword)     -> 同上
  check_freshness(pool, user_id, competitors, keyword)    -> 同上（附 fresh/stale 判定）

【关键决策】
  ★ 三跳全走 SQL，零 LLM ← 用户问的是**确定指代**（"上次""那份"），
    语义相似度会召回错任务，确定性查询只能用确定性工具
  ★ 排序一律 created_at DESC ← "上次"的字面含义就是时间最近，不需要模型理解
  ★ lookup 也念事实正文（2026-10-28 补）← 问「上次钉钉定价多少」要的是那句话，
    不是"有这么一份报告"。取法与 verify 相同：subject 定位 + content 第 2 行，
    读的是同一张表、同一个 `_fact_claim`，不新增检索路径、不走向量
  ★ verify 的数据源是 `agent_memories` 的 fact（带 observed_at / ttl_days），
    不是 evidence_map ← M3 之后两者分工明确：证据答"引用过什么"，
    事实答"当前有效观测是哪条"，只有后者能判"多久没核对"
  ★ 竞品过滤用 jsonb `?|` 而不是 LIKE ← tasks.competitors 是 JSONB 数组，
    LIKE 会把"飞书"匹配进任何含该子串的句子
  ★ 查不到就如实说查不到 ← 空结果编一句"可能是网络问题"最伤可信度
  ★ verify 的保鲜期**读取时现算**，优先用偏好 `ttl_overrides` ← 若只信落库的
    ttl_days，用户改完配置会看到"设置没生效"，而库里那列是写入时的快照
    （理由见 _effective_ttl）

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


def _as_objects(value: Any) -> list[dict]:
    """`json_agg` 出来的对象数组 → list[dict]；asyncpg 未注册 codec 时给字符串。

    与 `_as_list` 分开是因为两者校验的形态不同：一个是字符串数组，
    一个是对象数组。混用一个「能解析就返回」的宽容实现，会把
    `["a","b"]` 也悄悄放行，调用方随后对 str 取 `.get()` 才炸。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


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


def _override_hit(
    blob: str,
    dimension: str,
    overrides: dict[str, int] | None,
) -> tuple[int, str] | None:
    """在用户配置里找命中的关键词，返回 (天数, 命中的关键词)。

    先按维度、再按正文 ← 维度是结构化锚点，正文是自然语言：
    「与飞书相比钉钉的价格更轻」不该改变一条「功能」类事实的档位。
    """
    if not overrides:
        return None
    for key, days in overrides.items():
        if key and key in dimension:
            return days, key
    for key, days in overrides.items():
        if key and key in blob:
            return days, key
    return None


def ttl_days_for(
    text: str | None,
    dimension: str | None = None,
    overrides: dict[str, int] | None = None,
) -> int:
    """给事实挑一个保鲜期档位：用户配置 → 内置关键词表 → 默认值。

    `overrides` 来自偏好 `ttl_overrides`（见 `preferences.load_overrides`）。
    不传时行为与改造前完全一致，老调用方无需改。
    """
    dim = str(dimension or "")
    blob = f"{text or ''} {dim}".strip()

    hit = _override_hit(blob, dim, overrides)
    if hit:
        return hit[0]

    for keywords, days in _TTL_BY_KEYWORD:
        if any(k in blob for k in keywords):
            return days
    return DEFAULT_TTL_DAYS


def _effective_ttl(
    item: dict,
    overrides: dict[str, int] | None,
) -> tuple[int, str | None]:
    """读取侧定档：用户配置 > 落库快照 > 现场按表补算。

    ★ 为什么不直接信落库的 ttl_days：偏好可以在写入之后才改。
      以落库值优先等于「配置改了、历史数据不认」，而用户看不到这个例外，
      只会觉得设置没生效。落库值退化成「当时的默认档位」，只做兜底。

    返回 (天数, 命中的偏好关键词)；第二项为 None 表示没走到用户配置。
    """
    dim = str(item.get("dimension") or "")
    blob = f"{item.get('claim') or ''} {dim}"

    hit = _override_hit(blob, dim, overrides)
    if hit:
        return hit[0], hit[1]
    if item.get("ttl_days"):
        return int(item["ttl_days"]), None
    return ttl_days_for(blob, dim), None


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
       r.quality_score   AS quality_score,
       f.facts           AS facts
  FROM tasks t
  LEFT JOIN LATERAL (
        SELECT id, version, quality_score
          FROM reports
         WHERE task_id = t.id
         ORDER BY version DESC
         LIMIT 1
  ) r ON TRUE
  -- 该任务提炼出的事实。取的键是 source_task_id（不是 subject 全库最新）：
  --   问的是「上次分析里定价多少」，答案就该是那一次提炼的那条。
  --   LIMIT 8 是因为单任务 fact 通常 3~6 条，给足余量又不会把整行撑爆。
  LEFT JOIN LATERAL (
        SELECT json_agg(json_build_object(
                   'subject',     m.subject,
                   'content',     m.content,
                   'observed_at', m.observed_at
               ) ORDER BY m.observed_at DESC NULLS LAST) AS facts
          FROM (SELECT subject, content, observed_at
                  FROM agent_memories
                 WHERE source_task_id = t.id
                   AND memory_type = 'fact'
                   AND status = 'active'          -- 被顶掉的旧观测不念
                   AND is_active = true
                 ORDER BY observed_at DESC NULLS LAST
                 LIMIT 8) m
  ) f ON TRUE
 WHERE t.user_id = $1
   AND ($2::text[] IS NULL OR t.competitors ?| $2::text[])
 ORDER BY t.created_at DESC
 LIMIT $3
"""

# 回落查询：该任务没提炼到时要找的那条事实。
# ★ 为什么要有它：问「飞书定价多少」，若最近一次分析恰好没提炼定价，
#   而库里有一条三个月前的 active 事实，回一句"这次没提炼到"等于装作不知道。
# ★ 这里才按 subject 前缀取全库最新（上限查询是按 task 取的），两者分工：
#   先去答"上次那次说了什么"，答不上再答"最近一次知道的观测是哪条"。
_FACT_BY_SUBJECT_SQL = """
SELECT m.subject                         AS subject,
       m.content                         AS content,
       m.observed_at                     AS observed_at,
       m.source_task_id::text            AS task_id,
       t.title                           AS task_title
  FROM agent_memories m
  LEFT JOIN tasks t ON t.id = m.source_task_id
 WHERE m.user_id = $1
   AND m.memory_type = 'fact'
   AND m.status = 'active'
   AND m.is_active = true
   -- 竞品过滤走 subject 前缀，不用 content LIKE：对比句会让 LIKE 串竞品
   AND ($2::text[] IS NULL
        OR m.subject ~ ('^(' || array_to_string($2::text[], '|') || ')\\|'))
   AND ($3::text[] IS NULL
        OR m.subject ~ ('\\|(' || array_to_string($3::text[], '|') || ')$'))
 ORDER BY m.observed_at DESC NULLS LAST
 LIMIT $4
"""


def _age_cn(age_days: int | None) -> str:
    """距今天数的口语化。★ 「0 天前」不是人话，写成「今天」。"""
    if age_days is None:
        return ""
    if age_days <= 0:
        return "今天"
    if age_days == 1:
        return "昨天"
    return f"{age_days} 天前"


def _pick_facts(
    raw_facts: Any,
    dimensions: list[str] | None,
    limit: int = 2,
) -> list[dict]:
    """从该任务的事实里挑出要给用户念的那几条。

    ★ 维度优先：用户问「定价多少」时，把同一任务里「功能」的那条也念出来，
      不是信息丰富，是答非所问。没给维度就按 observed_at 取最新一条。
    ⚠️ 只挑不判：fresh / stale 是 verify 的职责（它还要叠用户偏好），
      这里只报「观测于哪天、距今几天」，让用户自己看新旧。
    """
    wanted = [str(d).strip() for d in (dimensions or []) if str(d).strip()]
    now = datetime.now(timezone.utc)
    picked: list[dict] = []
    for raw in _as_objects(raw_facts):
        competitor, dimension = _split_subject(raw.get("subject"))
        claim = _fact_claim(raw.get("content") or "")
        if not claim:
            continue
        if wanted and not any(w in dimension or dimension in w for w in wanted):
            continue
        observed = raw.get("observed_at")
        # ⚠️ json_agg 出来的时间戳是**字符串**（asyncpg 不对 json 里的值做类型转换），
        #   只有直接查列的 verify 才拿到 datetime。两种形态都要认，
        #   否则 age_days 恒为 None，摘要里就少一句"几天前"。
        observed_dt = None
        if isinstance(observed, datetime):
            observed_dt = observed
        elif isinstance(observed, str):
            try:
                observed_dt = datetime.fromisoformat(observed)
            except ValueError:
                observed_dt = None
        if observed_dt is not None and observed_dt.tzinfo is None:
            # naive 时间戳与 now(aware) 相减会 TypeError，按 UTC 补齐
            observed_dt = observed_dt.replace(tzinfo=timezone.utc)
        age_days = (now - observed_dt).days if observed_dt else None
        picked.append({
            "competitor": competitor,
            "dimension": dimension,
            "claim": claim,
            "observed_at": _iso(observed_dt) or (observed if isinstance(observed, str) else None),
            "age_days": age_days,
            # 只有回落查询会带这两项；上限查询（同任务）由调用方补
            "source_task_id": raw.get("task_id"),
            "source_title": raw.get("task_title"),
        })
        if len(picked) >= limit:
            break
    return picked


def describe_tasks(items, *, dims=None, competitors=None) -> str:
    """把一次 lookup 的结果说成人话。纯函数：不查库、不读时钟、不看全局状态。

    【为什么要单独一个函数】SQL 变了而文案忘了改，产出的是比"答不上"更糟的东西：
    跳过了几条还说"最近一次"、库里有 20 次却说"另有 4 条"。抽成函数是为了让
    「改 SQL」和「改说法」落在同一个视线里，也让文案能被直接断言而不必造池子。

    ⚠️ 输入契约：items 是 `lookup_reports` 建好的那种形态 —— created_at 已由
       `_iso()` 转成字符串、facts 已挑过。直接喂 asyncpg 的原始记录会崩（`_time_cn`
       只吃字符串），所以这函数不是"给任何 row 都能用"的通用格式化器。
    """
    if not items:
        return "没有找到匹配的历史任务。" + ("（按竞品筛选后为空）" if competitors else "")

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

    # 若念的结论全都不是这次分析提的，先交代一句 —— 否则读者会以为
    # 上面那份报告 v1 里写着这句话，而它其实来自更早的一次分析。
    cross = [f for f in head["facts"] if f.get("source_task_id") != head["task_id"]]
    if cross and len(cross) == len(head["facts"]):
        summary += "\n这次分析没提炼到对应结论，最新一条相关观测在更早的分析里："

    # 念结论时**必须带观测日期**：只报"45 元"不报是哪天观测的，
    # 等于把旧价当现价念 —— 那比答不上更糟。
    for fact in head["facts"]:
        when = (fact["observed_at"] or "")[:10] or "未知日期"
        age = _age_cn(fact["age_days"])
        tail = f"，{age}" if age else ""
        # 结论不是这次分析提的，要说清出处，否则用户会以为它出自上面那份报告
        origin = ""
        if fact.get("source_task_id") and fact["source_task_id"] != head["task_id"]:
            origin = f" · 来自「{_short(fact.get('source_title') or '更早一次分析', 18)}」"
        summary += (
            f"\n{fact['competitor']}·{fact['dimension']}："
            f"{_short(fact['claim'], 60)}（观测于 {when}{tail}{origin}）"
        )
    if not head["facts"]:
        # 诚实降级。两种情况回话不同，混成一句会误导：
        #   ① 一条事实都没有 → 任务可能没跑完，或提炼失败
        #   ② 有事实但没有这个维度 → 是该维度没提炼，不是整个没提炼
        dim_desc = f"「{'/'.join(dims)}」的" if dims else ""
        if head["fact_count"] and dims:
            summary += f"\n这次分析没提炼到{dim_desc}结论，只有其他维度的事实。"
        elif head["fact_count"]:
            # 有事实行但一条都念不出来（空正文/占位串）——不能说"有其他维度"，
            # 因为没有"其他维度"这回事，只是这批行不可用。
            summary += "\n这次分析的事实里没有可用结论，要看报告正文。"
        else:
            summary += f"\n这次分析还没提炼成事实（{dim_desc}结论要看报告正文）。" if dims \
                else "\n这次分析还没提炼成事实，结论要看报告正文。"
    if len(items) > 1:
        summary += f"\n另有 {len(items) - 1} 条更早的记录。"
    return summary


async def lookup_reports(
    pool,
    user_id: str,
    competitors: list[str] | None = None,
    dimensions: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """按用户（可选再按竞品、维度）取最近的任务与报告，并把结论一并念出来。

    【2026-10-28 补缺口】此前只回标题/状态/版本，问「上次钉钉定价多少」
    只答得上"有这么一份报告"。现在多挂一条 LATERAL 取该任务提炼的 fact，
    读 content 第 2 行当答案 —— 与 verify 用的是同一张表、同一个 `_fact_claim`，
    不新增检索路径，也不走向量（见模块头"读取靠键、写入靠向量"）。
    """
    competitors = [c for c in (competitors or []) if str(c).strip()] or None
    dims = [d for d in (dimensions or []) if str(d).strip()] or None
    rows = await pool.fetch(_LOOKUP_SQL, user_id, competitors, limit)

    items = []
    for row in rows:
        raw_facts = _as_objects(row.get("facts"))
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
            # fact_count 是「这个任务一共提炼了几条」，用于区分「没提炼」与
            # 「提炼了但没有你要的维度」——两种情况的回话不一样，别混成一句。
            "fact_count": len(raw_facts),
            "facts": _pick_facts(raw_facts, dims),
        })

    if items:
        head = items[0]
        # 先看「上次那次分析」自己提炼了什么；没有就回落到全库最新一条。
        # 回落这一步单独一次查询，且只在 miss 时走 —— 常走的路径仍然是一条 SQL。
        if not head["facts"]:
            # ⚠️ 这里要显式 dict(row)：直查拿到的是 asyncpg.Record，
            #   不是 dict，`_pick_facts` 会把它当"非对象"整批丢掉（踩过，静默空）。
            fallback_rows = await pool.fetch(_FACT_BY_SUBJECT_SQL, user_id, competitors, dims, 6)
            head["facts"] = _pick_facts([dict(r) for r in fallback_rows], dims)
        for fact in head["facts"]:
            fact.setdefault("source_task_id", head["task_id"])
            fact.setdefault("source_title", head["title"])

    return {
        "items": items,
        "summary": describe_tasks(items, dims=dims, competitors=competitors),
    }


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

# 【为什么这条 SQL 在本文件而不是 DAO】本文件三个执行器只依赖 `pool.fetch()`
#   一个方法（见文件头"零 DB 测试"那段），这是刻意维持的约定：三个查询意图
#   要做到"给个假池就能测"，多一层 DAO 就多一层 acquire() 要伪造。
_FACTS_SQL = """
SELECT id::text     AS memory_id,
       content      AS content,
       subject      AS subject,
       ttl_days     AS ttl_days,
       observed_at  AS observed_at,
       created_at   AS created_at,
       evidence_url AS evidence_url,
       source_task_id::text AS task_id
  FROM agent_memories
 WHERE user_id = $1
   AND memory_type = 'fact'
   AND status = 'active'          -- 被取代的旧观测不参与"当前是否新鲜"
   AND is_active = true
   -- 竞品过滤走 subject 前缀（`飞书|定价`），不用 content LIKE：
   -- "与飞书相比，钉钉…"这类对比句会让 LIKE 把竞品 X 的事实串到竞品 Y 上
   AND ($2::text[] IS NULL
        OR subject ~ ('^(' || array_to_string($2::text[], '|') || ')\\|'))
   AND ($3::text IS NULL OR content ILIKE '%' || $3 || '%')
 ORDER BY observed_at DESC NULLS LAST, created_at DESC
 LIMIT $4
"""


def _fact_claim(content: str) -> str:
    """从 fact 正文里取出结论行。

    模板由 `memory.semantic.FACT_TEMPLATE` 固定：第 1 行是「[事实] 竞品·维度（观测 日期）」，
    第 2 行是结论，之后是来源。这里只做展示，取不到就整段兜底——
    与其为了"必须取到第 2 行"抛异常，不如让用户看到完整正文。
    """
    lines = [ln for ln in (content or "").splitlines() if ln.strip()]
    return lines[1] if len(lines) >= 2 else (content or "")


def _split_subject(subject: str | None) -> tuple[str, str]:
    """`飞书|定价` → ("飞书", "定价")。"""
    left, _, right = str(subject or "").partition("|")
    return left or "未知竞品", right or "综合"


async def check_freshness(
    pool,
    user_id: str,
    competitors: list[str] | None = None,
    keyword: str | None = None,
    ttl_days: int | None = None,
    limit: int = 10,
) -> dict:
    """按事实的观测时间判 fresh / stale，不做重新采集。

    ⚠️ 这不是"校验事实真伪"，只是"这条结论多久没被核对过"。
    真伪要重新抓页面比原文，属于另一条链路，别在这里冒充。

    【2026-10-27 M3：数据源从 evidence_map 切到 agent_memories 的 fact】
    ------------------------------------------------------------
    两者答的不是同一个问题：
      evidence_map —— "某份报告引用过哪些原文"（逐条引用，含原文片段，用来溯源）
      fact 记忆   —— "关于某竞品的某个维度，当前有效的观测是哪一条"
                     （带 observed_at / ttl_days / 取代链）
    用户问"这些结论还新不新"，答案在 fact 上：只有它知道哪条已被更晚的观测取代、
    每条该按什么保鲜期算。查不到 fact 就如实说"还没提炼过"，
    不要退回去用证据行的 created_at 假扮事实——那是拿引用时间冒充观测时间。

    【2026-10-28 保鲜期可配】读取侧现读 `ttl_overrides` 偏好并优先采用，
    所以改一次偏好，历史 fact 立刻按新档位判定，不需要回填脚本。
    """
    competitors = [c for c in (competitors or []) if str(c).strip()] or None
    keyword = (keyword or "").strip() or None

    from src.memory.preferences import load_overrides   # 局部 import：避免 supervisor ↔ memory 循环

    overrides = await load_overrides(pool, user_id)
    rows = await pool.fetch(_FACTS_SQL, user_id, competitors, keyword, limit)

    items = []
    for row in rows:
        competitor, dimension = _split_subject(row.get("subject"))
        claim = _fact_claim(row.get("content") or "")
        items.append({
            "claim": claim,
            "source_url": row.get("evidence_url") or "",
            "source_text": None,          # fact 不存原文片段，要原文走 trace
            "dimension": dimension,
            "competitor": competitor,
            "observed_at": _iso(row.get("observed_at") or row.get("created_at")),
            "ttl_days": row.get("ttl_days"),
            "memory_id": str(row.get("memory_id") or ""),
            "task_id": row.get("task_id"),
        })

    if not items:
        return {"items": [], "summary": "没有可核对的事实记录。先跑一次分析，系统会把结论提成带保鲜期的事实。"}

    requested_ttl = ttl_days or ttl_days_for(
        keyword or " ".join(competitors or []), None, overrides
    )
    now = datetime.now(timezone.utc)

    fresh, stale = [], []
    applied_keys: list[str] = []      # 被用户配置改过档的关键词，用于给回执
    for item in items:
        # 偏好 > 落库快照 > 现场补算（见 _effective_ttl 的说明）
        ttl, hit_key = _effective_ttl(item, overrides)
        item["ttl_days"] = ttl
        item["ttl_from_user"] = bool(hit_key)
        if hit_key and hit_key not in applied_keys:
            applied_keys.append(hit_key)
        observed = item["observed_at"]
        age_days = None
        if observed:
            observed_dt = datetime.fromisoformat(observed)
            age_days = (now - observed_dt).days
        item["age_days"] = age_days
        item["status"] = (
            "unknown" if age_days is None
            else ("stale" if age_days >= ttl else "fresh")
        )
        (stale if item["status"] == "stale" else fresh).append(item)

    parts = []
    if stale:
        head = stale[0]
        # ⚠️ 措辞守死：说"超过保鲜期"，不说"已过期"。
        #   保鲜期是我们自己设的阈值，事实有没有变我们并不知道 ——
        #   一没重新抓页面、二没比对原文，说"已过期"就是拿阈值冒充事实。
        parts.append(
            f"{len(stale)} 条已超过保鲜期。最久的是「{_short(head['claim'], 60)}」，"
            f"{head['age_days']} 天前观测的，保鲜期 {head['ttl_days']} 天。"
        )
    if fresh:
        head = fresh[0]
        parts.append(
            f"{len(fresh)} 条仍在保鲜期内。最近的是「{_short(head['claim'], 60)}」，"
            f"{head['age_days']} 天前观测的。"
        )
    if applied_keys:
        # ★ 改了配置必须看到回执：否则判定从「在保鲜期内」变成「超期」时，
        #   用户分不清是设置生效了还是数据真的老了。
        parts.append(
            f"保鲜期按你自己设置的「{'、'.join(applied_keys)}」计算，不是内置档位。"
        )
    parts.append("以上只判断了「多久没核对」，不代表事实已变；要核实需重新抓取原文。")
    return {
        "items": items,
        "summary": "\n".join(parts),
        "requested_ttl_days": requested_ttl,
        "ttl_overrides_applied": applied_keys,
    }
