# -*- coding: utf-8 -*-
"""评估体系：上线前的回归尺子，不是生产运行时的一环。

【职责】
  做   发版前显式跑 Golden 回归，回答「这次改动让分数变好还是变坏」。
  不做 自动跟着生产任务跑、判门禁、上报 Prometheus——评估是离线工具。

【模块结构（四个文件）】
  golden_dataset.py  10 条 Golden Case + 分维度 / 分 cell 标准答案
  llm_factory.py     judge / embeddings / RunConfig 构造 + reference 三级解析
  ragas_core.py      四指标唯一实现 + CLI（跑分、出分数表、默认留痕）
  run_archive.py     run 归档 / compare / trend（纯读 JSON，零 LLM 成本）

  ⚠️ 刻意不做 re-export：直接 `from src.evaluation.ragas_core import ...`。
     旧版 `__init__.py` 把符号全导出一遍，导致任何 import 都会把 ragas /
     langchain 全链路（含 DB、Prometheus）拖进来。评估是离线工具，
     不配当「包级门面」。

【为什么不能自动跑（三条，都是口径问题）】
  1. 语义错位：RAGAS 核心是拿标准答案对拍；真实任务没有标准答案，打出来的分
     无参照系，只在**版本回归**（和上一版比）里才有意义。
  2. 成本错配：每个生产任务都要额外调 LLM 做裁判，换来的是没人看的数字。
  3. 观测误导：生产自动分与发版前 Golden 分是两种口径，画在同一条曲线上会误读。
"""