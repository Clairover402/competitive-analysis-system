"""RAGAS 四指标评估（检索质量 + 答案质量），一次跑 `(竞品@维度)` 一格。

三个文件，依赖单向往下：
────────────────────────
    dataset.py  10 条 Golden Case + 两层 ground truth（cell 级 / 维度级）。纯数据。
    llm.py      judge / embeddings / RunConfig 构造 + reference 解析。纯构造层。
    core.py     四指标唯一实现 + CLI（跑分 → 分数表 → 自动归档）。依赖上面两个。
    archive.py  run 归档 / compare / 总览。**不 import core**（core 在 import 期拉起
                ragas，而归档必须能在无 ragas 环境纯读 JSON）。

产物（`evaluation_reports/ragas/`，不入库）
    SUMMARY.md          常驻总览：最新一轮 + 全版本趋势
    runs/<run_id>/      run.json（manifest + 每格分数）+ summary.md（人读分数表）

命令
    python -m src.evaluation.ragas.core --golden          # 跑全量 Golden 并归档
    python -m src.evaluation.ragas.core --task <uuid>     # 评一个真实任务
    python -m src.evaluation.ragas.archive trend --write  # 重建总览
    python -m src.evaluation.ragas.archive compare A B    # 两轮逐 cell 对比

⚠️ `faithfulness` 是护栏指标，只呈现不参与「变好/变坏」判定。
"""
