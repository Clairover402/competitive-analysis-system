"""追问 —— 判不准或缺锚点时问用户一句。

【职责】
  两种追问的文案与选项；以及"该用哪一种"的判定。
【不做】
  判意图（classifier）、抽槽位（slots）。这里只回答"该问什么"。
【关键决策】
  ★ 两种追问**不能共用**：
      不知道要什么（CLARIFY）—— 意图层判不出来
      知道要分析但缺锚点（NO_ANCHOR）—— 意图已确定，只是没竞品/维度
    混用会让"调研一下市场"被回"你是想做什么？"，答非所问。
  ★ 选项自带**可直接发送的完整句子**（不是意图 key）。发回"②"会再判一次"②"→
    四层全判不出 → 再反问，死循环。契约：每条 message 必须能被下一轮判回该意图。
【读法】
  新加选项先想清楚"这句话 classifier 认不认"，否则等于换个方式死循环。
"""

CLARIFY_QUESTION = "这句话我没把握该怎么处理，你是想做什么？"

CLARIFY_CHOICES: tuple[dict[str, str], ...] = (
    {"label": "分析几家竞品", "message": "分析飞书和钉钉在功能和定价上的表现"},
    {"label": "找以前跑过的报告", "message": "我以前跑过的竞品分析报告在哪"},
    {"label": "查某条结论的证据", "message": "报告里飞书比钉钉便宜这个结论的证据在哪"},
    {"label": "核对数据是否过期", "message": "报告里飞书那个定价现在还是这个价吗"},
)

NO_ANCHOR_QUESTION = "要分析哪几家？至少给我竞品或维度之一。"
NO_ANCHOR_CHOICES: tuple[dict[str, str], ...] = (
    {"label": "分析飞书和钉钉", "message": "分析飞书和钉钉在功能和定价上的表现"},
    {"label": "按市场看定价", "message": "从定价角度分析协同办公市场"},
)

# 灰度开关关闭时的问法：此时多意图整体下线，不能让用户以为 lookup 还能用。
FUNNEL_OFF_QUESTION = "对话模式当前未开启，只能做竞品分析。要分析哪几家？"


def no_anchor_channel(funnel_off: bool = False) -> tuple[str, list[dict[str, str]]]:
    """缺锚点该问什么。开关关闭时换成"只能做竞品分析"，避免暗示其他意图可用。"""
    question = FUNNEL_OFF_QUESTION if funnel_off else NO_ANCHOR_QUESTION
    return question, list(NO_ANCHOR_CHOICES)
