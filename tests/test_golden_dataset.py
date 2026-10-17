# -*- coding: utf-8 -*-
"""Golden Dataset 与 reference 解析的守卫测试（纯本地，不依赖 DB / 网络 / LLM）。

═══════════════════════════════════════════════════════════════════════════════
                          【这个文件在钉什么】
═══════════════════════════════════════════════════════════════════════════════

  评估的分数可不可信，取决于三件事：**基准对不对、粒度对不对、退化会不会被静默
  吞掉**。本文件逐条钉死：

  P0-1  分维度标准答案不得是模板占位（"XX 维度关键事实：…等能力"）。
        它是 context_precision 的 ground truth，模板喂给 judge = 没有标准答案。
        整段 reference 零消费，已删除，本文件防它回潮。

  P0-4  reference 解析的**静默失真**：Golden 模式下维度名一旦与 reference_map
        的键对不上（hard 档维度由 LLM 生成，无强一致保证），原实现
        `(reference_map or {}).get(dimension, dimension)` 会静默退化成用
        「维度名」两字当标准答案，分数彻底失真且不报错。
        → `resolve_reference()` 三级解析，任一级缺失都显式 warning。

  P2-1  元描述型标准答案（"…档位差异"、"…的划分及各自母体"）——只说有差异、
        没说差异是什么，等于没有 ground truth。

  cell  评估单元 = (竞品, 维度)，基准必须同粒度：
        · 键集合必须等于 competitors × dimensions（漏一个就回退维度级复合句，
          该 cell 的 cp/cr 天花板重新掉到 0.5）；
        · cell 文本不得出现对方的竞品名（否则判据里又含"按定义不可能被支持"的半句）；
        · 已改造条目必须事实点化（≥3 个「；」事实点）——cr 的分母 = judge 切出的
          句子数，单句 reference 会让 cr 只能取 0/1。

  impl  评估模块不得再有并行实现（2026-10-17 架构简化）：老链路
        ragas_llm / golden_run / runner / judge_eval / cli 已删，不许回潮。

运行方式（项目未装 pytest 也能跑）：
    ./.venv/Scripts/python.exe tests/test_golden_dataset.py
或用 pytest：
    pytest tests/test_golden_dataset.py
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import sys

# 允许 `python tests/test_golden_dataset.py` 直接运行时找到 src 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.evaluation.golden_dataset import (  # noqa: E402
    GOLDEN_DATASET,
    get_reference_by_cell,
)
from src.evaluation.llm_factory import resolve_reference  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 模板占位语特征：出现即说明「不是事实点，而是元描述」
_TEMPLATE_MARKERS = ("关键事实", "维度关键", "应至少说明")

# 元描述信号（软判据）：与 docs/golden_dataset_audit.py 的 META_MARKERS 同源。
# 仅当「命中 + 文本短于 _META_MAX_LEN」才判元描述——避免误伤
# 「X 与 Y 的差异在于 A、B」这类合法的事实陈述。
_META_MARKERS = ("差异", "划分", "区别", "及各自", "母体", "市占")
_META_MAX_LEN = 42


def _capture_warnings(fn, *args, **kwargs):
    """调用 fn 并捕获 src.evaluation.llm_factory 的 WARNING 级日志记录。

    手写 handler 而不用 pytest 的 caplog —— 本文件要支持
    `python tests/test_golden_dataset.py` 无 pytest 直跑。
    返回 (返回值, [已格式化的告警文本...])。
    """
    import logging

    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):  # noqa: D102 —— Handler 必须实现
            records.append(record.getMessage())

    lg = logging.getLogger("src.evaluation.llm_factory")
    old_level = lg.level
    h = _H(level=logging.WARNING)
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)
    try:
        out = fn(*args, **kwargs)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
    return out, records


# ─────────────────────────────────────────────────────────────────────────────
# 1. 标准答案的内容质量（不得模板化 / 不得是元描述）
# ─────────────────────────────────────────────────────────────────────────────

def test_回归_每条分维度标准答案都不得模板化():
    """P0-1：reference_by_dimension 是 context_precision 唯一的 ground truth。

    ① 不得出现「XX 维度关键事实：…等能力」这类模板占位（等于没有标准答案）；
    ② 不得过短（信息量不足的短语撑不起 judge 判断）。
    """
    for case in GOLDEN_DATASET:
        rbd = case["reference_by_dimension"]
        # 前提：维度键已与 dimensions 严格对齐（下面的 cell 测试覆盖）
        assert set(rbd.keys()) == set(case["dimensions"]), (
            f"{case['task_id']} 维度键不对齐"
        )
        for dim, txt in rbd.items():
            marks = [m for m in _TEMPLATE_MARKERS if m in txt]
            assert not marks, (
                f"{case['task_id']} 维度「{dim}」仍是模板占位（命中 {marks}）：{txt[:40]}…"
            )
            assert len(txt) >= 20, (
                f"{case['task_id']} 维度「{dim}」标准答案过短（{len(txt)} 字），撑不起打分"
            )


def test_回归_分维度标准答案不得是元描述():
    """P2-1：标准答案必须是「事实点」，不能是「只说有差异/有划分」的元描述。

    判据与 docs/golden_dataset_audit.py 一致（软标记 + 短文本双条件）：
    单看「差异/划分」会误伤合法正文（「X 与 Y 的差异在于 A、B」是事实陈述），
    故要求同时满足「文本短于 _META_MAX_LEN」才判为元描述。
    """
    bad_all: list[str] = []
    for case in GOLDEN_DATASET:
        for dim, txt in case["reference_by_dimension"].items():
            hits = [m for m in _META_MARKERS if m in txt]
            if hits and len(txt) < _META_MAX_LEN:
                bad_all.append(f"{case['task_id']}「{dim}」命中{hits} 仅{len(txt)}字：{txt}")
    assert not bad_all, (
        "以下分维度标准答案是元描述（只说有差异/划分，没说差异内容），"
        "喂给 judge 等于没有 ground truth：\n  " + "\n  ".join(bad_all)
    )


def test_回归_整段reference字段已彻底移除():
    """零消费的整段 `reference` 已于 2026-10-13 连同 get_reference() 一并删除。

    它讲的是「该题评分要点」（rubric 语义），与分维度的事实级 ground truth
    层级不同、且已漂移，留着只会让「无死字段」的设计原则名实不符。
    """
    for case in GOLDEN_DATASET:
        assert "reference" not in case, (
            f"{case['task_id']} 仍带整段 reference 字段（已删除，不应回潮）"
        )

    import src.evaluation.golden_dataset as gd
    assert not hasattr(gd, "get_reference"), (
        "golden_dataset 仍导出 get_reference()，应随整段 reference 一并删除"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. cell 级标准答案（评估单元 = (竞品, 维度)）
# ─────────────────────────────────────────────────────────────────────────────

def test_get_reference_by_cell_命中与未命中():
    m = get_reference_by_cell("golden_001")
    assert set(m) == {"飞书@功能", "钉钉@功能", "飞书@定价", "钉钉@定价"}
    assert get_reference_by_cell("golden_999") == {}


def test_cell级标准答案_覆盖全部竞品维度组合():
    """守卫：pipeline 档（competitors 非空）必须为每个 (竞品, 维度) 给出 cell 级答案。

    漏一个 cell 就会被 resolve_reference 回退到维度级复合句 → 该 cell 的
    cr 天花板重新掉回 0.5。故把「键集合必须等于 competitors × dimensions」钉成测试。
    """
    for case in GOLDEN_DATASET:
        if not case["competitors"]:
            continue  # hard 档：competitors 为空，数据期无法枚举 cell 键
        expected = {f"{c}@{d}" for c in case["competitors"] for d in case["dimensions"]}
        got = set(case.get("reference_by_cell") or {})
        assert got == expected, (
            f"{case['task_id']} cell 键不一致：缺 {sorted(expected - got)}，"
            f"多 {sorted(got - expected)}"
        )


def test_cell级标准答案_不混入对方竞品():
    """守卫：cell = 单竞品 → 同一维度的 cell 文本不得出现其它竞品的名字。

    这是「基准粒度必须与评估单元对齐」的实质检查：一旦混入对方竞品，
    该 cell 的判据里就又出现了「按定义不可能被支持」的那半句。
    """
    for case in GOLDEN_DATASET:
        cell = case.get("reference_by_cell") or {}
        for key, text in cell.items():
            owner, _, dim = key.partition("@")
            others = [c for c in case["competitors"] if c and c != owner]
            if not others:  # hard 档：用同维度其它 cell 的 owner 当同侪
                others = [
                    k.partition("@")[0]
                    for k in cell
                    if k.partition("@")[0] != owner and k.partition("@")[2] == dim
                ]
            for other in others:
                assert other not in text, (
                    f"{case['task_id']} {key} 混入对方竞品 {other!r}：{text}"
                )


def test_cell级标准答案_已改造条目必须事实点化():
    """守卫：cr 的分辨率靠 reference 里的「；」数量撑起来，不许被改回单句。

    机制（源码 `_context_recall.py`）：分母 = judge 切出的句子数，且
    `generate_multiple(n=1)` 不投票。单句 reference → 分母恒为 1 → cr 取值域
    只有 {0,1}，judge 抖一次整格翻转（实测 `[0,1,0]`）。2026-10-17 把
    golden_001/002 的 8 个 cell 全部改成 ≥3 个独立事实点后，cr 才第一次能
    出现 0.25/0.5/0.75 的中间台阶。

    003~010 尚未改造，故用白名单而不是全局断言；每改造一条就往集合里加 id。
    """
    dotted = {"golden_001", "golden_002"}
    for case in GOLDEN_DATASET:
        if case["task_id"] not in dotted:
            continue
        for key, text in (case.get("reference_by_cell") or {}).items():
            points = [p for p in text.split("；") if p.strip()]
            assert len(points) >= 3, (
                f"{case['task_id']} {key} 只有 {len(points)} 个事实点（需 ≥3），"
                f"分母过小会让 cr 退回 0/1 二值：{text}"
            )
            assert all(len(p.strip()) >= 4 for p in points), (
                f"{case['task_id']} {key} 存在过短事实点（撑不起 judge 判定）：{text}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. reference 三级解析（防静默失真）
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_reference_维度名匹配时取标准答案():
    """P0-4 正常路径：维度名命中 → 返回该维度的标准答案。"""
    rm = {"功能": "事实A", "定价": "事实B"}
    assert resolve_reference("功能", rm) == "事实A"
    assert resolve_reference("定价", rm, "golden_005") == "事实B"


def test_resolve_reference_维度名不匹配时告警():
    """P0-4 核心：Golden 模式下维度名对不上，**必须告警**且不得静默。

    若不告警，会退化成拿「功能」两字当 ground truth，分数失真且无迹可循。
    """
    out, warns = _capture_warnings(resolve_reference, "产品功能", {"功能": "事实A"}, "golden_008")
    # 仍返回维度名兜底（不抛错，避免整条评估链断掉），但必须留下告警
    assert out == "产品功能"
    assert warns, "维度名对不上 reference_map 时未告警 → 静默失真"
    assert "标准答案缺失" in warns[0]
    assert "产品功能" in warns[0] and "功能" in warns[0]


def test_resolve_reference_真实任务不告警():
    """P0-4 真实任务：reference_map 为 None 时用维度名兜底是设计本意，不告警。"""
    out, warns = _capture_warnings(resolve_reference, "功能", None, "real-task-uuid")
    assert out == "功能"
    assert not warns, f"真实任务（无标准答案）不应告警，实际：{warns}"
    # 命中时同样不告警
    out2, warns2 = _capture_warnings(resolve_reference, "功能", {"功能": "事实A"})
    assert out2 == "事实A" and not warns2


def test_resolve_reference_优先cell级():
    out = resolve_reference(
        "功能",
        {"功能": "飞书以协同见长；钉钉以审批见长"},
        "golden_001",
        competitor="飞书",
        reference_cell_map={"飞书@功能": "飞书以协同见长"},
    )
    assert out == "飞书以协同见长"


def test_resolve_reference_cell缺失告警并回退维度级():
    out, warns = _capture_warnings(
        resolve_reference,
        "功能",
        {"功能": "维度级复合句"},
        "golden_001",
        competitor="钉钉",
        reference_cell_map={"飞书@功能": "只对飞书"},
    )
    assert out == "维度级复合句"
    assert warns, "cell 级缺失必须显式告警（不得静默回退）"
    assert "cell" in warns[0]


def test_resolve_reference_不传cell表时行为与改动前一致():
    """向后兼容：不传 cell 表 → 逐字走维度级。"""
    out, warns = _capture_warnings(resolve_reference, "功能", {"功能": "事实A"})
    assert out == "事实A"
    assert warns == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. 架构守卫：评估模块不得再有并行实现
# ─────────────────────────────────────────────────────────────────────────────

def test_回归_评估模块不得有并行实现():
    """2026-10-17 架构简化：老链路五件套已删，不许回潮。

    被删的都是「从未真正投入使用」的工程外壳，留着会让
    「改一处算法要动六个文件」的老问题复活（详见 __init__.py 说明）。
    """
    legacy = ["ragas_llm.py", "golden_run.py", "runner.py", "judge_eval.py", "cli.py"]
    alive = [
        name for name in legacy
        if os.path.exists(os.path.join(_REPO_ROOT, "src", "evaluation", name))
    ]
    assert not alive, (
        f"评估模块又出现了并行实现：{alive}。"
        "四指标只有 ragas_core.py 一个实现，judge 构造只在 llm_factory.py。"
    )
    # ragas_core 之外的模块不得再 import ragas 的指标（防止再造一份四指标）
    ragas_core = os.path.join(_REPO_ROOT, "src", "evaluation", "ragas_core.py")
    assert os.path.exists(ragas_core), "ragas_core.py 是四指标唯一实现，不应被删除"


# ─────────────────────────────────────────────────────────────────────────────
# 无 pytest 时的兜底运行器
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cases = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001 —— 测试运行器需要捕获全部异常
            failed += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    sys.exit(1 if failed else 0)
