# -*- coding: utf-8 -*-
"""run_archive 守卫测试。

守的是四条「留痕一旦破了就不叫留痕」的性质：

  1. run 目录写完只有两个文件（不多出第三个）；
  2. run_id 唯一（同分钟重跑要自动加后缀）；
  3. golden 指纹对 reference 改动敏感（否则「数据集变了」会被读成「系统变好」）；
  4. finish_run 不修改调用方传入的 entries（归档是抄一份，不是接手改）。

外加对比纪律的两条：阈值来自该 cell 自己的跨轮极差；数据集变了必须挂免责声明。

全部用 tmp_path 作为归档根，不污染仓库、不连库、不调 LLM。
"""

from __future__ import annotations

import json

from src.evaluation import run_archive as ra

# ── 构造样本数据 ────────────────────────────────────────────────────────────


def _entry(
    golden_id: str = "golden_001",
    cp: float | None = 0.5,
    cr: float | None = 0.6,
    fa: float | None = 0.4,
    ar: float | None = 0.7,
    repeat: int = 3,
    by_repeat: dict | None = None,
) -> dict:
    """一条 ragas_core 风格的 case entry（只保留归档/对比真正消费的字段）。"""
    return {
        "golden_id": golden_id,
        "task_id": "11111111-2222-3333-4444-555555555555",
        "route": "pipeline",
        "rows": [
            {
                "key": "飞书@功能",
                "competitor": "飞书",
                "dimension": "功能",
                "n_contexts": 6,
                "context_precision": cp,
                "context_recall": cr,
                "faithfulness": fa,
                "answer_relevancy": ar,
                "reference": "飞书以协同文档见长；视频会议支持 100 方。",
            }
        ],
        "aggregates": {
            "context_precision": cp,
            "context_recall": cr,
            "faithfulness": fa,
            "answer_relevancy": ar,
        },
        "n_contexts": {"avg": 6, "min": 5, "max": 7},
        "skipped": 0,
        "repeat": repeat,
        "repeat_spread": {"context_recall": 0.2},
        "scores_by_repeat": by_repeat
        or {
            "飞书@功能": {
                "context_precision": [cp, cp, cp],
                "context_recall": [cr, cr, cr],
                "faithfulness": [fa, fa, fa],
                "answer_relevancy": [ar, ar, ar],
            }
        },
        "error": None,
    }


def _payload(
    run_id: str,
    cp: float,
    cr: float,
    spread_cr: float = 0.2,
    golden: str = "a" * 64,
    note: str | None = None,
    n_contexts: int = 6,
) -> dict:
    """直接构造一个 run.json 载荷（供对比测试用，不落盘）。"""
    row = {
        "key": "飞书@功能",
        "competitor": "飞书",
        "dimension": "功能",
        "n_contexts": n_contexts,
        "context_precision": cp,
        "context_recall": cr,
        "faithfulness": 0.4,
        "answer_relevancy": 0.7,
        "reference": "……",
    }
    return {
        "manifest": {
            "run_id": run_id,
            "archive_schema": 1,
            "kind": "golden",
            "note": note,
            "started_at": "2026-10-17T21:19:00",
            "finished_at": "2026-10-17T23:30:00",
            "duration_sec": 7860,
            "git_commit": "0ded622",
            "git_dirty": False,
            "git_branch": "master",
            "golden_hash": golden,
            "settings": {"vector_search_top_k": 15, "per_competitor_page_cap": 20},
            "eval_config": {
                "repeat": 3,
                "ragas_eval_timeout": 600.0,
                "ragas_eval_max_workers": 8,
            },
            "metrics": {"names": list(ra.METRICS), "ragas_version": "0.4.3"},
            "cases": [
                {
                    "golden_id": "golden_001",
                    "task_id": "t",
                    "route": "pipeline",
                    "error": None,
                }
            ],
            "cases_completed": 1,
            "cases_requested": 1,
            "operator": "tester",
        },
        "cases": {
            "golden_001": {
                "golden_id": "golden_001",
                "task_id": "t",
                "route": "pipeline",
                "error": None,
                "repeat": 3,
                "rows": [row],
                "aggregates": {
                    "context_precision": cp,
                    "context_recall": cr,
                    "faithfulness": 0.4,
                    "answer_relevancy": 0.7,
                },
                "n_contexts": {"avg": n_contexts, "min": n_contexts, "max": n_contexts},
                "repeat_spread": {},
                "scores_by_repeat": {
                    # 该 cell 的 cr 跨轮极差 = spread_cr（判定阈值就来自这里）
                    "飞书@功能": {
                        "context_recall": [cr, cr + spread_cr],
                        "context_precision": [cp, cp],
                    }
                },
            }
        },
    }


