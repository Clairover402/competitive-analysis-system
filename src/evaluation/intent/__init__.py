"""意图识别评估 —— 题库 / 跑分 / 金标准 / 验收集 / 总览，一个包装齐。

为什么单独成包
──────────────
它们只服务一件事：判断「这句话属于哪一类意图」。2026-10-30 起与 ragas /
memory / trajectory 平级：四类评估各占一个目录，产物各有一个根。

包结构
──────
    cases.py     149 条评估集（48 条 unknown，含 30 条「拒绝类」负样本），是**靶子**
    runner.py    跑分：L2 → L3 → L4 全链路，落 runs/<ts>/（run.json + summary.md）
    golden.py    把一轮 run 导出成 golden jsonl，并刷新 trend.json
    holdout.py   24 句验收集，改 prompt 时**不许打开**
    overview.py  把人读总览渲染成 SUMMARY.md

产物（`evaluation_reports/intent/`，**全部不入库**）
    SUMMARY.md               常驻总览：最新一轮 + 口径 + 历史趋势
    trend.json               指标序列（机器读，可从 runs 重建）
    golden_v1.jsonl          数据集快照（源在 cases.py，可随时重建）
    runs/<ts>/run.json       每轮明细
    runs/<ts>/summary.md     每轮人读报告
    runs/<ts>/responses.jsonl  带 response 的快照（只有指定 --run 导出时才有）

四条命令
────────
    python -m src.evaluation.intent.runner             # 全量跑分（自动刷 SUMMARY.md）
    python -m src.evaluation.intent.runner --no-l3     # 只看 L2 接管率与下沉量
    python -m src.evaluation.intent.golden             # 刷数据集 + 打趋势
    python -m src.evaluation.intent.holdout            # 验收集（走完整漏斗）

⚠️ `cases.py` 与 `holdout.py` 不许合流：并进同一批之后，下一轮改 prompt 就是
   自己考自己（理由见 holdout.py 文件头）。

⚠️ 本 `__init__` 刻意不 import 子模块：`golden` 会拉 ragas、`runner` 会拉
   `supervisor.intent`，一 import 就把重依赖全带上。
"""
