"""三个查询类意图的 SQL 执行器 — lookup / trace / verify。

【职责】
  做：把"意图 + 实体"翻译成一条 SQL，返回可直接念给用户的结构化结果。
  不做：不做语义匹配（那是 L3 的事）、不写库、不调 Agent、不生成报告。

【输入 → 输出】
  parse_ordinal(text) -> {"skip": n, "limit": m} | {"unsupported": "最早"} | None
  lookup_reports(pool, user_id, competitors, dimensions, limit, skip)
      -> {"items": [...], "total": int | None, "summary": str}
  trace_evidence(pool, user_id, competitors, keyword)     -> 同上
  check_freshness(pool, user_id, competitors, keyword)    -> 同上（附 fresh/stale 判定）

【关键决策】
  ★ 三跳全走 SQL，零 LLM ← 用户问的是**确定指代**（"上次""那份"），
    语义相似度会召回错任务，确定性查询只能用确定性工具
  ★ 排序一律 created_at DESC ← "上次"的字面含义就是时间最近，不需要模型理解
  ★ 序数（上上次 / 前两次 / 第五次）由 `parse_ordinal` 用**规则**抽，不进 L3 ←
    封闭词表能穷举，交给模型只会多一次调用且拿不到结构化字段；L2 更不行，
    它返回的是类别得分。三条链路各自的分工见 parse_ordinal 上方注释
  ★ `ORDER BY` 必须带 `t.id DESC` 兜底 ← created_at 只到毫秒，批量建任务会并列，
    加了 OFFSET 之后一并列两次请求就会拿到不同行（"上上次"漂移）
  ★ SQL 的 WHERE 只写一份（_TASKS_FILTER），lookup 与 count 共用 ← 两处各写一遍
    就等于两个真相，改一处忘一处会让"一共几次"数错
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
import re
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

# ── 序数抽取：只在这一层用规则 ───────────────────────────────────────────
# 判据是「能不能枚举」：
#   封闭词表（第几次 / 前几次 / 全部 / 最早）→ 正则，零成本、可测、错了能改
#   开放词表（竞品名 / 维度 / 结论短语）      → 只能交给 L3 的 LLM
# 这也正是它**不放进 L3 prompt** 的原因：两处都能抽就是两套真相。
# L2 更不行 —— 它返回的是类别得分，给不出结构化字段（见 INTENT_L2_SAFE）。
#
# ⚠️ 中文的时间方向有歧义：「第 2 次」可以是"从最早数"也可以是"从最近数"。
#    这里一律按**从最近往回数**（= 倒数），因为 lookup 的语境天然是"上次/之前"。
#    因此「第一次 / 最早」不按 1 处理，走 unsupported 明说查不了 —— 答"最近一次"
#    是错的，承认做不到才对。
_ALL_LIMIT = 50          # "全部 / 都列出来"的上限；超过就明说只列这么多
_MAX_SKIP = 20
_MAX_LIMIT = 50

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM_CN = "一二三四五六七八九十"


def _cn_int(token: str) -> int | None:
    """把「两」「三」「7」转成 int；认不出返回 None（"一百"这种不展开）。"""
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def _ordinal_cn(n: int) -> str:
    """1~10 说成中文（"倒数第二次"比"倒数第 2 次"像人话），其余用数字。"""
    return _NUM_CN[n - 1] if 1 <= n <= 10 else str(n)


# ★ 量词在中文里几乎是自由变量（次 / 回 / 个 / 遍），**按说法枚举必然漏**：
#   实测漏过一次 —— 表里只写了「上上次|上上回」，用户说「上上个任务」就没命中，
#   静默退回 skip=0（最近一次），把倒数第二次的分析当最新的答出去。
#   不报错、不崩，只是答错 —— 所以这里枚举的是**结构**：
#   「上」重复几次 = 往前数几次，量词交给字符类自由匹配。
# ⚠️ 统一带 (?!月)：「上上个」结尾没有量词也算序号，但「上上个月」「第 3 个月」
#   说的是时间不是序号，两个都不能当 skip 用。
_W = r"[次回个遍](?!月)"


def parse_ordinal(text: str | None) -> dict | None:
    """从用户原话里抽「第几次」。不命中返回 None，走默认（最近一次）。

    ⚠️ 顺序敏感，两处不能调换：
       ① 「第一次/最早」必须先于「第 N 次」—— 否则会被当成 skip=0 的"最近一次"
       ② 「第 N 次」单独一条正则，不能和「前/最近 N 次」共用 —— 前缀不同，
          共用会让「前两次」被解析成 skip=1
    ⚠️ 「上次 / 最近一次」**故意不在表里**：它们等于默认行为，写进来只是把
       "不命中"改写成"命中 skip=0"，多一个分支却没有行为差异。
    ⚠️ 不命中是**静默**的 —— 这是本函数唯一危险的地方。新增说法时先补测试，
       否则用户问「上上个」会得到「最近一次」的答案而不自知。
    """
    s = (text or "").strip()
    if not s:
        return None

    # 「上上」= 倒数第二、「上上上」= 倒数第三。数「上」的个数，不枚举说法：
    # 老写法把「上上上次」「上上次」写成两条并列正则，量词换个字就漏。
    m = re.search(rf"(上{{2,}})\s*{_W}", s)
    if m:
        skip = len(m.group(1)) - 1
        return {"skip": skip, "limit": 1} if skip <= _MAX_SKIP else None

    m = re.search(rf"倒数第\s*([一二两三四五六七八九十]+|\d+)\s*{_W}", s)
    if m:
        n = _cn_int(m.group(1))
        return {"skip": n - 1, "limit": 1} if n and n - 1 <= _MAX_SKIP else None

    m = re.search(rf"(?:最近|前)\s*([一二两三四五六七八九十]+|\d+)\s*{_W}", s)
    if m:
        n = _cn_int(m.group(1))
        # ⚠️ n 必须 ≥ 2：「最近一次」「前一次」就等于默认行为，命中只会把
        #    limit 从 5 压到 1，反而丢掉"还有 N 条"那半句。
        return {"skip": 0, "limit": n} if n and 2 <= n <= _MAX_LIMIT else None

    if re.search(r"全部|所有|都列出|每一条", s):
        return {"skip": 0, "limit": _ALL_LIMIT}

    if re.search(r"最早|最初|头一次|第一次|第一回|头一个|第一个", s):
        return {"unsupported": "最早"}

    m = re.search(rf"第\s*([一二两三四五六七八九十]+|\d+)\s*{_W}", s)
    if m:
        n = _cn_int(m.group(1))
        return {"skip": n - 1, "limit": 1} if n and 0 < n - 1 <= _MAX_SKIP else None

    return None


# ★ 只写一份 WHERE：正常查询与"越界后补 count"必须用同一个过滤条件，
#   否则数出来的总数和列表对不上（改了一处忘了另一处，是这类 bug 的固定来源）。
_TASKS_FILTER = """
 WHERE t.user_id = $1
   AND ($2::text[] IS NULL OR t.competitors ?| $2::text[])
