"""意图识别分层漏斗 — L1 确定性 → L2 语义 → L3 LLM → L4 澄清。

【职责】
  做：把一句话判成 4 个意图之一，并交代判在**哪一层**、置信多少、抽到什么实体。
  不做：不执行意图（执行在 query_handlers / pipeline / supervisor），不建任务、不写库。

【输入 → 输出】
  run_funnel(query, competitors, dimensions, settings) -> FunnelResult
  FunnelResult(intent, layer, confidence, margin, entities, clarify, reason)
  intent ∈ {analysis, lookup, trace, verify, unknown}

【关键决策】
  ★ 层级按**单位成本递增**排，不按准确率递增 ← 同一次判断能用便宜的就别上贵的
  ★ 需要槽位的意图一律下沉 L3，只有槽位全可选的 lookup 停在 L2 ← L2 是纯分类器，
    恒返回零实体；analysis 缺竞品接不上执行，trace/verify 缺 keyword 会"问 A 答 B"
  ★ L2 用 top1 与 top2 的**差值**判犹豫，不用绝对相似度 ← 绝对分随句长与领域漂移
  ★ 判不出来就下沉，L4 只问不猜 ← 猜错要跑一整份报告，多问一句便宜得多
  ★ 漏斗只决定"走哪条执行链路"，不决定执行策略 ← 后者仍归 IntentRouter.classify()

【读法】
   这里的 L1~L4 是**识别层内部**的层级。执行侧那两段叫「意图层 / 策略层」，

   FLOOR / MARGIN 的默认值是经验值，上线前必须用自己语料跑 calibrate() 定。
  ️ 分析类查询会先付一次 embedding 再进 LLM（多 ~50ms），
     换来三条非分析意图各省一次 LLM（~500ms）。这笔账是净赚，但要知道它存在。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from src.agents import create_llm_client
from src.config import Settings
from src.mcp.tools_rag import embed_texts

logger = logging.getLogger(__name__)

INTENTS = ("analysis", "lookup", "trace", "verify")

# ── 哪些意图允许停在 L2 ────────────────────────────────────────────────────
# ★ 判据是「这个意图有没有**必填槽位**」，不是它叫什么名字
#   L2 是纯分类器：`l2_semantic` 返回的 entities 恒为 _empty_entities()、keyword 恒 None，
#   它一个槽位都给不出。所以能停在 L2 的，必须「没有槽位也能把活干对」。
#     lookup  → competitors / dimensions / skip 全可选，缺了就是"查最近一次"，语义完整
#     trace   → keyword 定生死：没有它，会把该报告引用的**全部** URL 列出来
#     verify  → 同上：没有它，核对的是整份报告的新鲜度
#   后两者的失败是"问 A 答 B"（答错），比多付一次 LLM 严重 —— 故下沉。
INTENTS_L2_SAFE = ("lookup",)
INTENTS_NEED_SLOTS = ("trace", "verify")

# ── 各意图的示例句（L2 的原型）────────────────────────────────────────────
# 这 28 句（6/6/8/8）就是 L2 的"训练集"，改动等于改模型行为。加句子要同步跑 calibrate()。
# ⚠️ 但别指望靠加句子提准确率：L2 只在 intent ∈ INTENTS_L2_SAFE 时才返回，
#    而那一类只有 lookup。补 verify/trace 的句子**不会**让它们停在 L2，
#    作用只是"别让 lookup 把它们抢走"——抢走就是直接答错，下沉到 L3 还有一次机会。
EXEMPLARS: dict[str, tuple[str, ...]] = {
    "analysis": (
        "对比飞书和钉钉在功能和定价上的表现",
        "帮我做一份协同办公竞品的分析报告",
        "分析一下王者荣耀和英雄联盟在玩法设计上的差异",
        "调研 Notion 和语雀的产品定位区别",
        "把这三家公司的商业化能力做个横向对比",
        "生成一份竞品分析，竞品是飞书、钉钉、Notion",
    ),
    "lookup": (
        "上次分析的那两家，报告还在吗",
        "我之前跑过的竞品分析在哪能找到",
        "把最近一次的报告发我看看",
        "我一共分析过哪些公司",
        "那份飞书钉钉的报告找出来",
        "上周那个分析任务跑完了没有",
    ),
    "trace": (
        "飞书比钉钉便宜这个结论，证据在哪",
        "报告里那句话的出处是哪个网页",
        "这个数据的来源链接给我",
        "凭什么说 Notion 定价最高，有原文吗",
        "把引用的原始片段调出来看看",
        # ↓ 2026-10-29 补：短问法与"引用了几篇"这类计数问法。
        #   老 5 条全是"证据/出处 + 长句"，用户只打"来源网址"时落不到 trace。
        "这几条结论的来源列一下",
        "这个数字出自哪里",
        "原文是怎么写的",
    ),
    "verify": (
        "报告里飞书 25 元，现在还是这个价吗",
        "这个定价过期了没有，帮我核一下",
        "官网上现在写的价格和报告里一致吗",
        "上次抓的数据还准不准",
        "这家的融资消息有没有更新",
        # ↓ 2026-10-29 补：**不带具体数值**的时效问法。
        #   老 5 条都长着"某个数字 + 现在还是吗"的样子，于是"保鲜期过了没"
        #   "数据新鲜度怎么样"在 L2 被 lookup 抢走、在 L3 被丢给 unknown。
        "数据新鲜度怎么样，多久没更新了",
        "这份报告还能信吗，保鲜期过了没",
        "上次分析的结论现在还算数吗",
    ),
}

# ── L3 prompt：意图与实体**同一次调用**产出 ────────────────────────────────
# ★ 与现状代码衔接的关键：routes.py 原先只为抽实体调一次 LLM，
#   把 intent 塞进同一份输出，新增意图识别就**不增加调用次数**。
# ★ 这份 prompt 由**两个入口共用**（2026-10-27）：
#   /api/chat 走 run_funnel 用它；/api/tasks 的 _extract_entities_from_query 也用它，
#   只是后者只取 competitors/dimensions，忽略 intent/keyword。
#   别再拆成两份：两份 prompt 必然漂移，同一个 query 在两个入口抽出不同实体。
INTENT_PROMPT = """判断用户这句话属于哪一类，并抽取实体。

