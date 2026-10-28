"""意图评估集 — 150 句带 gold 的说法，用来回答「L1/L2/L3 各准不准」。

【为什么需要这份东西】
此前所有关于"意图识别好不好"的判断都来自零散实测：`top1=0.832 gap=0.109`
这种日志能证明某一次判对了，证明不了「L2 在 150 句里错几类、错在哪两类上」。
没有分母的准确率不是准确率 —— 22 句 EXEMPLARS 既是"训练集"又是"全部样本"，
等于自己出题自己考。

【gold 按产品定义标，不按"我觉得"】
  analysis = 要一份**新**产出（跑报告）
  lookup   = 要一份**已有**产出（查报告、标题、历史记录）
  trace    = 要**证据**（URL、原文片段、引用链）
  verify   = 要判断**还准不准**（观测时间、保鲜期、现在变了没）
  unknown  = 该追问（信息不足 / 域外，猜一个不如问一句）

两个意图都说得通的句子标 `ambiguous=True`，**单独统计、不混进主指标**：
算进准确率会虚高错误率（模型怎么答都有一半算错），剔掉又会掩盖真实的边界模糊。
主指标只在 `ambiguous=False` 的样本上算。

【怎么用】
  python -m src.evaluation.intent_eval             # L1/L2 全量 + L3 只跑下沉样本
  python -m src.evaluation.intent_eval --no-l3      # 只看 L1/L2 接管率与下沉量
  python -m src.evaluation.intent_eval --sweep      # 顺带扫 floor × margin
"""

from __future__ import annotations

from dataclasses import dataclass, replace

INTENTS_GOLD = ("analysis", "lookup", "trace", "verify", "unknown")


@dataclass(frozen=True)
class Case:
    """一句待判的话 + 它应该被判成什么。"""

    text: str
    gold: str
    # 结构化入参：只有"标准模式"表单提交才会带，用来验证 L1 直通
    competitors: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    # 两种判法都说得通。单独统计，不进主指标。
    ambiguous: bool = False
    group: str = "core"          # core = 主体样本；adversarial = 对抗/边界
    note: str = ""               # 只在需要说明口径时写

    def __post_init__(self) -> None:
        if self.gold not in INTENTS_GOLD:
            raise ValueError(f"gold 非法: {self.gold!r}（{self.text!r}）")


# ══════════════════════════════════════════════════════════════════════════════
# 一、analysis — 要一份新产出（30 条）
# ══════════════════════════════════════════════════════════════════════════════

_ANALYSIS: list[Case] = [
    Case("分析飞书和钉钉的定价", "analysis"),
    Case("帮我分析一下 Notion 和飞书", "analysis"),
    Case("对比一下钉钉和企业微信的功能", "analysis"),
    Case("出一份飞书的竞品分析报告", "analysis"),
    Case("调研一下国内协同办公市场", "analysis"),
    Case("分析飞书、钉钉、企业微信的定价和功能", "analysis"),
    Case("帮我做一份 AI 编程工具的竞品分析", "analysis"),
    Case("看看 Cursor 和 GitHub Copilot 的差异", "analysis"),
    Case("我想了解一下通义灵码和文心快码", "analysis"),
    Case("研究下飞书的商业模式", "analysis"),
    Case("给我分析钉钉", "analysis"),
    Case("对飞书做一个全面的竞品分析", "analysis"),
    Case("帮我跑一次竞品分析：飞书 vs 钉钉", "analysis"),
    Case("分析一下这三家：飞书、钉钉、企业微信", "analysis"),
    Case("需要一份关于国内 IM 市场的分析", "analysis"),
    Case("深入分析飞书的功能模块", "analysis"),
    Case("帮我调研一下飞书的定价策略", "analysis"),
    Case("对比飞书和钉钉在协作和文档上的差异", "analysis"),
    Case("出一份报告，分析飞书和钉钉", "analysis"),
    Case("分析一下 GitHub Copilot 的市场表现", "analysis"),
    Case("我想看一份国产大模型的竞品分析", "analysis"),
    Case("帮我分析腾讯会议和 Zoom", "analysis"),
    Case("研究一下飞书的市场定位", "analysis"),
    Case("分析飞书、钉钉、企业微信在定价上的对比", "analysis"),
    Case("给我做一份竞品报告", "analysis"),
    Case("帮我评估一下钉钉的产品能力", "analysis"),
    Case("分析飞书近一年的产品变化", "analysis"),
    Case("帮忙看看飞书和钉钉这两家", "analysis"),
    Case("对比分析：飞书 vs 钉钉 vs 企业微信", "analysis"),
    # 标准模式：两个槽位都填了，L1 应当直接放行
    Case("", "analysis", competitors=("飞书", "钉钉"), dimensions=("功能", "定价")),
]

# ══════════════════════════════════════════════════════════════════════════════
# 二、lookup — 要一份已有产出（30 条）
# ══════════════════════════════════════════════════════════════════════════════

