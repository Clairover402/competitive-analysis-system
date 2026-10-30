"""意图识别评估 —— 一个目录装齐「题库 + 跑分 + 金标准 + 验收」。

为什么单独成包
──────────────
原先四个文件平铺在 `src/evaluation/` 下，与 ragas / trajectory / memory_eval
混放；而它们只服务一件事：判断"这句话属于哪一类意图"。产物也散在两处
（`golden/` 放数据集与趋势、`evaluation_runs/` 放每轮明细），跑完一轮要翻两个
目录才拼得出全貌。

包结构
──────
    cases.py    149 条评估集（其中 48 条 unknown，含 30 条"拒绝类"负样本），是**靶子**
    runner.py   跑分：L2 → L3 → L4 全链路，产出 data/runs/intent_eval_*.json
    golden.py   把一轮 run 导出成 golden jsonl，并刷新 data/trend.json
    holdout.py  24 句验收集，改 prompt 时**不许打开**

    data/               入库的产物：golden_v1.jsonl / trend.json / runs/*
    data/runs/          每轮明细（intent_eval_*.json）与带 response 的快照（intent_run_*.jsonl）

四条命令
────────
    python -m src.evaluation.intent.runner             # 全量跑分
    python -m src.evaluation.intent.runner --no-l3     # 只看 L2 接管率与下沉量
    python -m src.evaluation.intent.golden             # 刷数据集 + 打趋势
    python -m src.evaluation.intent.holdout            # 验收集（走完整漏斗）

⚠️ `cases.py` 与 `holdout.py` 不许合流：并进同一批之后，下一轮改 prompt 就是
   自己考自己（理由见 holdout.py 文件头）。

⚠️ 本 `__init__` 刻意不 import 子模块：`golden` 会拉 ragas、`holdout` 会拉
   `supervisor.intent`，一 import 就把两个重依赖带上，且容易循环。
"""