# ── 1. 目录形状 ─────────────────────────────────────────────────────────────


def test_finish_run只产两个文件(tmp_path):
    handle = ra.begin_run("golden", note="单测", root=tmp_path)
    run_dir = ra.finish_run(handle, [_entry()], root=tmp_path)

    names = sorted(p.name for p in run_dir.iterdir())
    assert names == [ra.RUN_JSON, ra.SUMMARY_MD], names
    # .pending 占位文件必须被清掉，否则就是「多出来的第三个文件」
    assert not (run_dir / ".pending").exists()
    # 根目录：TREND.md（finish_run 顺带重建）+ 无 baseline
    assert (tmp_path / ra.TREND_FILENAME).exists()


def test_run_json含manifest与cases两段(tmp_path):
    handle = ra.begin_run("golden", note="结构", root=tmp_path)
    run_dir = ra.finish_run(handle, [_entry()], root=tmp_path)
    payload = json.loads((run_dir / ra.RUN_JSON).read_text(encoding="utf-8"))

    assert set(payload) == {"manifest", "cases"}
    manifest = payload["manifest"]
    for key in (
        "run_id",
        "archive_schema",
        "kind",
        "note",
        "started_at",
        "finished_at",
        "duration_sec",
        "git_commit",
        "git_dirty",
        "golden_hash",
        "settings",
        "eval_config",
        "metrics",
        "cases",
        "cases_completed",
        "cases_requested",
        "operator",
    ):
        assert key in manifest, f"manifest 缺字段 {key}"
    assert manifest["note"] == "结构"
    assert manifest["kind"] == "golden"
    # 复现三要素之一：resolved 口径必须留（默认值 vs .env 不一致，只记默认等于没记）
    assert "vector_search_top_k" in manifest["settings"]
    # 密钥类字段一律脱敏
    for name, value in manifest["settings"].items():
        if any(tag in name.lower() for tag in ("key", "secret", "password", "token")):
            assert value in ("***", ""), f"{name} 未脱敏"


def test_summary含case表与全量均值(tmp_path):
    handle = ra.begin_run("golden", root=tmp_path)
    run_dir = ra.finish_run(handle, [_entry()], root=tmp_path)
    text = (run_dir / ra.SUMMARY_MD).read_text(encoding="utf-8")

    assert "## case 级总表" in text
    assert "全量均值" in text
    assert "飞书@功能" in text
    assert "0.500" in text  # cp
    assert "禁止引用绝对值" in text


# ── 2. run_id 唯一 ─────────────────────────────────────────────────────────


def test_同分钟重跑自动加后缀(tmp_path):
    h1 = ra.begin_run("golden", root=tmp_path)
    h2 = ra.begin_run("golden", root=tmp_path)
    h3 = ra.begin_run("golden", root=tmp_path)

    assert h1.run_id != h2.run_id
    assert h2.run_id.endswith("-2")
    assert h3.run_id.endswith("-3")
    assert len({h1.run_id, h2.run_id, h3.run_id}) == 3


def test_同目录不覆盖旧run(tmp_path):
    h1 = ra.begin_run("golden", root=tmp_path)
    (h1.dir / "mark.txt").write_text("老档不许动", encoding="utf-8")
    h2 = ra.begin_run("golden", root=tmp_path)
    assert h1.run_id != h2.run_id
    assert (h1.dir / "mark.txt").read_text(encoding="utf-8") == "老档不许动"


# ── 3. golden 指纹敏感 ──────────────────────────────────────────────────────


def test_golden指纹对reference改动敏感(monkeypatch):
    from src.evaluation import golden_dataset

    base = ra.golden_fingerprint()
    assert ra.golden_fingerprint() == base  # 幂等

    mutated = json.loads(json.dumps(golden_dataset.GOLDEN_DATASET, ensure_ascii=False))
    mutated[0]["reference_by_cell"] = {
        **mutated[0]["reference_by_cell"],
        "飞书@功能": "（手改过的一句话）",
    }
    monkeypatch.setattr(golden_dataset, "GOLDEN_DATASET", mutated)
    assert ra.golden_fingerprint() != base


# ── 4. finish_run 不改入参 ─────────────────────────────────────────────────


def test_finish_run不修改传入entries(tmp_path):
    entries = [_entry()]
    snapshot = json.loads(json.dumps(entries, ensure_ascii=False))
    handle = ra.begin_run("golden", root=tmp_path)
    ra.finish_run(handle, entries, root=tmp_path)
    assert entries == snapshot


# ── 5. 趋势表 ──────────────────────────────────────────────────────────────


