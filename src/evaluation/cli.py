# -*- coding: utf-8 -*-
"""评估 CLI — 命令行触发评估 + 落库。

用法:
    # 评估单个真实任务（跑满三类：检索 + 生成 + Judge）
    python -m src.evaluation.cli --task <task_id>

    # 批量评估多个任务（历史回填趋势）
    python -m src.evaluation.cli --task <id1> --task <id2>

    # Golden 数据完整性自检（不依赖 DB/LLM）
    python -m src.evaluation.cli --golden-check

    # 查看某任务的历史评估趋势（版本 diff + 回归）
    python -m src.evaluation.cli --trend <task_id> --eval-type generation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _cmd_evaluate(task_ids: list[str]) -> None:
    from src.config import Settings
    from src.db.connection import close_pool, create_pool
    from src.db.dao import TaskDAO
    from src.evaluation.runner import evaluate_task_full

    settings = Settings()
    pool = await create_pool(settings)
    dao = TaskDAO(pool)

    for tid in task_ids:
        print(f"\n=== 评估任务 {tid} ===")
        result = await evaluate_task_full(tid, dao, settings)
        _print_json(result)

    await close_pool()


async def _cmd_trend(task_id: str, eval_type: str | None) -> None:
    from src.config import Settings
    from src.db.connection import close_pool, create_pool
    from src.db.dao import EvalResultDAO

    settings = Settings()
    pool = await create_pool(settings)
    dao = EvalResultDAO(pool)

    results = await dao.get_by_task(task_id, eval_type)
    print(f"\n=== {task_id} 的评估历史（{len(results)} 次）===")
    for r in results:
        print(
            f"[{r['created_at']}] type={r['eval_type']} "
            f"overall={r['overall_score']} gate={r['gate']} "
            f"golden={r['is_golden']}"
        )
        for m in r.get("metrics", []):
            dim = f" ({m['dimension']})" if m.get("dimension") else ""
            print(f"    - {m['metric_name']}{dim}: {m['score']}")

    if eval_type:
        diff = await dao.diff_latest(task_id, eval_type)
        if diff:
            print("\n=== 最近两次版本 diff ===")
            print(f"overall: {diff['previous']['overall_score']} -> {diff['latest']['overall_score']}")
            print("指标 deltas:", diff["deltas"])
            for name in diff["deltas"]:
                reg = await dao.check_regression(task_id, eval_type, metric_name=name)
                if reg and reg["regressed"]:
                    print(f"  ⚠️ {name} 回归：{reg['previous']} -> {reg['latest']} ({reg['delta']})")

    await close_pool()


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description="评估 CLI — 触发评估 + 落库 + 趋势查询")
    parser.add_argument("--task", action="append", default=[], help="要评估的任务 ID（可多次）")
    parser.add_argument("--golden-check", action="store_true", help="Golden 数据完整性自检")
    parser.add_argument("--trend", help="查看某任务评估趋势（传 task_id）")
    parser.add_argument("--eval-type", choices=["generation", "retrieval", "judge"],
                        help="趋势查询时按评估类型过滤")
    args = parser.parse_args()

    if args.golden_check:
        from src.evaluation.runner import run_golden_checks
        _print_json(run_golden_checks())
        return

    if args.trend:
        asyncio.run(_cmd_trend(args.trend, args.eval_type))
        return

    if args.task:
        asyncio.run(_cmd_evaluate(args.task))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
