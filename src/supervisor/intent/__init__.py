"""意图识别 —— 判 label 与填槽位是两个独立的段。

【职责】
  一句话 → 一个 label（classifier）→ 一组槽位（slots）→ 该问就追问（clarify）。
【不做】
  执行查询（query_handlers）、写报告（pipeline）、决定任务怎么跑（supervisor）。
【关键决策】
  ★ 两段之间只有**单向**依赖：label 决定抽哪些槽，槽位**不**反过来影响 label。
    2026-10-29 之前是混在一起的（L3 一次调用同时产 intent + 三槽），
    后果是 L2 判对却因"缺槽位"被要求整句重判 —— 实测 45 条判对只放行 10 条，
    前两层接管率 7.3%。拆开后同一批数据：接管 32.1%，端到端 97.9% → 97.1%。
  ★ 判据从此是「这次判得准不准」，而不是「这个意图缺不缺输入」。
    缺输入是槽位段的活，它有自己的降级路径（analysis 追问、trace 少一层过滤）。
【读法】
  想改判据 → labels.py；改阈值 → classifier.py 的 calibrate()；
  改抽什么 → slots.py 顶上那张"缺了下游会不会答错"的表。
"""

from src.supervisor.intent.classifier import (
    Decision,
    calibrate,
    classify,
    l2_semantic,
    l3_llm,
    l4_clarify,
)
from src.supervisor.intent.clarify import (
    CLARIFY_CHOICES,
    CLARIFY_QUESTION,
    FUNNEL_OFF_QUESTION,
    NO_ANCHOR_CHOICES,
    NO_ANCHOR_QUESTION,
    no_anchor_channel,
)
from src.supervisor.intent.labels import EXEMPLARS, INTENTS, INTENT_PROMPT, UNKNOWN
from src.supervisor.intent.parsing import normalize_str_list, parse_json_object
from src.supervisor.intent.slots import (
    SLOT_PROMPT,
    Slots,
    fill_slots,
    llm_slots,
    rule_dimensions,
)

__all__ = [
    "CLARIFY_CHOICES",
    "CLARIFY_QUESTION",
    "Decision",
    "EXEMPLARS",
    "FUNNEL_OFF_QUESTION",
    "INTENTS",
    "INTENT_PROMPT",
    "NO_ANCHOR_CHOICES",
    "NO_ANCHOR_QUESTION",
    "SLOT_PROMPT",
    "Slots",
    "UNKNOWN",
    "calibrate",
    "classify",
    "fill_slots",
    "l2_semantic",
    "l3_llm",
    "l4_clarify",
    "llm_slots",
    "no_anchor_channel",
    "normalize_str_list",
    "parse_json_object",
    "rule_dimensions",
]
