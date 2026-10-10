# -*- coding: utf-8 -*-
"""评估编排器 — 串起三类评估 + 落库，支撑趋势/版本 diff/回归判定。

═══════════════════════════════════════════════════════════════════════════════
                    【L4 工程 — 评估 Runner 定位】
═══════════════════════════════════════════════════════════════════════════════

  评估函数（evaluate_task_with_ragas / evaluate_retrieval / evaluate_with_judge）
  各自独立、已内置落库逻辑。本模块的价值在于「编排」：

    1. evaluate_task_full(task_id) — 对单个真实任务，一次性串起三类评估，
       返回结构化汇总（供趋势面板/版本 diff 消费）。
    2. run_batch(task_ids)         — 批量评估多个任务（历史任务回填趋势）。
    3. run_golden_checks()         — Golden 数据完整性自检（reference/domains
       是否齐全），不依赖真实 DB。

  设计原则：
    - 评估是「旁路」——某类评估失败不应中断整体，逐类 try/except 隔离。
    - 检索评估依赖 retrieval_snapshot（老任务可能没有），没有则跳过并标注。
    - Judge 依赖报告正文，没有则跳过。

  为什么独立 CLI 而非塞进 API？
    - 评估是离线/后台任务，不该占用 HTTP 请求生命周期（RAGAS 跑一轮要几十秒）
    - 独立 CLI 便于接入 CI / 定时任务 / 发版前 Golden 回归
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.dao import TaskDAO

logger = logging.getLogger(__name__)


async def evaluate_task_full(
    task_id: str,
    dao: "TaskDAO",
    settings=None,
    *,
    reference: str | None = None,
    reference_domains: list[str] | None = None,
) -> dict:
    """对单个任务串起三类评估，返回结构化汇总。

    Args:
        task_id: 任务 ID
        dao: TaskDAO 实例
        settings: 可选 Settings
        reference: 事实级标准答案（Golden 用，真实任务通常为 None）
        reference_domains: 关键来源域名锚点（Golden 用）

    Returns:
        {
            "task_id": str,
            "generation": {...} | None,   # evaluate_task_with_ragas 结果
            "retrieval": {...} | None,    # evaluate_retrieval 结果
            "judge": {...} | None,        # evaluate_with_judge 结果
            "errors": {阶段: 错误信息},
        }
    """
    from src.evaluation.ragas_llm import evaluate_retrieval, evaluate_task_with_ragas
    from src.evaluation.judge_eval import evaluate_with_judge

    result: dict = {
        "task_id": task_id,
        "generation": None,
        "retrieval": None,
        "judge": None,
        "errors": {},
    }

    # 1. 检索质量（依赖 retrieval_snapshot，老任务可能没有）
    try:
        result["retrieval"] = await evaluate_retrieval(
            task_id, dao, settings,
            reference_map=None,
            reference_domains=reference_domains,
        )
    except Exception as e:
        logger.warning("检索评估失败 task_id=%s: %s", task_id, e)
        result["errors"]["retrieval"] = str(e)

    # 2. 生成质量（依赖 report + evidence，成稿后）
    try:
        result["generation"] = await evaluate_task_with_ragas(
            task_id, dao, settings, reference=reference,
        )
    except Exception as e:
        logger.warning("生成质量评估失败 task_id=%s: %s", task_id, e)
        result["errors"]["generation"] = str(e)

    # 3. Judge 报告质量（依赖 report）
    try:
        result["judge"] = await evaluate_with_judge(task_id, dao)
    except Exception as e:
        logger.warning("Judge 评估失败 task_id=%s: %s", task_id, e)
        result["errors"]["judge"] = str(e)

    return result






async def run_batch(
    task_ids: list[str],
    dao: "TaskDAO",
    settings=None,
) -> list[dict]:
    """批量评估多个任务（历史任务回填趋势）。

    顺序执行（而非并发）——每类评估都要调 LLM，并发会放大 API 限流风险
    和成本，批量评估本就是离线任务，顺序即可。
    """
    results = []
    for tid in task_ids:
        try:
            r = await evaluate_task_full(tid, dao, settings)
            results.append(r)
        except Exception as e:
            logger.error("任务评估整体失败 task_id=%s: %s", tid, e)
            results.append({"task_id": tid, "errors": {"fatal": str(e)}})
    return results


def run_golden_checks() -> dict:
    """Golden Dataset 数据完整性自检（不依赖真实 DB / LLM）。

    校验每条 Golden Case 是否具备事实级标准答案 + 关键来源域名锚点，
    这是 context_precision / recall_hit_rate 能归真的前提。缺失项列出，
    供补齐 Golden 数据用。

    Returns:
        {
            "total": 10,
            "missing_reference": [task_id...],
            "missing_domains": [task_id...],
            "complete": bool,
        }
    """
    from src.evaluation.golden_dataset import GOLDEN_DATASET

    missing_reference = [
        c["task_id"] for c in GOLDEN_DATASET
        if not c.get("reference")
    ]
    missing_domains = [
        c["task_id"] for c in GOLDEN_DATASET
        if not c.get("reference_domains")
    ]
    return {
        "total": len(GOLDEN_DATASET),
        "missing_reference": missing_reference,
        "missing_domains": missing_domains,
        "complete": not missing_reference and not missing_domains,
    }
