"""槽位填充 —— 意图确定之后的第二段。

【职责】
  知道 label 之后，决定要抽哪些槽，并抽取。
【不做】
  判意图（label 是入参）、执行查询（query_handlers）、追问（clarify.py）。
【关键决策】
  ★ 该不该抽一个槽，判据是「**缺了下游会不会答错**」，不是「这个意图有什么槽」：
      analysis 缺竞品/维度 → 检索没有锚点，根本跑不起来     → 必抽
      trace    缺竞品       → `t.competitors ?| $2` 失效，会列出别的竞品的证据 → 必抽
      verify   缺竞品       → 核该用户全部事实，**正是「数据新不新」要的** → 不抽
      lookup   缺序数       → 退回最近一次，是合理默认              → 不抽
    「keyword」在三条路径上全部**可空**（两条 SQL 都有 `$3 IS NULL` 分支），
    所以它一律走规则，抽不到就留空 —— 代价是"答得薄"，不是"答得错"。
  ★ 抽取顺序：显式参数 → 规则（维度词表） → LLM。只有 analysis/trace 会走到 LLM。
  ⚠️ 成本账（别再说"拆分不增加调用"）：判意图与抽竞品从一个调用变成两个，
     总调用上升约三成。换来的是 L2 判出即可停（32.1% 不进 LLM）。
【读法】
  排查"为什么没抽到竞品"先看 `Slots.source`（explicit/rule/llm/none）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.agents import create_llm_client
from src.config import Settings
from src.supervisor.intent.parsing import normalize_str_list, parse_json_object

logger = logging.getLogger(__name__)

# 维度词表：原话里的说法 → 标准维度名。
# ★ 这是**规则层**的唯一依据，也是它可以被穷举的原因 —— 维度是半封闭词表，
#   竞品是开放词表（新竞品随时出现），所以竞品只能靠 LLM 或显式参数。
_DIM_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("定价", ("定价", "价格", "收费", "套餐", "多少钱", "涨价", "降价", "贵不贵")),
    ("功能", ("功能", "特性", "版本", "更新", "发布")),
    ("市场", ("市场", "份额", "规模", "增长", "渗透")),
    ("生态", ("生态", "集成", "插件", "开放平台", "第三方")),
    ("体验", ("体验", "易用", "界面", "交互", "上手")),
    ("商业化", ("商业化", "变现", "营收", "收入", "盈利")),
    ("融资", ("融资", "估值", "投资", "轮次")),
)

SLOT_PROMPT = """从这句话里抽出提到的竞品名和分析维度。只抽**明确出现**的，不要联想或补全。

规则：
- competitors: 竞品/公司/产品名称列表，没提到返回 []
- dimensions: 分析维度列表（功能/定价/市场/生态/体验/商业化等），没提到返回 []
- 若输入里带【会话上下文】，可以用它消解「那几家」「还是那两个」这类指代；
  但上下文里出现过的竞品**不要**自动继承给一个全新问题

输出格式（仅 JSON，无其他文字）：
{"competitors": ["飞书"], "dimensions": ["定价"]}

