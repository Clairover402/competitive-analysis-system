"""分析结果单元格形态归一 —— 「结论取值」的唯一口径。

═══════════════════════════════════════════════════════════════════════════════
                         【L4 工程】为什么要有这个模块？
═══════════════════════════════════════════════════════════════════════════════

Analyzer 的产出 analysis_results 形状是：

    {维度名: {竞品名: 单元格}}

单元格在历史上出现过两种形态，必须兼容：

    A. 结构化对象（2026-10-06 加固后的目标形态）
       {"conclusion": "结论正文", "source_url": "https://..."}

    B. 自由文本（旧版本代码 / 历史 checkpoint 恢复出来的旧状态）
       "结论正文（附带 source_url）"

【2026-10-12 踩坑记录 —— 本模块存在的直接原因】
──────────────────────────────────────────────
给返回值「加字段」（str → {conclusion, source_url}）时，只改了产出方，
漏改了同一文件里的统计代码：analyzer 判断单元格是否有数据时仍写

    str(text).startswith("[分析失败]")   # text 此时已是 dict

而 str(dict) 的头部是 "{'conclusion'"，两个 startswith 恒为 False
→ 过滤条件恒 True → 「有数据的竞品数」恒等于全部竞品。
后果：写进 agent_logs 的 per_dimension 永远显示「所有竞品都有数据」，
Grafana 和审计里「哪个维度没数据」彻底失真，而主流程不报任何错。

【设计结论】
────────────
读：凡是要对单元格做「取正文 / 判空 / 判占位 / 统计长度 / 汇总维度状态」的地方，
    一律调用本模块的函数，**不要在调用点自己写 isinstance 或 str(...).startswith(...)**。
写：凡是要造一个「无实质结论」的占位单元格，一律 placeholder_cell(reason)，
    **不要在调用点手写 {"conclusion": ..., "source_url": ""} 字面量**。

两个方向都收敛到本模块后，「单元格形态」这个不变量才真正有单一源头
（2026-10-12 只做了读侧，写侧漏了 4 处手写字面量，2026-10-13 补齐）。
"""

from __future__ import annotations

# ── 无实质结论的占位标记 ──
# analyzer 产出这些标记表示「这一格没有可用结论」，各消费方据此跳过或降级处理。
MISSING_MARKERS: tuple[str, ...] = ("[数据不足]", "[待验证]", "[分析失败]")


def conclusion_text(val) -> str:
    """从单元格值中取出「结论文本」。

    兼容两种形态：
        dict → 读 conclusion 字段（忽略 source_url，避免 URL 混进结论）
        其他 → str() 兜底（旧形态自由文本）

    Args:
        val: 单元格值，dict 或 str 或任何可 str() 的对象

    Returns:
        结论文本；取不到时返回空字符串（永不返回 None）
    """
    if isinstance(val, dict):
        return str(val.get("conclusion") or "")
    return str(val or "")


def cell_source_url(val) -> str:
    """从单元格值中取出「来源链接」（仅结构化形态有；旧文本形态返回空串）。"""
    if isinstance(val, dict):
        return str(val.get("source_url") or "")
    return ""


def placeholder_cell(reason: str) -> dict[str, str]:
    """构造一个「无实质结论」的标准单元格 —— **产出侧**的唯一口径。

    【2026-10-13 补漏 —— 为什么产出侧也要有构造函数】
    ────────────────────────────────────────────────
    2026-10-12 修 has_data 恒 True 时，只在 analyzer_agent 里把三条失败分支
    统一成了 {conclusion, source_url}，**漏掉 _retrieve_and_analyze_dimension
    内「召回为空」的提前返回分支**——那一条仍在返回裸字符串，而同文件另外几处
    也是手写 dict 字面量。

    后果不是崩溃（消费侧的 conclusion_text 能把 str 接住），而是
    「analysis_results 单元格恒为 dict」这个**不变量被破坏**：
    只要还有任何一处产出裸值，消费方就永远不敢删 isinstance 兼容分支，
    下次改形态时依然要满仓库找解包点——这正是本模块想终结的循环。

    所以把「造格子」也收敛到这里：占位统一 placeholder_cell(reason)，
    reason 需自带标记前缀（[数据不足] / [分析失败] / [待验证]）。

    Args:
        reason: 完整的占位结论文本，如 "[数据不足] 未检索到相关网页内容"

    Returns:
        {"conclusion": reason, "source_url": ""}
        source_url 恒为空——占位意味着没有可追溯的依据。
    """
    return {"conclusion": str(reason or ""), "source_url": ""}


def is_placeholder(text: str) -> bool:
    """结论文本整体就是一个占位标记（用 startswith 判定）。

    例："[数据不足] 未找到相关信息" → True
        "定价 198 元，源自官网"     → False
    """
    text = (text or "").strip()
    return text.startswith(MISSING_MARKERS)


def has_missing_marker(text: str) -> bool:
    """结论文本中**任意位置**出现缺失标记（用 in 判定）。

    比 is_placeholder 更宽松，用于「LLM 在长结论里夹了一句 [待验证]」的场景
    （如 Quality 组装数据缺失清单）。
    """
    text = text or ""
    return any(m in text for m in MISSING_MARKERS)


def cell_has_data(val) -> bool:
    """判断单元格是否含「实质结论」（非空 且 不是占位标记）。

    这是「某竞品在某维度到底有没有分析结果」的唯一判定口径。
    """
    text = conclusion_text(val).strip()
    return bool(text) and not is_placeholder(text)


def summarize_per_dimension(analysis_results: dict) -> dict:
    """汇总每个维度的分析状态 —— 写入 agent_logs 的 per_dimension 字段。

    【L4 工程】抽成纯函数的原因：
        「每维度有效竞品数」原本内联在 analyzer_agent 里，导致
        ① 无法单测（要起 DB/LLM 才能跑到）；
        ② 形态变更时成为漏改重灾区（见模块 docstring 的踩坑记录）。
        抽成纯函数后既能单测，也让「口径」与共享模块绑在一起。

    Args:
        analysis_results: {维度名: {竞品名: 单元格}}

    Returns:
        {维度名: {"competitors_analyzed": int, "has_data": bool}}
    """
    per_dimension: dict[str, dict] = {}
    for dim_name, comp_analyses in (analysis_results or {}).items():
        if not isinstance(comp_analyses, dict):
            # 防御：维度级失败时旧代码会把整个维度写成 {"error": "..."}，
            # 该分支已在 analyzer 归一化，这里只做兜底，避免 AttributeError。
            per_dimension[dim_name] = {"competitors_analyzed": 0, "has_data": False}
            continue
        analyzed = [c for c, val in comp_analyses.items() if cell_has_data(val)]
        per_dimension[dim_name] = {
            "competitors_analyzed": len(analyzed),
            "has_data": len(analyzed) > 0,
        }
    return per_dimension
