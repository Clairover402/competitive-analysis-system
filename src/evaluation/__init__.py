# -*- coding: utf-8 -*-
"""评估体系 — 公开导出。

评估模块负责竞品分析系统的质量门禁：
  — golden_dataset: 10 条 Golden Case（easy/medium/hard 三档）
  — ragas_eval: RAGAS 四维指标 + 门禁决策
  — judge_eval: LLM-as-Judge 五维评分 + 模型隔离
"""

from src.evaluation.golden_dataset import (  # noqa: F401
    GOLDEN_DATASET,
    GoldenCase,
    assert_keywords,
    get_by_difficulty,
    get_pipeline_cases,
    get_supervisor_cases,
)
from src.evaluation.ragas_eval import (  # noqa: F401
    GATE_BLOCK,
    GATE_PASS,
    GATE_WARN,
    DEFAULT_THRESHOLDS,
    RagasMetrics,
    compute_ragas_metrics,
    evaluate_batch,
    evaluate_task,
    gate_decision,
)
from src.evaluation.judge_eval import (  # noqa: F401
    JudgeScores,
    build_judge_prompt,
    evaluate_with_judge,
)