用户输入：%s
JSON 输出："""


@dataclass
class Slots:
    """一次槽位抽取的结果。"""

    competitors: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    keyword: str | None = None
    source: str = "none"        # explicit / rule / llm / none

    @property
    def has_anchor(self) -> bool:
        """有没有分析锚点（竞品或维度之一）。analysis 缺它就只能追问。"""
        return bool(self.competitors or self.dimensions)

    def as_dict(self) -> dict:
        return {
            "competitors": self.competitors,
            "dimensions": self.dimensions,
            "keyword": self.keyword,
            "source": self.source,
        }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def rule_dimensions(text: str) -> list[str]:
    """从原话里认维度词，返回标准维度名（保序去重）。

    ⚠️ 只认表里的词。抽不到就返回 [] —— 不要为"再多认几种说法"往表里堆词，
       这是规则层，堆得越密，静默漏判的面越大（漏了不报错，只答得薄）。
    """
    found: list[str] = []
    for dim, words in _DIM_ALIASES:
        if any(w in (text or "") for w in words):
            found.append(dim)
    return found


def rule_keyword(text: str) -> str | None:
    """trace/verify 的检索短语，规则版：取第一个命中的维度名。

    抽不到返回 None —— 两条 SQL 都接受空 keyword，走"按竞品范围列全部/核全部"。
    """
    dims = rule_dimensions(text)
    return dims[0] if dims else None


async def llm_slots(
    text: str, settings: Settings | None = None, history_block: str = ""
) -> Slots:
    """一次 LLM 只抽槽位，不碰意图。

    失败时返回空槽而不是抛错：analysis 会因此走"缺锚点追问"，
    trace 会退化成不带竞品过滤 —— 都是可见的降级，好过整个入口 500。
    """
    cfg = settings or Settings()
    try:
        llm = create_llm_client(cfg, temperature=0.0)
        resp = await llm.ainvoke(SLOT_PROMPT % (history_block + text))
        data = parse_json_object(getattr(resp, "content", "") or "")
    except Exception:
        logger.exception("槽位抽取失败，按空槽继续（analysis 将走缺锚点追问）")
        return Slots(source="none")

    cs = _dedupe(normalize_str_list(data.get("competitors")))
    ds = _dedupe(normalize_str_list(data.get("dimensions")))
    logger.info("槽位 LLM: %r → competitors=%s dimensions=%s", text[:40], cs, ds)
    return Slots(competitors=cs, dimensions=ds, source="llm")


async def fill_slots(
    text: str,
    intent: str,
    *,
    explicit_competitors: list[str] | None = None,
    explicit_dimensions: list[str] | None = None,
    settings: Settings | None = None,
    history_block: str = "",
) -> Slots:
    """按 label 决定抽什么，返回槽位。

    explicit_* 是调用方从请求体直接拿到的结构化参数（表单路径），
    一定优先于任何推断 —— 用户亲手填的比模型猜的可靠。
    """
    text = (text or "").strip()
    cs = _dedupe(normalize_str_list(explicit_competitors))
    ds = _dedupe(normalize_str_list(explicit_dimensions))
    kw = rule_keyword(text)
    base_source = "explicit" if (cs or ds) else ("rule" if kw else "none")

    # lookup：序数由 parse_ordinal 在调用处规则抽，这里不占 LLM。
    if intent == "lookup":
        return Slots(competitors=cs, dimensions=ds, keyword=kw, source=base_source)

    # verify：缺竞品等于"核全部事实"，正是「数据新不新」想要的，不必抽。
    if intent == "verify":
        return Slots(competitors=cs, dimensions=ds, keyword=kw, source=base_source)

    # analysis：两边都齐才不调 LLM；缺一边就交给 LLM（竞品没有词表可查）。
    if intent == "analysis":
        ds = ds or rule_dimensions(text)
        if cs and ds:
            return Slots(competitors=cs, dimensions=ds, keyword=kw, source=base_source)
        got = await llm_slots(text, settings, history_block)
        return Slots(
            competitors=cs or got.competitors,
            dimensions=ds or got.dimensions,
            keyword=kw,
            source=got.source if got.source != "none" else base_source,
        )

    # trace：竞品是硬过滤（缺了会列出别家的证据），必须抽；keyword 走规则。
    if intent == "trace":
        if cs:
            return Slots(competitors=cs, dimensions=ds, keyword=kw, source=base_source)
        got = await llm_slots(text, settings, history_block)
        return Slots(
            competitors=got.competitors,
            dimensions=ds or got.dimensions,
            keyword=kw,
            source=got.source if got.source != "none" else base_source,
        )

    # 其余（unknown / 未识别）：不猜，什么都不抽。
    return Slots(source="none")
