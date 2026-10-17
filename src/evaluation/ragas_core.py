# -*- coding: utf-8 -*-
"""RAGAS 四指标评估（唯一实现）。

═══════════════════════════════════════════════════════════════════════════════
                           【目标形态】
═══════════════════════════════════════════════════════════════════════════════

  一次 `aevaluate` 跑齐四个 ragas 指标，输出「分数表」，不落库、不判门禁、
  不归档、不 diff。四个指标落在同一行 (竞品@维度) 样本上：

    - 检索层：context_precision / context_recall
    - 生成层：faithfulness / answer_relevancy

  context_recall 当前为「先实测再定」：跑出首次数字后，按三种结果决定去留
  （NaN=技术层失败可修；稳定低分=有据弃用；合理分=保留）。

  【2026-10-17 起本模块是唯一的评估实现】老链路（ragas_llm.py / golden_run.py /
  runner.py / judge_eval.py / cli.py）已整体删除，不再有并行实现。
  judge / embeddings / RunConfig 的构造与 reference 解析抽在 llm_factory.py，
  本模块只依赖它，不依赖任何落库 / 上报 / 归档逻辑。

═══════════════════════════════════════════════════════════════════════════════
                    【为什么要有这个模块（它在治什么病）】
═══════════════════════════════════════════════════════════════════════════════

  被删掉的旧链路，问题不是「指标变多」，而是「指标定义」和「工程外壳」焊死在一起：
  改一个指标的算法，要同步改落库 metadata、Grafana 上报、baseline 渲染、diff 列
  —— 一个函数干六件事。更麻烦的是四个指标分散在两个函数、两个数据源上，
  根本没法在同一行样本上对齐看：

    旧 · context_precision      ← retrieval_snapshot.reranked_texts（检索原始 chunk）
    旧 · faithfulness           ← evidence.source_text（LLM 质量 Agent 二次落库的产物）
    旧 · answer_relevancy       ← 整篇报告正文（response）
    旧 · (context_recall 缺席，被规则式 source_coverage 顶替)

  于是本模块只做一件事：把 (竞品@维度) 展开成 N 行样本，一行样本同时喂四个指标，
  **同一份 retrieved_contexts + 同一个 response + 同一个 reference**。

  ── 数据流（本模块内部）────────────────────────────────────────────────
    retrieval_snapshot 表（analyzer Step 5.5 落盘，成稿前）
        │  ├─ dimension / competitor / query（="竞品 维度"）
        │  ├─ reranked_texts（精排 TopK 原始 chunk）→ retrieved_contexts
        │  └─ response_text（该维度 LLM 结论，回填）→ response
        ▼
    build_samples()  ──►  list[SampleRow]（纯函数，可离线测）
        │
        ▼
    evaluate_four()  ──►  一次（或两次）aevaluate ──►  FourMetricResult
        │                    ├─ 有 response 样本：四指标全跑
        │                    └─ 无 response 样本：只跑检索层 cp / cr
        ▼
    render_markdown() / to_dict()  ──►  分数表（人读 / JSON）

  reference 由 Golden 的 `reference_by_cell["竞品@维度"]` 提供
  （`get_reference_by_cell`）——**评估单元是 (竞品, 维度)，基准必须同粒度**。
  取不到 cell 级时回退维度级 `reference_by_dimension`（跨竞品复合句），
  再回退维度名；三级回退均在 llm_factory.resolve_reference 里显式告警。
  ⚠️ 用维度级复合句当基准时 cr 天花板约 0.5（每个 cell 有一半讲对方竞品），
  2026-10-16 实测 `飞书@功能 cr=0.500` 即此。

═══════════════════════════════════════════════════════════════════════════════
                        【四个指标各自的输入契约】
═══════════════════════════════════════════════════════════════════════════════

  指标                    | 需要字段                                       | 本模块的数据来源
  ────────────────────────┼────────────────────────────────────────────────┼──────────────────────────
  context_precision (检索)| user_input + retrieved_contexts + reference     | query / reranked_texts / reference
  context_recall    (检索)| user_input + retrieved_contexts + reference     | 同上（★与 cp 同源，故能一行同评）
  faithfulness      (生成)| user_input + response + retrieved_contexts      | + response_text
  answer_relevancy  (生成)| user_input + response（+ embeddings）           | + response_text

  ★ 关键洞察：cp 与 cr 的 required_columns 完全相同（都不需要 response）。
    这直接证伪了旧 docstring 里「没有整段 reference 列 → context_recall 喂不进去」
    的理由① —— 既然 reference_by_dimension 能当 cp 的 reference，就能当 cr 的 reference。
    「先实测再定」正是为了用一次真跑，把「cr 恒判 0」从**推断**升级为**实测结论**。

═══════════════════════════════════════════════════════════════════════════════
                          【四指标的口径（读分数前必看）】
═══════════════════════════════════════════════════════════════════════════════

  本模块的数据源是 `retrieval_snapshot`，口径固定为：

    · retrieved_contexts = reranked_texts（精排后的原始 chunk）
    · response           = 该 (竞品, 维度) 的**维度级结论**，不是整篇报告
    · reference          = Golden 的 reference_by_cell["竞品@维度"]

  ⚠️ 这决定了两件事：
    1. faithfulness 测的是「这条维度结论有没有越出它自己的 contexts」，
       不是「整篇报告有没有幻觉」。它只当护栏，不能当分析质量分——
       复述得分、归纳扣分，而归纳正是分析的价值所在。
    2. cp / cr 的分母随该 cell 的 n_contexts 变化（3 条和 15 条不可直接比）。
       所以每行分数都必须带着 n_contexts 一起读。

  真实任务（非 Golden）没有 reference，cp / cr 会退化为「维度名当 ground truth」，
  分数无参照系、只能看相对变化——这是由评估本身的语义决定的，不是实现缺陷。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import types
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

# ── monkey-patch：ragas 0.4.3 顶层 import 了不存在的 vertexai 模块 ──
# 幂等补丁，必须在 import ragas 之前注入；llm_factory.py 里也有一份同款。
# 为什么幂等：两个模块可能先后被 import，用 `not in sys.modules` 守卫，
# 保证占位模块只注入一次（谁先 import 谁注入，后来者跳过）。
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _vertexai_stub = types.ModuleType("langchain_community.chat_models.vertexai")

    class _ChatVertexAI:  # 占位类：仅满足 isinstance 检查，本项目不用 Vertex AI
        pass

    _vertexai_stub.ChatVertexAI = _ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = _vertexai_stub

#    这里刻意用「旧式指标」（ragas.metrics 顶层 / _ 下划线模块），而不是
#    ragas.metrics.collections 的新式指标。原因见 llm_factory.py 模块 docstring 的
#    「两个 ragas 0.4.3 兼容坑」第 2 条：aevaluate() 的 isinstance 检查只认旧式
#    Metric，collections 指标会报 "All metrics must be initialised"。
#    代价：`from ragas.metrics import context_precision` 会触发 DeprecationWarning
#    （提示迁移到 collections）——这是可接受的，因为迁移过去反而跑不起来。
from ragas import EvaluationDataset, SingleTurnSample, aevaluate  # noqa: E402
from ragas.metrics import context_precision, context_recall  # noqa: E402
from ragas.metrics._answer_relevance import answer_relevancy  # noqa: E402
from ragas.metrics._faithfulness import faithfulness  # noqa: E402

# ── 复用旧模块的「纯函数 / 构造器」，不引入其落库与编排 ──
#   build_judge_llm     : 构造 DeepSeek judge（LangchainLLMWrapper 包装）
#   build_embeddings     : 构造 BGE-M3 embeddings（answer_relevancy 需要）
#   build_run_config     : 构造 ragas RunConfig（超时/并发，两套链路共用一份口径）
#   resolve_reference    : cell → ground truth（优先 cell 级，回退维度级/维度名，
#                           任一级对不上都显式告警）
#   load_json_list       : 反序列化 JSONB 字段（asyncpg 可能返回 str）
#   JUDGE_MODEL           : judge 模型名常量（与生成模型隔离）
from src.evaluation.llm_factory import (  # noqa: E402
    JUDGE_MODEL,
    build_embeddings,
    build_judge_llm,
    build_run_config,
    load_json_list,
    resolve_reference,
)

if TYPE_CHECKING:
    # 仅类型标注用，运行时不导入，避免与 src.db 形成循环依赖。
    from src.db.dao import TaskDAO

logger = logging.getLogger(__name__)

# 四指标的名字与顺序。
# 为什么用常量元组：行字典、聚合、渲染三处必须用**同一个**指标名单和顺序，
# 否则会出现「聚合时漏了某个指标」「表格列和字典键对不上」这类静默错位。
_METRIC_NAMES = (
    "context_precision",
    "context_recall",
    "faithfulness",
    "answer_relevancy",
)


@dataclass(frozen=True)
class SampleRow:
    """一条 (竞品@维度) 评估样本的输入与元信息（未打分）。

    「未打分」是刻意的分层：build_samples() 只负责把数据库快照**翻译**成
    ragas 能吃的样本，不碰任何 LLM —— 这样它可以被单元测试离线覆盖
    （见 tests/test_ragas_core.py），打分失败也不会污染样本构造逻辑。

    为什么 frozen：样本一旦构造完成，四个指标读的是同一份不可变输入，
    避免某个指标的计算过程「就地改样本」导致其余指标读到脏数据。
    """

    key: str                 # 打分键 = "竞品@维度"（无竞品时退化为维度名），唯一标识该行
    competitor: str          # 竞品名（可能为空字符串，历史维度级快照无此字段）
    dimension: str           # 维度名
    user_input: str          # = 快照 query（"竞品 维度"），与线上精排 rerank 同尺
    response: str | None     # 该维度的 LLM 结论；历史快照可能为空 → None（生成层不跑）
    retrieved_contexts: list[str]  # 精排后 TopK 的原始 chunk（过滤掉 <10 字符的）
    reference: str           # 该 cell ground truth（优先 reference_by_cell["竞品@维度"]，回退维度级）
    n_contexts: int          # 本行实际参与评分的 context 条数 = cp/cr 的**分母**


@dataclass
class FourMetricResult:
    """一次四指标评估的输出：逐样本分数 + 均值 + 分母分布。

    这里把「分数」与「分母」放在同一个结构体里，而不是像旧链路那样把
    n_contexts 散落到 result 字典 / 落库 metadata / Grafana 上报 / 表格单元格 /
    diff 列五个地方。理由：context_precision 是对**实际检索到的那几条**求
    Average Precision，分母会波动（实测 1~8），「分母小 ⇒ 分数系统性偏高」。
    一个指标要拆成「分数 + 分母」两个数、靠纪律保证同时出现，就说明它本该是
    一个复合值 —— 本结构体就是那个复合值。
    """

    task_id: str
    # 逐样本分数行：每行 {"key","competitor","dimension","n_contexts", <四个指标…>}
    rows: list[dict] = field(default_factory=list)
    # {指标名: 均值}；无有效分数的指标为 None（严格忽略 None/NaN，不静默计 0）
    aggregates: dict[str, float | None] = field(default_factory=dict)
    # cp/cr 分母的分布：{"avg": x, "min": n, "max": n}（跨任务比较前必看）
    n_contexts: dict[str, float | int | None] = field(default_factory=dict)
    # 被跳过的快照数（无有效上下文，未能构成样本）
    skipped: int = 0
    # 每个 (样本,指标) 实际跑了多少轮、分数取均值（1 = 单轮，与旧行为等价）。
    # 必须随分数一起暴露：repeat=1 与 repeat=3 的分数期望与方差不同，不标注会让
    # 「两轮 cr 从 0.5 变 1.0」被误读成回归。
    repeat: int = 1
    # 每指标的跨样本极差（max-min）；repeat=1 时为 None。
    # 用途：均值告诉你「期望多少」，极差告诉你「这个数有多可信」。
    # cr 的极差常为 1.000（二值抖动），此时均值只能当「支持率」读，不能当精确度。
    repeat_spread: dict[str, float | None] = field(default_factory=dict)


def build_samples(
    snapshots: list[dict],
    reference_map: dict[str, str] | None = None,
    task_id: str = "",
    reference_cell_map: dict[str, str] | None = None,
) -> list[SampleRow]:
    """把 retrieval_snapshot 行展开为 (竞品@维度) 评估样本（纯函数）。

    - user_input = 快照 query（= 线上精排 query「竞品 维度」）
    - retrieved_contexts = reranked_texts 中 ≥10 字符的原始 chunk
    - reference = **优先 cell 级** reference_cell_map["竞品@维度"]（只讲该竞品），
      缺失回退 reference_map[dimension]（维度级复合句），再缺省退化用维度名
    - response = response_text（细粒度维度结论；历史快照可能为空）
    - n_contexts = 实际参与评分的 context 条数（cp/cr 的分母）

    【为什么 reference 首选 cell 级，而不是维度级】
      评估单元是 (竞品, 维度)，而 `reference_map`（= Golden 的
      reference_by_dimension）是**维度级、跨竞品复合**（"飞书…；钉钉…"）。
      把它喂给某个 cell，判据里必然有一半讲的是**对方竞品**、按定义不可能
      被支持 ⇒ **context_recall 天花板被钉死 0.5**。
      2026-10-16 实测：`飞书@功能 cr=0.500` = 正好"半句"，即此机制的直接证据。
      故基准粒度必须与评估单元对齐 → 传 `reference_cell_map`
      （= `get_reference_by_cell(golden_id)`）。

    【为什么 user_input 要用「快照 query」而不是「维度名」】
      context_precision / context_recall 都在判「检索到的 context 是否支撑
      该 ground truth」，而线上精排 reranker 的 query 是 `"竞品 维度"`。
      评估时如果只用维度名当 query，等于拿「维度」这把尺子去量一条用
      「竞品 维度」尺子排出来的结果，两把尺子错配 → 分数不可比。
      快照里已把 query 原样落盘，直接取用即可保持同尺。

    【为什么 response 可能为 None】
      response_text 是 analyzer 成功路径**回填**的（见 analyzer.py Step 5.5）。
      历史快照（该列刚加、或分析失败）没有值。此时生成层指标无从评起，
      但检索层 cp/cr 与 response 无关 —— 所以这里不丢样本，只把 response 置 None，
      由 evaluate_four 把样本分流到「只跑检索层」的分支。
    """
    rows: list[SampleRow] = []
    for snap in snapshots:
        # 用 .get() 而非 snap["dimension"]：历史 / 异常快照可能缺字段，
        # 缺字段时宁可退化成空串也不要 KeyError 打断整批评分。
        dimension = snap.get("dimension") or ""
        competitor = snap.get("competitor") or ""

        # 【命名对照】DB 列名仍是 query / reranked_texts / response_text（历史名，未随改），
        # 这里立刻转成 RAGAS 官方字段名 user_input / retrieved_contexts / response，
        # 让下游（_to_ragas_sample → SingleTurnSample）逐字对齐、无需再做心智翻译。
        # 快照 query 已落为 "竞品 维度"；老快照可能为空 → 退化为维度名。
        user_input = snap.get("query") or dimension

        # reranked_texts 是 JSONB：asyncpg 读出来可能是 str（需反序列化）也可能是 list。
        retrieved_contexts = load_json_list(snap.get("reranked_texts"))
        # 过滤 <10 字符的碎片：导航文本、单字标题等既不含事实也无法被 judge 判读，
        # 留着只会虚增分母（让 cp 的 AP 分母变大、分数被系统性压低）。
        retrieved_contexts = [
            c for c in retrieved_contexts if c and len(str(c).strip()) >= 10
        ]
        if not retrieved_contexts:
            # 没有有效上下文 → 构不成评估样本，跳过（计入 skipped），不报错。
            logger.info(
                "跳过无上下文快照 task_id=%s dimension=%r competitor=%r",
                task_id[:8] if task_id else "-", dimension, competitor or "-",
            )
            continue

        # 取该 cell 的 ground truth：优先 cell 级（单竞品），回退维度级；
        # 两级键都对不上时 resolve_reference 会显式告警（不静默退化）。
        reference = resolve_reference(
            dimension,
            reference_map,
            task_id,
            competitor=competitor,
            reference_cell_map=reference_cell_map,
        )
        # response_text 可能为 None / 非字符串 → 统一成「strip 后的非空字符串或 None」。
        response_text = snap.get("response_text") or ""
        response = response_text.strip() if isinstance(response_text, str) else ""

        # 打分键：含竞品时用 "竞品@维度"（保证同维度多竞品不互相覆盖）；
        # 无竞品的历史维度级快照退化为纯维度名。
        key = f"{competitor}@{dimension}" if competitor else dimension
        rows.append(
            SampleRow(
                key=key,
                competitor=competitor,
                dimension=dimension,
                user_input=user_input,
                response=response or None,
                retrieved_contexts=retrieved_contexts,
                reference=reference,
                n_contexts=len(retrieved_contexts),
            )
        )
    return rows


def _to_ragas_sample(row: SampleRow) -> SingleTurnSample:
    """SampleRow → ragas 单轮样本（四指标共用的同一行）。"""
    return SingleTurnSample(
        user_input=row.user_input,
        retrieved_contexts=row.retrieved_contexts,
        reference=row.reference,
        response=row.response,
    )


def _fmt(value) -> str:
    """分数渲染：None / NaN 统一为「—」，否则保留 3 位小数。

    【为什么 None 和 NaN 要渲染成同一个符号「—」】
      对读者而言两者语义相同：「这一格没有可用分数」。NaN 是 ragas 内部
      拆句失败 / 请求失败时的产物，None 是我们主动跳过的产物 —— 区分它们
      只对调试有意义，对基线表没意义，且 NaN 直接打印会变成 "nan" 这种
      非分数噪音。
    """
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(num):
        return "—"
    return f"{num:.3f}"


def _aggregate_scores(rows: list[dict]) -> dict[str, float | None]:
    """各指标有效分数均值（忽略 None / NaN，不静默计入）。

    【为什么必须忽略而不是按 0 计】
      把「拿不到分」按 0 计入均值，会得出一个「看起来很低」但并不存在的分数
      —— 读者无法区分「真的差」和「没测到」。忽略后若全部为空则返回 None，
      表里显式显示「—」，把信息缺失暴露出来而不是抹平。
    """
    buckets: dict[str, list[float]] = {name: [] for name in _METRIC_NAMES}
    for row in rows:
        for name in _METRIC_NAMES:
            value = row.get(name)
            if value is None:
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if math.isnan(num):
                continue
            buckets[name].append(num)
    return {
        name: (round(sum(values) / len(values), 3) if values else None)
        for name, values in buckets.items()
    }


def _mean_score(values: list[float | None]) -> float | None:
    """多轮重复打分取均值（忽略 None / NaN）；无有效值返回 None。

    【为什么是均值而不是中位数（2026-10-17 改，推翻原实现）】
      原来用中位数，理由是「judge 非确定性产生的偏差是离群」。实测证伪了这一点：
      cr 的方差不是离群，而是**二值抖动** —— 同一个 cell 三轮跑出 [0, 1, 0]
      （2026-10-17 `ragas_repeat_probe`：钉钉@定价 / 钉钉@功能 均为此形态）。

      对这种形态：
        · 中位数 [0,1,0] → 排序 [0,0,1] → **0**，把「1/3 的支持率」直接抹平成一个 bit，
          信息全丢；
        · 均值 [0,1,0] → **0.333**，正是「3 轮里 1 轮判定被支持」的真实比例。

      ⇒ 二值/比例型指标（cr、faithfulness）必须看均值；连续型（cp、ar）用均值也
        无损（REPEAT 的目的是估计期望值，均值即无偏估计）。
        稳定性另看极差：`FourMetricResult.repeat_spread` 给出每指标 max-min。

    【repeat=1 时与改动前逐位等价】
      单元素的均值就是该元素本身，且取整位数相同（round(value, 3)），
      故默认不重复时行为不变。
    """
    valid: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(num):
            continue
        valid.append(num)
    if not valid:
        return None
    return round(sum(valid) / len(valid), 3)


async def evaluate_four(
    samples: list[SampleRow],
    settings,
    task_id: str = "",
    repeat: int = 1,
) -> FourMetricResult:
    """一次（或 repeat 次）aevaluate 跑齐四指标（不落库）。

    - 有 response 的样本：四指标全跑。
    - 无 response 的历史快照：只跑检索层 cp / cr，生成层留「—」。

    【为什么要按 response 有无分成两批，而不是一批全跑四指标】
      faithfulness / answer_relevancy 都必须吃 response。若把所有样本混成一批
      提交四指标，ragas 会对「response 为 None」的样本生成层返回 NaN/报错，
      白白把生成层的 LLM 调用（faithfulness 要拆句、answer_relevancy 要反推
      问题）浪费在无意义的样本上。分两批 = 生成层只对真正有结论的样本花钱。
      （代价：理想中「一次 aevaluate」在最坏情况下变成两次 —— 但两次都是
       批量调用，相对旧链路「逐 (竞品,维度) 循环各调一次」仍是数量级收敛。）

    【repeat 参数：压 judge 方差，代价是成本线性 ×N】
      n=4 的样本量下，judge 的非确定性大于指标效应本身（同输入两轮 cr 可差
      0.5），单轮分数不具备可读性。repeat>1 时每个 (样本,指标) 收 N 个分数
      取**均值**（见 _mean_score）。默认 1 = 与改动前逐位等价；
      要压方差必须显式传（CLI `--repeat 3`），因为它把 LLM 成本乘以 N。

    【⚠️ 全局单例：本函数会就地改写 ragas 的模块级指标对象】
      ragas 的旧式指标是**预实例化单例**，llm/embeddings 必须就地赋值（不能传参）。
      后果：任何 import 方共享同一份状态，测试无法隔离，并发调用会互相踩。
      ragas 旧式指标是**预实例化单例**，llm/embeddings 只能就地赋值、无法传参，
      故本函数会改写模块级状态（测试里需 monkeypatch 隔离）。已知坏味道，不在指标层翻修。
    """
    result = FourMetricResult(task_id=task_id)
    if not samples:
        # 空样本直接返回，避免无谓地构造 LLM/embeddings（构造本身会建 HTTP 客户端）。
        return result

    repeat = max(1, int(repeat or 1))
    result.repeat = repeat
    # run_config 必须在 aevaluate 显式传入，否则回落到 ragas 默认
    # RunConfig(timeout=180)：长 contexts 样本会被判超时 → 整格 NaN。
    run_config = build_run_config(settings)

    # judge / embeddings 各构造一次，四个指标共用（embedding 只有 answer_relevancy 需要）。
    judge = build_judge_llm(settings)
    embeddings = build_embeddings(settings)

    # ragas 旧式/顶层指标均为全局单例，就地赋值（与旧模块一致）。
    # judge 统一用 deepseek-chat，四个指标同一把尺子，分数口径一致。
    context_precision.llm = judge
    context_recall.llm = judge
    faithfulness.llm = judge
    answer_relevancy.llm = judge
    answer_relevancy.embeddings = embeddings
    # strictness 必须为 1：DeepSeek 不支持 n>1，否则 answer_relevancy 恒 NaN。
    # （ragas 把 strictness 当 `n` 透传给 LLM；n>1 → 400 Bad Request →
    #  内部重试耗尽 → 恒 NaN。实测 strictness=3 耗时 48.7s 后失败，
    #  =1 后 11.7s 返回有效分。）
    answer_relevancy.strictness = 1

    # 按 response 有无分流（理由见 docstring）。
    with_response = [s for s in samples if s.response]
    without_response = [s for s in samples if not s.response]

    # key → {指标名: [每轮分数]}；repeat 轮跑完后逐指标取中位（_median_score）。
    # 结构从「单值」升级为「列表」，是为了让「重复→中位」这件事只发生在**回填**这一步，
    # 各轮之间互不覆盖（若仍用单值 dict，后一轮会静默盖掉前一轮）。
    scores_by_key: dict[str, dict[str, list[float | None]]] = {}

    async def _run(subset: list[SampleRow], metrics: list) -> None:
        """对一批样本跑 repeat 轮同一组指标，逐轮把分数追加进 scores_by_key。

        内嵌闭包而非独立函数：它直接消费外层的 task_id / scores_by_key，
        抽出去反而要多传参、且只有本函数用。
        """
        if not subset:
            return
        metric_names = [m.name for m in metrics]
        for round_index in range(repeat):
            # 每轮重建 dataset：aevaluate 可能消费 / 改写 dataset 内部状态，
            # 复用同一实例在第二轮有踩脏数据的风险，重建成本可忽略。
            dataset = EvaluationDataset(samples=[_to_ragas_sample(s) for s in subset])
            logger.info(
                "开始 aevaluate task_id=%s 轮次=%d/%d 样本数=%d 指标=%s timeout=%ss",
                task_id[:8] if task_id else "-", round_index + 1, repeat,
                len(subset), ",".join(metric_names), run_config.timeout,
            )
            output = await aevaluate(
                dataset=dataset, metrics=metrics, run_config=run_config
            )
            frame = output.to_pandas()
            if frame.empty:
                # 空结果：本轮所有指标记 None（不写入分数），回填时按「无有效值」处理。
                for sample in subset:
                    bucket = scores_by_key.setdefault(
                        sample.key, {name: [] for name in _METRIC_NAMES}
                    )
                    for name in metric_names:
                        bucket[name].append(None)
                continue

            # ⚠️ 假设 ragas 的 to_pandas() **保持输入顺序**，故用 iloc[index] 按位对齐。
            #    这是 ragas 的既有行为；若未来 ragas 改变行序，此处需要改为按行内容匹配。
            for index, sample in enumerate(subset):
                bucket = scores_by_key.setdefault(
                    sample.key, {name: [] for name in _METRIC_NAMES}
                )
                for name in metric_names:
                    if name not in frame.columns:
                        # 该批没跑这个指标（例如检索批没有 faithfulness 列）→ 记 None。
                        bucket[name].append(None)
                        continue
                    raw = frame.iloc[index][name]
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        value = None
                    if value is not None and math.isnan(value):
                        # NaN 不是合法 JSON，且写 float 列会报错 —— 统一归 None，
                        # 并打 warning 留下「这里丢过一个分」的痕迹（不静默）。
                        # ⚠️ NaN 常见成因是 run_config.timeout 被撞穿（长 contexts 样本
                        #    串行判定），故把分母一并打出来，方便判断是不是这个原因。
                        logger.warning(
                            "指标 %s 返回 NaN key=%s contexts=%d 轮次=%d/%d "
                            "（若伴随 TimeoutError，调大 RAGAS_EVAL_TIMEOUT）",
                            name, sample.key, sample.n_contexts,
                            round_index + 1, repeat,
                        )
                        value = None
                    elif value is not None:
                        value = round(value, 3)
                    bucket[name].append(value)

    # 第一批：四指标全跑（有 response）。
    await _run(
        with_response,
        [context_precision, context_recall, faithfulness, answer_relevancy],
    )
    # 第二批：只跑检索层两指标（无 response 的历史快照，不浪费生成层 LLM 调用）。
    await _run(without_response, [context_precision, context_recall])

    # 按原始 samples 顺序回填 rows（保证输出顺序 = 样本顺序，便于逐条比对）。
    # 每个指标取 repeat 轮的**均值**（repeat=1 时即该值本身，行为与改动前等价）。
    # 均值而非中位：cr/faithfulness 是比例型，[0,1,0] 的真实含义是 1/3 支持率，
    # 中位会把它抹成 0（详见 _mean_score docstring）。
    # ⚠️ scores_by_key 以 sample.key 为键：若两行样本 key 相同（同竞品同维度重复），
    #    会互相覆盖（后者胜），且所有同 key 行共享同一分数。当前维度/竞品去重后
    #    不会出现；若将来放开重复，需要把 key 换成「行唯一 id」。
    spread: dict[str, float | None] = {}
    for name in _METRIC_NAMES:
        vals: list[float] = []
        for sample in samples:
            for v in scores_by_key.get(sample.key, {}).get(name, []):
                if v is None:
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if not math.isnan(f):
                    vals.append(f)
        spread[name] = round(max(vals) - min(vals), 3) if len(vals) >= 2 else None
    result.repeat_spread = spread

    for sample in samples:
        buckets = scores_by_key.get(sample.key, {})
        row = {
            "key": sample.key,
            "competitor": sample.competitor,
            "dimension": sample.dimension,
            "n_contexts": sample.n_contexts,  # 分母随行携带，读者一眼可见
        }
        for name in _METRIC_NAMES:
            row[name] = _mean_score(buckets.get(name, []))
        result.rows.append(row)

    result.aggregates = _aggregate_scores(result.rows)
    # cp/cr 分母分布（跨任务比较前必看；见 SampleRow.n_contexts 的说明）。
    ns = [s.n_contexts for s in samples]
    result.n_contexts = {
        "avg": round(sum(ns) / len(ns), 2),
        "min": min(ns),
        "max": max(ns),
    }
    return result


def to_dict(result: FourMetricResult) -> dict:
    """FourMetricResult → JSON 安全字典。"""
    return {
        "task_id": result.task_id,
        "rows": result.rows,
        "aggregates": result.aggregates,
        "n_contexts": result.n_contexts,
        "skipped": result.skipped,
        "repeat": result.repeat,
        "repeat_spread": result.repeat_spread,
    }


def render_markdown(result: FourMetricResult) -> str:
    """分数表 Markdown（逐样本行 + 均值行 + 分母分布）。"""
    lines = [
        f"### task_id={result.task_id or '-'}",
        "",
        "| 样本 | n_contexts | context_precision | context_recall | faithfulness | answer_relevancy |",
        "|---|---|---|---|---|---|",
    ]
    # 逐样本行：key = "竞品@维度"；n_contexts 与四个分数同格呈现，
    # 避免旧 baseline「分数可比性完全依赖读者记得看括号里的 n̄」的坑。
    for row in result.rows:
        lines.append(
            "| {key} | {n} | {cp} | {cr} | {fa} | {ar} |".format(
                key=row["key"],
                n=row["n_contexts"],
                cp=_fmt(row.get("context_precision")),
                cr=_fmt(row.get("context_recall")),
                fa=_fmt(row.get("faithfulness")),
                ar=_fmt(row.get("answer_relevancy")),
            )
        )
    # 均值行：各指标「有效分数」的均值（忽略 None/NaN），空则显示「—」。
    agg = result.aggregates
    lines.append(
        "| **均值** | {n} | {cp} | {cr} | {fa} | {ar} |".format(
            n=_fmt(result.n_contexts.get("avg")),
            cp=_fmt(agg.get("context_precision")),
            cr=_fmt(agg.get("context_recall")),
            fa=_fmt(agg.get("faithfulness")),
            ar=_fmt(agg.get("answer_relevancy")),
        )
    )
    lines.append("")
    # 附一行分母分布 + 跳过快照数 + 重复轮次：这是判断「分数能不能横向比」的必要上下文。
    lines.append(
        "n_contexts 分布: avg={avg} min={mn} max={mx} · 跳过快照={skipped} · repeat={rp}".format(
            avg=_fmt(result.n_contexts.get("avg")),
            mn=result.n_contexts.get("min"),
            mx=result.n_contexts.get("max"),
            skipped=result.skipped,
            rp=result.repeat,
        )
    )
    # repeat>1 时再附一行极差：均值说「期望多少」，极差说「这个数有多可信」。
    # cr 极差常为 1.000（二值抖动）→ 此时只能按「支持率比例」读，不能当精确度。
    spread = result.repeat_spread or {}
    if result.repeat > 1 and spread:
        lines.append(
            "跨轮极差(max-min): " + " · ".join(
                f"{name}={_fmt(spread.get(name))}" for name in _METRIC_NAMES
            )
        )
    return "\n".join(lines)


async def evaluate_task_four(
    task_id: str,
    dao: "TaskDAO",
    settings=None,
    reference_map: dict[str, str] | None = None,
    reference_cell_map: dict[str, str] | None = None,
    repeat: int = 1,
) -> FourMetricResult:
    """评一个已完成任务的四指标（读 retrieval_snapshot，不落库）。

    ⚠️ 两个 map 都为 None（真实任务默认）时，cp/cr 的 reference 会退化为
    「维度名」——这是弱 baseline，仅能跑通流程，分数参考价值有限
    （judge 拿「功能」两个字去判 context 是否支撑，几乎必然失真）。
    真实任务的强 baseline 需要另外提供该任务的标准答案；
    只给 `reference_map`（维度级复合句）时 cr 天花板约 0.5，
    **要拿到干净的 cp/cr 必须给 `reference_cell_map`（cell 级）。**

    repeat：每个 (样本,指标) 重复跑几轮取中位（>1 才压得住 judge 方差，成本 ×N）。
    """
    from src.config import Settings
    from src.db.dao import RetrievalSnapshotDAO

    settings = settings or Settings()
    # 只复用 dao 的连接池来建快照 DAO；本模块自身不写任何东西（不落库）。
    snapshot_dao = RetrievalSnapshotDAO(dao._pool)
    snapshots = await snapshot_dao.get_by_task(task_id)
    samples = build_samples(snapshots, reference_map, task_id, reference_cell_map)
    result = await evaluate_four(samples, settings, task_id=task_id, repeat=repeat)
    # skipped = 有快照但构不成样本的条数（上下文全为空/太短）。
    result.skipped = len(snapshots) - len(samples)
    return result


async def run_golden_proto(
    case_ids: list[str] | None = None, repeat: int = 1
) -> list[dict]:
    """跑 Golden 数据集（Pipeline 后立即四指标评分），只返回分数，不归档。

    只做一件事：跑 Pipeline → 对每个 (竞品, 维度) 打四指标分 → 打印分数表。
    不落库、不写 baseline、不建归档目录、不渲染 diff、不判门禁（2026-10-17
    按「只保留实际使用的代码」删掉了那套外壳）。

    repeat：每个 (样本,指标) 重复跑几轮取**均值**（默认 1；>1 时成本 ×N）。
    """
    from src.db.connection import close_pool, create_pool
    from src.db.dao import RetrievalSnapshotDAO, TaskDAO
    from src.evaluation.golden_dataset import (
        GOLDEN_DATASET,
        get_reference_by_cell,
        get_reference_by_dimension,
    )
    from src.supervisor.router import IntentRouter

    cases = GOLDEN_DATASET
    if case_ids:
        # --case 过滤：只跑指定 golden_id（调试单条时用，省 LLM 成本）。
        cases = [c for c in cases if c["task_id"] in case_ids]

    router = IntentRouter()
    results: list[dict] = []
    for case in cases:
        golden_id = case["task_id"]
        # 每条 Golden 生成一个真实 task_id（golden_id 只用于对答案，不落库为任务）。
        real_task_id = str(uuid.uuid4())
        settings = router.settings
        pool = await create_pool(settings)
        entry = {
            "golden_id": golden_id,
            "task_id": real_task_id,
            "route": None,
            "rows": [],
            "aggregates": {},
            "n_contexts": {},
            "skipped": 0,
            "repeat": repeat,
            "repeat_spread": {},
            "error": None,
        }
        try:
            # 1) 落一条真实 task（供链路写入报告/证据/快照）。
            task_dao = TaskDAO(pool)
            await task_dao.create(
                task_id=real_task_id,
                user_id="golden-runner",
                title=case["title"],
                competitors=case["competitors"],
                dimensions=case["dimensions"],
                pipeline_mode="pipeline",
            )
            task = {
                "id": real_task_id,
                "title": case["title"],
                "user_id": "golden-runner",
            }
            # 2) 构造 IntentionRouter 期望的解析结果（Golden 自带 competitors/dimensions，
            #    无需再过一遍意图解析）。intent_is_clear 为假会触发 Supervisor 探索。
            llm_parsed = {
                "competitors": case["competitors"],
                "dimensions": case["dimensions"],
                "intent_is_clear": bool(case["competitors"] and case["dimensions"]),
            }
            # 3) 跑真实链路（Pipeline 或 Supervisor）。
            exec_result = await router.route(task, llm_parsed)
            entry["route"] = exec_result.get("route")
            if "error" in exec_result:
                # 链路本身失败 → 记录错误、跳过评分（没有快照可评）。
                entry["error"] = exec_result.get("error")
                results.append(entry)
                continue

            # 4) 读链路落下的检索快照 → 构样本 → 四指标评分。
            snapshot_dao = RetrievalSnapshotDAO(pool)
            snapshots = await snapshot_dao.get_by_task(real_task_id)
            # reference 取 **cell 级**（"竞品@维度"，单竞品）——与评估单元同粒度。
            # 若只传维度级 reference_by_dimension（跨竞品复合句），每个 cell 的
            # 判据里有一半讲的是对方竞品，cr 天花板会被钉在 0.5。
            samples = build_samples(
                snapshots,
                get_reference_by_dimension(golden_id),
                real_task_id,
                get_reference_by_cell(golden_id),
            )
            result = await evaluate_four(
                samples, settings, task_id=real_task_id, repeat=repeat
            )
            entry.update(to_dict(result))
            entry["skipped"] = len(snapshots) - len(samples)
        except Exception as exc:
            # 单条 Golden 失败不影响其余：记录错误后继续跑下一条。
            logger.exception("【RAGAS Core】%s 失败: %s", golden_id, exc)
            entry["error"] = str(exc)
        finally:
            # ⚠️ 每个 case 用完即关池（不做连接复用）。Golden 跑批场景可接受，
            #    若要优化可提到循环外共用一池；此处为保持「一条一隔离」不动。
            await close_pool()
        results.append(entry)
    return results


def _setup_logging() -> None:
    """CLI 日志配置（info 级 + 短时间戳，方便交互式看进度）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _print_json(obj) -> None:
    """打印 JSON（中文不转义；default=str 兜底 UUID/datetime 等非原生类型）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _cmd_task(task_id: str, as_json: bool, repeat: int = 1) -> None:
    """CLI 子命令：评估一条已完成的真实任务。"""
    from src.config import Settings
    from src.db.connection import close_pool, create_pool
    from src.db.dao import TaskDAO

    settings = Settings()
    pool = await create_pool(settings)
    try:
        dao = TaskDAO(pool)
        # 注意：这里不传 reference_map → cp/cr 的 reference 退化为维度名（弱 baseline）。
        result = await evaluate_task_four(task_id, dao, settings, repeat=repeat)
        if as_json:
            _print_json(to_dict(result))
        else:
            print(render_markdown(result))
    finally:
        await close_pool()


async def _cmd_golden(
    case_ids: list[str] | None, as_json: bool, repeat: int = 1
) -> None:
    """CLI 子命令：跑 Golden 数据集四指标评分。"""
    results = await run_golden_proto(case_ids, repeat=repeat)
    if as_json:
        _print_json(results)
        return
    for entry in results:
        print(f"## {entry['golden_id']} (route={entry.get('route')})")
        if entry.get("error") and not entry.get("rows"):
            # 链路失败且无分数行 → 只打印错误。
            print(f"失败: {entry['error']}")
            print("")
            continue
        # 把 dict 还原成 FourMetricResult 以复用统一的 Markdown 渲染。
        result = FourMetricResult(
            task_id=entry["task_id"],
            rows=entry["rows"],
            aggregates=entry["aggregates"],
            n_contexts=entry["n_contexts"],
            skipped=entry["skipped"],
            repeat=entry.get("repeat", repeat),
            repeat_spread=entry.get("repeat_spread", {}),
        )
        print(render_markdown(result))
        print("")


def main() -> None:
    """CLI 入口。

    用法：
      python -m src.evaluation.ragas_core --task <uuid>            # 评真实任务
      python -m src.evaluation.ragas_core --golden                # 跑全部 Golden
      python -m src.evaluation.ragas_core --golden --case golden_003  # 只跑一条
      python -m src.evaluation.ragas_core --golden --json         # JSON 输出
      python -m src.evaluation.ragas_core --golden --case golden_001 --repeat 3
                                                                  # 重复 3 轮取中位压方差
    """
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="RAGAS 四指标评估（唯一实现：只出分数表，不落库）"
    )
    parser.add_argument("--task", help="评估已完成的真实任务 ID")
    parser.add_argument("--golden", action="store_true", help="跑 Golden 数据集并评分")
    # --case 可重复：--case golden_001 --case golden_002
    parser.add_argument(
        "--case", action="append", default=[], help="配合 --golden，只跑指定 golden_id"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每个 (样本,指标) 重复几轮取中位（默认 1；>1 会线性增加 LLM 成本，"
        "仅当需要压 judge 方差时使用，建议 3）",
    )
    args = parser.parse_args()
    repeat = max(1, int(args.repeat or 1))

    if args.task:
        asyncio.run(_cmd_task(args.task, args.json, repeat))
        return
    if args.golden:
        # args.case 为空列表时传 None（=跑全部）。
        asyncio.run(_cmd_golden(args.case or None, args.json, repeat))
        return
    # 无参数 → 打印帮助（不留空跑）。
    parser.print_help()


if __name__ == "__main__":
    main()
