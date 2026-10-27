"""程序性记忆：默认参数与展示偏好 —— 存、验、压制、注入四件事。

【职责】
  做：把"以后都这样"这类规则落成 `user_preferences` 的一行；
      决定它**能不能生效**（三级 source + 与硬规则冲突时压制）；
      把它拼成 writer prompt 里的 `用户偏好：…` 段；从对话里识别偏好提案。
  不做：不做 SOP / Prompt 模板的版本管理（那是 Prompt 工程，不是记忆）；
        不向量化（偏好按 key 精确取，见 schema §7.6 的三条理由）；
        不让 LLM 直接写 `confirmed`（只能 proposed，见下）。

【输入 → 输出】
  PUT /api/preferences/{key}  → `validate()` → PreferenceDAO.upsert()
  `effective()`               → `{key: value}`（只含 user_set/confirmed）
  `for_injection()`           → (可注入 dict, 被压制的 [(key, 原因)])
  `format_block()`            → writer prompt 的一段文本
  `detect_proposals()`        → 待确认提案列表（进确认卡，不进 prompt）

【关键决策】
  · 三级 source：user_set（UI 显式设置）/ confirmed（用户点过确认）/ proposed（仅提案）
      ← 允许 LLM 直接写 confirmed 等于让它静默改变系统行为，且用户无从察觉；
        "可以提取，不能静默生效"是这条链路唯一的纪律
  · 写入选做**结构校验**，注入侧再做**语义压制** ← 两处判的不是一回事：
      写入时不知道 system 硬规则长什么样（复制一份规则就会有第三个副本开始漂），
      注入时才拿得到上下文。代价是几微秒，收益是"规则只有一份"
  · 被压制要**回话**，不能静默忽略 ← 用户设了"只要一行"却照常输出六段，
      不说原因就等于骗他设置成功了
  · 偏好拼在角色设定之后、任务数据之前 ← 位置本身不产生优先级，
      真正的优先级由"压制"实现；放前面是为了让它影响后续所有写法描述
  · 提案识别先过关键词门 ← 每轮对话都调一次 LLM 判"有没有在说偏好"，
      90% 的轮次是白烧；命中"以后/默认/每次"才花这次钱

【读法/注意事项】
  ⚠️ `proposed` 永不进 prompt。`PreferenceDAO.get_effective()` 已在 SQL 层过滤，
    这里再断言一次——越权一次就够让用户失去对系统的信任。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from src.config import Settings

logger = logging.getLogger(__name__)

# ── 键注册表：三个键的语义、结构与前端文案 ────────────────────────────────
# 键空间必须可枚举，前端才能画"你设置过哪些偏好"；这也是偏好不进向量表的理由之一。


def _check_str_list(value: Any, max_items: int = 4, max_len: int = 20) -> str | None:
    """通用校验：非空字符串列表、元素不重复、条数与长度受限。返回错误文案或 None。"""
    if not isinstance(value, list) or not value:
        return "需要是至少含一项的字符串数组"
    items = [str(v).strip() for v in value if str(v).strip()]
    if not items:
        return "数组里没有有效值"
    if len(items) > max_items:
        return f"最多 {max_items} 项，实际 {len(items)} 项"
    if any(len(i) > max_len for i in items):
        return f"单项长度不超过 {max_len} 字"
    if len(set(items)) != len(items):
        return "有重复项"
    return None


_REPORT_LENGTHS = ("brief", "standard", "detailed")


def _check_report_style(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "需要是对象，如 {\"length\": \"brief\"}"
    unknown = set(value) - {"length", "tables", "cite_sources"}
    if unknown:
        return f"不认识的字段：{', '.join(sorted(unknown))}"
    length = value.get("length")
    if length is not None and length not in _REPORT_LENGTHS:
        return f"length 只能是 {'/'.join(_REPORT_LENGTHS)}"
    for flag in ("tables", "cite_sources"):
        if flag in value and not isinstance(value[flag], bool):
            return f"{flag} 需要是布尔值"
    return None


KNOWN_KEYS: dict[str, dict] = {
    "default_dimensions": {
        "label": "默认分析维度",
        "check": lambda v: _check_str_list(v, max_items=4),
        "describe": lambda v: "、".join(str(x) for x in v),
        "proposal_question": lambda v: f"以后建任务默认用「{'、'.join(str(x) for x in v)}」这几个维度？",
    },
    "frequent_competitors": {
        "label": "常用竞品",
        "check": lambda v: _check_str_list(v, max_items=4),
        "describe": lambda v: "、".join(str(x) for x in v),
        "proposal_question": lambda v: f"以后默认把「{'、'.join(str(x) for x in v)}」当作分析竞品？",
    },
    "report_style": {
        "label": "报告风格",
        "check": _check_report_style,
        "describe": lambda v: _describe_style(v),
        "proposal_question": lambda v: f"以后报告都按「{_describe_style(v)}」来写？",
    },
}

_LENGTH_CN = {"brief": "更短", "standard": "常规", "detailed": "更详细"}


def _describe_style(value: dict) -> str:
    parts = []
    if value.get("length"):
        parts.append(_LENGTH_CN.get(value["length"], value["length"]))
    if value.get("tables") is False:
        parts.append("不要表格")
    elif value.get("tables") is True:
        parts.append("保留表格")
    if value.get("cite_sources") is False:
        parts.append("不标来源")
    return "、".join(parts) or "未指定"


# ── 写入侧：结构校验 ───────────────────────────────────────────────────
# 只判"这值能不能存"（类型/形状/条数），不判"这值合不合规矩"（那要看硬规则）。


def validate(key: str, value: Any) -> str | None:
    """校验一条偏好能不能写。返回错误文案（给 400 用）或 None。"""
    spec = KNOWN_KEYS.get(key)
    if spec is None:
        return f"不认识的偏好键：{key}（可用：{', '.join(KNOWN_KEYS)}）"
    return spec["check"](value)


# ── 注入侧：语义压制 ───────────────────────────────────────────────────
# 【为什么规则不写进写入侧】system 里的硬规则（必须给表格、必须逐维度展开、
# 必须标来源）写在 writer prompt 里。把它们复制一份到写入校验，就等于有两个
# 副本开始各自漂——prompt 改了校验没改，用户就会设进一个"永远被压制"的偏好，
# 而他只会觉得系统坏了。所以写入只挡结构，语义冲突在注入时判。

_GUARDS: dict[str, Callable[[Any], str | None]] = {
    "report_style": lambda v: (
        f"与报告结构硬规则冲突：{_describe_style(v)}"
        "（system 要求必须给总览表格与逐维度分析，本次不注入）"
        if isinstance(v, dict)
        and (v.get("length") not in (None, *_REPORT_LENGTHS) or v.get("tables") is False)
        else None
    ),
    "default_dimensions": lambda v: (
        f"超出单任务 4 维度上限（system 优先，本次不注入；实际 {len(v)} 项）"
        if isinstance(v, list) and len(v) > 4
        else None
    ),
    "frequent_competitors": lambda v: (
        f"超出单任务 4 竞品上限（system 优先，本次不注入；实际 {len(v)} 项）"
        if isinstance(v, list) and len(v) > 4
        else None
    ),
}


def for_injection(effective: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """把可生效偏好筛成"本次真能注入的"，并交出被压制的键与原因。

    ★ 一句话记住：**偏好是默认值，不是覆盖规则的授权。**
      与 system 硬规则冲突时 system 赢，且要告诉用户"你的这条没生效"，
      否则他会以为设置成功了——静默失败比直接报错更伤人。
    """
    usable: dict[str, Any] = {}
    suppressed: list[tuple[str, str]] = []
    for key, value in (effective or {}).items():
        if key not in KNOWN_KEYS:
            suppressed.append((key, "未知键（可能是旧版本写入）"))
            continue
        reason = _GUARDS.get(key, lambda _v: None)(value)
        if reason:
            suppressed.append((key, reason))
            continue
        usable[key] = value
    if suppressed:
        logger.info(
            "偏好注入被压制: %s", "; ".join(f"{k}={why}" for k, why in suppressed)
        )
    return usable, suppressed


def format_block(usable: dict[str, Any]) -> str:
    """拼成 prompt 里的一段；无可注入偏好时返回空串（不留空段落）。"""
    lines = []
    for key, value in (usable or {}).items():
        spec = KNOWN_KEYS.get(key)
        if not spec:
            continue
        lines.append(f"- {spec['label']}：{spec['describe'](value)}")
    if not lines:
        return ""
    return (
        "用户偏好（默认值，与上述硬规则冲突时以硬规则为准）：\n"
        + "\n".join(lines)
        + "\n"
    )


def prefill_for_task(usable: dict[str, Any]) -> dict[str, Any]:
    """建任务时用来补空缺字段的两项（其余偏好与建任务无关）。"""
    out: dict[str, Any] = {}
    if isinstance(usable.get("default_dimensions"), list):
        out["dimensions"] = [str(x) for x in usable["default_dimensions"]]
    if isinstance(usable.get("frequent_competitors"), list):
        out["competitors"] = [str(x) for x in usable["frequent_competitors"]]
    return out


# ── 提案：从对话里提取"以后都这样" ─────────────────────────────────────

PROPOSAL_PROMPT = """从用户这句话里提取"以后都这样"的偏好。只提取用户明确表达的规则，
不提取一次性的要求（"这次用三段"不算偏好）。