类别（只选一个）：
- analysis: 要一份新的竞品分析/对比，或对某几家再做一轮分析
- lookup:   找回自己以前跑过的任务/报告**本身**（在哪、是什么、跑完没有、列出来）
- trace:    问某个结论/数字**从哪来**（来源、出处、引用、原文、哪个网页、链接）
- verify:   问某个结论/数据**现在是否还成立**（过没过期、还准不准、有没有更新、
            新鲜度、几号观测的、要不要重跑、还能不能信）
- unknown:  信息不足，无法判断

判别顺序（命中即止，不要跳步）：
1) 要新产出              → analysis
2) 问"哪来的"            → trace
3) 问"现在还成立吗/新不新" → verify
4) 问"我以前哪次跑的"     → lookup

★ 出现「上次报告/那份报告」**不代表** lookup，看诉求：
  「上次报告现在还能用吗」→ verify
  「上次报告引用了哪些网站」→ trace
  「上次报告在哪」          → lookup
★ 抽象问法照样算，不要求出现具体数值、竞品名或"是不是过期"这种字眼：
  「保鲜期过了没」「这份报告还能信吗」「数据新鲜度怎么样」「多久没更新了」
  「这个结论是不是该重跑了」都是 verify；
  「来源网址」「原文呢」「这个数字出自哪里」都是 trace。
★ 判 verify/trace 时 keyword 填最核心的检索短语（如"定价""新鲜度""来源"），没有就留空。

输出格式（仅 JSON，无其他文字）：
{"intent": "analysis", "competitors": [], "dimensions": [], "keyword": "", "intent_is_clear": false}

规则：
- competitors: 竞品名称列表，没提到返回 []
- dimensions: 分析维度列表（功能/定价/市场等），没提到返回 []
- keyword: 仅 trace/verify 需要 —— 用于筛选结论的短语（如"定价""便宜""25 元"），其他意图留空
- intent_is_clear: 只有意图和实体都明确时才为 true
- ★ 只有在**完全看不出诉求**时才输出 unknown（如"飞书和钉钉""嗯""那个东西"）。
  只要按上面 4 条判据能落到某一类，就必须给出该类，哪怕句子里没有具体数值或竞品名。
  unknown 的代价是用户被反问你到底想干嘛，比判得粗一点更伤。
