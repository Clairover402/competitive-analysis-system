# -*- coding: utf-8 -*-
"""评估体系 — 公开导出。

评估模块负责竞品分析系统的质量门禁：
  — golden_dataset: 10 条 Golden Case（easy/medium/hard 三档）
  — ragas_llm: 真 ragas 库评估（LLM-as-Judge，检索质量 + 生成质量）
  — judge_eval: LLM-as-Judge 五维评分 + 模型隔离
"""

from src.evaluation.golden_dataset import (  # noqa: F401
    GOLDEN_DATASET,
    GoldenCase,
    assert_keywords,
    get_by_difficulty,
    get_pipeline_cases,
    get_reference,
    get_reference_domains,
    get_supervisor_cases,
)
from src.evaluation.judge_eval import (  # noqa: F401
    JudgeScores,
    build_judge_prompt,
    evaluate_with_judge,
)
from src.evaluation.ragas_llm import (  # noqa: F401
    JUDGE_MODEL as RAGAS_JUDGE_MODEL,
    evaluate_retrieval,
    evaluate_task_with_ragas,
)
from src.evaluation.runner import (  # noqa: F401
    evaluate_task_full,
    run_batch,
    run_golden_checks,
)
