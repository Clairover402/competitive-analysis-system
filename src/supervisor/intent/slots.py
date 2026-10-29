"""槽位填充 —— 意图确定之后的第二段。

【职责】
  知道 label 之后，决定要抽哪些槽，并抽取。
【不做】
  判意图（label 是入参）、执行查询（query_handlers）、追问（clarify.py）。
【关键决策】
  ★ 该不该抽一个槽，判据是「**缺了下游会不会答错**」，不是「这个意图有什么槽」：
      analysis 缺竞品/维度 → 检索没有锚点，根本跑不起来            → 必抽
      trace    缺竞品       → `e.competitor = ANY($2)` 失效，会列出别家证据 → 必抽
      trace    缺维度       → 只是"查全部维度"，用户看得见的宽        → 可空
      verify   缺竞品       → 核该用户全部事实，**正是「数据新不新」要的** → 不抽
      lookup   缺序数       → 退回最近一次，是合理默认              → 不抽
  ★ 竞品 / 维度走**同一条解析管线**（`match_values` → `gate_llm`）：
      ① 值池字面匹配 —— 用户话里出现了池里的值，直接用。**零 LLM**
      ② 词表翻译（仅维度）—— 价格→定价；翻出来的名字**回池校验**，池里没有就丢
      ③ LLM 兜底 + 闸门 —— LLM 输出再跑一次值池匹配，只能翻译成池里已有的名字
      ④ 都没有 → 不限定，但必须**明确告知**（lookup 列表带总数；trace 说没找到）
    ★ 为什么值池排在词表前面：值池是用户真实产生过的值，词表一旦抢先就会把
      「用户规模」翻成「市场」（"规模"是市场组的别名）。子串匹配必然踩这种词，
      靠往词表里加别名治不好 —— 只能让数据说话。
    ★ 为什么 LLM 后置且要过闸门：LLM 会输出池里没有的名字（copilot→Microsoft Copilot），
      直接下推就是查 0 条，而用户以为筛过了。这是 lookup 当初拒绝 LLM 的原因，
      闸门把它关掉 —— LLM 只能当同义词翻译器，不能引进新值。
    ⚠️ `keyword` 现在只剩 verify 在用。trace 不再抽它（2026-10-29）：
      它的值本来就是 `rule_dimensions(text)[0]`，是维度名，却拿去 `claim ILIKE`。
  ★ 抽取顺序：值池 → 词表 → LLM。只有 analysis/trace 会走到 LLM，
    且 trace 只在**竞品**解析不出来时才调（维度空着是可见降级，不值得多付一次）。
    ⚠️ analysis **每轮都调**：竞品没有词表可查，只能问模型。别再写"参数齐全就省一次"
    —— 那个短路随显式参数一起删了，因为生产路径上从来没人传过显式参数。
  ⚠️ 成本账（别再说"拆分不增加调用"）：判意图与抽竞品从一个调用变成两个，
     总调用上升约三成。换来的是 L2 判出即可停（32.1% 不进 LLM）。
【读法】
  排查"为什么没抽到竞品"先看 `Slots.source`（pool/alias/rule/llm/none）。
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
    source: str = "none"        # pool / alias / rule / llm / none

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


def match_values(
    text: str,
    pool: list[str] | None,
    aliases: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
) -> tuple[list[str], str]:
    """值池 → 词表，两层，零 LLM。返回 (命中的池值, 来源)。

    ★ 为什么值池先于词表：值池是**这个用户真实产生过的值**，用户说什么就该匹配什么。
      词表是"把口语翻成标准名"的对照表，一旦让它抢先，就会把「用户规模」翻成「市场」
      （"规模"正是市场组的别名）—— 子串匹配必然踩这种词，实测已复现。
    ★ 词表翻出来的名字**必须回值池校验**：池里没有「变现」，翻成"商业化"也查不出东西。
      这一步让词表只能"认出池里有的"，不能引进池外的新值。
    ★ 词表只服务维度。竞品是开放集合（新竞品随时出现），没有词表可查。
    """
    hits = match_competitors(text, pool)
    if hits:
        return hits, "pool"
    if aliases and pool:
        mapped = [std for std, words in aliases if any(w in (text or "") for w in words)]
        mapped = [m for m in mapped if m in pool]
        if mapped:
            return mapped, "alias"
    return [], "none"


def gate_llm(values: list[str] | None, pool: list[str] | None) -> list[str]:
    """LLM 输出的闸门：把输出逐条对回池里的值，对不上的一律丢弃。

    ★ 整条管线里最关键的一步。LLM 会把「copilot」抽成「GitHub Copilot」，
      也会抽成「Microsoft Copilot」—— 后者池里没有，直接下推就是查 0 条，
      而用户以为筛过了。过闸门后它只能**翻译成池里已有的名字**，不能引进新名字。
    ★ 实测（该用户真实的 32 个竞品值池，temperature=0）：
        copilot → ['copilot']      闸门 → GitHub Copilot
        teams   → ['teams']        闸门 → Microsoft Teams
        Zapier  → ['Zapier']       闸门 → []（池外，正确丢弃）
      对照：把池子塞进提示词后 copilot 直接返回 [] —— 模型放弃了就没有补救通道，
      这正是选"先自由输出再过闸门"而不是"池子进提示词"的原因。

    ⚠️ 不能直接复用 `match_competitors`：它判的是「**池值**出现在用户原话里」，
      方向是"话 ⊇ 值"。LLM 输出的是简称（copilot ⊂ GitHub Copilot），方向必须反过来。
      所以这里做双向包含，且**精确匹配优先** —— 否则「飞书」会把「飞书文档」一起带上。
    """
    if not values:
        return []
    if not pool:
        # 没有值池 = 没有校验依据（没查 / 查失败 / 该用户还没有数据）。
        # 此时退回 LLM 直出：丢弃一切会让 trace 永远查不到东西，而放行的最坏结果
        # 只是"查少了几条"—— 查询范围本来就被 user_id 限制住了，串不到别人的数据。
        return list(values)
    squashed = [(c, _squash(c)) for c in pool if _squash(c)]
    out: list[str] = []
    for v in values:
        nv = _squash(v)
        if not nv:
            continue
        exact = [c for c, nc in squashed if nc == nv]
        if exact:
            out.extend(exact)
            continue
        out.extend(c for c, nc in squashed if nv in nc or nc in nv)
    return _dedupe(out)


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
    settings: Settings | None = None,
    history_block: str = "",
    competitor_pool: list[str] | None = None,
    dimension_pool: list[str] | None = None,
) -> Slots:
    """按 label 决定抽什么，返回槽位。

    ⚠️ **没有 explicit 参数了**（2026-10-29 删）。它原本是"调用方从请求体拿到的
    结构化参数，优先于任何推断"，但生产路径上从来没有调用方传它：
    `/api/chat` 的 `ChatRequest` 只有 message/conversation_id/page/page_size，
    根本没有竞品字段；`/api/tasks` 的表单参数走 `TaskRequest` → `_create_task_impl`，
    压根不经过本函数。它只活在测试里，却让 analysis 的"参数齐全就不调模型"
    短路分支永远进不去（`cs` 恒空）——挂着一个假兜底比没有更危险。

    competitor_pool / dimension_pool 是该用户自己的两个值池：
      · 竞品池 = `tasks.competitors` 的 DISTINCT（实测 2~32 个）
      · 维度池 = `evidence_map.dimension` 的 DISTINCT（实测 8 个，含"玩法设计"这类
        词表没有的：维度跟着产品品类走，不是固定的 7 个）
    由调用方查好传进来（本模块不碰 pool，保持纯函数可测）。传 None 等于"没有值池
    可用"，此时只能靠显式参数或 LLM。
    """
    text = (text or "").strip()
    cs: list[str] = []
    ds: list[str] = []
    kw = rule_keyword(text)
    base_source = "rule" if kw else "none"

    # lookup：竞品走值池字面匹配（零 LLM），序数由 parse_ordinal 在调用处抽。
    #   维度也走值池（词表翻译后回池校验），但它只决定**念哪条事实**（_pick_facts），
    #   不参与筛选任务 —— 问「飞书定价」要的是飞书那些报告里的定价那句话，
    #   而不是"只做过定价分析的任务"。
    if intent == "lookup":
        if not cs:
            hit, src = match_values(text, competitor_pool)
            if hit:
                cs, base_source = hit, src
        if not ds:
            ds = match_values(text, dimension_pool, _DIM_ALIASES)[0]
        return Slots(competitors=cs, dimensions=ds, keyword=kw, source=base_source)

    # verify：缺竞品等于"核全部事实"，正是「数据新不新」想要的，不必抽。
    if intent == "verify":
        return Slots(competitors=cs, dimensions=ds, keyword=kw, source=base_source)

    # analysis：维度先走规则，竞品只能靠 LLM（没有词表可查）。
    #   ⚠️ 这里**曾经**有个"两边都齐就不调 LLM"的短路，删显式参数时一并去掉了：
    #     那个短路判的是 `cs and ds`，而 `cs` 在生产路径上恒为空 —— 永远进不去。
    #     留着它会让下一个人以为"参数填满就能省一次调用"，从而把死代码当优化依据。
    if intent == "analysis":
        ds = ds or rule_dimensions(text)
        got = await llm_slots(text, settings, history_block)
        return Slots(
            competitors=got.competitors,
            dimensions=ds or got.dimensions,
            keyword=kw,
            source=got.source if got.source != "none" else base_source,
        )

    # trace：竞品和维度走同一条管线 —— 值池 → 词表 → LLM → 闸门。
    #
    #   ★ 不再产 keyword（2026-10-29）。它原本是 `rule_dimensions(text)[0]`，值就是
    #     维度名，却拿去 `claim ILIKE` —— 同一个东西抽两遍、还用错了匹配方式
    #     （实测漏 11 条真定价、捞错 8 条）。维度列已经在手，关键词槽位是重复品。
    #
    #   ★ 只有**竞品**解析不出来才调 LLM。判据回到"缺了下游会不会答错"：
    #     竞品是硬过滤（缺了 `e.competitor = ANY($2)` 失效 → 列出别家证据 → 答错，
    #     且用户看不出来）；维度缺了只是查全部维度（看得见的宽），不值得为它多付一次调用。
    #     一次调用同时抽两个字段，所以维度也能被 LLM 顺带救到。
    if intent == "trace":
        # 竞品是主槽位，`Slots.source` 只记它的来源；维度来源不混进来（否则
        # 排查"为什么没抽到竞品"时会被维度命中的 pool/alias 误导）。
        src = "none"

        if not cs:
            hit, s = match_values(text, competitor_pool)
            if hit:
                cs, src = hit, s
        if not ds:
            ds = match_values(text, dimension_pool, _DIM_ALIASES)[0]

        if not cs:
            got = await llm_slots(text, settings, history_block)
            cs = gate_llm(got.competitors, competitor_pool)
            if cs:
                src = "llm"
            if not ds:
                ds = gate_llm(got.dimensions, dimension_pool)

        return Slots(competitors=cs, dimensions=ds, keyword=None,
                     source=src if (cs or ds) else "none")

    # 其余（unknown / 未识别）：不猜，什么都不抽。
    return Slots(source="none")
