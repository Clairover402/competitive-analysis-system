# -*- coding: utf-8 -*-
"""LLM-as-Judge 离轨评估 — 五维质量评分。

═══════════════════════════════════════════════════════════════════════════════
                    【L5 面试 — LLM-as-Judge 设计原理】
═══════════════════════════════════════════════════════════════════════════════

  为什么需要 LLM-as-Judge，而不是纯规则评估？
  — RAGAS 衡量"检索→生成"链路质量，但不衡量"报告写得好不好"
  — Golden Dataset 的 expected 字段只能做关键词/覆盖率断言
  — 五维评分（完整性/准确性/可追溯性/可读性/客观性）需要语义理解

  模型隔离原则（核心考点）：
  — 评估模型 ≠ 生成模型，否则"自己评自己"有偏
  — 评估用 deepseek-chat（通用能力强），生成用 deepseek-v4-flash（快+便宜）
  — 如果评估也用 deepseek-v4-flash → "裁判和运动员同一个人"面试扣分

  五维评分公式：
  final_score = 完整性×30 + 准确性×30 + 可追溯性×20 + 可读性×10 + 客观性×10

  为什么权重这么分？
  — 完整性+准确性各 30 = 占 60%，报告不准不全什么都不是
  — 可追溯性 20 = 竞品分析报告的核心价值在于"有据可查"
  — 可读性+客观性各 10 = 锦上添花，不是核心
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.dao import TaskDAO  # noqa: F401


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class JudgeScores:
    """LLM-as-Judge 五维评分结果。"""
    完整性: int = 0     # 0-100，是否覆盖所有竞品+维度
    准确性: int = 0     # 0-100，数据来源是否可靠
    可追溯性: int = 0   # 0-100，是否有引用来源（evidence_map）
    可读性: int = 0     # 0-100，报告是否清晰易读
    客观性: int = 0     # 0-100，是否客观不偏袒

    @property
    def final_score(self) -> float:
        """加权总分。"""
        return (
            self.完整性 * 0.30
            + self.准确性 * 0.30
            + self.可追溯性 * 0.20
            + self.可读性 * 0.10
            + self.客观性 * 0.10
        )

    def to_dict(self) -> dict:
        """转为 dict（含 final_score）。"""
        return {
            "完整性": self.完整性,
            "准确性": self.准确性,
            "可追溯性": self.可追溯性,
            "可读性": self.可读性,
            "客观性": self.客观性,
            "final_score": round(self.final_score, 1),
        }

    def all_above(self, threshold: int) -> bool:
        """所有维度 >= threshold？"""
        return all(
            s >= threshold
            for s in [self.完整性, self.准确性, self.可追溯性, self.可读性, self.客观性]
        )


# ═════════════════════════════════════════════════════════════════════════════
# LLM-as-Judge 提示词模板
# ═════════════════════════════════════════════════════════════════════════════

JUDGE_SYSTEM_PROMPT = """你是一个竞品分析质量评估专家。请严格按五维标准评估以下报告。

【五维标准】

1. 完整性（0-100）：报告是否覆盖了所有要求的竞品和维度？
   — 100分：所有竞品+维度都有分析
   — 70分：覆盖了大部分，缺1-2个
   — 40分：缺失严重
   — 0分：几乎没覆盖

2. 准确性（0-100）：数据是否可靠？是否有事实错误？
   — 100分：数据均有来源，无误
   — 70分：大部分准确，个别可疑
   — 40分：多处不准确
   — 0分：胡编乱造

3. 可追溯性（0-100）：报告中是否有引用来源或证据？
   — 100分：每条关键结论都有引用
   — 70分：大部分有引用
   — 40分：少量引用
   — 0分：无引用

4. 可读性（0-100）：报告是否清晰易懂？
   — 100分：结构清晰，段落分明
   — 70分：基本可读
   — 40分：混乱
   — 0分：完全不可读

5. 客观性（0-100）：是否客观无偏见？
   — 100分：完全客观
   — 70分：基本客观，轻微偏向
   — 40分：明显偏向某个竞品
   — 0分：纯主观

【输出格式】
只输出一个 JSON 对象，别无其他文字：
{"完整性": 75, "准确性": 80, "可追溯性": 60, "可读性": 85, "客观性": 70, "摘要": "一句话总结"}
"""


def build_judge_prompt(
    title: str,
    competitors: list[str],
    dimensions: list[str],
    report: str,
) -> str:
    """构建 LLM-as-Judge 的 user prompt。

    Args:
        title: 任务标题
        competitors: 期望覆盖的竞品列表
        dimensions: 期望覆盖的维度列表
        report: 生成的报告正文

    Returns:
        完整的 user prompt
    """
    return f"""【任务标题】{title}

【要求覆盖的竞品】{', '.join(competitors) if competitors else '探索模式（无预设竞品）'}

【要求覆盖的维度】{', '.join(dimensions) if dimensions else '待探索'}

【报告正文】
{report[:5000]}