- 若输入里带【会话上下文】，只用它消解「那两家」「改成四个维度」这类指代，
  判定的对象**始终是【本轮】那一句**；上下文里出现过的竞品不要自动继承

用户输入：%s
JSON 输出："""


@dataclass
class FunnelResult:
    """漏斗一次判定的完整交代。"""

    intent: str
    layer: str
    confidence: float = 0.0
    margin: float = 0.0
    entities: dict = field(default_factory=dict)
    keyword: str | None = None
    clarify: str | None = None
    reason: str = ""
    scores: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """给 API 层用的扁平结构（不含内部 scores）。"""
        return {
            "intent": self.intent,
            "layer": self.layer,
            "confidence": self.confidence,
            "margin": self.margin,
            "entities": self.entities,
            "keyword": self.keyword,
            "clarify": self.clarify,
            "reason": self.reason,
        }


def _empty_entities() -> dict:
    return {"competitors": [], "dimensions": [], "intent_is_clear": False}


# ── L1 确定性：零成本，只收"参数已经摆明"的流量 ────────────────────────────

def l1_deterministic(
    query: str | None,
    competitors: list[str] | None,
    dimensions: list[str] | None,
) -> FunnelResult | None:
    """
       结构化入参齐全就直接判定，零 LLM 零 embedding。
       规则一多就变成维护灾难，且容易把 analysis 误判成 lookup。
       L1 只收"用户把参数填进表单"这一种确定情形。
    """
    cs = [c for c in (competitors or []) if str(c).strip()]
    ds = [d for d in (dimensions or []) if str(d).strip()]

    if cs and ds:
        return FunnelResult(
            intent="analysis",
            layer="L1",
            confidence=1.0,
            entities={"competitors": cs, "dimensions": ds, "intent_is_clear": True},
            reason="结构化入参齐全（competitors + dimensions）",
        )

    if not (query or "").strip() and (cs or ds):
        return FunnelResult(
            intent="analysis",
            layer="L1",
            confidence=0.9,
            entities={"competitors": cs, "dimensions": ds, "intent_is_clear": False},
            reason="无自然语言输入，参数不全 → 交给执行侧判 pipeline/supervisor",
        )

    return None


# ── L2 语义：一次 embedding，比 LLM 便宜一个数量级 ────────────────────────

_EXEMPLAR_VECS: dict[str, list[list[float]]] = {}
_EXEMPLAR_LOCK = asyncio.Lock()


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；维度不一致或空向量返回 0（当作不可比）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na ** 0.5 * nb ** 0.5)


async def _exemplar_vectors(settings: Settings) -> dict[str, list[list[float]]]:
    """把示例句编码成向量，进程内只做一次。

    ⚠️ 编码不可用（API 挂了 / 返回条数对不上）时返回 {}，
    调用方必须当作"L2 放弃"而不是"分数全是 0"——后者会把所有流量误判成同一类。
    """
    if _EXEMPLAR_VECS:
        return _EXEMPLAR_VECS
    async with _EXEMPLAR_LOCK:
        if _EXEMPLAR_VECS:
            return _EXEMPLAR_VECS

        texts: list[str] = []
        spans: list[tuple[str, int, int]] = []
        for intent in INTENTS:
            start = len(texts)
            texts.extend(EXEMPLARS[intent])
            spans.append((intent, start, len(texts)))

        vectors = await embed_texts(texts, settings)
        if not vectors or len(vectors) != len(texts):
            logger.warning("L2 示例句编码失败（%d/%d），本次跳过语义层", len(vectors or []), len(texts))
            return {}

        for intent, start, end in spans:
            _EXEMPLAR_VECS[intent] = vectors[start:end]
        return _EXEMPLAR_VECS


async def l2_semantic(
    text: str,
    settings: Settings | None = None,
    *,
    floor: float | None = None,
    margin: float | None = None,
) -> FunnelResult | None:
    """embedding 分类：类内 max 聚合 + floor/margin 双条件。

    返回 None 表示"L2 判不了"，调用方下沉到 L3。

    ★ 两个阈值**实测分工**（2026-10-29 在 12 条域外样本上量的，别再按"想当然"写）：
        floor=0.55 —— **几乎挡不住域外**。域外句子的 top1 最低 0.560、中位 0.714
          （"飞书和钉钉" 0.873、"报告" 0.748）。bge-m3 的绝对余弦刻的是"话题沾不沾边"，
          不是"是不是这个意图"，任何提到竞品/报告/数据的短句都拿高分。
          它现在的唯一作用：挡住编码异常（全 0 向量 → _cosine 返 0）。
        margin=0.10 —— **真正在挡的是它**。12 条域外里 11 条 gap<0.10 被拦；
          "今天天气怎么样" top1/top2 都是 0.583、gap=0.000，靠 floor 根本拦不住。
    域外还有第三道：意图白名单（只有 lookup 能停在 L2，见 INTENTS_L2_SAFE）。
    唯一漏网的是"定价"（gap=0.102 判 verify），因为 verify 要下沉，最后被 L3 判成 unknown。

    ⚠️ 因此**别靠调 floor 治"域外被硬塞"**——调不动，那是模型性质；要治就治 margin
       或收紧白名单。也别因为"域外 top1 很高"就以为 L2 没用：它拦的是类间模糊，
       不是话题无关。
    """
    cfg = settings or Settings()
    floor = cfg.intent_funnel_floor if floor is None else floor
    margin = cfg.intent_funnel_margin if margin is None else margin

    vectors = await _exemplar_vectors(cfg)
    if not vectors:
        return None

    encoded = await embed_texts([text], cfg)
    if not encoded or not encoded[0]:
        return None
    query_vec = encoded[0]

    scores = {intent: max(_cosine(query_vec, v) for v in vs) for intent, vs in vectors.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (top_intent, top1), (second_intent, top2) = ranked[0], ranked[1]
    gap = top1 - top2
    accepted = top1 >= floor and gap >= margin

    logger.info(
        "L2 语义: %r → %s top1=%.3f top2=%.3f gap=%.3f floor=%.2f margin=%.2f %s",
        text[:40], top_intent, top1, top2, gap, floor, margin,
        "接受" if accepted else "下沉 L3",
    )
    if not accepted:
        return None

    return FunnelResult(
        intent=top_intent,
        layer="L2",
        confidence=round(top1, 4),
        margin=round(gap, 4),
        entities=_empty_entities(),
        reason=f"语义层 top1={top_intent}({top1:.3f}) 领先 {second_intent}({top2:.3f})",
        scores={k: round(v, 4) for k, v in scores.items()},
    )


# ── L3 LLM：意图与实体同一次调用产出 ──────────────────────────────────────

def parse_intent_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍 ``` 围栏与前后废话）。

    公开给 routes.py 的实体抽取复用 —— 两个入口必须用同一个解析器，
    否则"模型多输出一行废话"在两个入口的容错表现会不一致。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text[3:]
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_entities(data: dict) -> tuple[list[str], list[str]]:
    """把模型输出的 competitors/dimensions 规整成干净的字符串列表。

    ★ 公开给 routes.py 的实体抽取复用：类型防御（可能是字符串而非列表）
      与空值过滤只该有**一份**实现，否则两个入口对同一份输出的接受度会不一致。
    """
    competitors = data.get("competitors") or []
    dimensions = data.get("dimensions") or []
    if isinstance(competitors, str):
        competitors = [competitors]
    if isinstance(dimensions, str):
        dimensions = [dimensions]
    competitors = [c for c in competitors if isinstance(c, str) and c.strip()]
    dimensions = [d for d in dimensions if isinstance(d, str) and d.strip()]
    return competitors, dimensions


async def l3_llm(
    text: str, settings: Settings | None = None, history_block: str = ""
) -> FunnelResult:
    """一次 LLM 调用同时拿意图与实体；失败兜底 analysis。

    ⚠️ 兜底选 analysis 而不是 unknown：模型挂了就退回现状行为
    （走 pipeline/supervisor 那张老图），不要让整个入口因为分类器故障而不可用。

    history_block（可选）：会话上下文前缀，只给 L3 —— 它负责消解"那两家"这类指代。
    L1/L2 不接它：规则与原型向量匹配的是"这一句"，掺上文会让指代句拿到假相似度。
    """
    cfg = settings or Settings()
    data: dict = {}
    try:
        llm = create_llm_client(cfg, temperature=0.0)
        resp = await llm.ainvoke(INTENT_PROMPT % (history_block + text))
        data = parse_intent_json(getattr(resp, "content", "") or "")
    except Exception:
        logger.exception("L3 LLM 意图判定失败，兜底 analysis")

    intent = str(data.get("intent") or "").strip().lower()
    if intent not in INTENTS and intent != "unknown":
        intent = "analysis" if not data else "unknown"

    competitors, dimensions = normalize_entities(data)
    keyword = str(data.get("keyword") or "").strip() or None

    logger.info("L3 LLM: %r → intent=%s competitors=%s dimensions=%s keyword=%r",
                text[:40], intent, competitors, dimensions, keyword)
    return FunnelResult(
        intent=intent,
        layer="L3",
        confidence=0.8 if intent in INTENTS else 0.0,
        entities={
            "competitors": competitors,
            "dimensions": dimensions,
            "intent_is_clear": bool(data.get("intent_is_clear")) and bool(competitors and dimensions),
        },
        keyword=keyword,
        reason="LLM 一次调用同时产出意图与实体",
    )


# ── L4 澄清：只问不猜 ────────────────────────────────────────────────────

CLARIFY_QUESTION = "这句话我没把握该怎么处理，你是想做什么？"

# ★ 选项自带**可直接发送的完整句子**（2026-10-27 改）。
#   之前是 ("analysis","lookup","trace","verify") 四个意图 key，前端只能发回
#   "②" 这种序号 → 下一轮漏斗再判一次"②"→ 四层全判不出 → 再反问，死循环。
#   ⚠️ 契约：每个 message 都必须能被下一轮漏斗判回它对应的意图。
#   写新选项时先想清楚"这句话 L2/L3 认不认"，否则等于换个方式死循环。
CLARIFY_CHOICES: tuple[dict[str, str], ...] = (
    {"label": "分析几家竞品", "message": "分析飞书和钉钉在功能和定价上的表现"},
    {"label": "找以前跑过的报告", "message": "我以前跑过的竞品分析报告在哪"},
    {"label": "查某条结论的证据", "message": "报告里飞书比钉钉便宜这个结论的证据在哪"},
    {"label": "核对数据是否过期", "message": "报告里飞书那个定价现在还是这个价吗"},
)

# 判出 analysis 但一个锚点都没有 —— 与 L4 的"意图不明"是两件事：
# 前者知道要什么、只是缺参数；后者连要什么都不知道。文案与选项都不该共用。
NO_ANCHOR_QUESTION = "要分析哪几家？至少给我竞品或维度之一。"
NO_ANCHOR_CHOICES: tuple[dict[str, str], ...] = (
    {"label": "分析飞书和钉钉", "message": "分析飞书和钉钉在功能和定价上的表现"},
    {"label": "按市场看定价", "message": "从定价角度分析协同办公市场"},
)

# 灰度开关关闭时的问法：此时多意图整体下线，不能让用户以为 lookup 还能用。
FUNNEL_OFF_QUESTION = "对话模式当前未开启，只能做竞品分析。要分析哪几家？"


def l4_clarify(text: str) -> FunnelResult:
    """前面三层都不确定时问一句，绝不猜。

    ★ 澄清比猜错便宜：猜成 analysis 会跑一整份报告（分钟级 + 全量检索），
      问一句只是多一轮对话。
    """
    logger.info("L4 澄清: %r", text[:40])
    return FunnelResult(
        intent="unknown",
        layer="L4",
        confidence=0.0,
        entities=_empty_entities(),
        clarify=CLARIFY_QUESTION,
        reason="前三层均未给出可接受判定",
    )


# ── 编排入口 ─────────────────────────────────────────────────────────────

async def run_funnel(
    *,
    query: str | None = None,
    competitors: list[str] | None = None,
    dimensions: list[str] | None = None,
    settings: Settings | None = None,
    history_block: str = "",
) -> FunnelResult:
    """按成本从低到高跑完漏斗，返回第一个可接受的判定。

    顺序即策略：L1 零成本 → L2 一次 embedding → L3 一次 LLM → L4 问用户。
    history_block 只在 L3 生效（见 l3_llm 注释），单轮调用方不传即可。
    """
    cfg = settings or Settings()

    # ★ 灰度开关（D3）：关闭时退回"一句话都当分析"，而不是关掉整个入口。
    #   注意**实体抽取照跑** —— 现状 /api/tasks 的 query 模式本来就抽一次，
    #   不抽的话"分析飞书和钉钉"也会被判成没锚点而追问，开关一关就不可用了。
    #   区别只在于：不判多意图，所以"报告还在吗"不会被当成查询，而是走追问。
    if not cfg.intent_funnel_enabled:
        text = (query or "").strip()
        if not text:
            return FunnelResult(
                intent="analysis",
                layer="off",
                entities=_empty_entities(),
                reason="intent_funnel_enabled=false 且无自然语言输入",
            )
        llm_result = await l3_llm(text, cfg, history_block)
        llm_result.intent = "analysis"
        llm_result.layer = "off"
        llm_result.keyword = None
        llm_result.reason = "intent_funnel_enabled=false，只保留实体抽取"
        return llm_result

    result = l1_deterministic(query, competitors, dimensions)
    if result is not None:
        return result

    text = (query or "").strip()
    if not text:
        # 既无结构化参数又无自然语言 → 交给 API 层的参数校验去 400
        return FunnelResult(
            intent="unknown",
            layer="L1",
            entities=_empty_entities(),
            reason="无任何可判定输入",
        )

    semantic = await l2_semantic(text, cfg)
    # ★ 只有槽位全可选的意图才允许停在 L2 —— analysis 要实体，trace/verify 要 keyword。
    #   这里放开一个，就等于允许一次"问 A 答 B"，比多付一次 LLM 贵。
    if semantic is not None and semantic.intent in INTENTS_L2_SAFE:
        return semantic
    if semantic is not None and semantic.intent in INTENTS_NEED_SLOTS:
        # 记一行：下沉不是免费的，要能回答"这个规则到底多花了多少"
        logger.info("L2 判出 %s 但缺必填槽位，下沉 L3: %r", semantic.intent, text[:40])

    llm_result = await l3_llm(text, cfg, history_block)
    if llm_result.intent in INTENTS:
        return llm_result

    return l4_clarify(text)


# ── 离线标定（不改行为，只回答"阈值该定多少"）─────────────────────────────

async def calibrate(
    samples: list[tuple[str, str]],
    settings: Settings | None = None,
    floors: tuple[float, ...] = (0.35, 0.40, 0.45, 0.50, 0.55),
    margins: tuple[float, ...] = (0.02, 0.03, 0.05, 0.08),
) -> list[dict]:
    """在带标注的样本上扫 floor × margin，返回各组合的命中率/覆盖率。

    samples: [(query, 真实 intent)]。只看 L2 单独的表现——L2 放弃（返回 None）
    的样本记为"下沉"，其真实意图若本来就不是 analysis，这格要靠 L3 兜，
    所以覆盖率比准确率更该看：**宁可多下沉，不要错接受**。
    """
    cfg = settings or Settings()
    vectors = await _exemplar_vectors(cfg)
    if not vectors:
        return []

    queries = [q for q, _ in samples]
    encoded = await embed_texts(queries, cfg)
    if not encoded or len(encoded) != len(queries):
        return []

    scored: list[tuple[dict, str]] = []
    for (query, truth), qv in zip(samples, encoded):
        scores = {intent: max(_cosine(qv, v) for v in vs) for intent, vs in vectors.items()}
        scored.append((scores, truth))

    report: list[dict] = []
    for floor in floors:
        for margin in margins:
            accepted = 0
            correct = 0
            per_intent: dict[str, int] = {}
            for scores, truth in scored:
                ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
                (top_intent, top1), (_, top2) = ranked[0], ranked[1]
                if top1 >= floor and (top1 - top2) >= margin:
                    accepted += 1
                    if top_intent == truth:
                        correct += 1
                        per_intent[truth] = per_intent.get(truth, 0) + 1
            report.append({
                "floor": floor,
                "margin": margin,
                "coverage": round(accepted / len(scored), 3) if scored else 0.0,
                "precision": round(correct / accepted, 3) if accepted else 0.0,
                "accepted_by_intent": per_intent,
            })
    return report
