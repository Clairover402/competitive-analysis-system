"""内容质控 —— 文本规整（normalize）+ 样板 chunk 判定（boilerplate）。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 问题定位】
═══════════════════════════════════════════════════════════════════════════════

web_fetch / Tavily 回吐的是「markdown 化的网页文本」，它把页面上所有链接、
图片、data URI 原样保留成标记语法：

    ![](https://p1-hera.feishucdn.com/tos-cn-i-jbbdkfciu3/xxx~tplv-...:0:0.image)
    [在表格中设置数据格式](/hc/zh-CN/articles/360033771233 "在表格中设置数据格式")
    [![ONES](data:image/svg+xml,%3csvg%20viewBox='0%200%2086%2024'...)](https://ones.cn)

这些标记对「语义检索」是**纯噪音**：

  ① 进入 embedding —— 向量方向被 URL/图片路径带偏（长 URL 的字符量远超正文）；
  ② 进入 cross-encoder —— reranker 看到 query「飞书 定价」与一屏
     `feishu.cn/...` 路径的字面重合，把导航块排到头部（实测精排后样板占比反而升高）；
  ③ 进入 LLM prompt —— 挤占 1500 字符的上下文预算。

【2026-10-14 实测（baseline=runs/20261013-230903，1352 chunk）】
    剥离标记后文本总量 2,051,868 → 1,201,879 字符（-41%），
    即原先把 41% 的「喂给模型的东西」花在了 URL/图片占位符上。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 设计决策】
═══════════════════════════════════════════════════════════════════════════════

【决策1】为什么先"规整"再"判定"，而不是直接按链接密度判样板？
─────────────────────────────────────────────────────────────
朴素做法「链接占位符长度 / 总长度 > 0.2 即样板」在实测中**大量误杀官方正文**：
    weaver.com.cn/e10/function  ld=0.32  → 泛微 E10 功能正文
    feishu.cn/content/article/… ld=0.55  → 飞书多维表格能力介绍（真正文）
原因：正文里的 markdown 链接很长（URL + title 属性），会稀释成高密度。
判据错在「把 URL 长度当成噪音量」，而正文的价值恰恰不体现在 URL 上。

正确口径：先剥掉 URL/图片标记（**保留锚文本**——锚文本是有语义的），
再看「还剩多少可读字符」。纯样板块剥完几乎什么都不剩，正文块剥完仍是长文。

【决策2】为什么 `_MIN_READABLE` 取 60 这种小值（而不是 200+）？
─────────────────────────────────────────────────────────────
因为**误杀官方正文的代价远高于漏掉几个页脚**：
    - 少一条官方原文 → context_precision 直接掉（实测官网页数 0 的竞品 cp 恒 0）；
    - 少一条页脚 → 影响微乎其微。
所以把「纯碎片」阈值压到很低（只砍 readable<60 的残渣），
再用**页脚/导航签名**（京ICP备 / 版权所有 / 联系我们 / 立即试用 …）做第二道、
且要求「可读字符也不多（<300）」才判样板——保证真正的长正文页不会被签名误杀。

【决策3】这个模块是「单一事实来源」
────────────────────────────────────
Collector（写入侧）与 Analyzer（检索侧）共用同一套判据，杜绝两处口径漂移。
"""
from __future__ import annotations

import re

# ═════════════════════════════════════════════════════════════════════════════
# §1 标记清洗（normalize）
# ═════════════════════════════════════════════════════════════════════════════

# data URI：既覆盖 base64（AVIF/PNG 内联图），也覆盖 svg+xml 的 URL 编码形态
#   data:image/png;base64,iVBORw0KG...
#   data:image/svg+xml,%3csvg%20viewBox='0%200%2086%2024'...
_DATA_URI = re.compile(r"data:[\w/+.\-]+(?:;[\w\-=.]+)*,[^\s)\"']+")

# markdown 图片 ![alt](url) —— 整块丢弃（图片路径无检索价值）
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# 尖括号包起来的裸链接 <https://...>
_ANGLE_URL = re.compile(r"<(?:https?:)?//[^>\s]+>")

# markdown 链接 [锚文本](url "title") —— 保留锚文本，丢弃 URL 与 title
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# 裸 URL（含相对路径 //host/path 形态）
_BARE_URL = re.compile(r"https?://\S+")
_REL_URL = re.compile(r"(?<![\w:/])//[\w.\-]+/[\w\-/.%]+")

