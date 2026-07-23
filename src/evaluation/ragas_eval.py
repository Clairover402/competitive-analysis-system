# -*- coding: utf-8 -*-
"""RAGAS 评估 — 四维 RAG 质量指标。

═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — RAGAS 评估定位】
═══════════════════════════════════════════════════════════════════════════════

  RAGAS 评估的是"检索增强生成"质量，回答三个核心问题：
    1. 检索到的上下文和问题相关吗？（context_relevancy）
    2. 生成的回答忠于检索到的上下文吗？（faithfulness）
    3. 回答真的回答了问题吗？（answer_relevancy）
    4. 检索精不精准？（context_precision）

  为什么在竞品分析系统里用 RAGAS？
  — Collector 搜到的资料 → 是"检索增强"的上下文
  — Writer 生成的报告 → 是"生成"的结果
  — 需要验证"搜到的东西准确"且"生成的东西忠于搜到的内容"

  ⚠️ RAGAS 依赖 LLM 做评估（需要 API Key），测试环境建议用 mock 替换
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.dao import TaskDAO  # noqa: F401


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class RagasMetrics:
    """RAGAS 四维指标 + 通过判断。

    threshold_overrides: 允许单个维度设置不同门槛（如 easy 用例放宽 faithfulness）
    """
    context_relevancy: float = 0.0
    faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    context_precision: float = 0.0
    overall_score: float = 0.0
    passes: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的 dict。"""
        return {
            "context_relevancy": round(self.context_relevancy, 3),
            "faithfulness": round(self.faithfulness, 3),
            "answer_relevancy": round(self.answer_relevancy, 3),
            "context_precision": round(self.context_precision, 3),
            "overall_score": round(self.overall_score, 3),
            "passes": self.passes,
        }


# ═════════════════════════════════════════════════════════════════════════════
# 默认阈值（来自 DEVELOPMENT_PLAN.md Phase 10 门禁标准）
# ═════════════════════════════════════════════════════════════════════════════

DEFAULT_THRESHOLDS = {
    "context_relevancy": 0.75,
    "faithfulness": 0.80,
    "answer_relevancy": 0.70,
    "context_precision": 0.70,
}

# 门禁档位
GATE_PASS = "PASS"     # overall >= 0.85, 所有 RAGAS 指标 >= threshold
GATE_WARN = "WARN"     # overall >= 0.70
GATE_BLOCK = "BLOCK"   # overall < 0.70


# ═════════════════════════════════════════════════════════════════════════════
# 核心评估函数
# ═════════════════════════════════════════════════════════════════════════════

def compute_ragas_metrics(
    question: str,
    answer: str,
    contexts: list[str],
    thresholds: dict | None = None,
) -> RagasMetrics:
    """计算 RAGAS 四维指标。

    当前实现使用启发式算法（不依赖 ragas 库的 LLM 评估）：
    — 生产环境中可替换为 ragas.evaluate() 调用真实 LLM
    — 研发阶段用启发式算法快速验证流程，不消耗 API Token

    Args:
        question: 用户问题/任务标题
        answer: 生成的报告
        contexts: 检索到的上下文（Collector 采集的资料）
        thresholds: 可选自定义阈值，默认 DEFAULT_THRESHOLDS

    Returns:
        RagasMetrics 对象，含四维指标 + 门禁判断
    """
    thr = thresholds or DEFAULT_THRESHOLDS

    # ── 中文分词辅助函数 ──
    # split() 对中文无用（无空格分隔），用 2-3 字滑动窗口提取特征
    def _char_bigrams(text: str) -> set[str]:
        """提取 2-3 字 bigram/trigram，兼容中英文混合。"""
        t = text.lower().strip()
        # 英文词 + 中文 2-gram + 中文 3-gram
        words = set(t.replace(" ", "").split())  # 英文词
        chars = "".join(t.split())  # 去空格
        for i in range(len(chars) - 1):
            words.add(chars[i:i+2])
            if i + 2 < len(chars):
                words.add(chars[i:i+3])
        return words

    # ── context_relevancy ──
    # 有多少比例的 context 和 question 相关
    # 启发式：context 中非空（中文放宽到 >= 10 字符即有效）
    cr = len([c for c in contexts if c and len(c) >= 10]) / max(len(contexts), 1)

    # ── faithfulness ──
    # answer 中有多少内容能在 contexts 中找到依据
    # 启发式：answer 和 contexts 的特征 n-gram 重叠率
    if contexts and answer:
        ctx_text = " ".join(contexts).lower()
        ctx_features = _char_bigrams(ctx_text)
        ans_features = _char_bigrams(answer)
        if ans_features:
            hit = len(ans_features & ctx_features)
            ft = min(1.0, hit / len(ans_features) * 2.0)  # 放大系数，n-gram 天然重叠低
        else:
            ft = 0.0
    else:
        ft = 0.0

    # ── answer_relevancy ──
    # answer 是否真的回答了 question
    # 启发式：question 的特征 n-gram 在 answer 中出现的比例
    q_features = _char_bigrams(question)
    ans_text = answer.lower()
    if q_features:
        ar = sum(1 for f in q_features if f in ans_text) / len(q_features)
    else:
        ar = 0.0

    # ── context_precision ──
    # 检索的上下文中有多少是真正相关的
    # 启发式：context 中包含 question 特征 n-gram 的比例
    if contexts:
        cp = sum(
            1 for c in contexts
            if any(f in c.lower() for f in q_features)
        ) / len(contexts)
    else:
        cp = 0.0

    # ── overall ──
    overall = (cr * 0.25 + ft * 0.35 + ar * 0.25 + cp * 0.15)

    # ── 门禁判断 ──
    passes = {
        "context_relevancy": cr >= thr["context_relevancy"],
        "faithfulness": ft >= thr["faithfulness"],
        "answer_relevancy": ar >= thr["answer_relevancy"],
        "context_precision": cp >= thr["context_precision"],
    }

    return RagasMetrics(
        context_relevancy=cr,
        faithfulness=ft,
        answer_relevancy=ar,
        context_precision=cp,
        overall_score=overall,
        passes=passes,
    )


