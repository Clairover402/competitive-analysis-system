"""意图评估器 — 用 150 句带 gold 的说法，算出各层到底准不准。

【为什么要有它】
`top1=0.832 gap=0.109` 这种日志只能说明某一次判对了；它回答不了
「L2 在 150 句里错几类、错在哪两类上、floor 定 0.55 合不合适」。
这份评估器把"我觉得挺准"换成三个可引用的数字：
**层接管率 / 各层准确率 / 混淆矩阵**。

【三层口径，别混着看】
  · **接管率**：L1 收走多少、L2 收走多少、多少落到 L3、多少被判"该追问"。
    这个数字决定成本 —— 每多下沉一条就是多一次 LLM。
  · **各层准确率**：L1 接走的那批里对了多少（L1 只在参数齐全时出手，
    正常应该是 100%；掉下来说明规则漏了）。L2 同理。
  · **端到端准确率**：用户实际感受到的那个数。它一定 ≤ 各层准确率的加权，
    因为错在早层的不会被后面纠正 —— 漏斗只往下走，不回头。

【主指标只算非边界样本】
`ambiguous=True` 的句子两种判法都说得通（"飞书多少钱" 是问当前值还是
要一次分析？），把它们算进准确率等于用没有唯一答案的题算对错：
模型怎么答都有一半算错，看起来"准确率低"其实是题目有问题。
它们单独统计，只用来观察"模型的倾向"。

【跑一次要多久 / 花多少】
L1 零成本；L2 每句一次本地 embedding；L3 只跑下沉样本，一次 LLM 调用
（deepseek，几分钱）。150 句默认并发 4，约 1~2 分钟。

用法:
    python -m src.evaluation.intent_eval
    python -m src.evaluation.intent_eval --no-l3     # 只看 L1/L2 接管与下沉量
    python -m src.evaluation.intent_eval --sweep     # 顺带扫 floor × margin
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.evaluation.intent_cases import CASES, Case, stats
from src.supervisor.intent import (
    INTENTS,
    l1_deterministic,
    l2_semantic,
    l3_llm,
    l4_clarify,
)

logger = logging.getLogger(__name__)

_OUT_DIR = Path(__file__).parent / "evaluation_runs"
_ALL_INTENTS = (*INTENTS, "unknown")


async def judge_case(case: Case, settings, *, run_l3: bool) -> dict:
    """跑一句，返回 {gold, pred, layer, ...}。分支顺序与 classify 完全一致。

    ★ 2026-10-29 拆包后可以直接调 classify 了（它不再需要 pool），但这里仍逐层调：
      `--no-l3` 要在 L2 之后截断，classify 没有这个开关。
      ⚠️ 代价是两处顺序要人工对齐 —— 改 classify 的分支顺序时同步改这里。
    """
    r1 = l1_deterministic(
        case.text, list(case.competitors), list(case.dimensions)
    )
    if r1 is not None:
        return _row(case, r1.intent, "L1", r1)

    text = case.text.strip()
    if not text:
        return _row(case, "unknown", "L1", None, note="无任何可判定输入")

    r2 = await l2_semantic(text, settings)
    if r2 is not None:
        # 白名单已删（2026-10-29）：过阈即停，不再看意图类别缺不缺槽位。
        return _row(case, r2.intent, "L2", r2)

    if not run_l3:
        # 不下沉也能看出问题：这一批就是"要花一次 LLM"的流量
        return _row(case, None, "skipped", r2, note="未跑 L3")

    r3 = await l3_llm(text, settings, "")
    if r3.intent in INTENTS:
        return _row(case, r3.intent, "L3", r3)

    r4 = l4_clarify(text)
    return _row(case, r4.intent, "L4", r4)


def _row(case: Case, pred: str | None, layer: str, result, note: str = "") -> dict:
    return {
        "text": case.text,
        "gold": case.gold,
        "pred": pred,
        "layer": layer,
        "ambiguous": case.ambiguous,
        "group": case.group,
        "confidence": round(getattr(result, "confidence", 0.0) or 0.0, 4),
        "margin": round(getattr(result, "margin", 0.0) or 0.0, 4),
        "note": note or case.note,
    }


def summarize(rows: list[dict]) -> dict:
    """把逐条结果算成可引用的数字。纯函数，不做 IO。"""
    layers = Counter(r["layer"] for r in rows)

    def _acc(subset: list[dict]) -> dict:
        judged = [r for r in subset if r["pred"] is not None]
        correct = sum(1 for r in judged if r["pred"] == r["gold"])
        return {
            "n": len(judged),
            "correct": correct,
            "accuracy": round(correct / len(judged), 4) if judged else None,
        }

    # ★ 主指标只算非边界样本。
    main = [r for r in rows if not r["ambiguous"]]
    amb = [r for r in rows if r["ambiguous"]]

    # ★ 层的准确率也只算非边界样本：接管条数（layers）是全部 150 条的真实分布，
    #   那是成本口径；准确率是质量口径，两者混用会让"L3 错了 9%"里混进
    #   几道本来就没有唯一答案的题。
    by_layer = {
        layer: _acc([r for r in main if r["layer"] == layer])
        for layer in ("L1", "L2", "L3", "L4")
    }
    by_layer = {k: v for k, v in by_layer.items() if v["n"]}

    per_intent = {}
    for intent in _ALL_INTENTS:
        gold = [r for r in rows if r["gold"] == intent and not r["ambiguous"]]
        if gold:
            per_intent[intent] = _acc(gold)

    confusion: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["ambiguous"] or r["pred"] is None:
            continue
        confusion.setdefault(r["gold"], {}).setdefault(r["pred"], 0)
        confusion[r["gold"]][r["pred"]] += 1

    errors = [
        r for r in main if r["pred"] is not None and r["pred"] != r["gold"]
    ]
    errors.sort(key=lambda r: (r["group"], r["gold"]))

    return {
        "total": len(rows),
        "layers": dict(layers),
        "main": _acc(main),
        "ambiguous": {**_acc(amb), "items": [
            {"text": r["text"], "gold": r["gold"], "pred": r["pred"]} for r in amb
        ]},
        "by_layer": by_layer,
        "per_intent": per_intent,
        "confusion": confusion,
        "errors": errors,
    }


async def evaluate(
    cases: list[Case] | None = None,
    settings=None,
    *,
    run_l3: bool = True,
    concurrency: int = 4,
) -> dict:
    """跑完整个评估集，返回汇总 + 逐条明细。"""
    cases = cases or CASES
    sem = asyncio.Semaphore(concurrency)

    async def _one(c: Case) -> dict:
        async with sem:
            try:
                return await judge_case(c, settings, run_l3=run_l3)
            except Exception as e:  # noqa: BLE001
                # 单条炸掉不能拖垮整轮：记成 skipped 并在明细里留痕，
                # 否则跑 100 条挂 1 条就白跑。
                logger.warning("判定失败 %r: %s", c.text, e)
                return {
                    "text": c.text, "gold": c.gold, "pred": None,
                    "layer": "error", "ambiguous": c.ambiguous,
                    "group": c.group, "confidence": 0.0, "margin": 0.0,
                    "note": f"异常: {e}",
                }

    rows = await asyncio.gather(*(_one(c) for c in cases))
    out = summarize(list(rows))
    out["rows"] = list(rows)
    out["dataset"] = stats()
    out["l3_enabled"] = run_l3
    out["ts"] = datetime.now().isoformat(timespec="seconds")
    return out


# ── 打印 ──────────────────────────────────────────────────────────────────

def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def render(report: dict) -> str:
    """人读的报表。刻意不引第三方表格库 —— 一眼能看出列对齐就行。"""
    L: list[str] = []
    total = report["total"]
    d = report["dataset"]
    L.append(f"意图评估 · {total} 句（core {d['by_group'].get('core', 0)} / "
             f"adversarial {d['by_group'].get('adversarial', 0)}，"
             f"边界样本 {d['ambiguous']} 条单独统计）")
    L.append("")

    L.append("层接管")
    L.append(f"  {'层':<6}{'条数':>6}{'占比':>9}{'该层准确率':>14}")
    for layer, name in (("L1", "L1 规则"), ("L2", "L2 语义"),
                        ("L3", "L3 LLM"), ("L4", "L4 追问")):
        n = report["layers"].get(layer, 0)
        if not n:
            continue
        acc = report["by_layer"].get(layer, {}).get("accuracy")
        L.append(f"  {name:<6}{n:>6}{n / total * 100:>8.1f}%{_pct(acc):>12}")
    skipped = report["layers"].get("skipped", 0)
    if skipped:
        L.append(f"  {'跳过 L3':<6}{skipped:>6}{'—':>9}{'—':>12}")
    err = report["layers"].get("error", 0)
    if err:
        L.append(f"  {'异常':<6}{err:>6}{'—':>9}{'—':>12}")
    L.append("")

    m = report["main"]
    L.append(f"主指标（非边界 {m['n']} 条，已判出）：准确率 {_pct(m['accuracy'])}"
             f"（{m['correct']}/{m['n']}）")
    a = report["ambiguous"]
    if a["n"]:
        L.append(f"边界样本 {a['n']} 条：命中 {a['correct']} 条（仅供参考，不进主指标）")
    L.append("")

    L.append("按意图 recall")
    for intent, s in report["per_intent"].items():
        L.append(f"  {intent:<9}{s['correct']:>4}/{s['n']:<4}{_pct(s['accuracy']):>8}")
    L.append("")

    L.append("混淆矩阵（行 = gold，列 = 预测；边界样本已排除）")
    cols = [i for i in _ALL_INTENTS]
    L.append("  " + " " * 10 + "".join(f"{c:>9}" for c in cols))
    for gold in cols:
        row = report["confusion"].get(gold)
        if not row:
            continue
        cells = "".join(f"{row.get(c, 0) or '':>9}" for c in cols)
        L.append(f"  {gold:<10}{cells}")
    L.append("")

    if report["errors"]:
        L.append(f"错例 {len(report['errors'])} 条（按 gold 分组）")
        for r in report["errors"][:12]:
            L.append(f"  [{r['gold']}→{r['pred']}] {r['text']}"
                     f"（{r['layer']}, conf={r['confidence']:.3f}）")
        if len(report["errors"]) > 12:
            L.append(f"  …另有 {len(report['errors']) - 12} 条，见 JSON")
    else:
        L.append("错例：无")
    return "\n".join(L)


def save(report: dict) -> Path:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _OUT_DIR / f"intent_eval_{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def main() -> None:
    ap = argparse.ArgumentParser(description="意图识别评估")
    ap.add_argument("--no-l3", action="store_true", help="不下沉 L3，只看 L1/L2")
    ap.add_argument("--sweep", action="store_true", help="顺带扫 floor × margin")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    report = await evaluate(run_l3=not args.no_l3, concurrency=args.concurrency)
    print(render(report))

    if args.sweep:
        from src.supervisor.intent import calibrate

        samples = [(c.text, c.gold) for c in CASES if c.text.strip() and not c.ambiguous]
        rows = await calibrate(samples)
        print("\nL2 阈值扫描（只看 L2 单独放行的那部分）")
        print(f"  {'floor':>6}{'margin':>8}{'放行率':>9}{'放行内准确率':>14}")
        for r in rows:
            print(f"  {r['floor']:>6.2f}{r['margin']:>8.2f}"
                  f"{r['coverage'] * 100:>8.1f}%{r['precision'] * 100:>13.1f}%")

    path = save(report)
    print(f"\n明细已存 {path}")


if __name__ == "__main__":
    asyncio.run(main())
