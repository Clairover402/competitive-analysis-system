# -*- coding: utf-8 -*-
"""评估体系：上线前的回归尺子，不是生产运行时的一环。

【职责】
  做   显式跑一次评估，回答「现在什么水平 / 比上一版好了还是坏了」。
  不做 自动跟着生产任务跑、判门禁、上报 Prometheus——评估是离线工具。

【模块结构：四类评估各占一个目录，产物各有一个根】
  common/     共用件：产物路径（paths.py）+ markdown 排版（report.py）
  intent/     意图识别：149 条题库 / 验收集 / 总览          → 准确率与分层接管率
  ragas/      RAGAS 四指标：Golden 对拍 + 归档 + A/B 对比    → cp / cr / faith / ar
  memory/     长期记忆：8 条取代组，真库实跑                 → 取代召回 / 误合并
  trajectory/ 轨迹：把一次任务的 agent_logs 重建成报告        → 编排级 / 工具级指标

  产物统一落在仓库根的 `evaluation_reports/<类名>/`，**整目录不入库**：
    SUMMARY.md          常驻总览（人读，每次跑完自动重建）
    runs/<run_id>/      run.json（机器）+ summary.md（该轮人读）
  唯一的例外是 trajectory —— 它评估的是「某一个任务」，不随版本累积，
  所以没有常驻总览（理由见其 __init__）。

  ⚠️ 刻意不做 re-export：直接 `from src.evaluation.ragas.core import ...`。
     旧版 `__init__.py` 把符号全导出一遍，导致任何 import 都会把 ragas /
     langchain 全链路（含 DB、Prometheus）拖进来。评估是离线工具，
     不配当「包级门面」。

【为什么不能自动跑（三条，都是口径问题）】
  1. 语义错位：RAGAS 核心是拿标准答案对拍；真实任务没有标准答案，打出来的分
     无参照系，只在**版本回归**（和上一版比）里才有意义。
  2. 成本错配：每个生产任务都要额外调 LLM 做裁判，换来的是没人看的数字。
  3. 观测误导：生产自动分与发版前 Golden 分是两种口径，画在同一条曲线上会误读。
"""
