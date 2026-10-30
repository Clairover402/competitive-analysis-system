"""轨迹评估：把一次任务的 agent_logs 重建成「想了什么 → 调了谁 → 结果如何」。

与前三个评估的区别
──────────────────
它**不跑分、不存档、不调 LLM**：输入是线上真实任务的日志，输出是一份即时
Markdown 报告（`--json` 则出结构化 dict），`--persist` 才额外 upsert 一张
`trajectory_metrics` 指标表供 Grafana 读。

所以本包**没有** `SUMMARY.md`、也没有 `runs/` —— 它评估的是「某一个任务跑得
怎么样」，不是一个会随版本累积的基准。硬套常驻总览只会得到一张没有纵轴的图。

命令
    python -m src.evaluation.trajectory.runner --task <uuid>     # 单个任务
    python -m src.evaluation.trajectory.runner --since 7d        # 时间窗批量
    python -m src.evaluation.trajectory.runner --since 24h --persist   # 落指标表
"""
