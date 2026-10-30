"""长期记忆评估：8 条「取代组」用例，真库 + 真 embedding 实跑。

测什么
──────
只测一件事：同一 subject 的新观测该不该把旧的顶掉。拆两个方向报 ——
该取代的（召回）、不该取代的（精确率 / 误合并）。

不测什么（2026-10-30 起）
────────────────────────
不再测「事实保鲜期过了没」：verify 意图下线后，那个读取口不存在了。
留着用例等于给死代码写测试。⚠️ 因此本评估**不回答**「按竞品查事实会不会串」。

产物（`evaluation_reports/memory/`，不入库）
    SUMMARY.md        常驻总览（最新一轮 + 历史）
    runs/<ts>/        run.json（机器）+ summary.md（人读）

命令
    python -m src.evaluation.memory.runner              # 真跑（会写测试数据再删）
    python -m src.evaluation.memory.runner --sweep      # 顺带扫 0.85/0.90/0.95 三档阈值
    python -m src.evaluation.memory.runner --keep       # 保留测试数据，自行清理

⚠️ embedding 失败直接报错退出，**不静默降级**：没有向量时一条候选都找不到，
   「取代判定 100%」会变成假的。
"""
