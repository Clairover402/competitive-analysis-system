# -*- coding: utf-8 -*-
"""RAGAS 四指标评估（唯一实现）：跑分 → 分数表 → 自动留痕。

【职责】
  做   把 retrieval_snapshot 展开成 (竞品@维度) 样本，一次 aevaluate 跑齐四指标，
       打印分数表，并默认落一个不可变 run 目录。
  不做 落库、门禁、Prometheus 上报、锁 contexts 重打——那套工程外壳 2026-10-17 已删，
       留痕与版本对比在 run_archive.py（跑完自动 begin_run / finish_run）。

【输入 → 输出】
  retrieval_snapshot ─build_samples()
  → [SampleRow] ─evaluate_four()
  → FourMetricResult─render_markdown()/to_dict()
  → 分数表（人读 / JSON）·─run_archive→ run.json + summary.md

【四指标的输入契约】★ cp 与 cr 的 required_columns 完全相同，故能一行同评
  指标                     需要字段                              本模块的数据来源
  context_precision (检索) user_input + contexts + reference      query / reranked_texts / reference
  context_recall    (检索) user_input + contexts + reference      同上
  faithfulness      (生成) user_input + response + contexts      + response_text
  answer_relevancy  (生成) user_input + response (+ embeddings)  + response_text

【关键决策】
  · 一行样本喂四指标
  · reference 优先 cell 级 ← 维度级是跨竞品复合句，判据里有一半讲对方竞品、按定义
    不可能被支持，cr 天花板钉死 0.5、cp 系统性趋 0（实测 飞书@功能 cr=0.500，正好半句）。
    三级回退在 llm_factory.resolve_reference，任一级缺键都显式告警、不静默兜底。
  · 按 response 有无分两批跑 ← 无 response 的样本喂生成层只会换回 NaN 与白花的调用。
  · 多轮取均值而非中位 ← cr/faithfulness 是比例型，judge 抖动是二值不是离群：
    [0,1,0] 的中位是 0（把 1/3 支持率抹成一个 bit），均值才是真实比例。

【读分数前必看】
  · response 是**该维度的结论**，不是整篇报告 ⇒ faithfulness 只当护栏，
    复述得分、归纳扣分，不能当分析质量分。
  · cp / cr 的分母 = 该 cell 的 n_contexts（3 条与 15 条不可直接比），
    每行分数必须带着 n_contexts 一起读。
  · 真实任务没有 reference，会退化为「维度名当 ground truth」，分数无参照系、
    只能看相对变化——这是评估语义决定的，不是实现缺陷。
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
# 必须在 import ragas 之前注入；用 sys.modules 守卫保证多处 import 只注入一次
# （llm_factory.py 里有同款，谁先 import 谁注入）。
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _vertexai_stub = types.ModuleType("langchain_community.chat_models.vertexai")

    class _ChatVertexAI:  # 占位类：仅满足 isinstance 检查，本项目不用 Vertex AI
        pass

    _vertexai_stub.ChatVertexAI = _ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = _vertexai_stub

# 指标刻意用「旧式」（ragas.metrics 顶层 / _ 下划线模块）而非 collections 新式：
# aevaluate 的 isinstance 检查只认旧式 Metric，新式会报 "All metrics must be initialised"。
# 代价是 DeprecationWarning——可接受，迁过去反而跑不起来（详见 llm_factory 模块头）。
from ragas import EvaluationDataset, SingleTurnSample, aevaluate  # noqa: E402
from ragas.metrics import context_precision, context_recall  # noqa: E402
from ragas.metrics._answer_relevance import answer_relevancy  # noqa: E402
from ragas.metrics._faithfulness import faithfulness  # noqa: E402

# 本模块对外部能力的全部依赖都在这里：judge / embeddings / RunConfig / reference 解析。
# 口径只此一处定义，不与其他链路各写一份。
from src.evaluation.llm_factory import (  # noqa: E402
    JUDGE_MODEL,
    build_embeddings,
    build_judge_llm,
    build_run_config,
    load_json_list,
    resolve_reference,
)
# 留痕：默认开，--no-archive 才关。依赖方向单向——run_archive 不 import 本模块，
# 因为本模块 import 期会拉起 ragas，而对比/趋势必须在无 ragas 环境也能跑。
from src.evaluation.run_archive import begin_run, finish_run  # noqa: E402

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
    """一条 (竞品@维度) 样本的输入与元信息（未打分）。

    「未打分」是刻意的分层：build_samples() 只做「数据库快照 → ragas 样本」的翻译，
    不碰任何 LLM，因而可被单测离线覆盖，打分失败也不会污染样本构造。

    frozen：四个指标读同一份不可变输入，避免某个指标就地改样本、让其余指标读到脏数据。
    """

    key: str                 # 打分键 = "竞品@维度"（无竞品时退化为维度名），唯一标识该行
    competitor: str          # 竞品名（可能为空，历史维度级快照无此字段）
    dimension: str           # 维度名
    user_input: str          # = 快照 query（"竞品 维度"），与线上精排 rerank 同尺
    response: str | None     # 该维度的 LLM 结论；历史快照为空 → None（生成层不跑）
    retrieved_contexts: list[str]  # 精排后 TopK 的原始 chunk（已滤掉 <10 字符的）
    reference: str           # 该 cell ground truth（优先 reference_by_cell，回退维度级）
    n_contexts: int          # 实际参与评分的 context 条数 = cp/cr 的**分母**


@dataclass
class FourMetricResult:
    """一次评估的输出：逐样本分数 + 均值 + 分母分布 + 每轮原始分。

    「分数」与「分母」放在同一个结构体里，因为 cp 是对实际检索到的那几条求 AP，
    分母会波动且「分母小 ⇒ 分数系统性偏高」——两者必须同时出现才读得对。
    """

    task_id: str
    # 逐样本分数行：每行 {"key","competitor","dimension","n_contexts","reference", <四指标…>}
    rows: list[dict] = field(default_factory=list)
    # {指标名: 均值}；无有效分数的指标为 None（严格忽略 None/NaN，不按 0 补）
    aggregates: dict[str, float | None] = field(default_factory=dict)
    # cp/cr 分母的分布：{"avg": x, "min": n, "max": n}（跨任务比较前必看）
    n_contexts: dict[str, float | int | None] = field(default_factory=dict)
    # 被跳过的快照数（无有效上下文，构不成样本）
    skipped: int = 0
    # 每个 (样本,指标) 跑了几轮、分数取均值。必须随分数暴露：repeat=1 与 3 的期望
    # 与方差不同，不标注会让「cr 从 0.5 变 1.0」被误读成回归。
    repeat: int = 1
    # 每指标的跨样本极差（max-min），repeat=1 时为 None。
    # 均值说「期望多少」，极差说「多可信」；cr 极差常为 1.000（二值抖动），
    # 此时均值只能当支持率读，不能当精确度。
    repeat_spread: dict[str, float | None] = field(default_factory=dict)
    # {cell key: {指标名: [每轮原始分]}}（repeat=1 时每项是单元素列表）。
    # 不能只留「均值 + 跨样本极差」：跨版本判定阈值是**该 cell 自己**的跨轮极差，
    # 而跨样本 max-min 分不清「两轮 0/1」和「两轮 0.4/0.6」。
    # 落档后逐 cell 才有噪声尺子（见 run_archive._cell_spread）。
    scores_by_repeat: dict[str, dict[str, list[float | None]]] = field(
        default_factory=dict
    )


def build_samples(
    snapshots: list[dict],
    reference_map: dict[str, str] | None = None,
    task_id: str = "",
    reference_cell_map: dict[str, str] | None = None,
) -> list[SampleRow]:
    """把 retrieval_snapshot 行展开为 (竞品@维度) 评估样本（纯函数，不碰 LLM）。

    字段映射：
      user_input         = 快照 query（= 线上精排 query「竞品 维度」）
      retrieved_contexts = reranked_texts 中 ≥10 字符的原始 chunk
      reference          = 优先 cell 级（只讲该竞品），回退维度级，再退维度名
      response           = response_text（维度级结论；历史快照可能为空）
      n_contexts         = 实际参与评分的 context 条数（cp/cr 的分母）

    两个非显然的选择：
      · user_input 用「快照 query」而非维度名 ← 精排 reranker 的 query 是「竞品 维度」，
        用维度名当 query 会让评估尺子与检索尺子错配，分数不可比。
      · reference 首选 cell 级 ← 维度级是跨竞品复合句，判据一半讲对方竞品，
        cr 天花板被钉死 0.5（实测 飞书@功能 cr=0.500，正好半句）。
    """
    rows: list[SampleRow] = []
    for snap in snapshots:
        # .get() 而非下标：历史 / 异常快照可能缺字段，宁可退化成空串，
        # 也不要 KeyError 打断整批评分。
        dimension = snap.get("dimension") or ""
        competitor = snap.get("competitor") or ""

        # DB 列名（query / reranked_texts / response_text）保持历史命名，
        # 在这里一次性转成 RAGAS 官方字段名，下游逐字对齐、不再做心智翻译。
        user_input = snap.get("query") or dimension

        # reranked_texts 是 JSONB：asyncpg 读出可能是 str（需反序列化）也可能是 list。
        retrieved_contexts = load_json_list(snap.get("reranked_texts"))
        # 滤掉 <10 字符的碎片：导航文本、单词标题既无事实也判读不出，
        # 留着只会虚增分母、把 cp 系统性压低。
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
    """分数渲染：None / NaN → 「—」，否则保留 3 位小数。

    None 与 NaN 统一成「—」：对读者语义相同（这格没有可用分数）。NaN 是 ragas
    内部拆句/请求失败的产物，None 是主动跳过；区分只对调试有意义，且 nan 直接
    打印出来是噪音而非分数。
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
    """各指标有效分数的均值（忽略 None / NaN，绝不按 0 补）。

    按 0 补会造出一个「看起来很低」但并不存在的分数，读者无法区分「真的差」
    和「没测到」。全为空时返回 None，表里显式显示「—」，把缺失暴露出来。
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

    ★ 用均值而不是中位数（2026-10-17 推翻原实现）：
      judge 的抖动是**二值抖动**而不是离群。同一 cell 三轮跑出 [0,1,0] 时，
      中位数（排序后 [0,0,1]）取 0，把「1/3 轮支持」抹成一个 bit；
      均值取 0.333，才是真实支持率。连续型指标用均值同样无损。
      稳定性另看极差：FourMetricResult.repeat_spread。

    repeat=1 时与改动前逐位等价：单元素均值即自身，取整位数相同。
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

    · 有 response 的样本：四指标全跑。
    · 无 response 的历史快照：只跑检索层 cp / cr，生成层留「—」。

    两批而不是一批 ← faithfulness / answer_relevancy 必须吃 response，
    混批会让生成层对空样本返回 NaN/报错，白花拆句与反推问题的调用。

    repeat > 1 时每个 (样本,指标) 收 N 个分数取均值（见 _mean_score），
    成本线性 ×N，故默认 1；要压方差必须显式传（CLI `--repeat 3`）。

    ⚠️ 本函数会**就地改写** ragas 的模块级指标对象：旧式指标是预实例化单例，
    llm/embeddings 只能就地赋值、不能传参。后果是 import 方共享状态、测试需
    monkeypatch 隔离、并发调用会互相踩。已知坏味道，不在指标层翻修。
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

    # 旧式指标是模块级单例，llm/embeddings 只能就地赋值（见本函数 docstring 的 ⚠️）。
    # 四个指标共用同一把 judge，分数口径才一致。
    context_precision.llm = judge
    context_recall.llm = judge
    faithfulness.llm = judge
    answer_relevancy.llm = judge
    answer_relevancy.embeddings = embeddings
    # strictness 必须为 1：ragas 把它当 `n` 透传给 LLM，而 DeepSeek 不支持 n>1
    # （n>1 → 400 → 重试耗尽 → ar 恒 NaN。实测 strictness=3 跑 48.7s 后失败，
    #  =1 时 11.7s 出有效分）。
    answer_relevancy.strictness = 1

    # 按 response 有无分流（理由见 docstring）。
    with_response = [s for s in samples if s.response]
    without_response = [s for s in samples if not s.response]

    # key → {指标名: [每轮分数]}；repeat 轮跑完后逐指标取**均值**（_mean_score）。
    # 用列表而非单值：聚合只发生在回填这一步，各轮互不覆盖
    # （单值 dict 会让后一轮静默盖掉前一轮）。
    scores_by_key: dict[str, dict[str, list[float | None]]] = {}

    async def _run(subset: list[SampleRow], metrics: list) -> None:
        """对一批样本跑 repeat 轮，逐轮把分数追加进 scores_by_key。

        内嵌闭包：直接消费外层的 task_id / scores_by_key，抽出去反而多传参。
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
                        # NaN 不是合法 JSON，写 float 列也会报错 → 统一归 None，
                        # 但打 warning 留下「这里丢过一个分」的痕迹（不静默）。
                        # 常见成因是 timeout 被撞穿（长 contexts 串行判定），
                        # 故把分母一并打出来，便于判断是不是这个原因。
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

    # ① 极差：把**全部**有效分数（跨样本 × 跨轮）拉平求 max-min，看这一轮跑的离散程度。
    #    ⚠️ 这不是「每格自己的跨轮极差」——那个在 run_archive._cell_spread。
    #    对比判定必须用后者：这里混入了样本间差异，当噪声阈值会放大或吞掉真实变化。
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

    # ② 按原始 samples 顺序回填 rows（输出顺序 = 样本顺序，便于逐条比对），
    #    每格取 repeat 轮均值（repeat=1 即该值本身，与改动前等价）。
    #    ⚠️ scores_by_key 以 sample.key 为键：同 key 两行会互相覆盖（后者胜）。
    #    当前竞品/维度已去重不会出现；将来放开重复需换成行唯一 id。
    for sample in samples:
        buckets = scores_by_key.get(sample.key, {})
        row = {
            "key": sample.key,
            "competitor": sample.competitor,
            "dimension": sample.dimension,
            "n_contexts": sample.n_contexts,  # 分母随行携带，读者一眼可见
            # reference 一起落档：它是这格分数的判据，留着才能解释「为什么只有 0.33」。
            # 不留 contexts（体积大，且明确不做锁 contexts 重打）。
            "reference": sample.reference,
        }
        for name in _METRIC_NAMES:
            row[name] = _mean_score(buckets.get(name, []))
        result.rows.append(row)

    # 每轮原始分原样带出（对比时靠它算「这一格自己的噪声尺子」，见 run_archive）。
    # 只读拷贝：不把内部可变结构直接暴露出去（调用方后续若改它，不该影响下一次评估）。
    result.scores_by_repeat = {
        key: {name: list(vals) for name, vals in bucket.items()}
        for key, bucket in scores_by_key.items()
    }

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
        # 每格每轮原始分：对比判定的阈值来源，缺了就只剩「看 Δ 不判噪声」。
        "scores_by_repeat": result.scores_by_repeat,
    }