"""

_COUNT_TASKS_SQL = f"SELECT count(*) AS n FROM tasks t{_TASKS_FILTER}"

_LOOKUP_SQL = f"""
SELECT t.id::text        AS task_id,
       t.title           AS title,
       t.competitors     AS competitors,
       t.dimensions      AS dimensions,
       t.status          AS status,
       t.created_at      AS created_at,
       r.id::text        AS report_id,
       r.version         AS version,
       r.quality_score   AS quality_score,
       f.facts           AS facts,
       -- 窗口函数在 LIMIT 之前算，所以每行都带着全量总数。
       -- ⚠️ 加 OFFSET 之后要记住：越界时一行都不返回，这个值也拿不到，
       --    那种情况得由调用方补一次 count，别指望窗口函数兜。
       COUNT(*) OVER ()  AS total
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
{_TASKS_FILTER}
 -- ★ t.id DESC 不是装饰：created_at 只到毫秒，批量建任务会并列；
 --   加了 OFFSET 之后，一并列两次请求就会拿到不同行，"上上次"会漂。
 ORDER BY t.created_at DESC, t.id DESC
 LIMIT $3 OFFSET $4
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


def describe_tasks(items, *, total=None, skip=0, dims=None, competitors=None,
                   page=None, page_size=None) -> str:
    """把一次 lookup 的结果说成人话。纯函数：不查库、不读时钟、不看全局状态。

    【为什么要单独一个函数】SQL 变了而文案忘了改，产出的是比"答不上"更糟的东西：
    跳过了几条还说"最近一次"、库里有 20 次却说"另有 4 条"。抽成函数是为了让
    「改 SQL」和「改说法」落在同一个视线里，也让文案能被直接断言而不必造池子。

    【page/page_size 为什么影响文案】分页模式下有三句话必须改口，否则会自相矛盾：
      · "最近 N 次里最新的是" → 第 2 页里根本没有"最近"这回事
      · "另有 N 条更早的记录" → 翻页由分页控件承担，再说一句就重复了
      · 空结果的"没有找到" → 翻到末页之后库里有数据，只是这一页没内容

    ⚠️ 输入契约：items 是 `lookup_reports` 建好的那种形态 —— created_at 已由
       `_iso()` 转成字符串、facts 已挑过。直接喂 asyncpg 的原始记录会崩（`_time_cn`
       只吃字符串），所以这函数不是"给任何 row 都能用"的通用格式化器。
    """
    paged = bool(page and page_size)
    scope = "、".join(competitors) if competitors else ""

    if not items:
        # ★ 空结果有两种，别混成一句：库里真没有，和"有，但跳过这么多条就没了"。
        #   后者回"没有找到匹配的历史任务"是谎报 —— 库里有，只是不在这个位置。
        if paged:
            if total:
                return (f"「{scope}」相关分析一共 {total} 份，没有第 {page} 页 —— "
                        f"最多翻到第 {-(-total // page_size)} 页。")
            return f"没有「{scope}」相关的分析记录。" if scope else "还没有任何分析记录。"
        if skip and total:
            return f"你一共只有 {total} 次分析记录，没有倒数第{_ordinal_cn(skip + 1)}次。"
        return "没有找到匹配的历史任务。" + ("（按竞品筛选后为空）" if competitors else "")

    head = items[0]
    # 称呼必须跟着 skip / page / 条数走：
    #   · 跳过了两条还说"最近一次是…" → 用户以为拿到的是最新的那份
    #   · 返回了两条却只说"最近一次是…" → 用户以为只要到一条
    if paged:
        # 分页只列清单，不逐条念结论（列表已经把它们画出来了）。
        lead = f"「{scope}」相关分析共 {total} 份，第 {page} 页最新的是" if scope \
            else f"相关分析共 {total} 份，第 {page} 页最新的是"
    elif skip:
        lead = f"倒数第{_ordinal_cn(skip + 1)}次是"
    elif len(items) > 1:
        # 数量用阿拉伯数字：「两次」对、「二次」不对，而 _ordinal_cn 给的是后者。
        # 序数说「倒数第二次」、数量说「最近 2 次」，各用各的才都像人话。
        lead = f"最近 {len(items)} 次里最新的是"
    else:
        lead = "最近一次是"
    # ⚠️ report 是 LEFT JOIN：跑完没出报告时 version 也是 None，
    #   直接 `v{version}` 会念成"报告版本 vNone"（实测踩过）。
    report_desc = (
        f"报告版本 v{head['version']}（{head['report_id']}）"
        if head["version"] is not None
        else "尚无报告"
    )
    summary = (
        f"{lead}「{head['title']}」，竞品 {'/'.join(head['competitors']) or '未记录'}，"
        f"{_status_cn(head['status'])}，创建于 {_time_cn(head['created_at'])}；{report_desc}。"
    )

    # ★ 结论只在单条时念。多任务时把 head 的结论混进列表，读者会把"某一次的
    #   结论"读成"这几次的共同结论" —— 一条摘要里混两个任务的判断，就是这个来源。
    #   多任务的活是"让你看见有哪几次"，结论要点进去看。
    if len(items) == 1:
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
            src_task = fact.get("source_task_id")
            # ⚠️ 判据是 `src_task is None or !=`，不是 `src_task and !=`：
            #   None 是"出处未知"（回落来的行 source_task_id 可能为空），
            #   它同样不等于当前任务，照样得标出处。用 and 会让这批悄悄不标，
            #   读者就以为这话出自上面那份报告。
            if src_task is None or src_task != head["task_id"]:
                origin = f" · 来自「{_short(fact.get('source_title') or '更早一次分析', 18)}」"
            summary += (
                f"\n{fact['competitor']}·{fact['dimension']}："
                f"{_short(fact['claim'], 60)}（观测于 {when}{tail}{origin}）"
            )
        if not head["facts"]:
            # 诚实降级。先按"有没有报告"分叉：没有报告时说"结论要看报告正文"
            # 是空话 —— 用户翻不到。三种情况的回话互不替代：
            #   ① 没产出报告 → 直说，别让人去翻
            #   ② 有报告但没提炼到你要的维度 → 只有其他维度
            #   ③ 有报告、有事实行，但一条都念不出来（空正文/占位串）
            dim_desc = f"「{'/'.join(dims)}」的" if dims else ""
            if head["report_id"] is None:
                summary += "\n这次分析没有产出报告。"
            elif head["fact_count"] and dims:
                summary += f"\n这次分析没提炼到{dim_desc}结论，只有其他维度的事实。"
            elif head["fact_count"]:
                summary += "\n这次分析的事实里没有可用结论，要看报告正文。"
            else:
                summary += f"\n这次分析还没提炼成事实（{dim_desc}结论要看报告正文）。" if dims \
                    else "\n这次分析还没提炼成事实，结论要看报告正文。"
    # ⚠️ 判据是 rest > 0，不是 len(items) > 1。skip=1、limit=1 时只返回一条，
    #    但确实还有更早的记录要说 —— 旧条件会把这句话吞掉。
    # ⚠️ 必须减掉 skip：跳过 1 条、显示 1 条时，剩下的不是 total-1 而是 total-2。
    #    这两个错在 C3（没有 skip）里都看不见，加了 OFFSET 才会露出来 ——
    #    也正是"改 SQL 和改文案必须在一处"的理由。
    rest = (total - skip - len(items)) if total else (len(items) - 1)
    # 分页模式下这句话由分页控件承担，再说一遍就是同一件事讲两次。
    if rest > 0 and not paged:
        # 恰好取满且没有更多时，别念"另有 0 条"
        summary += (
            f"\n另有 {rest} 条更早的记录"
            + (f"（一共 {total} 次分析）" if total else "") + "。"
        )
    return summary