def test_trend列出版本并按上一版算Δ(tmp_path):
    h1 = ra.begin_run("golden", note="首基", root=tmp_path)
    ra.finish_run(h1, [_entry(cr=0.5)], root=tmp_path)
    h2 = ra.begin_run("golden", note="top_k 15→20", root=tmp_path)
    ra.finish_run(h2, [_entry(cr=0.8)], root=tmp_path)

    text = (tmp_path / ra.TREND_FILENAME).read_text(encoding="utf-8")
    assert h1.run_id in text and h2.run_id in text
    assert "首基" in text and "top_k 15→20" in text
    assert "+0.300" in text  # 0.8 - 0.5


def test_trend可重建_删除run后自动对齐(tmp_path):
    h1 = ra.begin_run("golden", root=tmp_path)
    ra.finish_run(h1, [_entry(cr=0.5)], root=tmp_path)
    h2 = ra.begin_run("golden", root=tmp_path)
    ra.finish_run(h2, [_entry(cr=0.8)], root=tmp_path)

    # 模拟「有人手动删了目录 / 手改坏了 TREND.md」
    (tmp_path / ra.TREND_FILENAME).write_text("手改坏的内容", encoding="utf-8")
    ra.write_trend(tmp_path)
    text = (tmp_path / ra.TREND_FILENAME).read_text(encoding="utf-8")
    assert "手改坏的内容" not in text
    assert h1.run_id in text and h2.run_id in text


# ── 6. 对比纪律 ────────────────────────────────────────────────────────────


def test_对比_Δ超出该格极差才判疑似变化():
    a = _payload("A", cp=0.5, cr=0.5, spread_cr=0.2)
    b = _payload("B", cp=0.5, cr=0.9, spread_cr=0.2)  # Δ=0.4 > 0.2
    text = ra.build_compare(a, b)
    assert "疑似上升" in text


def test_对比_Δ在极差内只记噪声内():
    a = _payload("A", cp=0.5, cr=0.5, spread_cr=0.3)
    b = _payload("B", cp=0.5, cr=0.6, spread_cr=0.3)  # Δ=0.1 < 0.3
    text = ra.build_compare(a, b)
    assert "噪声内" in text
    # 只有「⚠️ 疑似」才是判定出的变化；表头/脚注里的说明文字不算
    assert "⚠️ 疑似" not in text


def test_对比_数据集变了必须挂免责声明():
    a = _payload("A", cp=0.5, cr=0.5, golden="a" * 64)
    b = _payload("B", cp=0.9, cr=0.9, golden="b" * 64)
    text = ra.build_compare(a, b)
    assert "数据集已变" in text


def test_对比_缺格记不可比且不按0补():
    a = _payload("A", cp=0.5, cr=0.5)
    b = _payload("B", cp=0.5, cr=0.5)
    b["cases"]["golden_001"]["rows"] = []  # B 侧缺该格
    text = ra.build_compare(a, b)
    assert "不可比" in text


def test_对比_faithfulness只标护栏不判定():
    a = _payload("A", cp=0.5, cr=0.5)
    b = _payload("B", cp=0.5, cr=0.5)
    b["cases"]["golden_001"]["rows"][0]["faithfulness"] = 0.05  # 大幅下降
    text = ra.build_compare(a, b)
    assert "护栏" in text


def test_对比_材料量变化超过两成要标注():
    a = _payload("A", cp=0.5, cr=0.5, n_contexts=10)
    b = _payload("B", cp=0.5, cr=0.5, n_contexts=4)  # -60%
    text = ra.build_compare(a, b)
    assert "材料量已变" in text


# ── 7. baseline 指针 ───────────────────────────────────────────────────────


def test_baseline设置与解析(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "PROJECT_ROOT", tmp_path)
    h = ra.begin_run("golden", root=tmp_path)
    ra.finish_run(h, [_entry()], root=tmp_path)

    assert ra.read_baseline(tmp_path) is None
    ra.set_baseline(h.run_id, tmp_path)
    assert ra.read_baseline(tmp_path) == h.run_id
    # baseline 关键字解析成具体 run_id
    assert ra.resolve_run_id("baseline", tmp_path) == h.run_id


def test_baseline指向不存在的run要报错(tmp_path):
    try:
        ra.set_baseline("不存在", tmp_path)
    except SystemExit as exc:
        assert "不存在" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当报错")


# ── 8. 单评不进趋势 ────────────────────────────────────────────────────────


def test_task类型不进趋势(tmp_path):
    h = ra.begin_run("task", root=tmp_path)
    entry = _entry(golden_id="task_11111111")
    ra.finish_run(h, [entry], root=tmp_path, cases_requested=1)

    manifest = ra.load_run(h.dir)["manifest"]
    assert manifest["kind"] == "task"
    assert ra.list_runs(tmp_path) == []  # golden 过滤后为空
