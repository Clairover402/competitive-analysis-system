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
  ★ lookup 的竞品走**值池字面匹配**（`match_competitors`），零 LLM：
      用户问「飞书那份报告」时，竞品名就是库里的键，不需要"猜出"它。
      匹配不到就不限定竞品 —— 这个降级是**用户看得见的**（列表末尾有
      「另有 N 条更早的记录」），所以宁可漏也不要为了覆盖简称去调 LLM：
      LLM 抽出用户没说过的竞品会变成"答窄"，用户看不出漏了谁。
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
import re
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


def _squash(s: str) -> str:
    """去空白 + 小写。`GitHub Copilot` / `github  copilot` / `GitHubCopilot` 归一。

    ⚠️ 只是归一化，不是模糊匹配 —— 不做拼音、不做编辑距离。规则层一旦开始
       「差不多就算」，静默误判的面会盖过它多认出来的那几条。
    """
    return re.sub(r"\s+", "", str(s or "")).lower()


def match_competitors(text: str, pool: list[str] | None) -> list[str]:
    """在**这个用户自己的**竞品值池里做字面匹配，返回命中的竞品名。

    【为什么 lookup 不用 LLM】用户问「飞书那份报告」时，他说出的就是竞品名，
    而这个名字本身已经是 `tasks.competitors` 里的一个值 —— 对齐对象是封闭集合
    （该用户分析过的 N 个竞品），不是"世界上所有竞品"。所以这是**查值**，
    不是**猜语义**。实测 36 句真实 query：18 句命中且零误命中，未命中的
    18 句全部本来就不带竞品（「我以前跑过的报告在哪」这类）。

    ★ 三条判据：
      1. 长名优先。值池里 `飞书` ⊂ `飞书文档`，说「飞书文档的定价」时两个都会
         命中；去掉被包含的短名，否则顺带把「飞书」的任务也捞进来。
      2. 空白归一（见 `_squash`）。
      3. 命中为空返回空，由调用方走"不限定竞品"。**不要在这里兜底调 LLM**：
         那会引入 lookup 原本没有的失败模式 —— 抽出用户没说过的竞品 → 只查
         那一个 → 返回 0 条，而用户以为筛过了、看不出漏了谁。
    """
    nq = _squash(text)
    if not nq or not pool:
        return []
    hits = [c for c in pool if _squash(c) and _squash(c) in nq]
    if len(hits) > 1:
        # 只留"不包含于任何其他命中项"的那些
        hits = [h for h in hits
                if not any(h != o and _squash(h) in _squash(o) for o in hits)]
    return _dedupe(hits)


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
    competitor_pool: list[str] | None = None,
) -> Slots:
    """按 label 决定抽什么，返回槽位。

    explicit_* 是调用方从请求体直接拿到的结构化参数（表单路径），
    一定优先于任何推断 —— 用户亲手填的比模型猜的可靠。

    competitor_pool 是该用户 `tasks.competitors` 的 `DISTINCT` 值池，只有
    lookup 用它做字面匹配。由调用方查好传进来（本模块不碰 pool，保持纯函数
    可测）；传 None 等于"没有值池可用"，lookup 就退回不限定竞品。
    """
    text = (text or "").strip()
    cs = _dedupe(normalize_str_list(explicit_competitors))
    ds = _dedupe(normalize_str_list(explicit_dimensions))
    kw = rule_keyword(text)
    base_source = "explicit" if (cs or ds) else ("rule" if kw else "none")

    # lookup：竞品走值池字面匹配（零 LLM），序数由 parse_ordinal 在调用处抽。
    if intent == "lookup":
        if not cs:
            hit = match_competitors(text, competitor_pool)
            if hit:
                cs, base_source = hit, "pool"
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