_LOOKUP: list[Case] = [
    Case("我上次的任务是什么", "lookup"),
    Case("之前的报告在哪", "lookup"),
    Case("我上上个任务是啥", "lookup"),
    Case("最近五次的分析都列一下", "lookup"),
    Case("帮我找一下飞书的那份报告", "lookup"),
    Case("我跑过哪些分析", "lookup"),
    Case("上次分析的结论是什么", "lookup"),
    Case("有一份叫协同办公竞品分析的报告吗", "lookup"),
    Case("我最近的分析记录", "lookup"),
    Case("把历史任务列出来", "lookup"),
    Case("倒数第三次分析是什么", "lookup"),
    Case("前两次的报告给我看看", "lookup"),
    Case("我做过钉钉的分析吗", "lookup"),
    Case("上次那份报告叫什么名字", "lookup"),
    Case("还有更早的报告吗", "lookup"),
    Case("查一下我的分析记录", "lookup"),
    Case("最近一次分析是什么时候", "lookup"),
    Case("列出所有报告", "lookup"),
    Case("之前分析飞书的报告还在吗", "lookup"),
    Case("哪次分析是关于定价的", "lookup"),
    Case("我上周跑的那份报告", "lookup"),
    Case("历史记录里有没有关于 Copilot 的", "lookup"),
    Case("帮我找回上次的报告", "lookup"),
    Case("上次那份报告是哪天生成的", "lookup"),
    Case("我有哪些已完成的任务", "lookup"),
    Case("报告列表", "lookup"),
    Case("上一次分析的标题是什么", "lookup"),
    Case("找一下我之前做的竞品分析", "lookup"),
    Case("我总共有几次分析", "lookup"),
    Case("报告 v1 和 v2 有什么区别", "lookup"),
]

# ══════════════════════════════════════════════════════════════════════════════
# 三、trace — 要证据（30 条）
# ══════════════════════════════════════════════════════════════════════════════

_TRACE: list[Case] = [
    Case("这个结论你从哪看到的", "trace"),
    Case("飞书定价的数据来源是哪个网页", "trace"),
    Case("那句话的出处是什么", "trace"),
    Case("证据在哪", "trace"),
    Case("你这个结论有链接吗", "trace"),
    Case("给我看看原文", "trace"),
    Case("定价 45 元这条是哪来的", "trace"),
    Case("引用来源列一下", "trace"),
    Case("这条结论引用了什么", "trace"),
    Case("我要看原始出处", "trace"),
    Case("数据是从哪个网站抓的", "trace"),
    Case("报告里的 URL 都是什么", "trace"),
    Case("这句话的证据链", "trace"),
    Case("你说飞书份额 30%，谁说的", "trace"),
    Case("来源网址", "trace"),
    Case("那条结论对应的原文片段", "trace"),
    Case("引用了几篇", "trace"),
    Case("这个数字出自哪里", "trace"),
    Case("帮我把证据列出来", "trace"),
    Case("上次报告引用了哪些网站", "trace"),
    Case("这条分析的依据是什么网页", "trace"),
    Case("我想核对一下原文", "trace"),
    Case("出处链接给我", "trace"),
    Case("哪个页面写的功能对比", "trace"),
    Case("证据映射里有什么", "trace"),
    Case("引用来源是否可访问", "trace"),
    Case("这句话你抄的哪里", "trace"),
    Case("报告的证据在哪张表里", "trace"),
    Case("原文是怎么说的", "trace"),
    Case("给我看抓取的原文片段", "trace"),
]

# ══════════════════════════════════════════════════════════════════════════════
# 四、verify — 要判断还准不准（30 条）
# ══════════════════════════════════════════════════════════════════════════════

_VERIFY: list[Case] = [
    Case("这个结论现在还准吗", "verify"),
    Case("飞书定价现在是多少", "verify"),
    Case("上次的数据过期了吗", "verify"),
    Case("那个价格还有效吗", "verify"),
    Case("这份报告的内容是不是过时了", "verify"),
    Case("飞书现在的定价变了没", "verify"),
    Case("那条结论还成立吗", "verify"),
    Case("数据是最新的吗", "verify"),
    Case("三个月前的定价现在还能用吗", "verify"),
    Case("这个结论需要更新吗", "verify"),
    Case("飞书最近涨价了吗", "verify"),
    Case("报告里提到的功能现在还有吗", "verify"),
    Case("保鲜期过了没", "verify"),
    Case("这些数据多久没更新了", "verify"),
    Case("现在钉钉定价是 45 吗", "verify"),
    Case("结论的有效期到什么时候", "verify"),
    Case("这份报告还能信吗", "verify"),
    Case("数据新鲜度怎么样", "verify"),
    Case("上次观测到现在多久了", "verify"),
    Case("飞书定价有没有变动", "verify"),
    Case("这个结论是不是该重跑了", "verify"),
    Case("报告里的信息现在还准确不", "verify"),
    Case("那条事实是不是已经失效", "verify"),
    Case("现在的最新定价是多少", "verify"),
    Case("上次分析的结论还作数吗", "verify"),
    Case("这些数据是几号观测的", "verify"),
    Case("有没有更新的观测", "verify"),
    Case("现在市场上还是这个价吗", "verify"),
    Case("报告过时了吧", "verify"),
    Case("这个结论能信几天", "verify"),
]

