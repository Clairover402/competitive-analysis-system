"""Quality Agent — LLM-as-Judge 五维评分。

═══════════════════════════════════════════════════════════════════════════════
                         【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Quality 是竞品分析系统的"质量门禁"。它的角色不是写报告，而是评估报告。

【L3 核心考点】LLM-as-Judge 范式
───────────────────────────────
传统做法：人工审报告 → 打分 → 通过/不通过
LLM-as-Judge：把评分标准写成 prompt → LLM 读报告 → 输出结构化的评分 JSON

优势：
  — 自动化：不需要人工逐篇审核
  — 一致性：同一个 prompt 对同一份报告每次评分基本一致（temperature=0.0）
  — 可追溯：每个维度有 comment + score，可以回溯为什么扣分

局限：
  — LLM 可能对"客观性"评分偏主观
  — 长报告的评分可能不一致（上下文窗口问题）
  — 依赖 prompt 的评分标准是否合理

【L4 工程】为什么权重用代码算而不是 LLM 算？
─────────────────────────────────────────
LLM 不擅长精确算术。prompt 里说"按 (完整性*0.3 + ...) 计算",
但 LLM 经常给出一个大约的分数，而不是精确计算。
所以：
  — LLM 产出每个维度的 score（0-100）
  — 代码执行 weighted_sum = sum(score × weight)
  — 重算 overall_score 覆盖 LLM 的输出值

这就是"取 LLM 的判断力，取代码的计算力"的分工。

【L5 决策】阈值 overall_score >= 70 的设计依据
─────────────────────────────────────────────
不是拍脑袋定的。三个约束：
  ① 五维等权重，每维 60 分算及格 → 5 × 60 = 300 → 平均分 = 60
     70 在 60 以上，有 10 分的质量缓冲区
  ② 太低（<60）：太宽松，低质量报告通过
     太高（>80）：太严格，导致频繁 rewrite → 浪费 LLM token
  ③ 实际经验：一份维度的 source_url 缺失会扣可追溯性（20% 权重），
     如果报告缺了 2-3 个 source_url，可追溯性会掉到 50 左右，
     weighted 后影响约 10 分，70 的阈值正好能抓住这种情况
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from src.db.connection import create_pool
from src.db.dao import ReportDAO, AgentLogDAO

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# Prompt 设计 + 权重定义
# ═════════════════════════════════════════════════════════════════════════════

_QUALITY_PROMPT = """你是报告质量评审专家。对以下竞品分析报告进行五维度打分。

任务: %s
竞品: %s
分析维度: %s

报告:
%s

请严格按以下JSON格式输出评分（不要输出任何其他文字）：
{
  "overall_score": 0,
  "passed": true,
  "dimensions": {
    "完整性": {"score": 0, "comment": "..."},
    "准确性": {"score": 0, "comment": "..."},
    "可追溯性": {"score": 0, "comment": "..."},
    "可读性": {"score": 0, "comment": "..."},
    "客观性": {"score": 0, "comment": "..."}
  },
  "issues": ["问题1", "问题2"],
  "rewrite_suggestions": ["建议1", "建议2"]
}

评分标准：
- 完整性(30%): 所有维度和竞品是否覆盖，是否有漏分析
- 准确性(30%): 数据是否有来源引用，是否无编造信息
- 可追溯性(20%): 结论是否附带 source_url，引用是否可验证
- 可读性(10%): Markdown 结构是否清晰、表格是否完整
- 客观性(10%): 是否无明显倾向性语言、无主观臆断

通过阈值: overall_score >= 70
不通过必须提供至少 2 条 rewrite_suggestions。