def gate_decision(metrics: RagasMetrics) -> str:
    """根据 overall_score 和四维通过状态返回门禁档位。

    决策逻辑（DEVELOPMENT_PLAN.md Phase 10 门禁标准）：
    — PASS: overall >= 0.85 AND 所有 RAGAS 指标 >= threshold
    — WARN: overall >= 0.70（但未达 PASS）
    — BLOCK: overall < 0.70
    """
    if metrics.overall_score >= 0.85 and all(metrics.passes.values()):
        return GATE_PASS
    elif metrics.overall_score >= 0.70:
        return GATE_WARN
    else:
        return GATE_BLOCK


async def evaluate_task(task_id: str, dao: "TaskDAO") -> dict | None:
    """对已完成任务做 RAGAS 评估。

    Args:
        task_id: 要评估的任务 ID
        dao: TaskDAO 实例（用于读取任务和报告数据）

    Returns:
        {
            "task_id": str,
            "ragas": {...},      # RagasMetrics.to_dict()
            "gate": "PASS|WARN|BLOCK"
        }
        如果任务无报告或状态不是 completed，返回 None
    """
    # 1. 获取任务数据
    task = await dao.get(task_id)
    if task is None or task.get("status") != "completed":
        return None

    # 2. 获取报告内容
    from src.db.dao import ReportDAO  # noqa: F811
    report_dao = ReportDAO(dao._pool)
    reports = await report_dao.get_by_task(task_id)
    if not reports:
        return None

    # 3. 拼接上下文（从 evidence_map 中取引用内容）
    answer = "\n\n".join(r["content"] for r in reports if r.get("content"))
    evidence = await report_dao.get_evidence(task_id) if hasattr(report_dao, "get_evidence") else []
    contexts = [e.get("snippet", "") for e in evidence] if evidence else [answer[:500]]

    # 4. 计算 RAGAS 指标
    question = task.get("title", "")
    metrics = compute_ragas_metrics(question, answer, contexts)
    gate = gate_decision(metrics)

    return {
        "task_id": task_id,
        "ragas": metrics.to_dict(),
        "gate": gate,
    }


# ═════════════════════════════════════════════════════════════════════════════
# 批量评估
# ═════════════════════════════════════════════════════════════════════════════

async def evaluate_batch(task_ids: list[str], dao: "TaskDAO") -> list[dict]:
    """批量 RAGAS 评估多个任务。

    Args:
        task_ids: 任务 ID 列表
        dao: TaskDAO 实例

    Returns:
        评估结果列表（已完成+有报告的任务）
    """
    results = await asyncio.gather(
        *(evaluate_task(tid, dao) for tid in task_ids),
        return_exceptions=True,
    )
    return [r for r in results if r is not None and not isinstance(r, Exception)]
