# -*- coding: utf-8 -*-
"""意图的 golden dataset —— 与 `golden_dataset.py`（RAG 的 10 条标准答案）并列的另一份。

【职责】
  做   把 `intent_cases.CASES` 转成 ragas 的样本形状，并把每轮评估结果拼成趋势。
  不做 判定、不打分给业务用 —— 这里只做「搬运 + 统计」，gold 的定义在 intent_cases.py。

【为什么要有它：RAG 有 golden，意图没有】
  报告质量有 10 条 reference 可对（`golden_dataset.py`），意图识别此前只有
  `top1=0.832` 这种单次日志。同一套怀疑标准下，意图这边等于没验收。
  补齐后两条链路用同一种方式回答「你凭什么说它准」。

【rabas 字段映射（0.4.3）】
    ragas                       →  这里
    ─────────────────────────────────────────────────
    user_input                  →  用户原话
    reference                   →  gold intent（"标准答案"）
    response                    →  系统判出的 intent
    reference_contexts          →  [期望路由]（如 handler:check_freshness）
    reference_context_ids       →  [case id]（对齐用）
    retrieved_contexts          →  []（意图识别不检索）

【关键决策】
1. **分层指标不进 ragas。** `layer` 是架构指标（L1/L2 省不省 LLM），
   不是单样本误差；硬塞进 SingleTurnSample 只能把 "verify@L2" 编进 response，
   污染标准答案。所以 ragas 只跑 `intent_match`，分层留在自算的 trend 里。
2. **不依赖 ragas 也能用。** 主产物是 jsonl + trend.json；`--ragas` 才真的
   `EvaluationDataset` + 自定义 metric 跑一遍。评估核心不该绑在 deprecated API 上
   （0.4.3 的 `aevaluate` 已标 deprecation，官方推 `@experiment`）。
3. **response 置 null 不置空串。** 空串会被当成"判了个空意图"，null 才能识别成"这轮没跑"。

【读法】
    golden/intent_golden_v1.jsonl   数据集本体（150 行）
    golden/trend.json               每轮 run 的指标序列（时间升序）
    python -m src.evaluation.intent_golden                  # 刷数据集 + 打趋势
    python -m src.evaluation.intent_golden --ragas          # 额外用 ragas 跑一遍
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ragas.metrics.base import SingleTurnMetric

from src.evaluation.intent_cases import CASES

# gold → 期望的处理路径。判对意图却走错 handler，用户看到的仍是错答案，
# 所以 reference_contexts 记的是路由，不是标签本身。
EXPECTED_ROUTE: dict[str, str] = {
    "analysis": "task:pipeline",
    "lookup": "handler:lookup_reports",
    "trace": "handler:trace_evidence",
    "verify": "handler:check_freshness",
    "unknown": "clarify:l4",
}

# 「前两层」= 完全不调 LLM。这个比例才是漏斗的成本价值。
EARLY_LAYERS: tuple[str, ...] = ("L1", "L2")

GOLDEN_DIR = Path(__file__).parent / "golden"
RUN_DIR = Path(__file__).parent / "evaluation_runs"


# ── golden 本体 ─────────────────────────────────────────────────────────

def golden_records() -> list[dict]:
    """CASES → ragas 形状的记录（无 response）。"""
    out: list[dict] = []
    for i, case in enumerate(CASES):
        out.append({
            "id": f"intent-{i:03d}",
            "user_input": case.text,
            "reference": case.gold,
            "reference_contexts": [EXPECTED_ROUTE.get(case.gold, "clarify:l4")],
            "retrieved_contexts": [],
            "metadata": {
                "group": case.group,
                "ambiguous": bool(case.ambiguous),
                "note": case.note,
                "competitors": list(case.competitors),
                "dimensions": list(case.dimensions),
            },
        })
    return out


def to_ragas_samples(records: list[dict]) -> list:
    """记录 → ragas SingleTurnSample。真要用 ragas 时才 import（不拖慢普通跑法）。"""
    from ragas import SingleTurnSample

    return [
        SingleTurnSample(
            user_input=r["user_input"],
            response=r.get("response") or "",
            reference=r["reference"],
            reference_contexts=r.get("reference_contexts") or [],
            reference_context_ids=[r["id"]],
            retrieved_contexts=[],
        )
        for r in records
    ]


def build_dataset(records: list[dict]):
    """ragas EvaluationDataset（`--ragas` 路径用）。"""
    from ragas import EvaluationDataset

    return EvaluationDataset(samples=to_ragas_samples(records))


@dataclass
class IntentMatch(SingleTurnMetric):
    """意图命中率 —— 完全不需要 LLM 的 ragas metric。

    ⚠️ ragas 0.4.3 的自定义 metric 四件套缺一不可（每一件都踩过）：
       ① **必须继承 `SingleTurnMetric`** —— 光有 @dataclass 不行，
          aevaluate 会抛 "All metrics must be initialised metric objects"
       ② `name` 字段
       ③ `_required_columns` 的 key 必须是 `MetricType` 枚举，不是字符串
          （写字符串报 `'str' object has no attribute 'name'`）
       ④ 必须实现 `init(run_config)`，否则实例化就抛 abstract method
    """

    name: str = "intent_match"
    _required_columns: dict = field(
        default_factory=lambda: _metric_columns()
    )

    def __post_init__(self) -> None:
        pass

    def init(self, run_config) -> None:
        pass

    async def _single_turn_ascore(self, sample, callbacks) -> float:
        got = (sample.response or "").strip()
        want = (sample.reference or "").strip()
        return 1.0 if got == want else 0.0


def _metric_columns() -> dict:
    from ragas.metrics.base import MetricType

    return {MetricType.SINGLE_TURN: {"response", "reference"}}


# ── 结果：贴 response + 算趋势 ──────────────────────────────────────────

def load_run(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def attach_run(records: list[dict], run: dict) -> list[dict]:
    """把一次评估的 prediction 贴到 golden 上。

    ⚠️ 用 `user_input` 对齐而不是下标：CASES 增删一条会让下标整体错位，
    而错位后每条都"有 response"，看着完全正常 —— 不崩、全错。
    """
    by_text = {r["text"]: r for r in run.get("rows", [])}
    merged: list[dict] = []
    for rec in records:
        r = by_text.get(rec["user_input"])
        item = dict(rec)
        if r is None:
            item["response"] = None
            item["metadata"] = {**rec["metadata"], "layer": None,
                                "confidence": None, "margin": None}
            item["scores"] = {"intent_match": None, "early_exit": None}
        else:
            item["response"] = r["pred"]
            item["metadata"] = {**rec["metadata"], "layer": r["layer"],
                                "confidence": r["confidence"], "margin": r["margin"]}
            item["scores"] = {
                "intent_match": (r["pred"] == r["gold"]) if r["pred"] else None,
                "early_exit": r["layer"] in EARLY_LAYERS,
            }
        merged.append(item)
    return merged


def run_metrics(run: dict) -> dict:
    """一次 run 的横向指标 —— 趋势表的一行。"""
    layers = run.get("layers") or {}
    total = run.get("total") or sum(layers.values()) or 0
    early = sum(layers.get(k, 0) for k in EARLY_LAYERS)
    main = run.get("main") or {}

    def rate(n):
        return round(n / total, 4) if total else None

    return {
        "ts": run.get("ts"),
        "total": total,
        "accuracy": main.get("accuracy"),
        "n": main.get("n"),
        "L1": rate(layers.get("L1", 0)),
        "L2": rate(layers.get("L2", 0)),
        "L3": rate(layers.get("L3", 0)),
        "L4": rate(layers.get("L4", 0)),
        "early_exit_rate": rate(early),
        "recall": {k: v.get("accuracy") for k, v in (run.get("per_intent") or {}).items()},
        "l3_enabled": run.get("l3_enabled"),
    }


def collect_trend(paths: list[Path] | None = None) -> list[dict]:
    """扫全部 run，按 ts 升序返回。不按文件名排序 —— 文件名给人看，ts 给机器用。"""
    ps = paths if paths is not None else sorted(RUN_DIR.glob("intent_eval_*.json"))
    rows = [run_metrics(load_run(p)) for p in ps]
    return sorted((r for r in rows if r.get("ts")), key=lambda r: r["ts"])


def format_trend(rows: list[dict]) -> str:
    if not rows:
        return "（还没有评估记录）"
    head = (f"{'时间':<17}{'准确率':>8}{'L1':>7}{'L2':>7}"
            f"{'前两层':>8}{'L3':>7}{'L4':>7}")
    lines = [head, "─" * 62]

    def pct(v):
        return f"{v:.1%}" if isinstance(v, (int, float)) else "—"

    for r in rows:
        lines.append(
            f"{(r['ts'] or '')[:16]:<17}"
            f"{pct(r['accuracy']):>8}{pct(r['L1']):>7}{pct(r['L2']):>7}"
            f"{pct(r['early_exit_rate']):>8}{pct(r['L3']):>7}{pct(r['L4']):>7}"
        )
    return "\n".join(lines)


def export(
    out_dir: Path = GOLDEN_DIR,
    run_path: Path | None = None,
    records: list[dict] | None = None,
) -> dict:
    """写 golden jsonl（+ 指定 run 的 response 版）。

    records 为空时用全量。留这个口子是为了导出子集（比如只导 core 组、
    或只导刚改过的那一类），也让测试能造小数据集而不是每次写 150 行。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = records if records is not None else golden_records()
    written: dict[str, str] = {}

    base = out_dir / "intent_golden_v1.jsonl"
    with base.open("w", encoding="utf-8", newline="\n") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    written["golden"] = str(relative(base))

    if run_path is not None:
        run = load_run(run_path)
        stamp = (run.get("ts") or "")[:19].replace(":", "").replace("T", "-")
        target = out_dir / f"intent_run_{stamp or 'latest'}.jsonl"
        with target.open("w", encoding="utf-8", newline="\n") as f:
            for rec in attach_run(recs, run):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        written["run"] = str(relative(target))

    return written


