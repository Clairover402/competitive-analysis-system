"""模型输出解析 —— 容忍围栏与前后废话的 JSON 抠取。

【职责】
  把 LLM 原始文本变成 dict；把其中的实体字段规整成干净字符串列表。
【不做】
  判断意图对不对（classifier）、决定槽位必不必填（slots）。
【关键决策】
  ★ 两个环节（判意图 / 填槽位）共用同一份解析器。分成两份必然漂移，
    同一个模型回复在"判意图"环节能解析、在"填槽位"环节解析失败。
  ★ 类型防御只在这里做一次：模型常把单元素列表写成字符串（"飞书" 而非 ["飞书"]）。
【读法】
  解析返回 {} 表示"模型没说人话"，调用方各自决定兜底（意图兜 analysis、槽位留空）。
"""

from __future__ import annotations

import json


def parse_json_object(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍 ``` 围栏与前后废话）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text[3:]
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_str_list(value) -> list[str]:
    """把模型给的单值或列表规整成去空字符串列表。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