# 零宽字符（飞书等站点大量插入 \u200b，会把短行撑成噪音）
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")

# 连续空行收敛（3+ → 2），避免空白把 chunk 撑满 token 预算
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t\u00a0]{2,}")


def normalize_text(text: str) -> str:
    """把「markdown 化网页文本」规整为「可读正文」。

    处理顺序有讲究（不可随意调换）：
      1. data URI —— 必须最先砍，否则超长内联串会干扰后续所有正则；
      2. markdown 图片 —— 图片路径无检索价值，整块丢弃；
      3. data URI 残留的「![alt]()」空壳由第 2 步一并带走；
      4. markdown 链接 —— 保留锚文本（锚文本承载语义）；
      5. 裸 URL / 相对 URL —— 丢弃；
      6. 零宽字符 / 连续空白 —— 收敛。

    Args:
        text: 原始文本（可能含大量标记）。

    Returns:
        规整后的可读正文。原文为空时返回 ""。
    """
    if not text:
        return ""
    t = _DATA_URI.sub(" ", text)
    t = _MD_IMG.sub(" ", t)
    t = _ANGLE_URL.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _BARE_URL.sub(" ", t)
    t = _REL_URL.sub(" ", t)
    t = _ZERO_WIDTH.sub("", t)
    t = _MULTI_SPACE.sub(" ", t)
    t = _MULTI_BLANK.sub("\n\n", t)
    return t.strip()


# ═════════════════════════════════════════════════════════════════════════════
# §2 可读字符统计与样板判定
# ═════════════════════════════════════════════════════════════════════════════

_LATIN = re.compile(r"[A-Za-z]")