def relative(path: Path) -> str:
    """打印用相对路径，别把 D:\\AAAagent\\... 甩到终端。"""
    try:
        return str(path.relative_to(Path(__file__).resolve().parents[2]))
    except ValueError:
        return str(path)


# ── CLI ─────────────────────────────────────────────────────────────────

async def _run_ragas(run_path: Path | None) -> None:
    """用 ragas 真的跑一遍 intent_match，验证数据集能被标准工具消费。

    ⚠️ 这里的均值与 `intent_eval` 的主指标**不是一个口径**：ragas 默认把 150 条全算，
    而主指标剔除 10 条边界句。同一轮 run 会出现 93.3% vs 97.9% 两个数，
    不是谁算错了 —— 报数时必须带口径，否则看起来像打架。
    """
    from ragas import aevaluate

    records = golden_records()
    if run_path is not None:
        records = attach_run(records, load_run(run_path))
    dataset = build_dataset(records)
    result = await aevaluate(dataset=dataset, metrics=[IntentMatch()])
    df = result.to_pandas()

    amb = sum(1 for r in records if r["metadata"]["ambiguous"])
    scored = [r for r in records if r.get("response")]
    main = [r for r in scored if not r["metadata"]["ambiguous"]]
    main_ok = sum(1 for r in main if r["response"] == r["reference"])

    print(f"\nragas aevaluate（{len(df)} 条，含 {amb} 条边界句）")
    print(f"  intent_match 均值 = {df['intent_match'].mean():.4f}　← 全样本口径")
    print(f"  未命中 {int((df['intent_match'] == 0).sum())} 条")
    if main:
        print(f"  剔除边界后 = {main_ok / len(main):.4f}（{main_ok}/{len(main)}）"
              f"　← 与 intent_eval 主指标同口径")


def main() -> None:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description="意图 golden dataset 与趋势")
    ap.add_argument("--run", type=str, default=None, help="指定某轮 run（默认最新）")
    ap.add_argument("--ragas", action="store_true", help="额外用 ragas aevaluate 跑一遍")
    ap.add_argument("--no-export", action="store_true", help="只打趋势不写文件")
    args = ap.parse_args()

    runs = sorted(RUN_DIR.glob("intent_eval_*.json"))
    run_path = Path(args.run) if args.run else (runs[-1] if runs else None)

    if not args.no_export:
        written = export(run_path=run_path)
        print("已写出：")
        for k, v in written.items():
            print(f"  {k:<7}{v}")

    rows = collect_trend()
    print("\n结果趋势（前两层 = 完全不调 LLM）")
    print(format_trend(rows))
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / "trend.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n趋势已存 {relative(GOLDEN_DIR / 'trend.json')}")

    if args.ragas:
        asyncio.run(_run_ragas(run_path))


if __name__ == "__main__":
    main()
