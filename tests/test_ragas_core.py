# -*- coding: utf-8 -*-
"""ragas_core 纯逻辑测试（不依赖 DB / LLM）。"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.evaluation.llm_factory import build_run_config as _build_run_config  # noqa: E402
from src.evaluation.ragas_core import (  # noqa: E402
    SampleRow,
    _aggregate_scores,
    _fmt,
    _mean_score,
    build_samples,
)


def _make_snapshot(
    *,
    dimension: str = "功能",
    competitor: str = "飞书",
    query: str | None = None,
    texts=None,
    response_text: str = "",
) -> dict:
    return {
        "dimension": dimension,
        "competitor": competitor,
        "query": query or f"{competitor} {dimension}",
        "reranked_texts": texts if texts is not None else ["足够长的检索上下文片段"],
        "response_text": response_text,
    }


def test_build_samples_展开并过滤短上下文():
    snaps = [
        _make_snapshot(texts=["短", "足够长的检索上下文片段"]),
        _make_snapshot(competitor="钉钉", texts=["另一个足够长的上下文"]),
    ]
    rows = build_samples(snaps, {"功能": "功能标准答案"}, "task-1234")
    assert len(rows) == 2
    assert [r.key for r in rows] == ["飞书@功能", "钉钉@功能"]
    assert rows[0].n_contexts == 1
    assert rows[0].retrieved_contexts == ["足够长的检索上下文片段"]
    assert rows[0].user_input == "飞书 功能"
    assert rows[0].reference == "功能标准答案"


def test_build_samples_全短上下文跳过():
    rows = build_samples([_make_snapshot(texts=["abc"])], {"功能": "x"})
    assert rows == []


def test_build_samples_reference缺省回退维度名():
    rows = build_samples([_make_snapshot()], None)
    assert rows[0].reference == "功能"


def test_build_samples_response_text缺失为None():
    rows = build_samples([_make_snapshot(response_text="")])
    assert rows[0].response is None
    rows = build_samples([_make_snapshot(response_text="  飞书功能结论  ")])
    assert rows[0].response == "飞书功能结论"


def test_fmt_nan与None渲染为横线():
    assert _fmt(None) == "—"
    assert _fmt(float("nan")) == "—"
    assert _fmt(0.125) == "0.125"


def test_build_samples_reference优先cell级():
    """cell 表命中 → 取「只讲该竞品」的那半句（与评估单元同粒度）。"""
    snaps = [
        _make_snapshot(competitor="飞书", texts=["飞书功能上下文足够长"]),
        _make_snapshot(competitor="钉钉", texts=["钉钉功能上下文足够长"]),
    ]
    rows = build_samples(
        snaps,
        {"功能": "飞书以协同见长；钉钉以审批见长"},
        "golden_001",
        {"飞书@功能": "飞书以协同见长", "钉钉@功能": "钉钉以审批见长"},
    )
    assert [r.reference for r in rows] == ["飞书以协同见长", "钉钉以审批见长"]


def test_build_samples_cell缺失回退维度级():
    rows = build_samples(
        [_make_snapshot(competitor="飞书")],
        {"功能": "维度级复合句"},
        "golden_001",
        {"钉钉@功能": "只对钉钉"},
    )
    assert rows[0].reference == "维度级复合句"


def test_build_samples_无竞品快照回退维度级():
    """历史维度级快照（competitor 为空）→ cell 键退化维度名，仍走维度级兜底。"""
    rows = build_samples(
        [_make_snapshot(competitor="")],
        {"功能": "维度级兜底"},
        "t",
        {"飞书@功能": "只对飞书"},
    )
    assert rows[0].reference == "维度级兜底"


def test_build_samples_不传cell表时与改动前一致():
    rows = build_samples([_make_snapshot()], {"功能": "功能标准答案"}, "task-1234")
    assert rows[0].reference == "功能标准答案"
    assert _fmt(1.0) == "1.000"


def test_aggregate_scores_忽略NaN与None():
    rows = [
        {
            "context_precision": 0.5,
            "context_recall": None,
            "faithfulness": float("nan"),
            "answer_relevancy": 0.2,
        },
        {
            "context_precision": 0.1,
            "context_recall": 0.9,
            "faithfulness": None,
            "answer_relevancy": None,
        },
    ]
    agg = _aggregate_scores(rows)
    assert agg["context_precision"] == 0.3
    assert agg["context_recall"] == 0.9
    assert agg["faithfulness"] is None
    assert agg["answer_relevancy"] == 0.2


# ─────────────────────────────────────────────────────────────────────────────
# 超时 / 重复（2026-10-16）：修 `钉钉@功能 cp=NaN`（Job[12]: TimeoutError）
# 与「n=4 时 judge 方差主导、单轮不可读」两个缺陷
# ─────────────────────────────────────────────────────────────────────────────


def test_mean_score_取均值并忽略None与NaN():
    assert _mean_score([0.1, 0.9, 0.5]) == 0.5
    assert _mean_score([0.2, None, float("nan"), 0.4]) == 0.3
    assert _mean_score([None, float("nan")]) is None
    assert _mean_score([]) is None


def test_mean_score_二值抖动不能被抹成0():
    """[0,1,0] 的真实含义是「3 轮里 1 轮被支持」= 0.333。

    2026-10-17 改：旧实现取中位数 → 排序 [0,0,1] → 0，把 1/3 的支持率
    直接抹成一个 bit（实测钉钉@定价 / 钉钉@功能 正是 [0,1,0] 形态）。
    这条测试钉住「比例型指标必须看均值」这个约定。
    """
    assert _mean_score([0.0, 1.0, 0.0]) == 0.333
    assert _mean_score([0.0, 1.0]) == 0.5
    # 中位数会得到 0 / 0.5，前者丢掉信息 —— 明确禁止回退
    assert _mean_score([0.0, 0.0, 1.0]) != 0.0


def test_mean_score_单值与旧行为等价():
    """repeat=1 时均值就是该值本身，且保留 3 位 —— 与改动前 round(v,3) 等价。"""
    assert _mean_score([0.1234567]) == 0.123


def test_build_run_config_读配置并兜底默认值():
    from types import SimpleNamespace

    cfg = _build_run_config(
        SimpleNamespace(ragas_eval_timeout=999, ragas_eval_max_workers=3)
    )
    assert cfg.timeout == 999
    assert cfg.max_workers == 3

    # 老 settings（无新字段）/ 空值 → 回落到「长 contexts 安全值」，而不是 ragas 默认 180
    fallback = _build_run_config(SimpleNamespace())
    assert fallback.timeout == 600
    assert fallback.max_workers == 8
    assert _build_run_config(SimpleNamespace(ragas_eval_max_workers=0)).max_workers == 8


def test_evaluate_four_repeat大于1时逐轮取中位并传run_config(monkeypatch):
    """钉住两条关键接线：
    ① repeat=N 真的跑了 N 轮 aevaluate（方差才压得住）；
    ② run_config 显式传入（否则回落 180s → 长 contexts 样本整格 NaN）。
    """
    import asyncio
    from types import SimpleNamespace

    import pandas as pd

    import src.evaluation.ragas_core as core

    # judge / embeddings 不真构造（避免建 HTTP 客户端、读 key）
    monkeypatch.setattr(core, "build_judge_llm", lambda settings: object())
    monkeypatch.setattr(core, "build_embeddings", lambda settings: object())

    seen_run_configs: list = []
    calls = {"n": 0}

    class _FakeOutput:
        def __init__(self, frame):
            self._frame = frame

        def to_pandas(self):
            return self._frame

    async def fake_aevaluate(dataset, metrics, run_config=None):
        calls["n"] += 1
        seen_run_configs.append(run_config)
        names = [m.name for m in metrics]
        # 每轮给不同分数：轮1=0.1 / 轮2=0.9 / 轮3=0.5 → 中位应为 0.5
        seq = [0.1, 0.9, 0.5]
        value = seq[(calls["n"] - 1) % len(seq)]
        frame = pd.DataFrame(
            {name: [value] * len(dataset.samples) for name in names},
            index=range(len(dataset.samples)),
        )
        return _FakeOutput(frame)

    monkeypatch.setattr(core, "aevaluate", fake_aevaluate)

    samples = [
        SampleRow(
            key="飞书@功能",
            competitor="飞书",
            dimension="功能",
            user_input="飞书 功能",
            response="飞书功能结论",
            retrieved_contexts=["足够长的上下文片段甲"],
            reference="飞书以协同见长",
            n_contexts=1,
        ),
        # 无 response → 走第二批（只跑 cp/cr），验证批次划分不受 repeat 影响
        SampleRow(
            key="钉钉@功能",
            competitor="钉钉",
            dimension="功能",
            user_input="钉钉 功能",
            response=None,
            retrieved_contexts=["足够长的上下文片段乙"],
            reference="钉钉以审批见长",
            n_contexts=1,
        ),
    ]
    settings = SimpleNamespace(ragas_eval_timeout=777, ragas_eval_max_workers=2)

    result = asyncio.run(core.evaluate_four(samples, settings, "task-1", repeat=3))

    # 两批 × 3 轮 = 6 次 aevaluate
    assert calls["n"] == 6
    assert all(rc is not None for rc in seen_run_configs), "run_config 必须显式传入"
    assert all(rc.timeout == 777 and rc.max_workers == 2 for rc in seen_run_configs)
    assert result.repeat == 3
    # 中位 0.5（三值 0.1/0.9/0.5）
    assert result.rows[0]["context_precision"] == 0.5
    # 第二批样本的生成层保持 None（没跑那两个指标）
    assert result.rows[1]["faithfulness"] is None
    assert result.rows[1]["answer_relevancy"] is None
    # 行顺序仍与输入一致
    assert [r["key"] for r in result.rows] == ["飞书@功能", "钉钉@功能"]


def test_evaluate_four_repeat默认为1时只跑一轮且分数精确(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pandas as pd

    import src.evaluation.ragas_core as core

    monkeypatch.setattr(core, "build_judge_llm", lambda settings: object())
    monkeypatch.setattr(core, "build_embeddings", lambda settings: object())
    calls = {"n": 0}

    class _FakeOutput:
        def to_pandas(self):
            return pd.DataFrame(
                {
                    "context_precision": [0.333],
                    "context_recall": [float("nan")],
                    "faithfulness": [1.0],
                    "answer_relevancy": [0.25],
                },
                index=[0],
            )

    async def fake_aevaluate(dataset, metrics, run_config=None):
        calls["n"] += 1
        return _FakeOutput()

    monkeypatch.setattr(core, "aevaluate", fake_aevaluate)

    sample = SampleRow(
        key="飞书@功能",
        competitor="飞书",
        dimension="功能",
        user_input="飞书 功能",
        response="飞书功能结论",
        retrieved_contexts=["足够长的上下文片段甲"],
        reference="飞书以协同见长",
        n_contexts=1,
    )
    result = asyncio.run(
        core.evaluate_four([sample], SimpleNamespace(), "task-1")
    )
    assert calls["n"] == 1, "默认 repeat=1 只跑一轮（成本不翻倍）"
    assert result.repeat == 1
    assert result.rows[0]["context_precision"] == 0.333
    assert result.rows[0]["context_recall"] is None  # NaN → None（渲染为「—」）
    assert result.rows[0]["faithfulness"] == 1.0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))