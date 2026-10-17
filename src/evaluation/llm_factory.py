# -*- coding: utf-8 -*-
"""评估用的 judge / embeddings / RunConfig 构造器 + reference 解析（唯一实现）。

═══════════════════════════════════════════════════════════════════════════════
                         【为什么单独一个模块】
═══════════════════════════════════════════════════════════════════════════════

  2026-10-17：本模块从 ragas_llm.py 抽出，随后 ragas_llm.py 整体删除。

  老链路把「构造 judge」和「落库 + 门禁 + 归档 + 趋势上报」焊在同一个 938 行文件里，
  于是任何只想跑指标的地方（新引擎、诊断脚本、单测）都得 import 一整个工程外壳，
  还得连带启动 DB 连接与 Prometheus。抽出后：

    · ragas_core（四指标唯一实现）只依赖本模块，不碰任何落库/上报逻辑；
    · 模型名、超时口径、reference 解析规则**只有一处定义**，不会两条链路各写一份；
    · 删掉老链路时，这些被真实使用的纯函数有了明确归宿，不必跟着一起删。

═══════════════════════════════════════════════════════════════════════════════
                      【两个 ragas 0.4.3 的兼容坑（已处理）】
═══════════════════════════════════════════════════════════════════════════════

  1. ragas 顶层硬 import `langchain_community.chat_models.vertexai`，
     但该模块在新版 langchain-community 已移除 → 注入占位模块兜底。
     ⚠️ 必须在 `import ragas` **之前**执行，故本模块的 import 顺序不能重排。

  2. ragas 0.4.3 有两套指标：collections（新式，SimpleBaseMetric）与
     _ 下划线模块（旧式，Metric）。`aevaluate()` 的 isinstance 检查只认旧式，
     collections 指标会报 "All metrics must be initialised"。
     → 指标侧统一用旧式（见 ragas_core.py），本模块只负责构造 judge，不受影响。
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import sys
import types

# ── monkey-patch：ragas 0.4.3 顶层 import 了不存在的 vertexai 模块 ──
# 必须在 import ragas 之前注入，否则 ModuleNotFoundError。
# 用 `not in sys.modules` 守卫，保证多处 import 时只注入一次。
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _vertexai_stub = types.ModuleType("langchain_community.chat_models.vertexai")

    class _ChatVertexAI:  # 占位类：仅满足 isinstance 检查，本项目不用 Vertex AI
        pass

    _vertexai_stub.ChatVertexAI = _ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = _vertexai_stub

from langchain_deepseek import ChatDeepSeek  # noqa: E402
from langchain_openai import OpenAIEmbeddings  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.run_config import RunConfig  # noqa: E402

logger = logging.getLogger(__name__)

# ── judge 模型：deepseek-chat，与生成模型 deepseek-v4-flash 隔离 ──
# 模型隔离原则：裁判与运动员不能是同一个人，否则"自己评自己"有系统性偏好。
JUDGE_MODEL = "deepseek-chat"


def build_judge_llm(settings) -> LangchainLLMWrapper:
    """构造 ragas 评估 judge（DeepSeek，OpenAI 兼容协议）。

    ragas 内部定义了自己的 LLM 抽象（`BaseRagasLLM`），不能直接传 LangChain 的
    ChatDeepSeek，必须用 `LangchainLLMWrapper` 做适配，否则指标无法调用。

    为什么 temperature=0：评估是判定任务，要可复现。但注意——温度 0 不能消除
    `context_recall` 的二值抖动（那是"整句判 Yes/No"的分母问题，见 golden_dataset
    的"事实点化"说明），它能压的只是采样随机性。
    """
    judge = ChatDeepSeek(
        model=JUDGE_MODEL,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=0.0,
        request_timeout=120.0,
        max_retries=2,
    )
    return LangchainLLMWrapper(judge)


def build_embeddings(settings) -> LangchainEmbeddingsWrapper:
    """构造评估用 embeddings（SiliconFlow 的 BGE-M3，OpenAI 兼容）。

    只有 answer_relevancy 需要：它把 response 反推成「问题」，再与原 user_input
    算语义相似度。其余三个指标纯 LLM 判定，不用 embedding。
    """
    emb = OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )
    return LangchainEmbeddingsWrapper(emb)


def build_run_config(settings) -> RunConfig:
    """按配置构造 ragas 的 RunConfig（单作业超时 + 并发上限）。

    【为什么必须显式传，不能用 ragas 默认】
      ragas 默认 `RunConfig(timeout=180, max_workers=16)`。那个 180s 是**单个
      (样本, 指标) 作业**的上限，而 `context_precision` 对 retrieved_contexts 是
      **串行**逐条判定的（ragas 0.4.3 `LLMContextPrecisionWithReference._ascore`
      里就是 `for context in retrieved_contexts:`）。于是「12 条 contexts 的长样本」
      必须在 180s 内接连跑完 12 次 judge 调用才不超时 —— 实际跑不完，整个作业被
      `asyncio.wait_for` 掐断，分数归档为 np.nan。
      2026-10-16 的 `钉钉@功能 cp=NaN` + 日志 `Job[12]: TimeoutError()` 正是此机制
      （12 是全局作业号，16 个作业里第 12 个正好是该 cell 的 context_precision）。

      两处调整：
        · timeout 180 → 配置值（默认 600）：宁可慢，不可静默丢一格分。
          （ragas 会把这个值同时写进 langchain_llm.request_timeout，
           故它也覆盖了 build_judge_llm 里那个 120s 的 HTTP 超时。）
        · max_workers 16 → 8：4 样本 × 4 指标 = 16 个作业同时在打 judge，
          并发过高容易触发上游限流 → 重试 → 更慢 → 反而更容易撞 timeout。
    """
    timeout = int(getattr(settings, "ragas_eval_timeout", 600.0) or 600.0)
    max_workers = int(getattr(settings, "ragas_eval_max_workers", 8) or 8)
    return RunConfig(timeout=timeout, max_workers=max(1, max_workers))


def resolve_reference(
    dimension: str,
    reference_map: dict[str, str] | None,
    task_id: str = "",
    *,
    competitor: str = "",
    reference_cell_map: dict[str, str] | None = None,
) -> str:
    """取该 cell 的 ground truth 文本（**优先 cell 级**，缺失回退维度级）。

    【为什么基准必须优先切到 cell 级】
    ──────────────────────────────
    评估单元是 (竞品, 维度) —— 每条 retrieval_snapshot 就是一个 cell。
    `reference_map` 是**维度级**且**跨竞品复合**（"飞书…；钉钉…"），把它喂给
    某个 cell，判据里必然有一半讲的是**对方竞品**、按定义不可能被检索结果支持
    ⇒ `context_recall` 天花板被钉死 0.5、`context_precision` 系统性趋 0。
    2026-10-16 实测：`飞书@功能 cr=0.500` —— 正好"半句"，即此机制的直接证据。
    故首选 `reference_cell_map["竞品@维度"]`（只讲该竞品）；
    真实任务 / 历史数据没有 cell 表 → 回退维度级 → 再退化维度名。

    【为什么必须告警，不能静默兜底】
    ──────────────────────────────
    原实现是 `(reference_map or {}).get(dimension, dimension)`：维度名只要差一个字
    （hard 档的维度来自 Supervisor/LLM 生成，无强一致保证），就会**静默**退化成
    「拿维度名本身当标准答案」——judge 拿 "功能" 两个字去判上下文是否支撑，分数
    彻底失真，而且不报错、不打日志，跑出来和正常的一模一样。
    这类「静默失真」比直接抛错危险得多，故此处显式 warning。

    ⚠️ 真实任务（两级 map 都为 None）本来就用维度名兜底，属设计本意，不告警。
    """
    cell_key = f"{competitor}@{dimension}" if competitor else dimension
    # ① 首选：cell 级标准答案（单竞品，与评估单元同粒度）。
    if reference_cell_map and cell_key in reference_cell_map:
        return reference_cell_map[cell_key]
    # ② cell 表在、但缺该 cell → 属数据作者遗漏：显式告警后回退维度级。
    if reference_cell_map and competitor and cell_key not in reference_cell_map:
        logger.warning(
            "cell 级标准答案缺失 task_id=%s cell=%r → 回退维度级 reference"
            "（跨竞品复合句，该 cell 的 cp/cr 天花板约 0.5，分数不可直接采信）",
            task_id[:8] if task_id else "-",
            cell_key,
        )
    # ③ 回退：维度级（跨竞品复合句；真实任务走这里）。
    if not reference_map:
        return dimension
    if dimension in reference_map:
        return reference_map[dimension]
    logger.warning(
        "标准答案缺失 task_id=%s 维度%r 不在 reference_map（键=%s）→ "
        "context_precision 将退化为用「维度名」作 ground truth，该维度分数不可信",
        task_id[:8] if task_id else "-", dimension, list(reference_map.keys()),
    )
    return dimension


def load_json_list(value) -> list:
    """反序列化 JSONB 列表字段（asyncpg 可能返回 str 或已解析的 list）。

    不做类型纠正：解析结果不是 list 就返回空列表，宁可少几条 contexts
    也不要把一个 dict 塞进 retrieved_contexts 让指标层崩掉。
    """
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