async def lookup_reports(
    pool,
    user_id: str,
    competitors: list[str] | None = None,
    dimensions: list[str] | None = None,
    limit: int = 5,
    skip: int = 0,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    """按用户（可选再按竞品、维度）取最近的任务与报告，并把结论一并念出来。

    【2026-10-28 补缺口】此前只回标题/状态/版本，问「上次钉钉定价多少」
    只答得上"有这么一份报告"。现在多挂一条 LATERAL 取该任务提炼的 fact，
    读 content 第 2 行当答案 —— 与 verify 用的是同一张表、同一个 `_fact_claim`，
    不新增检索路径，也不走向量（见模块头"读取靠键、写入靠向量"）。

    【skip 从哪来】由 `parse_ordinal` 在 routes 层按规则抽好，这里只收数字。
    刻意不在这里解析自然语言 —— 本模块的性质是"只依赖 pool.fetch 的 SQL 执行器"，
    一旦开始 import re 读用户原话，"给个假池就能测"就没了。

    【page/page_size 与 skip 是两套语义，不叠加】page_size 给出时按分页算：
    `skip = (page-1) * page_size`，传进来的 skip 被丢弃。叠加会让"第 2 页的
    上上次"有定义 —— 这个位置不存在，只是把两种偏移硬加在一起。
    """
    competitors = [c for c in (competitors or []) if str(c).strip()] or None
    dims = [d for d in (dimensions or []) if str(d).strip()] or None
    if page_size:
        page = max(1, int(page or 1))
        limit, skip = page_size, (page - 1) * page_size
    else:
        page = None

    rows = await pool.fetch(_LOOKUP_SQL, user_id, competitors, limit, skip)
    # 库里的真实总数（窗口函数给的），用来把"另有 N 条"说准。
    # 空结果时无从得知，留 None 让 describe_tasks 退回窗口内计数。
    total = rows[0].get("total") if rows else None
    if not rows:
        # ⚠️ OFFSET 越界时一行都不返回，窗口函数算好的 total 也就拿不到 ——
        #    窗口函数是"每行带上总数"，没有行就没有总数。这里补问一次。
        #    分页把这个路径从"罕见"变成了"翻到末页就会走"，所以必须准。
        counted = await pool.fetch(_COUNT_TASKS_SQL, user_id, competitors)
        total = counted[0]["n"] if counted else None

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
        # 先看「上次那次分析」自己提炼了什么；没有就回落到库里的同竞品事实。
        # 回落这一步单独一次查询，且只在 miss 时走 —— 常走的路径仍然是一条 SQL。
        if not head["facts"]:
            # ⚠️ 回落的锚点必须是**这个任务自己的竞品**，不能是请求里的过滤条件。
            #   请求没带竞品时 $2 是 NULL，条件退化成"全库最新 6 条"，于是把
            #   不相干竞品的结论挂到这个任务头上（实测：「我上上个任务是啥」答出
            #   一个"竞品未记录、尚无报告"的任务，底下却念着 GitHub Copilot 的定价）。
            #   连竞品都没记的任务根本无从对齐，跳过回落比乱念好。
            scope = head.get("competitors") or []
            if scope:
                # ⚠️ 显式 dict(row)：直查拿到的是 asyncpg.Record 不是 dict，
                #   `_pick_facts` 会把它当"非对象"整批丢掉（踩过，静默空）。
                fallback_rows = await pool.fetch(_FACT_BY_SUBJECT_SQL, user_id, scope, dims, 6)
                head["facts"] = _pick_facts([dict(r) for r in fallback_rows], dims)
        for fact in head["facts"]:
            fact.setdefault("source_task_id", head["task_id"])
            fact.setdefault("source_title", head["title"])

    pagination = None
    if page and page_size:
        # total 为 None 只会出现在"补 count 也失败"，按 0 页处理而不是崩
        pages = max(1, -(-(total or 0) // page_size))
        pagination = {
            "page": page,
            "page_size": page_size,
            "total": total or 0,
            "total_pages": pages,
            "has_prev": page > 1,
            "has_next": page < pages,
        }

    return {
        "items": items,
        "total": total,
        "pagination": pagination,
        "summary": describe_tasks(items, total=total, skip=skip, dims=dims,
                                  competitors=competitors,
                                  page=page, page_size=page_size),
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
