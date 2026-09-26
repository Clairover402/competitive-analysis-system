"""Writer Agent — 将分析结果组装为结构化 Markdown 报告。

═══════════════════════════════════════════════════════════════════════════════
                         【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Writer 是四个 Agent 中最"轻"的一个——它是纯 LLM Agent（不调用任何工具）。

【L3 核心考点】三种 Agent 模式对比
─────────────────────────────────
  类型          | 有工具？ | 典型场景     | 本系统示例
  ──────────────┼──────────┼──────────────┼────────────
  Agent+Tool    | 有       | 需要外部数据  | Collector (web_search/web_fetch)
  Agent+RAG     | 有       | 需要知识检索  | Analyzer (similarity_search/rerank)
  纯 LLM Agent  | 无       | 格式化/转换   | Writer + Quality ← 这两个！

  纯 LLM Agent 的特征：
    — 输入: 结构化数据（分析结果 dict）
    — 输出: 格式化文本（Markdown 报告）
    — 不需要任何外部工具（无 web_search、无 RAG、无 DB 写入）
    — 唯一的依赖是 LLM 的文本生成能力

【L5 决策】为什么 Writer 不调工具？
────────────────────────────────
Writer 的输入是 Analyzer 已经分析好的结构化结果。
它的工作是把这些结果"翻译"成排版整洁的 Markdown。
不需要上网查新数据，不需要做检索——那是 Collector 和 Analyzer 的事。

如果 Writer 调了工具，反而是架构bug——表示前面的 Agent 完成度不够。

【L5 决策】Quality→Writer 的改写循环
──────────────────────────────────
报告流程: Collector → Analyzer → Writer → Quality
                                         │
                                    passed? ──是──→ 写入 reports 表 ✓
                                         │
                                        否
                                         │
                                    rewrite_suggestions
                                         │
                                         ▼
                                    Writer（再次调用）
                                    task["rewrite_suggestions"] 存在时
                                    同时传入分析结果 + 修改建议

这是 Supervisor 层的编排逻辑，Writer 自己只需处理 rewrite_suggestions
这个可选字段即可。
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from src.db.connection import create_pool
from src.db.dao import AgentLogDAO

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# Prompt 设计
# ═════════════════════════════════════════════════════════════════════════════

"""
    Prompt 统一写在代码里（单一事实来源）
        └─ _WRITER_PROMPT 带 %s 占位，运行时拼入 title/竞品/维度/分析结果
        └─ LLM 真正读到的就是这段文字
        └─ 不再维护独立的 prompts/*.md 文件，避免代码与文档两处不一致
"""


_WRITER_PROMPT = """你是一个技术报告撰写专家。根据给定的竞品分析结果，生成一份专业的 Markdown 竞品分析报告。

任务: %s
竞品: %s
分析维度: %s

分析结果:
%s

%s

报告结构要求：
# 报告标题
## 概述 (1-2段)
## 竞品对比总览 (表格，行=维度，列=竞品)
## 逐维度深度分析
## 关键发现 (3-5条)
## 风险与建议

规则：
- 只使用给定的分析结果，不添加未经验证的信息
- 数据缺失标注 [数据不足]
- 保持客观中立，禁用情绪化语言
- 表格至少包含维度列和所有竞品列
- 每个维度的分析包含来源引用

【数据不足/待验证的呈现规则】
- 某竞品在某维度的分析结果仅为 [数据不足] 或 [待验证] 时，**不要**展开写整段分析，更不要逐条罗列缺失的具体子项（如"地图结构[数据不足]、单局时长[数据不足]、英雄分类[数据不足]..."）。
- 正确做法：在总览表格该单元格标注 [数据不足]/[待验证] 即可；逐维度深度分析中，用一句话合并说明（如"该竞品此维度资料不足，暂不展开对比"），不再展开。
- 仅当"数据不足"本身是重要结论时（如某竞品整体信息严重缺失、需警示读者），才在「关键发现」或「风险」里简要说明，正文不再重复堆砌。
- 优先写"有什么"，不写"缺什么"；缺什么只在总览表和风险段汇总一次。
"""


async def writer_agent(
    task: dict,
    mcp_server: MCPServer,
    llm: ChatDeepSeek,
) -> dict:
    """Writer Agent — 生成 Markdown 竞品分析报告。

    【L5 架构】纯 LLM Agent（无工具调用）
    输入结构化分析结果 dict → 输出格式化 Markdown 文本。
    不调用 MCP 工具，不读写数据库（除日志外）。

    【L4 工程】支持 rewrite 回退
    如果 task 包含 rewrite_suggestions（来自 Quality 的不通过反馈），
    会将修改建议一起传给 LLM，让报告在重写时针对性地修正问题。

    Args:
        task: {
            id, title, competitors: [str], dimensions: [str],
            analysis_results: {dimension: {competitor: "结论"}},
            rewrite_suggestions: [str] | None  ← Quality 的改写建议
        }
        mcp_server: MCP 工具服务器（仅获取 settings）
        llm: ChatDeepSeek 客户端（temperature=0.3）

    Returns:
        {report_markdown: str}
    """
    settings = mcp_server.settings
    task_id = task["id"]
    title = task["title"]
    competitors = task["competitors"]
    dimensions = task["dimensions"]
    analysis_results = task.get("analysis_results", {})
    rewrite_suggestions = task.get("rewrite_suggestions")

    pool = await create_pool(settings)
    log_dao = AgentLogDAO(pool)

    t0 = time.perf_counter()

    # ── ✏️ 输入日志 ──
    analysis_snippet = {}
    for dim_name, comp_data in analysis_results.items():
        analysis_snippet[dim_name] = {
            k: f"{len(str(v))}字符" for k, v in comp_data.items()
        }
    logger.info(
        "【Writer】开始 task=%s 标题=%r 竞品=%s 维度=%s rewrite=%s 分析数据=%s",
        task_id, title,
        ",".join(competitors), ",".join(dimensions),
        bool(rewrite_suggestions), analysis_snippet,
    )

    # 格式化分析结果为 JSON 字符串
    # 【L4 工程】ensure_ascii=False + indent=2
    # 让中文原样输出（不入为 \\u-escape），带缩进方便 LLM 理解结构
    results_str = json.dumps(analysis_results, ensure_ascii=False, indent=2)

    # 改写建议（来自 Quality Agent 的不通过反馈）
    # 【L4 工程】把改写建议注入 prompt 的 %s 占位
    # 这样初写报告和改写报告用的是同一套 prompt，避免了维护两套模板
    suggestions_str = ""
    if rewrite_suggestions:
        # ── 构建完整的改写指引 ──
        # 【2026-07-27 修复】之前的 prompt 只有 rewrite_suggestions
        # （LLM 随口说的"补引用""精简概述"），Writer 不知道具体哪里扣了分。
        # 现在把 Quality 的维度级评分+评语一起传给 Writer，
        # 让它知道：完整性 42 分（漏了维度X）、可追溯性 30 分（3处缺source_url）。
        prev_quality = task.get("previous_quality", {})
        prev_score = prev_quality.get("overall_score", 0)
        prev_dims = prev_quality.get("dimensions", {})

        lines = []
        lines.append("## ⚠️ 上一版质量评分: %.0f/100（不通过，阈值 70）" % prev_score)
        lines.append("")
        lines.append("### 各维度评分明细（请针对性修正）：")
        for dim_name in ("完整性", "准确性", "可追溯性", "可读性", "客观性"):
            info = prev_dims.get(dim_name, {})
            score = info.get("score", "?")
            comment = info.get("comment", "")
            lines.append(f"- {dim_name}: {score}/100 — {comment}")
        lines.append("")
        lines.append("### 修正要求：")
        for s in rewrite_suggestions:
            lines.append(f"- {s}")
        lines.append("")
        lines.append("请针对以上每个维度的问题逐一修正，确保修正后 overall_score >= 70。")

        suggestions_str = "\n".join(lines)

    prompt = _WRITER_PROMPT % (
        title,
        "、".join(competitors),
        "、".join(dimensions),
        results_str,
        suggestions_str,
    )

    resp = await llm.ainvoke(prompt)
    report = resp.content.strip()

    duration_ms = (time.perf_counter() - t0) * 1000
    await log_dao.log(
        task_id=task_id,
        agent_name="writer",
        action="generate_report",
        request={
            "title": title,
            "rewrite": bool(rewrite_suggestions),
        },
        response={
            "report_length": len(report),
            "rewrite": bool(rewrite_suggestions),
        },
        duration_ms=round(duration_ms, 1),
    )

    # ── ✏️ 输出日志 ──
    logger.info(
        "【Writer】完成 task=%s report=%d字符 耗时%.0fms%s",
        task_id,
        len(report),
        duration_ms,
        " (改写)" if rewrite_suggestions else "",
    )
    return {"report_markdown": report}