# ══════════════════════════════════════════════════════════════════════════════
# 五、对抗样本（30 条）— 易混对、复合句、信息不足、域外
# ══════════════════════════════════════════════════════════════════════════════
#
# 这一组不是为了拉低分数，是为了暴露"模型在哪儿会犹豫"。
# ambiguous=True 的样本单独统计 —— 它们的 gold 本身就有争议，
# 混进主指标等于用一个没有唯一答案的题去算对错。

_ADV_RAW: list[Case] = [
    # ── 易混：analysis / verify ──
    Case("飞书和钉钉哪个好", "analysis", ambiguous=True,
         note="要结论还是要新报告？产品上会跑分析，但也可能想听现成判断"),
    Case("飞书多少钱", "verify", ambiguous=True,
         note="问当前值 → verify；但没有主语时也可能是在要一次分析"),
    # ── 易混：lookup / verify ──
    Case("上次分析飞书的结果现在还对吗", "verify", ambiguous=True,
         note="先 lookup 再 verify，复合句"),
    Case("上次报告现在还能用吗", "verify"),
    Case("上次报告里那个定价，现在还是 45 吗", "verify", ambiguous=True,
         note="带具体旧值地问现状，最接近 verify 的典型形态"),
    # ── 易混：lookup / trace ──
    Case("那份报告的定价部分", "lookup", ambiguous=True),
    Case("报告里说的定价，来源是哪", "trace", note="复合句，取后半段的诉求"),
    # ── 复合句：两个诉求，取向看后半句 ──
    Case("这个数你从哪抄的，现在变了没", "verify", ambiguous=True),
    Case("那条结论的依据，以及它过没过期", "verify", ambiguous=True),
    Case("把上次报告的来源列一下", "trace"),
    Case("报告里那几条结论的出处都给我", "trace"),
    Case("我要看飞书定价的原始网页", "trace"),
    # ── 指代：缺上下文时到底该查什么 ──
    Case("你上次说的那个", "lookup", ambiguous=True, note="指代句，靠会话记忆才判得准"),
    # ── 信息不足 → 该追问 ──
    Case("分析", "unknown"),
    Case("帮我看下", "unknown"),
    Case("报告", "unknown"),
    Case("飞书", "unknown", note="只有竞品名，没有动作"),
    Case("定价", "unknown"),
    Case("飞书和钉钉", "unknown"),
    # ★ 这条原本标 unknown，改了。判 analysis 但缺锚点时走的是
    #   NO_ANCHOR_QUESTION（"要分析哪几家？"），比 L4 的
    #   "这句话我没把握该怎么处理" 准确得多 —— 用户说得挺清楚，只是没说谁。
    #   标注要按"产品该有的行为"标，不能因为它最终也会反问就算 unknown。
    Case("帮我分析下竞品", "analysis", note="缺竞品 → analysis 分支会问「要分析哪几家」"),
    Case("这个准吗", "unknown", ambiguous=True, note="没有指代对象"),
    Case("上次是谁分析的", "unknown", ambiguous=True),
    # ── 域外：不该被硬塞进四个意图 ──
    Case("今天天气怎么样", "unknown"),
    Case("帮我写首诗", "unknown"),
    Case("你好", "unknown"),
    Case("1+1 等于几", "unknown"),
    # ── 显式要重跑：别被当成 lookup ──
    Case("再分析一次飞书和钉钉", "analysis"),
    Case("上次那个分析重跑一遍", "analysis"),
    Case("重新出一份飞书的报告", "analysis"),
    Case("之前那两次分析都给我", "lookup"),
]

# 对抗组统一打标：`replace` 而不是逐条加参数 —— 29 处重复不如一次声明。
_ADVERSARIAL: list[Case] = [replace(c, group="adversarial") for c in _ADV_RAW]


CASES: list[Case] = _ANALYSIS + _LOOKUP + _TRACE + _VERIFY + _ADVERSARIAL


def stats() -> dict:
    """数据集自身的形状 —— 先确认题目分布，再看分数。"""
    by_gold: dict[str, int] = {}
    by_group: dict[str, int] = {}
    for c in CASES:
        by_gold[c.gold] = by_gold.get(c.gold, 0) + 1
        by_group[c.group] = by_group.get(c.group, 0) + 1
    return {
        "total": len(CASES),
        "by_gold": by_gold,
        "by_group": by_group,
        "ambiguous": sum(1 for c in CASES if c.ambiguous),
    }
