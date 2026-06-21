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
    prompts/writer.md           →  给人看的"需求文档"
        └─ 输入格式、输出格式、规则、正例、反例
        └─ 用来说明"这个 Agent 要干什么"
    
    _WRITER_PROMPT（代码里）    →  喂给 LLM 的"实际 prompt"
        └─ 带 %s 占位，运行时拼入 title/竞品/维度/分析结果
        └─ LLM 真正读到的就是这段文字
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

    # 格式化分析结果为 JSON 字符串
    # 【L4 工程】ensure_ascii=False + indent=2
    # 让中文原样输出（不入为 \\u-escape），带缩进方便 LLM 理解结构
    results_str = json.dumps(analysis_results, ensure_ascii=False, indent=2)

    # 改写建议（来自 Quality Agent 的不通过反馈）
    # 【L4 工程】把改写建议注入 prompt 的 %s 占位
    # 这样初写报告和改写报告用的是同一套 prompt，避免了维护两套模板
    suggestions_str = ""
    if rewrite_suggestions:
        suggestions_str = "改写作要求:\n" + "\n".join(f"- {s}" for s in rewrite_suggestions)

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
            "rewrite": bool(rewrite_suggestions),  # 标记是否为改写
        },
        response={"report_length": len(report)},
        duration_ms=round(duration_ms, 1),
    )

    logger.info(
        "Writer done: %d chars in %.0fms%s",
        len(report),
        duration_ms,
        " (rewrite)" if rewrite_suggestions else "",
    )
    return {"report_markdown": report}