def render_markdown(result: FourMetricResult) -> str:
    """分数表 Markdown（逐样本行 + 均值行 + 分母分布）。"""
    lines = [
        f"### task_id={result.task_id or '-'}",
        "",
        "| 样本 | n_contexts | context_precision | context_recall | faithfulness | answer_relevancy |",
        "|---|---|---|---|---|---|",
    ]
    # 逐样本行：n_contexts 与四个分数同格呈现——分数可比性依赖读者看得到分母。
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
    # repeat>1 时附极差：均值说「期望多少」，极差说「这个数有多可信」。
    # ⚠️ 这里是**全体有效分数**的极差（跨样本 × 跨轮），只能粗略看离散程度；
    #    判定某格有没有真变化要用 run_archive 的逐 cell 跨轮极差（θ）。
    spread = result.repeat_spread or {}
    if result.repeat > 1 and spread:
        lines.append(
            "全量极差(max-min): " + " · ".join(
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

    ⚠️ 真实任务默认不传 map → reference 退化为「维度名」，属弱 baseline，
    仅能跑通流程、分数参考价值有限（judge 拿「功能」两个字判 context 是否支撑）。
    要拿到干净的 cp/cr 必须给 `reference_cell_map`（cell 级）；只给维度级时
    cr 天花板约 0.5。

    repeat：每个 (样本,指标) 跑几轮取均值（>1 才压得住 judge 方差，成本 ×N）。
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


async def run_golden(
    case_ids: list[str] | None = None, repeat: int = 1
) -> list[dict]:
    """跑 Golden：每条 case 走真实 Pipeline，再对每个 (竞品@维度) 打四指标分。

    只算分、不碰磁盘——留痕由调用方（_cmd_golden）跑完后调 finish_run，
    故本函数仍可被测试完全离线驱动。

    每条 case 新建一个真实 task_id（golden_id 只用于对答案，不落库为任务），
    用完即关连接池：一条一隔离，跑批场景可接受。

    repeat：每个 (样本,指标) 跑几轮取均值（默认 1；>1 时成本 ×N）。
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
    settings = router.settings  # 循环不变量，提到外面（每条 case 重取一次没有意义）
    results: list[dict] = []
    for case in cases:
        golden_id = case["task_id"]
        # 每条 Golden 生成一个真实 task_id：golden_id 只用于对答案，不落库为任务。
        real_task_id = str(uuid.uuid4())
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
            "scores_by_repeat": {},
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
            # 2) 直接构造 router 期望的解析结果：Golden 自带 competitors/dimensions，
            #    不必再过一遍意图解析。intent_is_clear 为假会触发 Supervisor 探索。
            llm_parsed = {
                "competitors": case["competitors"],
                "dimensions": case["dimensions"],
                "intent_is_clear": bool(case["competitors"] and case["dimensions"]),
            }
            # 3) 跑真实链路（Pipeline 或 Supervisor）。
            exec_result = await router.route(task, llm_parsed)
            entry["route"] = exec_result.get("route")
            if "error" in exec_result:
                # 链路本身失败 → 记错误、跳过评分（没有快照可评）。
                entry["error"] = exec_result.get("error")
                results.append(entry)
                continue

            # 4) 读链路落下的检索快照 → 构样本 → 四指标评分。
            snapshot_dao = RetrievalSnapshotDAO(pool)
            snapshots = await snapshot_dao.get_by_task(real_task_id)
            # reference 必须传 **cell 级**（与评估单元同粒度）；只传维度级会让
            # 每个 cell 的判据有一半讲对方竞品，cr 天花板钉在 0.5。
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
            # 每个 case 用完即关池（一条一隔离）。跑批场景可提到循环外共用一池，
            # 此处不为性能牺牲隔离。
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


async def _cmd_task(
    task_id: str,
    as_json: bool,
    repeat: int = 1,
    note: str | None = None,
    archive: bool = True,
) -> None:
    """CLI 子命令：评一条已完成的真实任务（默认留痕）。

    ⚠️ 单评没有 reference，cp/cr 退化为「维度名当 ground truth」= 弱 baseline；
    归档 kind=task，不进趋势表、不能当 baseline。
    """
    from src.config import Settings
    from src.db.connection import close_pool, create_pool
    from src.db.dao import TaskDAO

    settings = Settings()
    pool = await create_pool(settings)
    handle = begin_run("task", note=note) if archive else None
    try:
        dao = TaskDAO(pool)
        # 注意：这里不传 reference_map → cp/cr 的 reference 退化为维度名（弱 baseline）。
        result = await evaluate_task_four(task_id, dao, settings, repeat=repeat)
        if handle:
            entry = to_dict(result)
            # 单评没有 golden_id，用 task 前缀当 cases 键（避免与 golden_00x 混淆）。
            entry["golden_id"] = f"task_{task_id[:8]}"
            entry["error"] = None
            run_dir = finish_run(handle, [entry], settings=settings, cases_requested=1)
        else:
            run_dir = None
        if as_json:
            _print_json(to_dict(result))
        else:
            print(render_markdown(result))
        if run_dir:
            print(f"\n已归档：{run_dir}")
    finally:
        await close_pool()


async def _cmd_golden(
    case_ids: list[str] | None,
    as_json: bool,
    repeat: int = 1,
    note: str | None = None,
    archive: bool = True,
) -> None:
    """CLI 子命令：跑 Golden 数据集四指标评分（并默认留痕）。"""
    from src.evaluation.golden_dataset import GOLDEN_DATASET

    requested = len(case_ids) if case_ids else len(GOLDEN_DATASET)
    # begin_run 放在跑之前：先占住 run_id 并写下 .pending，
    # 即使跑到一半崩了，目录里也留得下「这次是谁、什么时候、想干什么」。
    handle = begin_run("golden", note=note) if archive else None
    results = await run_golden(case_ids, repeat=repeat)
    run_dir = finish_run(handle, results, cases_requested=requested) if handle else None
    if as_json:
        _print_json(results)
    else:
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
                scores_by_repeat=entry.get("scores_by_repeat", {}),
            )
            print(render_markdown(result))
            print("")
    if run_dir:
        print(f"已归档：{run_dir}")
        print("看结果：summary.md（本次明细）/ TREND.md（历史趋势）")


def main() -> None:
    """CLI 入口。

    用法：
      python -m src.evaluation.ragas_core --task <uuid>            # 评真实任务
      python -m src.evaluation.ragas_core --golden                # 跑全部 Golden
      python -m src.evaluation.ragas_core --golden --case golden_003  # 只跑一条
      python -m src.evaluation.ragas_core --golden --json         # JSON 输出
      python -m src.evaluation.ragas_core --golden --case golden_001 --repeat 3
                                                                  # 重复 3 轮取均值压方差
      python -m src.evaluation.ragas_core --golden --repeat 3 --note "top_k 15→20"
                                                                  # 跑并留痕，备注进 manifest
      python -m src.evaluation.ragas_core --golden --no-archive    # 本次不留痕（调试用）
    """
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="RAGAS 四指标评估（唯一实现：出分数表 + 默认自动留痕）"
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
        help="每个 (样本,指标) 重复几轮取均值（默认 1；>1 会线性增加 LLM 成本，"
        "仅当需要压 judge 方差时使用，建议 3）",
    )
    # 留痕默认开：归档必须长在跑分入口上，不能靠人记得调（旧 golden_run.py 就死在这）。
    # 要关必须显式写 --no-archive，不给「这次不用留」的默认选项。
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="本次不落 evaluation_runs/（仅调试用；全量跑不要加）",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="一行备注写进 manifest，例如 \"top_k 15→20\"（趋势表的「备注」列）",
    )
    args = parser.parse_args()
    repeat = max(1, int(args.repeat or 1))
    archive = not args.no_archive

    if args.task:
        asyncio.run(_cmd_task(args.task, args.json, repeat, args.note, archive))
        return
    if args.golden:
        # args.case 为空列表时传 None（=跑全部）。
        asyncio.run(
            _cmd_golden(args.case or None, args.json, repeat, args.note, archive)
        )
        return
    # 无参数 → 打印帮助（不留空跑）。
    parser.print_help()


if __name__ == "__main__":
    main()