可选键与取值：
- default_dimensions: 字符串数组，如 ["功能","定价"]
- frequent_competitors: 字符串数组
- report_style: 对象，可含 length(brief/standard/detailed)、tables(bool)、cite_sources(bool)

用户这句话：%s

只输出 JSON，没有偏好则输出 {"preferences": []}：
{"preferences": [{"key": "...", "value": ..., "confidence": 0.0~1.0}]}"""


def has_cue(text: str, settings: Settings | None = None) -> bool:
    """关键词门：这句话里有"以后/默认/每次"这类词才值得花一次 LLM。

    ★ 前置过滤不是省钱的技巧，是分层的必然：判"有没有在说偏好"本身就是
      一次分类任务，用字符串命中就能挡掉 90%，剩下的才轮到模型。
    """
    cfg = settings or Settings()
    cues = [c.strip() for c in (cfg.preference_cue_keywords or "").split(",") if c.strip()]
    blob = text or ""
    return any(c in blob for c in cues)


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def detect_proposals(
    text: str, llm: Any, settings: Settings | None = None
) -> list[dict]:
    """识别出偏好提案（**未落库**）。校验不过的键直接丢掉并记日志。

    返回 [{"key","value","confidence"}]。空列表 = 这句话里没有偏好。
    """
    cfg = settings or Settings()
    if not cfg.memory_preferences_enabled or not has_cue(text, cfg) or llm is None:
        return []
    try:
        resp = await llm.ainvoke(PROPOSAL_PROMPT % text)
        data = _parse_json(getattr(resp, "content", "") or "")
    except Exception as exc:  # noqa: BLE001 — 提案识别失败不影响这句对话
        logger.warning("偏好提案识别失败: %s", exc)
        return []

    out: list[dict] = []
    for item in data.get("preferences") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = item.get("value")
        reason = validate(key, value)
        if reason:
            logger.info("丢弃无效偏好提案 key=%s: %s", key or "?", reason)
            continue
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({"key": key, "value": value, "confidence": min(max(confidence, 0.0), 1.0)})
    return out


def pending_cards(rows: list[dict]) -> list[dict]:
    """把 proposed 行转成前端确认卡要的结构（含中文问题文案）。"""
    cards = []
    for row in rows or []:
        if row.get("source") != "proposed":
            continue
        key = row.get("key")
        spec = KNOWN_KEYS.get(key)
        if not spec:
            continue
        value = row.get("value")
        raw = value
        if isinstance(raw, (str, bytes)):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
        try:
            question = spec["proposal_question"](value)
        except Exception:  # noqa: BLE001 — 文案拼不出来就不显示，不影响数据
            continue
        cards.append({"key": key, "value": value, "question": question})
    return cards
