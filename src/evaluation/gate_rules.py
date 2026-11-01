# -*- coding: utf-8 -*-
"""报告规则判据 —— 三条不依赖 LLM 的「合格」定义。

【职责】
  做   读一份 Markdown 报告 + 该任务的 (竞品, 维度)，算出三个可复算的数：
       结构完整率 R1 / 出处覆盖率 R2 / 数值冲突数 R3；
       再把三条合成一个任务级结论（classify_report → verified/failed/unverifiable）。
  不做 判断报告「写得好不好」（那是模型评分的事，且会漂）；不连库、不调 LLM
       （调用方给文本，所以这三条可以在 pytest 里对字符串跑）。

【关键决策】
  · 为什么是规则而不是 LLM 评分 ← 模型自评不可复算：同一份报告今天 87 分、
    明天 83 分（temperature 不保证 0 生效，且 prompt 里带随机项）。
    一个会漂的指标不能当门禁。规则不会变得更宽容。
  · `[数据不足]` / `[待验证]` 计为**合格** ← 报告的价值是「知道什么、不知道
    什么」。空着才是错；把「查不到」写成「没有」才是真错。
    所以「空」的判据是『没写』，不是『没查到来东西』。
  · R3 宁可漏判不可误判 ← 同一句里并列套餐价（基础版 ¥10 / 专业版 ¥18）不是
    冲突。只在「同竞品 + 同字段 + 同币种 + 同计费周期 + 同档位词」出现两个不同
    数值时才记冲突；判不准的进 warnings，不计 fail。
  · 章节标题按「包含」匹配 ← writer prompt 只约束了标题文字，模型常加后缀
    （`## 竞品对比总览（维度 × 竞品）`）。精确相等会把合格报告判成残废。

【读法】
  check_structure / check_source_coverage → Verdict(ok, total, notes)
      ok / total 就是指标；notes 是**指向具体位置**的那句话（哪一格空、哪节没来源）。
  check_conflicts → ConflictReport(conflicts, details, warnings)
      与 Verdict 不同形，因为它的指标是「计数」，不是「比例」。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

# ── 结构契约：来源 src/agents/writer.py:_WRITER_PROMPT「报告结构要求」 ──────────
# ⚠️ 改 writer prompt 的章节名必须同步改这里，否则门禁会把新报告全判成残废。
REQUIRED_SECTIONS: tuple[str, ...] = (
    "概述",
    "竞品对比总览",
    "逐维度深度分析",
    "关键发现",
    "风险与建议",
)

# 「写了等于没写」的单元格内容。注意这里**不含** [数据不足]/[待验证]：
# 那两种是显式承认缺失，属于合格输出（见模块头「关键决策」第二条）。
_EMPTY_CELLS: frozenset[str] = frozenset(
    {"", "-", "—", "–", "/", "n/a", "na", "无", "未知", "暂无", "待补充", "?", "？", "..."}
)

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_H3_RE = re.compile(r"^###\s+(.+?)\s*$", re.M)
_URL_RE = re.compile(r"https?://")
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")


@dataclass
class Verdict:
    """比例型判据的结果。`total == 0` 表示「无从判起」，调用方应按 SKIP 处理。"""

    ok: int
    total: int
    notes: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.ok / self.total if self.total else 0.0


@dataclass
class ConflictReport:
    """计数型判据（R3）的结果。

    `conflicts` 是硬拦的（同字段出现互斥值）；`ambiguous` 是**规则判不了的**
    （同一句里并列多个值：可能是套餐清单，也可能是自相矛盾），只提示不拦。

    ★ 两者必须分别计数，不能都塞进 warnings：混在一起就退化成「打印一行、
      没人看」，而这正是 2026-11-01 之前「真冲突被静默吞掉」的那条路径。
    """

    conflicts: int = 0
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ambiguous: int = 0
    ambiguous_details: list[str] = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════════════
# §1 Markdown 解析（只做这三条判据需要的部分）
# ═════════════════════════════════════════════════════════════════════════════


def _norm(text: str) -> str:
    """比较用的归一化：去空白、强调符、括号、全角圆括号。

    ⚠️ 不要拿它去改写正文 —— 它会把 `[数据不足]` 变成 `数据不足`。
    """
    return re.sub(r"[\s*`_\[\]()（）]", "", text or "").lower()


def _same(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def split_h2(markdown: str) -> list[tuple[str, str]]:
    """按 `## ` 切章节，返回 [(标题, 正文)]，保持原顺序。"""
    marks = list(_H2_RE.finditer(markdown))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(markdown)
        out.append((m.group(1).strip(), markdown[m.end():end]))
    return out


def find_section(sections: list[tuple[str, str]], name: str) -> str | None:
    """按「包含」找章节，返回正文；找不到返回 None。

    多命中时取第一个 —— 顺序即文档顺序，第一个才是正主。
    """
    for title, body in sections:
        if name in title:
            return body
    return None


def parse_markdown_table(text: str) -> list[list[str]]:
    """取章节里的第一张表：[[表头], [行], ...]；分隔行（`|---|`）丢掉。

    只认行首是 `|` 的行；遇到表后第一段非表格文本即收工 —— 一个章节里
    通常只有一张表，多张表的场景这里不猜。
    """
    rows: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if any(cells) and all(_SEP_CELL_RE.match(c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def _cell_is_filled(cell: str) -> bool:
    return cell.strip().lower() not in _EMPTY_CELLS


# ═════════════════════════════════════════════════════════════════════════════
# §2 R1 · 结构完整率
# ═════════════════════════════════════════════════════════════════════════════


def check_structure(content: str, competitors: list[str], dimensions: list[str]) -> Verdict:
    """R1：`len(竞品) × len(维度)` 个格子，非空且落在允许集合里才算 1 分。

    缺一个必需章节 → 直接 0 分（结构塌了，逐格数没有意义）。
    """
    expected = len(competitors) * len(dimensions)
    if expected == 0:
        return Verdict(0, 0, ["任务缺少竞品或维度，无从判结构"])

    sections = split_h2(content or "")
    titles = [t for t, _ in sections]
    missing = [name for name in REQUIRED_SECTIONS if not any(name in t for t in titles)]
    if missing:
        return Verdict(0, expected, [f"缺章节：{'、'.join(missing)}"])

    table = parse_markdown_table(find_section(sections, "竞品对比总览") or "")
    if len(table) < 2:
        return Verdict(0, expected, ["「竞品对比总览」不是表格，或缺表头"])

    header, body = table[0], table[1:]
    col_of: dict[str, int] = {}
    for idx, cell in enumerate(header[1:], start=1):
        for comp in competitors:
            if _same(cell, comp):
                col_of.setdefault(comp, idx)

    notes: list[str] = []
    for comp in competitors:
        if comp not in col_of:
            notes.append(f"表头缺列「{comp}」")

    ok = 0
    for dim in dimensions:
        row = next((r for r in body if r and _same(r[0], dim)), None)
        if row is None:
            notes.append(f"表里缺行「{dim}」")
            continue
        for comp in competitors:
            col = col_of.get(comp)
            cell = row[col] if col is not None and col < len(row) else ""
            if _cell_is_filled(cell):
                ok += 1
            else:
                notes.append(f"空格：{comp} × {dim}")
    return Verdict(ok, expected, notes[:6])


# ═════════════════════════════════════════════════════════════════════════════
# §3 R2 · 出处覆盖率
# ═════════════════════════════════════════════════════════════════════════════


def check_source_coverage(content: str) -> Verdict:
    """R2：「逐维度深度分析」下每个 `###` 小节是否至少有一个 http(s) 链接。

    ⚠️ 口径说明（与计划文档的一处偏差）：计划写的是「每个（竞品 × 维度）小节」，
    而 writer 实际产出的是**按维度**的 `### 定价分析`，竞品是节内的自然段。
    这里按**实际结构**判（节级），因为按竞品切段需要猜分段格式，猜到就错、猜不到
    就静默漏判 —— 不如把口径说清楚。代价：一节里只要有一处来源就算过，
    漏掉某个竞品的出处查不出来（这是已知的松）
    """
    body = find_section(split_h2(content or ""), "逐维度深度分析")
    if body is None:
        return Verdict(0, 1, ["缺「逐维度深度分析」章节"])

    subs = list(_H3_RE.finditer(body))
    if not subs:
        return Verdict(0, 1, ["「逐维度深度分析」下没有 ### 小节"])

    ok = 0
    notes: list[str] = []
    for i, m in enumerate(subs):
        end = subs[i + 1].start() if i + 1 < len(subs) else len(body)
        if _URL_RE.search(body[m.end():end]):
            ok += 1
        else:
            notes.append(f"无出处：{m.group(1).strip()}")
    return Verdict(ok, len(subs), notes[:6])


# ═════════════════════════════════════════════════════════════════════════════
# §4 R3 · 数值冲突（保守版）
# ═════════════════════════════════════════════════════════════════════════════

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"

# 字段标签白名单 → 规范化字段名。只收「同一字段有唯一正确值」的那些；
# 「市场份额」「增长率」这类不同口径可以并存的，故意不收。
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "价格": ("定价", "价格", "售价", "收费", "订阅费", "费用", "人均"),
    "用户数": ("用户数", "用户量", "用户规模", "月活", "MAU", "DAU"),
    "融资": ("融资", "估值"),
    "成立时间": ("成立时间", "成立于", "创立于"),
}

# 档位词：出现即认为这是个「分档报价」而不是「唯一的价」。同档位才比数值。
# 促销/优惠类也算档位：「19 元/月，尝鲜价 15 元/月」是两档并存，不是矛盾
# （2026-11-01：不带这批词时被当成同档冲突，实测误报）。
_TIER_WORDS: tuple[str, ...] = (
    "免费", "基础", "标准", "专业", "企业", "旗舰", "团队", "个人", "尊享",
    "高级", "入门", "试用", "家庭", "商业", "尊贵", "pro", "plus", "business",
    "enterprise", "free", "team", "starter", "premium",
    "尝鲜", "优惠", "促销", "特惠", "限时", "原价", "活动价", "折后", "首年",
)

_PRICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CNY", re.compile(rf"[¥￥]\s*(?P<num>{_NUM})")),
    ("CNY", re.compile(rf"(?P<num>{_NUM})\s*(?:元(?!素)|人民币)")),
    ("USD", re.compile(rf"\$\s*(?P<num>{_NUM})")),
    ("USD", re.compile(rf"(?P<num>{_NUM})\s*(?:美元|美金|USD)")),
)
# 用户数：三种常见写法，都不能少
#   ① 3000 万用户    ② 用户数 3000 万    ③ 用户数 6 亿
_COUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?P<num>{_NUM})\s*(?P<mag>万|亿)\s*(?:用户|人|MAU|月活|付费用户)?"),
    re.compile(rf"(?P<num>{_NUM})\s*(?:用户|人|MAU|月活|付费用户)"),
)
# 融资额：必须带量级或币种，否则「融资 3 轮」的 3 会被当成金额
_FUND_PATTERN = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<mag>万|亿)?\s*(?P<cur>元|美元|美金|USD|¥|￥)"
)
_YEAR_PATTERN = re.compile(r"(?P<num>(?:19|20)\d{2})\s*年")

_MAGNITUDE = {"万": 1e4, "亿": 1e8}

# 字段标签与数值的最大间隔：超过这个距离就不是「这个字段的值」了，
# 而是同一段里另一件事的数字（例：「钉钉定价…；智能绩效 800 元/年起」）。
_LABEL_WINDOW = 30
_TIER_WINDOW = 14


@dataclass(frozen=True, order=True)
class _Block:
    """数值所在的「最小可比单元」—— 冲突判定的粒度。

    line = 正文一行；cell = 表格里的一个单元格（行号, 列号）。

    ★ 必须区分这两者：「一个单元格里并列三个价」几乎只可能是套餐清单（该忽略），
      而「一句话里并列两个价」往往是自相矛盾（该判冲突）。用同一个 block 概念
      区分不出来，就会把真冲突当清单放过（2026-11-01 实测漏判）。
    """

    kind: str          # "line" / "cell"
    row: int
    col: int = 0


@dataclass
class _Value:
    block: _Block
    field: str
    amount: float
    unit: str        # 币种/量纲：CNY / USD / 人 / 年
    period: str      # 月 / 年 / ""
    tier: str
    span: tuple[int, int] = (0, 0)   # 在所属窗口里的位置 ← 用来取「两个值之间夹了什么」


def _tier_before(text: str, pos: int) -> str:
    head = text[max(0, pos - _TIER_WINDOW):pos].lower()
    for word in _TIER_WORDS:
        if word in head:
            return word
    return ""


def _period_after(text: str, end: int) -> str:
    tail = text[end:end + 8]
    if "年" in tail:
        return "年"
    if "月" in tail:
        return "月"
    return ""


def _label_before(text: str, pos: int) -> str | None:
    """数值前 30 字内有白名单字段标签吗？有就返回规范化字段名。"""
    head = text[max(0, pos - _LABEL_WINDOW):pos]
    # 取**最靠右**的那个标签：一句话里先提定价再提人数时，数值归属后者。
    best: tuple[int, str] | None = None
    for canon, labels in _FIELD_LABELS.items():
        for label in labels:
            idx = head.rfind(label)
            if idx >= 0 and (best is None or idx > best[0]):
                best = (idx, canon)
    return best[1] if best else None


def _extract_values(window: str, block: _Block) -> list[_Value]:
    """从一个「竞品窗口」里抽所有 (字段, 数值)。

    四种字段各写一段，不合并成通用正则：合并之后每加一个字段都要回头确认
    另外三个没被影响，而四个字段的形态差异太大（币种前缀 / 量级后缀 / 年份）。
    """
    out: list[_Value] = []

    def add(pos: int, end: int, canon: str, amount: float, unit: str, period: str = "") -> None:
        out.append(_Value(block, canon, amount, unit, period,
                          _tier_before(window, pos), (pos, end)))

    def num(m: re.Match[str]) -> float:
        return float(m.group("num").replace(",", ""))

    def mag(m: re.Match[str]) -> float:
        return _MAGNITUDE.get(m.groupdict().get("mag") or "", 1.0)

    for unit, pattern in _PRICE_PATTERNS:
        for m in pattern.finditer(window):
            if _label_before(window, m.start()) != "价格":
                continue
            amount = num(m) * mag(m)
            add(m.start(), m.end(), "价格", amount, unit, _period_after(window, m.end()))

    for pattern in _COUNT_PATTERNS:
        for m in pattern.finditer(window):
            if _label_before(window, m.start()) != "用户数":
                continue
            add(m.start(), m.end(), "用户数", num(m) * mag(m), "人")

    for m in _FUND_PATTERN.finditer(window):
        if _label_before(window, m.start()) != "融资":
            continue
        unit = "USD" if m.group("cur") in ("美元", "美金", "USD") else "CNY"
        add(m.start(), m.end(), "融资", num(m) * mag(m), unit)

    for m in _YEAR_PATTERN.finditer(window):
        if _label_before(window, m.start()) != "成立时间":
            continue
        add(m.start(), m.end(), "成立时间", num(m), "年")

    return out


def _table_windows(content: str, competitors: list[str]) -> Iterator[tuple[_Block, str, str]]:
    """把 Markdown 表格按「列 → 竞品」映射，逐个单元格产出 (block, 竞品, 窗口)。

    ★ 为什么表格必须单独扫：`| 定价 | $10.99/用户/月 |` 这行**不含竞品名**
      （名字在表头），按「行内出现竞品名」切窗口的扫法会整张表跳过 ——
      而总览表恰恰是价格最常出现的地方（2026-11-01 实测：表里 10.99、正文
      12.99，判出 0 个冲突）。列映射逻辑与 R1 的 `col_of` 同源。

    窗口 = 行首维度名 + 单元格文本 ← 字段标签（「定价」）在行首、不在单元格里，
    必须拼进去 `_label_before` 才认得出这是什么字段。
    """
    lines = (content or "").splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue

        rows: list[tuple[int, list[str]]] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            if not (any(cells) and all(_SEP_CELL_RE.match(c) for c in cells if c)):
                rows.append((i, cells))
            i += 1
        if len(rows) < 2:                       # 单行不成表（没有数据行）
            continue

        col_of: dict[int, str] = {}
        for idx, cell in enumerate(rows[0][1][1:], start=1):
            for comp in competitors:
                if _same(cell, comp):
                    col_of.setdefault(idx, comp)
        if not col_of:
            continue

        for line_no, row in rows[1:]:
            dim = row[0] if row else ""
            for col, comp in col_of.items():
                cell = row[col] if col < len(row) else ""
                if cell.strip():
                    yield _Block("cell", line_no, col), comp, f"{dim} {cell}"


def _conflict_where(blocks: list[_Block]) -> str:
    """冲突发生在哪 —— 复盘时要靠这句话定位，不然只知道「有个冲突」。"""
    kinds = {b.kind for b in blocks}
    if len(kinds) > 1:
        return "表格与正文不一致"
    if kinds == {"cell"}:
        return "表格内跨行不一致"
    return "同一句内并列" if len(set(blocks)) == 1 else "正文多处不一致"


# 同段并列多值时，用来区分「同一件事说了两遍」与「并列了多件事」的话语标记。
# ⚠️ 这是**有损**的启发式：没有标记词的一律进 ambiguous 交给人，不硬判。
#    宁可漏报成「待确认」，也不把「氚云 4280 / 酷学院 4900」这种生态应用
#    清单报成冲突（2026-11-01 实测：8 份真实报告里 3 处都是这一类）。
_CONTRADICTION_MARKERS: tuple[str, ...] = (
    "另一", "另有", "又写", "又提", "一处", "但也", "但", "然而", "却",
    "实际", "更新为", "调整为", "此前", "早前", "同时", "官网",
)


def _has_contradiction_marker(text: str, vals: list[_Value]) -> bool:
    """相邻两个数值之间夹的文字里，有没有转折/更正/指代类标记。"""
    ordered = sorted(vals, key=lambda v: v.span[0])
    between = " ".join(text[a.span[1]:b.span[0]] for a, b in zip(ordered, ordered[1:]))
    return any(word in between for word in _CONTRADICTION_MARKERS)


def check_conflicts(content: str, competitors: list[str]) -> ConflictReport:
    """R3：同一竞品同一字段出现两个互斥数值 → 记一次冲突。

    判决顺序（每一步都是为了压误报，但**不得把真冲突压没**）：
      1. 正文按「竞品出现的位置」切窗口；表格按「列 → 竞品」映射切单元格
         （表格数据行里没有竞品名，只能靠表头）—— 两条路径都不漏；
      2. 数值必须前有字段标签（30 字内）、且有币种/单位 —— 裸数字不参与；
      3. 只把「一个单元格里并列多个数值」当套餐清单剔除（一个格子装多个价，
         几乎只能是分档）；**正文同一句里的多值不剔除**，改按 4 判；
      4. 剩下的按档位词分组，同档组内仍有 ≥2 个不同数值 → 冲突。
         有档位词区分的多值会被自然分到不同组，不需要剔除也不误报。
         同一句内的多值再看「两个值之间夹了什么」：带转折/更正/指代标记
         （另一处、又写、实际、官网…）→ 记冲突；没有标记 → 记 ambiguous
         （可能是多实体清单，规则判不了，交给人看）。
    """
    report = ConflictReport()
    if not competitors:
        return report

    grouped: dict[tuple[str, str, str, str], list[_Value]] = defaultdict(list)
    block_text: dict[_Block, str] = {}

    # ── ① 正文 ──
    for line_no, raw in enumerate((content or "").splitlines()):
        line = raw.strip()
        if not line or line.startswith("|"):
            continue                        # 表格行交给 ②：两边都扫会重复计数
        occ: list[tuple[int, int, str]] = []
        for comp in competitors:
            for m in re.finditer(re.escape(comp), line):
                occ.append((m.start(), m.end(), comp))
        occ.sort()
        for i, (_, end, comp) in enumerate(occ):
            nxt = occ[i + 1][0] if i + 1 < len(occ) else len(line)
            block = _Block("line", line_no)
            block_text[block] = line[end:nxt]
            for val in _extract_values(line[end:nxt], block):
                grouped[(comp, val.field, val.unit, val.period)].append(val)

    # ── ② 表格（按列）──
    for block, comp, window in _table_windows(content, competitors):
        block_text[block] = window
        for val in _extract_values(window, block):
            grouped[(comp, val.field, val.unit, val.period)].append(val)

    # ── ③ 先全局识别「单元格套餐清单」，再逐字段比对 ──
    # ⚠️ 必须跨 period/unit 分组识别：`¥19 / ¥99 / ¥199 每人每月` 里前两个
    #    抽不到「月」、第三个抽得到，分在同一字段的两个组里。若按组各判一次，
    #    一个格子会被拆成两半（一半当清单、一半留作比对）。
    cell_hits: dict[_Block, list[tuple[str, str, float]]] = defaultdict(list)
    for (comp, fld, _unit, _period), values in grouped.items():
        for v in values:
            if v.block.kind == "cell":
                cell_hits[v.block].append((comp, fld, round(v.amount, 4)))

    cells_multi = {
        b for b, hits in cell_hits.items()
        if len({f for _, f, _ in hits}) == 1 and len({a for _, _, a in hits}) > 1
    }
    if cells_multi:
        sample = cell_hits[sorted(cells_multi)[0]]
        amounts = sorted({a for _, _, a in sample})
        report.warnings.append(
            f"{sample[0][0]}·{sample[0][1]} 单元格内并列 {len(amounts)} 个数值"
            f"（按套餐清单处理，不计冲突）：" + " / ".join(f"{x:g}" for x in amounts[:4])
        )

    for (comp, fld, unit, period), values in grouped.items():
        if cells_multi:
            values = [v for v in values if v.block not in cells_multi]

        per_tier: dict[str, dict[float, list[_Block]]] = defaultdict(lambda: defaultdict(list))
        for v in values:
            per_tier[v.tier][round(v.amount, 4)].append(v.block)

        for tier, amounts in per_tier.items():
            if len(amounts) < 2:
                continue
            blocks = [b for bs in amounts.values() for b in bs]
            where = _conflict_where(blocks)
            head = (
                f"{comp}·{fld}{('(' + tier + ')') if tier else ''}"
                f"[{unit}{('/' + period) if period else ''}]："
                + " / ".join(f"{x:g}" for x in sorted(amounts)[:5])
            )
            # 同一句里并列多值：靠「两值之间夹了什么」决定是矛盾还是清单。
            # 没标记词的不硬判 —— 记 ambiguous，门禁会把它打出来但不算红。
            if where == "同一句内并列" and not _has_contradiction_marker(
                block_text.get(blocks[0], ""), [v for v in values if v.block == blocks[0]]
            ):
                report.ambiguous += 1
                report.ambiguous_details.append(
                    head + "（同段并列，判不出是分档清单还是矛盾）"
                )
                continue
            report.conflicts += 1
            report.details.append(head + f"（{where}）")
    return report


# ── 任务级结论：Verified Success ──────────────────────────────────────────────
# 上面三条是「报告级比例」，这里是「任务级结论」。要分开是因为：结构 8/8 但
# 出处 1/2 的报告，在 R1/R2 上看得见欠账，在「这单算不算交付成功」上只有一个答案。

VERIFIED, FAILED, UNVERIFIABLE = "verified", "failed", "unverifiable"


@dataclass
class TaskVerdict:
    state: str                # VERIFIED / FAILED / UNVERIFIABLE
    reason: str = ""

    @property
    def is_verified(self) -> bool:
        return self.state == VERIFIED


def classify_report(content: str, competitors: list[str], dimensions: list[str]) -> TaskVerdict:
    """三条规则合成一个任务级结论 —— 「这单算不算交付成功」。

    ★ **三档而不是两档**：`unverifiable` 必须单列。
      竞品/维度为空时 R1 的 total=0，规则**不是判不过，是判不了**：
      并进 PASS 会虚高（空报告也算成功），并进 FAIL 会冤枉（报告可能挺好）。
      这和门禁的 `SKIP 判失败` 不冲突 —— SKIP 说的是「**不算合格**」，
      不是「记为 0 分」。前者影响结论，后者会污染均值。

    ⚠️ 出处这一条不做「total==0 就跳过」：R2 在缺章节时返回 `0/1`，
      total 恒 ≥1，所以不存在「没东西可查 → 自动通过」的空转。
    """
    if not content or not content.strip():
        return TaskVerdict(FAILED, "无最新版报告")

    s1 = check_structure(content, competitors, dimensions)
    if s1.total == 0:
        return TaskVerdict(UNVERIFIABLE, "竞品或维度为空，无从判结构")

    gaps: list[str] = []
    if s1.ok < s1.total:
        gaps.append(f"结构 {s1.ok}/{s1.total}")
    # 不必判 `s2.total == 0`：R2 在缺章节/缺 `###` 小节时返回 `Verdict(0, 1, ...)`，
    # total 恒 ≥1 —— 「没东西可查」已经被表达成 0/1 而不是 0/0，这里不会空过。
    s2 = check_source_coverage(content)
    if s2.ok < s2.total:
        gaps.append(f"出处 {s2.ok}/{s2.total}")
    s3 = check_conflicts(content, competitors)
    if s3.conflicts:
        gaps.append(f"冲突 {s3.conflicts} 处")

    return TaskVerdict(FAILED, "；".join(gaps)) if gaps else TaskVerdict(VERIFIED)

