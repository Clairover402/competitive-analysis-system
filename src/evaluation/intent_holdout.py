"""holdout：24 句**不在 CASES 里**的新说法，用来验收"改完 prompt 到底有没有泛化"。

为什么需要它
────────────
`intent_cases.CASES` 既是改 prompt 时的靶子，又是算准确率的样本 ——
照着错例改完再在同一批上量，量到的是"我记得答案"，不是"模型学会了"。
2026-10-28 那轮就是这样：verify recall 45.2% → 改定义后 100%，
但那个 100% 只能证明"那 19 条错例被修好了"。

所以另开一批，**改 prompt 时不许打开这个文件**（真话：做过一次就失效了，
每轮改完描述记得补几条新的、删掉旧的）。

用法
────
    python -m src.evaluation.intent_holdout

口径
────
- 走完整 classify（含 L1/L2/L3/L4），不是单测 L3
- gold 有争议的句子要在注释里写明争议点，别把"标注争议"算成"系统出错"
- ⚠️ 这 24 句不许并进 CASES：两批一旦合流，下一轮又变成自己考自己
"""

from __future__ import annotations

import asyncio

from src.config import Settings
from src.supervisor.intent import classify

# (原话, gold, 争议点说明) —— 第三条只在 gold 存疑时写
HOLDOUT: tuple[tuple[str, str, str], ...] = (
    # ── analysis：竞品塞在冒号后、或只说"出个分析" ──
    ("帮我看看这几家谁更强：飞书、钉钉、企业微信", "analysis", ""),
    ("钉钉和飞书，从定价和生态两个角度比比", "analysis", ""),
    ("给我出个 Notion 的竞品分析", "analysis", ""),
    ("这三家的商业化谁做得好", "analysis", ""),
    # ── lookup：用"历史/昨天/发我"而不是"上次报告" ──
    ("我昨天跑的那个分析呢", "lookup", ""),
    ("历史任务列表调出来", "lookup", ""),
    ("之前那份报告发我", "lookup", ""),
    ("我做过几次飞书的分析", "lookup", ""),
    # ── trace：口语化甚至带质问 ──
    ("你这话有依据吗", "trace", ""),
    ("给个链接证明一下", "trace", ""),
    ("这段是从哪篇抄来的", "trace", ""),
    ("结论后面该跟个引用吧", "trace", ""),
    # ── 原 verify：一个"过期/最新"都不出现（现在应一律 unknown + 明确告知）──
    ("这价格现在还有效么", "unknown", ""),
    ("隔了这么久还准吗", "unknown", ""),
    ("该重新抓一遍了吧", "unknown", ""),
    ("上次那个数还能用吗", "unknown", ""),
    ("飞书是不是悄悄涨价了", "unknown", ""),
    # ── unknown：域外与信息不足 ──
    ("随便说说", "unknown", ""),
    ("你是谁", "unknown", ""),
    ("明天要下雨吗", "unknown", ""),
    ("给我讲个笑话", "unknown", ""),
    # ── 复合句：诉求在后半 ──
    ("飞书和钉钉的报告，顺便说下数据新不新", "unknown",
     "「给我出报告」也成立；实测判 analysis 不算错，别当回归指标"),
    ("把上次报告里所有引用的网站列出来", "trace", ""),
    ("那次分析的结论现在还成立吗", "unknown", ""),
)


async def main() -> None:
    cfg = Settings()
    ok = 0
    misses: list[tuple[str, str, str, str]] = []
    for text, gold, _ in HOLDOUT:
        r = await classify(query=text, settings=cfg)
        hit = r.intent == gold
        ok += hit
        if not hit:
            misses.append((text, gold, r.intent, r.layer))
        print(f"{'OK ' if hit else 'XX '}{gold:<9}->{r.intent:<9}{r.layer:<4} {text}")

    total = len(HOLDOUT)
    print(f"\nholdout 准确率 {ok}/{total} = {ok / total:.1%}")
    if misses:
        print("错例：")
        for text, gold, pred, layer in misses:
            print(f"  {gold} -> {pred} ({layer})  {text}")


if __name__ == "__main__":
    asyncio.run(main())