overall_score 按权重计算：(完整性*0.3 + 准确性*0.3 + 可追溯性*0.2 + 可读性*0.1 + 客观性*0.1)
"""

# 【L4 工程】权重定义在代码中而非 prompt 中
# 好处: 修改权重不需要调整 prompt（prompt 的内容影响 LLM 的评分逻辑）
_WEIGHTS = {
    "完整性": 0.30,
    "准确性": 0.30,
    "可追溯性": 0.20,
    "可读性": 0.10,
    "客观性": 0.10,
}

# ═════════════════════════════════════════════════════════════════════════════
# Quality Agent 主函数
# ═════════════════════════════════════════════════════════════════════════════

async def quality_agent(
    task: dict,
    mcp_server: MCPServer,
    llm: ChatDeepSeek,
) -> dict:
    """Quality Agent — LLM-as-Judge 五维评分 + 写入 reports 表。

    【L5 架构】纯 LLM Agent（无工具调用），与 Writer 同类。
    不调用 MCP 工具（除日志外）。

    【L4 工程】temperature=0.0
    评分任务需要最大化确定性。即使重复调用，同一份报告应该得到相同分数。
    这是 temperature 最恰当的用法——不是生成多样性文本，而是做判断。

    Args:
        task: {
            id, title, competitors: [str], dimensions: [str],
            report_markdown: str  ← Writer 的输出
        }
        mcp_server: MCP 工具服务器（仅获取 settings）
        llm: ChatDeepSeek 客户端（temperature=0.0，评分日需要确定性）

    Returns:
        {overall_score, passed, dimensions, issues, rewrite_suggestions}
    """
    settings = mcp_server.settings
    task_id = task["id"]
    title = task["title"]
    competitors = task["competitors"]
    dimensions = task["dimensions"]
    report = task.get("report_markdown", "")

    pool = await create_pool(settings)
    report_dao = ReportDAO(pool)
    log_dao = AgentLogDAO(pool)

    t0 = time.perf_counter()

    prompt = _QUALITY_PROMPT % (
        title,
        "、".join(competitors),
        "、".join(dimensions),
        report,
    )

    resp = await llm.ainvoke(prompt)
    text = resp.content.strip()

    # 【L4 工程】LLM 输出清理——比 analyzer 多一种情况处理
    # ```json 开头 → 跳过 "```json" 标记
    # ``` 开头 → 跳过 "```" 标记
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 3:
            text = parts[2].strip()
        else:
            text = parts[1].strip()

    # 解析 JSON（如果 LLM 不按规范输出 → 直接抛异常，由上层 Supervisor 处理）
    parsed = json.loads(text)

    # ═══════ 【L4 工程】代码重算分数 ← 防 LLM 算术错误 ═══════
    # LLM 输出的 overall_score 可能不是真正的加权和。
    # 代码重新算一遍，用代码的值覆盖 LLM 的声明值。
    # 这是评分防作弊的核心手段。
    dim_scores = parsed.get("dimensions", {})
    computed_score = 0.0
    for dim_name, weight in _WEIGHTS.items():
        if dim_name in dim_scores:
            computed_score += dim_scores[dim_name]["score"] * weight

    # 使用代码计算值，LLM 的 overall_score 只作为参考（不信任）
    overall_score = round(computed_score, 1)
    passed = overall_score >= 70

    result = {
        "overall_score": overall_score,
        "passed": passed,
        "dimensions": dim_scores,
        "issues": parsed.get("issues", []),
        "rewrite_suggestions": parsed.get("rewrite_suggestions", []),
    }

    # ─── 写入 reports 表 ───
    # 【L5 决策】为什么在 Quality 写 reports 表而不是 Writer？
    #   ① 职责分离：Writer 负责生成，Quality 负责评估 + 持久化
    #   ② 如果 Writer 不通过 → rewrite → 新报告 → 覆盖旧记录
    #   ③ 评分和报告放在同一条记录里，方便查询 "哪些任务通过了"
    await report_dao.create(
        task_id=task_id,
        content=report,
        quality_score=overall_score,
        quality_details=dim_scores,
    )

    duration_ms = (time.perf_counter() - t0) * 1000
    await log_dao.log(
        task_id=task_id,
        agent_name="quality",
        action="judge_report",
        request={"title": title},
        response={"score": overall_score, "passed": passed},
        duration_ms=round(duration_ms, 1),
    )

    logger.info("Quality done: score=%.0f passed=%s", overall_score, passed)

    """
    quality 输出示例：
    
    {
      "overall_score": 82.5,
      "passed": true,
      "dimensions": {
        "完整性":   {"score": 85, "comment": "5个维度全部覆盖，竞品齐全"},
        "准确性":   {"score": 80, "comment": "数据均有引用来源，飞书定价降幅待验证[待验证]"},
        "可追溯性": {"score": 75, "comment": "大部分结论附source_url，企业微信定价缺引用"},
        "可读性":   {"score": 90, "comment": "Markdown结构清晰，表格完整"},
        "客观性":   {"score": 85, "comment": "无明显倾向性语言"}
      },
      "issues": [
        "企业微信定价缺少source_url",
        "概述章节略长，建议精简到2段"
      ],
      "rewrite_suggestions": []
    }
    
    
    几个关键字段的流向
    overall_score  passed  rewrite_suggestions             Supervisor 怎么用
    ──────────── ─────── ────────────────────            ────────────────────
     82.5          true          []                         通过 ✓ → 任务完成

     58.0          false  ["补source_url", "概述太长"]  不通过 → 把建议塞给 Writer 改写
                                                           → Quality 再评
                                                           → 最多循环 2 次

    """

    return result
