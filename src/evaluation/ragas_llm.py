# -*- coding: utf-8 -*-
"""真 RAGAS 评估 — 基于 ragas 库 + LLM-as-Judge（DeepSeek）。

═══════════════════════════════════════════════════════════════════════════════
                     【2026-10-06 从启发式升级为真 ragas】
═══════════════════════════════════════════════════════════════════════════════

  之前的 ragas_eval.py 是「启发式替身」——用中文 n-gram 重叠率模拟四个指标，
  不调用 LLM，结果对文本长度/措辞极敏感（context_precision 从 0 跳到 1、
  faithfulness 换输入就暴跌）。
  （该文件及其对应测试已删除，本模块是真 ragas 的唯一实现。）

  本模块改用官方 ragas 库，每个指标内部用 LLM 判断：
    - faithfulness       → 把 response 拆成原子陈述，逐条问"是否被 context 支持"
    - answer_relevancy   → 从 response 反推问题，与 user_input 做语义相似度
    - context_precision  → 每条 context 是否精确支撑 reference
    - recall_hit_rate    → 检索召回覆盖度（规则式，非 ragas context_recall）

  【为什么不用 ragas 的 context_recall？】
  本系统是「分维度」架构：一个 task → N 个维度 → 每个维度独立检索+精排+成稿，
  reference（标准答案）也是按维度拆的。而 ragas 的 context_recall 假设的是
  「单问题 → 单答案 → 单组检索结果」的颗粒度，在多维度场景下颗粒度错配，且
  DeepSeek judge 分句遵循度不足、恒判 0。召回「全不全」改由 recall_hit_rate
  （快照 recalled_urls 与标准答案 URL 的集合覆盖度，纯规则、0 LLM 成本）度量。

  【模型隔离】评估 judge 用 deepseek-chat（通用能力），
  与生成用的 deepseek-v4-flash 隔离——延续 judge_eval.py 的既有设计。

  【两个 ragas 0.4.3 的兼容坑（已处理）】
  1. ragas 顶层硬 import langchain_community.chat_models.vertexai，
     但该模块在新 langchain-community 已移除 → 注入占位模块兜底。
  2. ragas 0.4.3 有两套指标：collections（新式，继承 SimpleBaseMetric）
     和 _ 下划线模块（旧式，继承 Metric）。aevaluate() 的 isinstance 检查
     只认旧式 Metric，collections 指标会报 "All metrics must be initialised"。
     → 故用旧式指标（_faithfulness 等）+ LangchainLLMWrapper。
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import sys
import types
from typing import TYPE_CHECKING

# ── monkey-patch：ragas 0.4.3 顶层 import 了不存在的 vertexai 模块 ──
# 必须在 import ragas 之前注入，否则 ModuleNotFoundError。
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _vertexai_stub = types.ModuleType("langchain_community.chat_models.vertexai")

    class _ChatVertexAI:  # 占位类：仅满足 isinstance 检查，本项目不用 Vertex AI
        pass

    _vertexai_stub.ChatVertexAI = _ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = _vertexai_stub

from ragas import EvaluationDataset, SingleTurnSample  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics._answer_relevance import answer_relevancy  # noqa: E402
from ragas.metrics._faithfulness import faithfulness  # noqa: E402

from langchain_deepseek import ChatDeepSeek  # noqa: E402
from langchain_openai import OpenAIEmbeddings  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402

if TYPE_CHECKING:
    from src.db.dao import TaskDAO  # noqa: F401


# ── judge 模型：deepseek-chat，与生成模型 deepseek-v4-flash 隔离 ──
JUDGE_MODEL = "deepseek-chat"

# ── 模块级 logger：RAGAS 评估的控制台日志入口 ──
logger = logging.getLogger(__name__)


async def _persist_eval_result(
    dao: "TaskDAO",
    task_id: str,
    eval_type: str,
    *,
    report_id: str | None,
    overall_score: float | None,
    gate: str | None,
    metrics: list[dict],
    metadata: dict | None = None,
) -> None:
    """把评估结果落库到 eval_results + eval_metric_scores（失败不阻断评估）。

    评估结果落库是「沉淀趋势 + 版本 diff + 回归判定」的基础，但它是
    旁路数据——落库失败不应让评估本身失败，故 try/except 包裹，失败只告警。
    """
    try:
        from src.db.dao import EvalResultDAO

        eval_dao = EvalResultDAO(dao._pool)
        eval_id = await eval_dao.insert_result(
            task_id=task_id,
            eval_type=eval_type,
            report_id=report_id,
            overall_score=overall_score,
            gate=gate,
            judge_model=JUDGE_MODEL,
            is_golden=bool(metadata and metadata.get("is_golden")),
            metadata=metadata,
        )
        if eval_id and metrics:
            await eval_dao.insert_metric_scores(eval_id, metrics)
        logger.info(
            "评估结果落库成功 task_id=%s eval_type=%s overall=%s metrics=%d",
            task_id[:8], eval_type, overall_score, len(metrics),
        )
    except Exception:
        logger.warning(
            "评估结果落库失败 task_id=%s eval_type=%s", task_id, eval_type,
            exc_info=True,
        )


# 该方法用于构建并包装LangChain的LLM以供RAGAS使用，返回包装后的LLM（LangchainLLMWrapper）
def _build_judge_llm(settings) -> LangchainLLMWrapper:
   # 构建 ragas 评估 judge LLM（DeepSeek，OpenAI 兼容）。

   #  LangchainLLMWrapper
   #  Ragas 内部定义了一套自己的 LLM 抽象接口 `BaseRagasLLM`，
   #  不能直接传入 LangChain 的 ChatOpenAI / ChatBedrock 等实例**，必须经过这个包装器做适配，
   #  才能用于：
   #  1. RAGAS 指标评估（Faithfulness、ContextRecall 等）
   #  2. 测试集生成（Testset Generator）
   #  3. 自定义 Metric 里调用 LLM 做打分判断Ragas


   # 现在得到的是 LangChain的LLM对象
    judge = ChatDeepSeek(
        model=JUDGE_MODEL,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=0.0,  # 评估任务，零温度保证一致性
        request_timeout=120.0,
        max_retries=2,
    )
   # 把LangChain的LLM对象传入 LangchainLLMWrapper
    return LangchainLLMWrapper(judge)


def _build_embeddings(settings) -> LangchainEmbeddingsWrapper:
    """构建 ragas 评估 embeddings（SiliconFlow 的 BGE-M3，OpenAI 兼容）。

    answer_relevancy 需要 embedding 做「answer 反推问题」与「原始问题」的
    语义相似度。项目 embedding 走 SiliconFlow API（BAAI/bge-m3）。
    """
    emb = OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )
    return LangchainEmbeddingsWrapper(emb)


async def evaluate_task_with_ragas(
    task_id: str,
    dao: "TaskDAO",
    settings=None,
    is_golden: bool = False,
) -> dict | None:
    """用真 ragas 库评估已完成任务的「生成质量」（成稿后）。

    本函数只评「生成质量」两个无需标准答案的指标：
    - faithfulness（忠实度）：回答是否来源检索上下文、有无幻觉
    - answer_relevancy（回答相关性）：回答是否紧扣用户问题

    context_precision（上下文精确率）是「检索质量」指标，已移交给
    evaluate_retrieval（吃 retrieval_snapshot、按维度打分），本函数不再重复计算。

    Args:
        task_id: 要评估的任务 ID
        dao: TaskDAO 实例（用于读任务/报告/证据）
        settings: 可选 Settings，默认从环境构建
        is_golden: 是否为 Golden 用例。仅用于落库时打 is_golden 标记，
            不影响指标集（生成质量指标无需标准答案）。

    Returns:
        {
            "task_id": str,
            "ragas": {metric_name: score},   # faithfulness / answer_relevancy（0~1）
            "gate": "PASS|WARN|BLOCK|SKIP",
            "judge_model": str,
        }
        任务无报告或证据不足则返回 None / SKIP
    """
    from src.config import Settings
    from src.db.dao import EvidenceDAO, ReportDAO

    settings = settings or Settings()

    logger.info("开始生成质量评估 task_id=%s judge=%s", task_id[:8], JUDGE_MODEL)

    # 1. 取任务
    task = await dao.get(task_id)
    if task is None or task.get("status") != "completed":
        logger.info("跳过生成质量评估 task_id=%s：任务不存在或未 completed", task_id[:8])
        return None

    # 2. 取最新报告（作为 response）
    report_dao = ReportDAO(dao._pool)
    report = await report_dao.get_latest(task_id)
    if report is None or not report.get("content"):
        logger.info("跳过生成质量评估 task_id=%s：无报告正文", task_id[:8])
        return None
    response = report["content"]
    logger.info("已取报告 task_id=%s report_id=%s 正文长度=%d", task_id[:8], str(report["id"])[:8], len(response))


    # 3. 取证据（作为 retrieved_contexts，优先 source_text 原文，其次 claim）
    # 初始化证据DAO，复用数据库连接池，用于查询报告对应的证据记录
    evidence_dao = EvidenceDAO(dao._pool)

    # 根据当前报告ID，查询该报告下全部关联的证据数据
    evidences = await evidence_dao.get_by_report(report["id"])

    # 构建上下文分片列表：
    # 优先取 source_text（证据的原始来源文本）；
    # 如果 source_text 为空，则降级使用 claim（证据摘要/断言文本）；
    # 两者都没有则兜底为空字符串
    contexts = [
        e.get("source_text") or e.get("claim") or ""
        for e in evidences
    ]

    # 过滤清洗上下文：
    # 1. 过滤掉空字符串；
    # 2. 剔除文本长度小于10字符的短内容，避免无效脏数据进入评估；
    # 过滤后得到有效上下文分片，后续会作为 retrieved_contexts 送入RAGAS评估
    contexts = [c for c in contexts if c and len(c.strip()) >= 10]
    logger.info(
        "已取证据 task_id=%s 原始证据=%d 有效上下文=%d",
        task_id[:8], len(evidences), len(contexts),
    )

    # 5. 组装 SingleTurnSample + 跑 ragas
    #    RAGAS 里【单轮问答样本的数据容器】，封装一条 RAG 评估所需的全部字段，
    #    给指标函数读取打分。SingleTurn = 单轮对话（对比 MultiTurnSample 多轮）。
    #    注意：生成质量指标（faithfulness / answer_relevancy）无需 reference，
    #    故这里不传 reference——context_precision 这类「检索质量」指标已由
    #    evaluate_retrieval 在检索阶段单独评估，不在此重复。
    sample = SingleTurnSample(
        user_input=task.get("title", ""),  # 任务标题也就是用户输入
        response=response,       #response是最终生成的report
        retrieved_contexts=contexts,  # 召回文本（证据 source_text/claim）
    )

    # 批量评估的容器，存放一堆 SingleTurnSample / MultiTurnSample，
    # 专门喂给 `evaluate()` 函数做批量打分。
    # SingleTurnSample = 单条样本；EvaluationDataset = 样本的集合（测试集）
    dataset = EvaluationDataset(samples=[sample])

    judge = _build_judge_llm(settings)
    embeddings = _build_embeddings(settings)

    # 旧式指标是预实例化单例，直接赋 llm/embeddings
    faithfulness.llm = judge
    answer_relevancy.llm = judge
    answer_relevancy.embeddings = embeddings

    # 生成质量只跑两个「无需标准答案」的指标。
    # 1. faithfulness 忠实度：生成的回答是否来源于检索上下文，不允许编造信息
    # 2. answer_relevancy 回答相关性：生成回答和用户提问的相关程度
    metrics = [faithfulness, answer_relevancy]

    from ragas import aevaluate

    # 异步执行RAGAS批量评估
    metric_names = ["faithfulness", "answer_relevancy"]
    logger.info(
        "开始跑 ragas 指标 task_id=%s is_golden=%s metrics=%s",
        task_id[:8], is_golden, ",".join(metric_names),
    )
    result = await aevaluate(
        dataset=dataset,
        metrics=metrics,
    )

    # 6. 汇总分数
    # 【L4 工程】NaN 过滤：ragas 的 faithfulness 在「无法从回答拆出原子陈述」时
    # 返回 np.nan（_faithfulness.py 中 statements==[] → return np.nan）。NaN 不是
    # 合法 JSON（json.dumps 会输出非标准 NaN），且 asyncpg 写 float 列会报错。
    # 这里统一把 NaN 跳过（不纳入 scores），让 overall/gate 只基于有效分数计算。
    import math

    df = result.to_pandas()
    scores = {}
    if not df.empty:
        for col in df.columns:
            if col in ("faithfulness", "answer_relevancy"):
                raw = df.iloc[0][col]
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isnan(val):
                    logger.warning(
                        "指标 %s 返回 NaN（ragas 拆句失败），已跳过 task_id=%s",
                        col, task_id[:8],
                    )
                    continue
                scores[col] = round(val, 3)
    logger.info("生成质量分数 task_id=%s scores=%s", task_id[:8], scores)

    # 7. 落库（沉淀趋势/版本 diff 基础，失败不阻断评估）
    overall = round(sum(scores.values()) / len(scores), 3) if scores else None
    gate = _gate_from_scores(scores)
    await _persist_eval_result(
        dao,
        task_id,
        "generation",
        report_id=report["id"],
        overall_score=overall,
        gate=gate,
        metrics=[
            {"metric_name": name, "score": val}
            for name, val in scores.items()
        ],
        metadata={"is_golden": is_golden},
    )

    logger.info(
        "生成质量评估完成 task_id=%s overall=%s gate=%s",
        task_id[:8], overall, gate,
    )

    return {
        "task_id": task_id,
        "ragas": scores,
        "gate": gate,
        "judge_model": JUDGE_MODEL,
    }


def _gate_from_scores(scores: dict) -> str:
    """由真 ragas 分数映射门禁（沿用 Phase 10 阈值）。"""
    if not scores:
        return "SKIP"
    overall = sum(scores.values()) / max(len(scores), 1)
    if overall >= 0.85:
        return "PASS"
    if overall >= 0.70:
        return "WARN"
    return "BLOCK"


async def evaluate_retrieval(
    task_id: str,
    dao: "TaskDAO",
    settings=None,
    reference_map: dict[str, str] | None = None,
    reference_domains: list[str] | None = None,
) -> dict | None:
    """用真 ragas 评估「检索阶段」质量（在 analyzer 检索时就地评，非成稿后）。

    【为什么检索评估要独立于生成评估？】
    RAGAS 的 context_precision 测的是「检索本身准不准」，
    对象必须是 analyzer 精排后落盘的原始 retrieved_contexts（retrieval_snapshot），
    不能是 LLM 二次加工的结论或事后按 URL 反查的 chunk。故本函数吃
    retrieval_snapshot 表（analyzer 在精排后、喂 LLM 前落盘），逐个维度打分。

    与 evaluate_task_with_ragas（生成质量：faithfulness/answer_relevancy，
    在成稿后评）分离——检索质量与生成质量是不同时间点、不同对象的事。

    【两个检索质量指标（分维度架构下的正确选择）】
    1. context_precision —— 精排准不准（LLM 判）：每条精排后的 chunk 是否
       支撑该维度的标准答案。
    2. recall_hit_rate —— 召回全不全（规则判）：recalled_urls 与标准答案的
       关键来源域名做集合覆盖度对拍，0 LLM 成本、可解释。

    为什么不用 ragas 的 context_recall？——它是「单问题→单答案→单组检索」的
    颗粒度假设，与本系统「一个 task → N 维度 → 各自独立检索」错配；且 DeepSeek
    judge 分句遵循度不足导致恒 0。召回质量改由 recall_hit_rate 度量（见上）。

    Args:
        task_id: 任务 ID
        dao: TaskDAO 实例
        settings: 可选 Settings
        reference_map: {dimension: 该维度的标准答案}。Golden 用例应传入
            分维度 reference（`get_reference` 的整段标准答案需按维度拆）。
            为 None 时用维度名兜底（弱 baseline，仅能跑通，分数参考价值有限）。
        reference_domains: 该任务的关键来源域名锚点（`get_reference_domains`）。
            用于 recall_hit_rate 对拍；为 None 时召回覆盖度不可计算，仅返回
            context_precision。

    Returns:
        {
            "task_id": str,
            "dimensions": {key: {"context_precision": x}},  # key = "竞品@维度" 或维度名
            "avg_context_precision": float,
            "recall_hit_rate": float,   # 0~1，仅在 reference_domains 提供时计算
            "judge_model": str,
        }
        无检索快照返回 None
    """
    from src.config import Settings
    from src.db.dao import RetrievalSnapshotDAO

    settings = settings or Settings()

    logger.info("开始检索质量评估 task_id=%s judge=%s", task_id[:8], JUDGE_MODEL)

    # 拿 召回上下文分片 和 top@K
    snapshot_dao = RetrievalSnapshotDAO(dao._pool)
    snapshots = await snapshot_dao.get_by_task(task_id)
    if not snapshots:
        logger.info("跳过检索质量评估 task_id=%s：无 retrieval_snapshot 快照", task_id[:8])
        return None
    logger.info("已取检索快照 task_id=%s 维度数=%d", task_id[:8], len(snapshots))

    judge = _build_judge_llm(settings)

    dim_scores: dict[str, dict[str, float]] = {}

    # 竞品×维度粒度：每条快照 = 一个竞品在一个维度下的精排头部。
    # user_input（=快照 query="竞品 维度"）与线上精排 rerank("竞品 维度") 的尺子对齐。
    for idx, snap in enumerate(snapshots, start=1):
        dimension = snap["dimension"]
        competitor = snap.get("competitor") or ""

        # 快照 query 已落为 "竞品 维度"；历史维度级快照（competitor 为空）退化为维度名
        query = snap["query"] or dimension
        # JSONB 字段读出来可能是 str（asyncpg 返回 text），需反序列化
        contexts = _load_json_list(snap.get("reranked_texts"))
        contexts = [c for c in contexts if c and len(str(c).strip()) >= 10]
        if not contexts:
            logger.info(
                "跳过快照 task_id=%s dimension=%s competitor=%s：精排上下文为空",
                task_id[:8], dimension, competitor or "-",
            )
            continue

        # 根据维度从`reference_map`取参考文本；没有则用维度名作为参考
        reference = (reference_map or {}).get(dimension, dimension)

        # 打分 key：竞品×维度（含竞品时），保证同维度多竞品不互相覆盖
        score_key = f"{competitor}@{dimension}" if competitor else dimension

        logger.info(
            "评估快照 [%d/%d] task_id=%s key=%s 上下文数=%d",
            idx, len(snapshots), task_id[:8], score_key, len(contexts),
        )

        # ！！！！核心调用：传入评判 LLM、用户 query、召回上下文分片、参考文本，
        # 计算 context_precision（上下文精确率）
        # context_precision：每条 context 是否与用户query相关，因此需要传query
        precision = await _score_context_precision(judge, query, contexts, reference)

        dim_scores[score_key] = {
            "context_precision": precision,
        }
        logger.info(
            "快照打分完成 task_id=%s key=%s context_precision=%s",
            task_id[:8], score_key, precision,
        )

    result: dict = {
        "task_id": task_id,
        "dimensions": dim_scores,
        "judge_model": JUDGE_MODEL,
    }

    # 平均 context_precision
    if dim_scores:
        precisions = [v["context_precision"] for v in dim_scores.values()]
        result["avg_context_precision"] = round(sum(precisions) / len(precisions), 3)

    # 召回覆盖度 recall_hit_rate（规则式，吃 recalled_urls 全集）
    if reference_domains:
        result["recall_hit_rate"] = _compute_recall_hit_rate(snapshots, reference_domains)
        logger.info(
            "召回覆盖度 task_id=%s recall_hit_rate=%s reference_domains=%s",
            task_id[:8], result["recall_hit_rate"], reference_domains,
        )

    # 落库（检索评估按竞品×维度细粒度，失败不阻断）
    metrics: list[dict] = []
    for score_key, d in dim_scores.items():
        metrics.append({
            "metric_name": "context_precision",
            "dimension": score_key,  # "竞品@维度" 或纯维度名（历史快照）
            "score": d["context_precision"],
        })
    if "recall_hit_rate" in result:
        metrics.append({
            "metric_name": "recall_hit_rate",
            "dimension": None,  # 召回覆盖度是任务级，非单维度
            "score": result["recall_hit_rate"],
        })
    overall = result.get("avg_context_precision")
    await _persist_eval_result(
        dao,
        task_id,
        "retrieval",
        report_id=None,  # 检索评估发生在成稿前，无报告
        overall_score=overall,
        gate=None,
        metrics=metrics,
        metadata={"is_golden": bool(reference_map), "has_recall": "recall_hit_rate" in result},
    )

    logger.info(
        "检索质量评估完成 task_id=%s avg_context_precision=%s dimensions=%d",
        task_id[:8], result.get("avg_context_precision"), len(dim_scores),
    )

    return result


def _compute_recall_hit_rate(snapshots: list[dict], reference_domains: list[str]) -> float:
    """计算召回覆盖度：recalled_urls 全集覆盖了多少个关键来源域名。

    规则式、0 LLM 成本：把每个快照的 recalled_urls（召回全集，非精排 TopK）
    取并集，域名归一化后与 reference_domains 做集合交集，命中率 = 命中数 / 总锚点数。

    度量的是「召回是否漏掉了关键来源」——这是 context_recall 想测但测不准的
    东西，用域名对拍更稳健、更可解释。
    """
    if not reference_domains:
        return 0.0

    recalled_domains: set[str] = set()
    for snap in snapshots:
        urls = _load_json_list(snap.get("recalled_urls"))
        for u in urls:
            d = _extract_domain(u)
            if d:
                recalled_domains.add(d)

    if not recalled_domains:
        return 0.0

    hit = sum(1 for d in reference_domains if d in recalled_domains)
    return round(hit / len(reference_domains), 3)


def _extract_domain(url: str) -> str | None:
    """从 URL 提取归一化域名（如 https://www.feishu.cn/a → feishu.cn）。

    去掉协议、www 前缀、端口、路径，只保留注册域名。非法输入返回 None。
    """
    from urllib.parse import urlparse

    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url or url.startswith("http://") is False and url.startswith("https://") is False:
        # 容忍无协议前缀（如裸 "feishu.cn"）
        if "://" not in url and "/" not in url and "." in url:
            host = url
        else:
            return None
    else:
        host = urlparse(url).netloc

    if not host:
        return None
    # 去端口
    host = host.split(":", 1)[0]
    # 去 www.
    if host.startswith("www."):
        host = host[4:]
    return host.lower() or None


def _load_json_list(value) -> list:
    """反序列化 JSONB 列表字段（asyncpg 可能返回 str 或已解析的 list）。"""
    import json

    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


async def _score_context_precision(judge, question: str, contexts: list[str], reference: str) -> float:
    """
    单维度的 context_precision：复用官方 context_precision 指标，不手写 AP。
    注意：该指标 _required_columns 只要求 user_input + retrieved_contexts + reference（无需 response），
    正好对应检索评估「context vs 标准答案」的场景。

    :param judge: 评判LLM，LangchainLLMWrapper包装后的实例
    :param question: 用户原始query问题
    :param contexts: 重排序后的上下文分片列表，顺序会影响AP分数
    :param reference: 参考标准答案 ground‑truth
    :return: context_precision分数，保留3位小数
    """
    # 导入ragas异步评估相关组件
    from ragas import EvaluationDataset, SingleTurnSample, aevaluate
    from ragas.metrics import context_precision

    # ========== 关键：旧式RAGAS指标单例模式 ==========
    # context_precision 是一个全局单例对象，不是类实例；
    # 必须手动给它赋值 llm，指定用哪个Judge模型做打分；
    # 这里复用和生成质量评估相同的judge模型，保持打分标准一致
    context_precision.llm = judge

    # 构造单条评估样本
    # SingleTurnSample：RAGAS标准单轮对话样本对象
    sample = SingleTurnSample(
        user_input=question,          # 用户提问，对应原逻辑的question
        retrieved_contexts=contexts,  # 检索返回的分片列表（重排序后的顺序）
        reference=reference,          # 参考标准答案，ground‑truth
        # 不需要 response（模型生成的回答），context_precision只评估检索，不需要答案
    )

    # 构建评估数据集：只包含这一条样本
    eval_dataset = EvaluationDataset(samples=[sample])

    # 异步执行RAGAS评估 aevaluate，传入数据集 + 需要计算的指标
    # 内部会自动：遍历contexts分片、调用Judge‑LLM获取verdicts、执行AP平均精确率计算
    result = await aevaluate(
        dataset=eval_dataset,
        metrics=[context_precision],
    )

    # 将评估结果转为pandas DataFrame，方便取数值
    df = result.to_pandas()

    # 边界防护：结果为空，或者没有context_precision字段，返回0分
    if df.empty or "context_precision" not in df.columns:
        return 0.0

    # 取出第一条样本的context_precision分数，保留3位小数返回
    return round(float(df.iloc[0]["context_precision"]), 3)


