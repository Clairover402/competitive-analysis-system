# -*- coding: utf-8 -*-
"""Golden 回归脚本 — 把 10 条 Golden Case 跑一遍真实 Pipeline/Supervisor + 三类评估，
产出 baseline 分数表（Markdown + JSON），并落库沉淀趋势。

═══════════════════════════════════════════════════════════════════════════════
                          【L4 工程 — Golden Run 定位】
═══════════════════════════════════════════════════════════════════════════════

  之前评估体系的断点：Golden Dataset（golden_dataset.py）写好了 10 条含
  事实级 reference 的用例，但「没有脚本真正驱动它们走完系统拿分数」——
  test_e2e.py 只测离网纯函数，RAGAS 代码「能 import、能跑单任务」，
  却从未产出过一条 baseline 分数。没有 baseline，就没有「版本 diff 是否改善」
  「回归是否发生」的参照系。

  本脚本补齐这一环，回答三个问题：
    1. 系统在 10 条标准问题上，检索准不准（context_precision / recall_hit_rate）
    2. 生成有没有幻觉（faithfulness / answer_relevancy / context_precision）
    3. 报告写得好不好（Judge 五维）

  关键设计：
    ┌─────────────────────────────────────────────────────────────────────┐
    │ 1. 复用 IntentRouter.route() 跑「真实链路」——                     │
    │    不 mock、不跳步，走与生产完全一致的 Pipeline/Supervisor，      │
    │    让 quality_agent 落 report/evidence、finalize 标记 completed，   │
    │    analyzer 落 retrieval_snapshot。评估吃的都是真实落库数据。       │
    │ 2. Golden 用真实 UUID 落库（golden_001 只是数据集内标识，          │
    │    不能当 tasks.id 主键），并把 golden_id 写进 metadata 供追溯。    │
    │ 3. 评估时把 get_reference_domains 喂进检索评估，                   │
    │    让 recall_hit_rate「归真」——这正是真实任务缺失、Golden 独有的价值。│
    │    （reference 标准答案用于检索阶段 context_precision，需按维度拆；  │
    │     生成阶段 faithfulness/answer_relevancy 无需标准答案。）          │
    │ 4. 顺序执行（非并发）：每条都要大量调 LLM + 搜索，并发会放大        │
    │    限流风险与成本。                                                 │
    └─────────────────────────────────────────────────────────────────────┘

  用法:
    # 全量跑 10 条（最慢，约十几分钟，大量 LLM 调用）
    python -m src.evaluation.golden_run

    # 只跑 easy 档（4 条，适合先快速验证链路通不通）
    python -m src.evaluation.golden_run --difficulty easy

    # 只跑前 2 条（冒烟测试）
    python -m src.evaluation.golden_run --limit 2

    # 输出到指定文件（默认 golden_baseline.md / .json）
    python -m src.evaluation.golden_run --out-dir ./phase_report

  【运行前置】需要 .env 里的 DeepSeek key 有效、PostgreSQL 已启动、
  schema 已建（含 retrieval_snapshot / eval_results / eval_metric_scores 表）。
  ⚠️ 成本提醒：全量 10 条 ≈ 每条约 4 Agent 生成 + 3 类评估，几十次 LLM 调用。
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from src.evaluation.golden_dataset import (
    GOLDEN_DATASET,
    get_reference_by_dimension,
    get_reference_domains,
)

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 核心流程
# ═════════════════════════════════════════════════════════════════════════════

async def run_one_golden(case: dict, router) -> dict:
    """跑单条 Golden Case：真实执行 + 三类评估，返回结构化结果。

    Args:
        case: Golden Case dict（含 task_id/title/competitors/dimensions/reference/...）
        router: IntentRouter 实例（复用，避免重复构建）

    Returns:
        {
            "golden_id": "golden_001",
            "task_id": "<真实UUID>",
            "difficulty": "easy",
            "route": "pipeline" | "supervisor",
            "exec": {...},          # route() 返回（含 final_report/quality_score）
            "evaluation": {...},    # evaluate_task_full 返回
            "elapsed_sec": float,
            "error": str | None,
        }
    """
    golden_id = case["task_id"]
    real_task_id = str(uuid.uuid4())

    # ── 1. 落 tasks 表（route 不落 task，需脚本自己落）──
    from src.db.connection import create_pool
    from src.db.dao import TaskDAO

    settings = router.settings
    pool = await create_pool(settings)
    task_dao = TaskDAO(pool)
    await task_dao.create(
        task_id=real_task_id,
        user_id="golden-runner",
        title=case["title"],
        competitors=case["competitors"],
        dimensions=case["dimensions"],
        pipeline_mode="pipeline",  # 初始值，route 后按实际回写
    )
    logger.info(
        "【Golden】%s → task=%s 已落库 title=%r 难度=%s",
        golden_id, real_task_id[:8], case["title"], case["difficulty"],
    )

    # ── 2. 真实执行（复用 IntentRouter.route）──
    task = {
        "id": real_task_id,
        "title": case["title"],
        "user_id": "golden-runner",
    }
    llm_parsed = {
        "competitors": case["competitors"],
        "dimensions": case["dimensions"],
        "intent_is_clear": bool(case["competitors"] and case["dimensions"]),
    }
    start = time.monotonic()
    exec_result = await router.route(task, llm_parsed)
    elapsed = time.monotonic() - start

    if "error" in exec_result:
        logger.warning(
            "【Golden】%s 执行失败: %s", golden_id, exec_result.get("error"),
        )
        return {
            "golden_id": golden_id,
            "task_id": real_task_id,
            "difficulty": case["difficulty"],
            "route": exec_result.get("route", "unknown"),
            "exec": exec_result,
            "evaluation": None,
            "elapsed_sec": round(elapsed, 1),
            "error": exec_result.get("error"),
        }

    # ── 3. 三类评估（is_golden 打标 + reference_domains 喂召回对拍归真）──
    from src.evaluation.runner import evaluate_task_full

    evaluation = await evaluate_task_full(
        real_task_id,
        task_dao,
        settings,
        is_golden=True,
        reference_map=get_reference_by_dimension(golden_id),
        reference_domains=get_reference_domains(golden_id),
    )

    logger.info(
        "【Golden】%s 完成 route=%s elapsed=%.0fs gen=%s ret=%s judge=%s",
        golden_id,
        exec_result.get("route"),
        elapsed,
        "ok" if evaluation.get("generation") else "skip",
        "ok" if evaluation.get("retrieval") else "skip",
        "ok" if evaluation.get("judge") else "skip",
    )

    return {
        "golden_id": golden_id,
        "task_id": real_task_id,
        "difficulty": case["difficulty"],
        "route": exec_result.get("route"),
        "exec": exec_result,
        "evaluation": evaluation,
        "elapsed_sec": round(elapsed, 1),
        "error": None,
    }


async def run_golden(
    *,
    difficulty: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """按条件跑 Golden 回归，返回结果列表。

    Args:
        difficulty: 只跑某难度（easy/medium/hard），None = 全量
        limit: 只跑前 N 条，None = 全部
    """
    from src.supervisor.router import IntentRouter

    cases = GOLDEN_DATASET
    if difficulty:
        cases = [c for c in cases if c["difficulty"] == difficulty]
    if limit:
        cases = cases[:limit]

    logger.info("【Golden Run】启动：共 %d 条，难度=%s", len(cases), difficulty or "all")

    router = IntentRouter()
    results: list[dict] = []
    for i, case in enumerate(cases, start=1):
        logger.info(
            "【Golden Run】[%d/%d] 开始 golden=%s (%s)",
            i, len(cases), case["task_id"], case["difficulty"],
        )
        try:
            r = await run_one_golden(case, router)
        except Exception as e:
            logger.exception("【Golden】%s 整体失败: %s", case["task_id"], e)
            r = {
                "golden_id": case["task_id"],
                "difficulty": case["difficulty"],
                "error": str(e),
            }
        results.append(r)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# 汇总输出
# ═════════════════════════════════════════════════════════════════════════════

def _extract_scores(result: dict) -> dict:
    """从单条结果提取便于制表的扁平分数。

    三类评估分数语义不同，统一拉到顶层：
      - generation: faithfulness / answer_relevancy / context_precision（0~1）
      - retrieval:  avg_context_precision / recall_hit_rate（0~1）
      - judge:      final_score（0~100，五维加权）
    """
    ev = result.get("evaluation") or {}
    out: dict = {}

    gen = ev.get("generation")
    if gen and gen.get("ragas"):
        for k, v in gen["ragas"].items():
            out[f"gen_{k}"] = v

    ret = ev.get("retrieval")
    if ret:
        if ret.get("avg_context_precision") is not None:
            out["ret_avg_context_precision"] = ret["avg_context_precision"]
        if ret.get("recall_hit_rate") is not None:
            out["ret_recall_hit_rate"] = ret["recall_hit_rate"]

    judge = ev.get("judge")
    if judge and judge.get("judge_scores"):
        out["judge_final_score"] = judge["judge_scores"].get("final_score")

    # 生成阶段的门禁（PASS/WARN/BLOCK）
    if gen and gen.get("gate"):
        out["gen_gate"] = gen["gate"]

    return out


def _fmt(v, nd=3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _render_markdown(results: list[dict]) -> str:
    """把结果渲染成 Markdown baseline 表。"""
    lines: list[str] = []
    lines.append("# Golden 回归 Baseline")
    lines.append("")
    lines.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　·　共 {len(results)} 条")
    lines.append("")
    lines.append("| # | Golden | 难度 | 路由 | 生成 faithfulness | 生成 answer_rel | 生成 context_prec | 检索 context_prec | 召回命中率 | Judge 总分 | 门禁 | 耗时(s) |")
    lines.append("|---|--------|------|------|------------------|----------------|-------------------|-------------------|------------|------------|------|---------|")

    for i, r in enumerate(results, start=1):
        s = _extract_scores(r)
        lines.append(
            f"| {i} | {r.get('golden_id', '?')} | {r.get('difficulty', '?')} "
            f"| {r.get('route', '?')} "
            f"| {_fmt(s.get('gen_faithfulness'))} "
            f"| {_fmt(s.get('gen_answer_relevancy'))} "
            f"| {_fmt(s.get('gen_context_precision'))} "
            f"| {_fmt(s.get('ret_avg_context_precision'))} "
            f"| {_fmt(s.get('ret_recall_hit_rate'))} "
            f"| {_fmt(s.get('judge_final_score'), 1)} "
            f"| {_fmt(s.get('gen_gate'), 0)} "
            f"| {_fmt(r.get('elapsed_sec'), 1)} |"
        )

    # 错误汇总
    errors = [r for r in results if r.get("error")]
    if errors:
        lines.append("")
        lines.append("## 失败/异常用例")
        lines.append("")
        for r in errors:
            lines.append(f"- `{r.get('golden_id', '?')}`：{r.get('error')}")

    return "\n".join(lines) + "\n"


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description="Golden 回归 — 跑真实链路 + 三类评估，产出 baseline")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard"], help="只跑某难度")
    parser.add_argument("--limit", type=int, help="只跑前 N 条（冒烟测试）")
    parser.add_argument("--out-dir", default=".", help="输出目录（默认当前目录）")
    args = parser.parse_args()

    results = asyncio.run(run_golden(difficulty=args.difficulty, limit=args.limit))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # JSON（完整结构，含 exec/evaluation 明细）
    json_path = out_dir / "golden_baseline.json"
    json_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Markdown（可读 baseline 表）
    md_path = out_dir / "golden_baseline.md"
    md_path.write_text(_render_markdown(results), encoding="utf-8")

    print("\n" + _render_markdown(results))
    print(f"结果已写入：{json_path} 和 {md_path}")


if __name__ == "__main__":
    main()