def readable_char_count(text: str) -> int:
    """统计「可读字符」数 = CJK 汉字 + 拉丁字母。

    刻意不计入数字、标点、空白——它们无法独立承载语义，
    且混杂在 URL 残渣里会虚高计数。

    契约：入参应为 `normalize_text()` 之后的文本。
    （若直接喂原始文本，URL 里的字母会被计入 → 计数虚高。）
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + len(_LATIN.findall(text))


# 页脚 / 导航签名：出现即高度疑似站点公共区块。
#
# 【L4 工程】只收「强信号」——刻意剔除 立即试用 / 联系我们 / 关注我们 / 购买热线 这类
# 「CTA 词」。原因：它们会以行内链接的形式出现在**真正的正文里**
# （实测飞书「跨平台同步」正文块内含 [立即试用](…)，据此判样板会误杀正文）。
# 保留的都是几乎只出现在页脚/导航的结构性词：
#   备案号、版权声明、上一页/下一页、回到顶部、扫一扫、推荐位标题。
_BOILERPLATE_SIGNATURES: tuple[str, ...] = (
    # 备案 / 版权（几乎只出现在页脚）
    "京ICP备", "ICP备", "ICP证", "公网安备", "增值电信业务经营许可证",
    "版权所有", "Copyright ©", "Copyright(C)", "All Rights Reserved",
    # 结构化导航控件（几乎只出现在页面控件区）
    "上一篇", "下一篇", "回到顶部", "扫码", "扫一扫", "扫描二维码",
    "下载APP", "打开APP",
    # 推荐位 / 列表标题
    "热门推荐", "相关推荐", "推荐文章", "最新文章", "热门标签",
    "猜你喜欢", "为您推荐",
)

# 纯碎片阈值：规整后可读字符低于此值 → 一定是残渣（图片墙/URL 残留/被切碎的尾块）
_MIN_READABLE = 60
# 签名判定的可读上限：只有「内容本来就少」时才允许用签名定样板
_SIGNATURE_MAX_READABLE = 300
# 短行菜单判据：行数 ≥ 此值 且 平均每行可读字符 < 阈值 且 最长行也 < 上限 → 判为导航菜单
_MENU_MIN_LINES = 8
_MENU_AVG_LINE_READABLE = 16
_MENU_MAX_LINE_READABLE = 24

# 列表符号（markdown bullet / 全角项目符号），统计前剥掉
_BULLET_PREFIX = re.compile(r"^[\s*+\-•·▪‣\u2022\u25cf]+")

# ── R4：代码文件名密度（2026-10-16 新增，试算驱动）──
# 【要解决的问题】存在「API 文档代码清单」型长块：
#   task 33a0856e 的「钉钉@定价[3]」「钉钉@功能[5]」= 1958 字，内容是一串源文件名
#   （meeting.py / calendar_free_slot_finder.py / todo_batch_create.py …）。
#   整块可读 1127 > 300（R2 的分支进不去）、块内含长句（R3 要求「每行都短」不成立）
#   → 穿透全部旧规则。
# 【为什么这条安全（试算结论，见 docs/rule_tuning.py）】
#   在「必须保留」回归集（泛微正文 / 飞书正文 / Notion FAQ / 英文文档 / Trae 列表）上
#   命中数**全为 0**；在本次 24 条真实 contexts 上仅那 2 条 API 清单命中 → 零误杀。
# 【为什么不同时加「短行占比」规则】
#   试算显示「行数≥12 且短行占比≥70%」会命中 11/24 条真实 contexts（含可能带价格
#   信息的碎片块），但现有回归集全是 1~7 行的短文本，**无法证明长正文不被误杀**——
#   违背本模块决策2「宁可漏页脚，不可误杀正文」。故先不上，待积累长正文负样本
#   （如 FAQ 列表、步骤说明）后再评估。试算数据已留在 docs/rule_tuning.py。
_CODE_FILE_RE = re.compile(
    r"\b[\w\-]+\.(?:py|js|ts|tsx|jsx|java|go|sql|json|ya?ml|md|sh|rb|php|c|cpp|h)\b",
    re.IGNORECASE,
)
_CODE_FILE_MIN_HITS = 5


def _is_short_line_menu(norm: str) -> bool:
    """判断规整后的文本是否为「短行菜单」（导航/页脚链接列）。

    【L4 工程】为什么需要这条单独规则？
    ────────────────────────────────────
    R1（可读<60）会漏掉**英文导航菜单**——它们每行一个短词
    （`* Homepage` / `* Pricing` / `* Help`），可读字符总数能到 70~90，
    但语义为零。而这类块的英文单词无法被中文签名单（"热门推荐"…）命中。

    特征：行数多、**每一行都短**（无长句）。故要求「行数 ≥ 8 且
    平均每行可读 < 16 且最长行也 < 24」——真·正文项目符号里只要
    出现**一条长句**（如"数据源只有一个，管理入口也只有一个…"），
    最长行就会超上限，从而不被误杀。
    """
    lines = [_BULLET_PREFIX.sub("", ln).strip() for ln in norm.split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) < _MENU_MIN_LINES:
        return False
    counts = [readable_char_count(ln) for ln in lines]
    return (
        max(counts) < _MENU_MAX_LINE_READABLE
        and sum(counts) / len(counts) < _MENU_AVG_LINE_READABLE
    )


def is_boilerplate_chunk(text: str) -> bool:
    """判断单个 chunk 是否为「样板内容」（导航/页脚/图片墙/URL 残渣）。

    【L4 工程】四条规则，宁漏不误杀：
      R1 规整后可读字符 < 60 → 纯残渣，判样板；
      R2 可读字符 < 300 且命中页脚/导航签名 → 判样板；
      R3 短行菜单（行数 ≥ 8 且每行平均可读 < 16 且最长行 < 24）→ 判样板；
      R4 块内代码文件名命中 ≥ 5 次 → API 文档清单，判样板（2026-10-16 新增，试算驱动）。

    Args:
        text: chunk 原文（可以是未规整的）。

    Returns:
        True = 样板（应丢弃）；False = 视为有效正文（保留）。
    """
    if not text or not text.strip():
        return True
    norm = normalize_text(text)
    r = readable_char_count(norm)
    if r < _MIN_READABLE:
        return True
    if r < _SIGNATURE_MAX_READABLE and any(s in norm for s in _BOILERPLATE_SIGNATURES):
        return True
    if _is_short_line_menu(norm):
        return True
    # R4 代码/接口清单：块内源文件名密度过高 → 是 API 文档目录，不是竞品分析素材
    if len(_CODE_FILE_RE.findall(norm)) >= _CODE_FILE_MIN_HITS:
        return True
    return False


def clean_chunk_text(text: str) -> str:
    """规整 + 兜底：若规整后为空则退回原文。

    供「写入侧」使用——避免极端情况下把一条本来有内容的 chunk 变成空串。
    """
    norm = normalize_text(text)
    return norm if norm.strip() else (text or "")
