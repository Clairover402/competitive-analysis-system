"""意图分类 —— 一句话进，一个 label 出。

【职责】
  L2 向量 → L3 LLM → L4 追问，返回 `Decision`。
【不做】
  抽竞品/维度/keyword（slots.py）、执行查询（query_handlers）、组织追问文案（clarify.py）。
【关键决策】
  ★ **没有 L1**（2026-10-29 删）。原来那层"结构化入参齐全就判 analysis"零流量：
    `/api/chat` 拿不到 competitors/dimensions，`/api/tasks` 又不调 `classify()`——
    它用 `intent_is_clear = bool(competitors and dimensions)` 一个布尔值就做完了。
    **判据：一层的存在价值 = 它在生产路径上有没有输入。没有输入就不叫规则层，
    叫死代码。** 留着它的唯一后果是报告里多一个需要解释的 0。
  ★ `Decision` 里**没有槽位字段**，这是刻意的结构约束。加回 entities/keyword
    就等于把"意图与槽位耦合"重新放回来，而这正是 2026-10-29 拆掉的东西。
  ★ L2 过阈即停，不再按意图类别看白名单。旧白名单只有 lookup 能停，理由是
    "analysis 要实体、trace/verify 要 keyword"——那是**下游缺不缺输入**，
    不是**这次判得准不准**。实测代价与收益见 l2_semantic 的 docstring。
  ★ 删白名单后"把 lookup 误判成 analysis"仍不会跑错报告：槽位阶段抽不到锚点
    就追问。第三道防线从"意图层按类别拦"移到了"槽位层必填校验"。
【读法】
  日志里 L2 的 top1/gap 说明这层在不在干活；阈值该定多少问 `calibrate()`。
  `layer="none"` 表示空输入连层都没进；`layer="off"` 是灰度开关关掉漏斗。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from src.agents import create_llm_client
from src.config import Settings
from src.mcp.tools_rag import embed_texts
from src.supervisor.intent.clarify import CLARIFY_QUESTION
from src.supervisor.intent.labels import EXEMPLARS, INTENTS, INTENT_PROMPT, UNKNOWN
from src.supervisor.intent.parsing import parse_json_object

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """一次意图判定的完整交代。**只有 label，没有槽位。**"""

    intent: str
    layer: str
    confidence: float = 0.0
    margin: float = 0.0
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
            "clarify": self.clarify,
            "reason": self.reason,
        }


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
) -> Decision | None:
    """embedding 分类：类内 max 聚合 + floor/margin 双条件。过阈即停。

    返回 None 表示"L2 判不了"，调用方下沉到 L3。

    ★ 两个阈值**实测分工**（2026-10-29 在 12 条域外样本上量的，别按"想当然"写）：
        floor=0.55 —— **几乎挡不住域外**。域外句子的 top1 最低 0.560、中位 0.714
          （"飞书和钉钉" 0.873、"报告" 0.748）。bge-m3 的绝对余弦刻的是"话题沾不沾边"，
          不是"是不是这个意图"。它现实的作用：挡编码异常（全 0 向量 → _cosine 返 0）。
        margin=0.15 —— **真正在挡的是它**。12 条域外里 11 条 gap<0.10 被拦；
          "今天天气怎么样" top1/top2 都是 0.583、gap=0.000，靠 floor 拦不住。
          定 0.15 不是拍脑袋：150 条上 4 条错例 gap=0.100/0.109/0.125/0.143 全在 0.15 左侧，
          gap≥0.15 的 24 条零错误 —— 门槛正好卡在"分得清"与"没分清"之间。
    ⚠️ 别靠调 floor 治"域外被硬塞"——调不动，那是模型性质；要治就治 margin。

    ★ 2026-10-29 删掉按类别的白名单（原 `INTENTS_L2_SAFE = ("lookup",)），
      改成"过阈即停 + margin 分档"。140 条非边界样本上三档实测：
        只放行 lookup       端到端 97.9%，L2 接管 10 条(6.7%)
        margin=0.10        端到端 95.7%，L2 接管 46 条(30.7%)，接管内 40/43
        margin=0.15        端到端 98.6%，L2 接管 24 条(16.0%)，接管内 24/24
      ⇒ 白名单是拿"类别缺不缺槽位"去否决"这次判得准不准"；换成 margin 分档后，
        接管率仍是拆前的 2.4 倍，准确率反超 0.7 个点。
      原白名单挡掉的三类里，只有 analysis 是真必填，而它的误判会被槽位阶段的
      "缺锚点追问"接住，不会跑错报告 —— 所以"多接"本身不是风险，"没把握还接"才是。
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

    return Decision(
        intent=top_intent,
        layer="L2",
        confidence=round(top1, 4),
        margin=round(gap, 4),
        reason=f"语义层 top1={top_intent}({top1:.3f}) 领先 {second_intent}({top2:.3f})",
        scores={k: round(v, 4) for k, v in scores.items()},
    )


# ── L3 LLM：只判 label，不问槽位 ─────────────────────────────────────────

async def l3_llm(
    text: str, settings: Settings | None = None, history_block: str = ""
) -> Decision:
    """一次 LLM 调用，只拿意图 label。

    ⚠️ 兜底选 analysis 而不是 unknown：模型挂了就退回现状行为
    （走 pipeline/supervisor 那张老图），不要让整个入口因为分类器故障而不可用。

    history_block（可选）：会话上下文前缀，只给 L3 —— 它负责消解"那两家"这类指代。
    L1/L2 不接它：规则与原型向量匹配的是"这一句"，掺上文会让指代句拿到假相似度。

    ★ 2026-10-29：这里**不再**要求模型输出 competitors/dimensions/keyword。
      槽位由 slots.fill_slots 在 label 确定后单独处理。合在一起时，
      L2 判对却为了拿槽位被要求整句重判，等于把已做对的部分丢掉再做一遍。
    """
    cfg = settings or Settings()
    data: dict = {}
    try:
        llm = create_llm_client(cfg, temperature=0.0)
        resp = await llm.ainvoke(INTENT_PROMPT % (history_block + text))
        data = parse_json_object(getattr(resp, "content", "") or "")
    except Exception:
        logger.exception("L3 LLM 意图判定失败，兜底 analysis")

    intent = str(data.get("intent") or "").strip().lower()
    if intent not in INTENTS and intent != UNKNOWN:
        intent = "analysis" if not data else UNKNOWN

    logger.info("L3 LLM: %r → intent=%s", text[:40], intent)
    return Decision(
        intent=intent,
        layer="L3",
        confidence=0.8 if intent in INTENTS else 0.0,
        reason="LLM 只判意图标签",
    )


# ── L4 澄清：只问不猜 ────────────────────────────────────────────────────

def l4_clarify(text: str) -> Decision:
    """前面三层都不确定时问一句，绝不猜。

    ★ 澄清比猜错便宜：猜成 analysis 会跑一整份报告（分钟级 + 全量检索），
      问一句只是多一轮对话。
    """
    logger.info("L4 澄清: %r", text[:40])
    return Decision(
        intent=UNKNOWN,
        layer="L4",
        confidence=0.0,
        clarify=CLARIFY_QUESTION,
        reason="前三层均未给出可接受判定",
    )


# ── 编排入口 ─────────────────────────────────────────────────────────────

async def classify(
    *,
    query: str | None = None,
    settings: Settings | None = None,
    history_block: str = "",
) -> Decision:
    """按成本从低到高跑完分类，返回第一个可接受的 label。

    顺序即策略：L2 一次 embedding → L3 一次 LLM → L4 问用户。
    history_block 只在 L3 生效（见 l3_llm 注释），单轮调用方不传即可。

    ⚠️ 这里**不返回槽位**。调用方拿到 label 后自行决定要不要 `fill_slots`：
       analysis 必填、trace/verify 的 keyword 可空、lookup 不需要。

    ★ 2026-10-29 删掉 L1（确定性规则层），漏斗从四层变三层。理由不是"没接上线"：
      它想判的「参数齐全 ⇒ analysis」已经被**产品结构**表达掉了 ——
      标准模式在 `_create_task_impl` 里用 `intent_is_clear = bool(competitors
      and dimensions)` 一个布尔值就完成了；对话模式压根拿不到结构化参数。
      所以本函数的 `competitors`/`dimensions` 参数也一并删了：没有分支读它们。
    """
    cfg = settings or Settings()

    # ★ 灰度开关（D3）：关闭时退回"一句话都当分析"，而不是关掉整个入口。
    #   槽位抽取不受开关影响 —— 它已独立成 fill_slots，由调用方照常调用。
    #   区别只在于：不判多意图，所以"报告还在吗"不会被当成查询，而是走追问。
    if not cfg.intent_funnel_enabled:
        return Decision(
            intent="analysis",
            layer="off",
            reason="intent_funnel_enabled=false，不判多意图（槽位照抽）",
        )

    text = (query or "").strip()
    if not text:
        # 空输入连层都进不去 → layer="none"，不冒充某一层的结论。
        # API 层本会用 Pydantic 400 拦住，这里只防内部调用方漏传。
        return Decision(
            intent=UNKNOWN,
            layer="none",
            reason="无任何可判定输入",
        )

    semantic = await l2_semantic(text, cfg)
    if semantic is not None:
        return semantic

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