请按五维标准评分，只输出 JSON。"""


# ═════════════════════════════════════════════════════════════════════════════
# 评估函数
# ═════════════════════════════════════════════════════════════════════════════

async def evaluate_with_judge(
    task_id: str,
    dao: "TaskDAO",
    judge_model: str = "deepseek-chat",
) -> dict | None:
    """用 LLM-as-Judge 评估已完成任务的报告质量。

    核心考点：模型隔离——评估用 deepseek-chat，与生成用的 deepseek-v4-flash 不同。

    Args:
        task_id: 任务 ID
        dao: TaskDAO 实例
        judge_model: 评估模型名（默认 deepseek-chat，与生成模型隔离）

    Returns:
        {
            "task_id": str,
            "judge_scores": {...},   # JudgeScores.to_dict()
            "judge_model": str,
            "passed": bool           # 所有维度 >= 60
        }
        任务无报告则返回 None
    """
    from src.db.dao import ReportDAO  # noqa: F811
    from src.config import Settings

    # 1. 获取任务数据
    task = await dao.get(task_id)
    if task is None or task.get("status") != "completed":
        return None

    # 2. 获取报告
    report_dao = ReportDAO(dao._pool)
    reports = await report_dao.get_by_task(task_id)
    if not reports:
        return None
    report_text = "\n\n".join(r.get("content", "") for r in reports)

    # 3. 调用 LLM 评估（模型隔离：judge_model != deepseek_model）
    settings = Settings()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
        response = await client.chat.completions.create(
            model=judge_model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": build_judge_prompt(
                    title=task.get("title", ""),
                    competitors=task.get("competitors", []),
                    dimensions=task.get("dimensions", []),
                    report=report_text,
                )},
            ],
            temperature=0.0,  # 评估任务，零温度保证一致性
            max_tokens=500,
        )
        content = response.choices[0].message.content or "{}"
    except Exception:
        # 无法调用 LLM 时返回启发式评分
        content = _heuristic_judge(task, report_text)

    # 4. 解析评分
    scores = _parse_judge_response(content)

    # 5. 门禁：所有维度 >= 60
    passed = scores.all_above(60)

    return {
        "task_id": task_id,
        "judge_scores": scores.to_dict(),
        "judge_model": judge_model,
        "passed": passed,
    }


def _parse_judge_response(content: str) -> JudgeScores:
    """从 LLM 响应中解析出五维评分。

    容错策略：
    — 尝试 JSON 解析
    — 失败则尝试逐行正则提取 "维度": 数字
    — 完全失败返回全 0
    """
    import re

    # 先清理（去掉 markdown 代码块包裹）
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```\w*\n?", "", content)
        content = re.sub(r"\n```$", "", content)

    try:
        obj = json.loads(content)
        return JudgeScores(
            完整性=int(obj.get("完整性", 0)),
            准确性=int(obj.get("准确性", 0)),
            可追溯性=int(obj.get("可追溯性", 0)),
            可读性=int(obj.get("可读性", 0)),
            客观性=int(obj.get("客观性", 0)),
        )
    except (json.JSONDecodeError, ValueError):
        pass

    # 兜底：正则逐字段提取
    try:
        fields = {"完整性": 0, "准确性": 0, "可追溯性": 0, "可读性": 0, "客观性": 0}
        for name in fields:
            m = re.search(rf'"{name}"\s*:\s*(\d+)', content)
            if m:
                fields[name] = int(m.group(1))
        return JudgeScores(**fields)
    except Exception:
        return JudgeScores()


def _heuristic_judge(task: dict, report_text: str) -> str:
    """无 LLM 时的启发式评分兜底。

    基于规则快速打分——不准确但可用，保证评估管线不因为 API 异常而中断。
    """
    title = task.get("title", "")
    competitors = task.get("competitors", [])
    dimensions = task.get("dimensions", [])

    # 完整性：report 中有多少竞品和维度被提及
    comp_hit = sum(1 for c in competitors if c in report_text)
    dim_hit = sum(1 for d in dimensions if d in report_text)
    comp_rate = comp_hit / max(len(competitors), 1)
    dim_rate = dim_hit / max(len(dimensions), 1)
    完整性 = int((comp_rate * 0.5 + dim_rate * 0.5) * 100)

    # 准确性：看有没有引用标记 [来源] 或 URL
    import re
    sources = len(re.findall(r'\[来源\d*\]|https?://', report_text))
    准确性 = min(100, sources * 20 + 40)

    # 可追溯性：来源越多越高
    可追溯性 = min(100, sources * 15 + 30)

    # 可读性：报告长度和分段
    paragraphs = report_text.count("\n\n") + 1
    可读性 = min(100, 50 + paragraphs * 5) if len(report_text) > 200 else 30

    # 客观性：检查是否有偏袒词
    bias_words = ["毫无疑问", "绝对", "完全碾压", "完胜", "吊打"]
    bias_count = sum(1 for bw in bias_words if bw in report_text)
    客观性 = max(20, 80 - bias_count * 10)

    import json
    result = {
        "完整性": 完整性,
        "准确性": 准确性,
        "可追溯性": 可追溯性,
        "可读性": 可读性,
        "客观性": 客观性,
        "摘要": f"启发式兜底评估（LLM API 不可用）",
    }
    return json.dumps(result, ensure_ascii=False)


import json  # noqa: E402（模块级导入放在文件头，此处为兼容性重导）
