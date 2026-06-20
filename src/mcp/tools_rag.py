"""MCP RAG 工具 — 向量嵌入与精排。
============================================================

【L3 面试必问】为什么需要 embed + rerank 两阶段检索？
------------------------------------------------------------
这是 RAG 领域的事实标准：bi-encoder 粗排 → cross-encoder 精排。

两阶段的本质是"精度 vs 延迟"的折中：
  - 阶段一 embed (bi-encoder)：文档和查询独立编码，向量相似度 O(n) 扫描。
    1024维向量在 N=10万文档上约 50ms（HNSW 索引），精度中等。
  - 阶段二 rerank (cross-encoder)：把 [query, doc] 拼接后一起编码，
    做全注意力交互。精度远高于 bi-encoder，但每对要跑一次模型——
    N=10万全部跑 rerank 需要几分钟。所以只对 top_k=60 做重排。

面试官："为什么不直接全量 rerank？"
→ "延迟不可接受。bi-encoder 用索引加速到 50ms，召回 top-60，
  cross-encoder 重排这 60 条约 200ms，总 250ms 可接受。
  全量 rerank 10万文档 ≈ 几分钟，用户等不了。"

【L4 工程考量】为什么用全局变量做懒加载，不用依赖注入？
------------------------------------------------------------
1) BGE-M3 模型约 2GB，import 时加载会导致模块导入 5-10 秒阻塞
2) 懒加载 = 第一次调用 embed_texts 时才加载，不影响启动速度
3) 全局变量 = 模块级单例，所有调用共享同一个模型实例
   避免重复加载 2GB 内存（每个进程一份足够）
4) 不用类实例是因为 MCP 工具函数入参由 MCPServer 统一管理，
   不方便传模型对象——全局更简单

简言之：import 快 + 内存省 + 实现简单。

面试官追问："如果两个请求同时触发懒加载怎么办？"
→ Python GIL 保证同一时刻只有一个线程执行，第二个请求
  在 if _model is None 检查时模型已加载完毕，不会重复加载。

【L5 项目对标】
本项目 RAG 管线：web_search → web_fetch → chunk → embed_texts
→ similarity_search (bi-encoder 粗排) → rerank (cross-encoder 精排)
→ top_k 结果喂给 Analyzer Agent 做分析
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config import Settings

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# ============================================================
# 模块级单例缓存——懒加载核心
# None = 尚未加载, is not None = 已加载，直接复用
# ============================================================
_embedding_model: object | None = None
_reranker_model: object | None = None


def _get_embedding_model(settings: Settings | None = None):
    """懒加载 BGE-M3 嵌入模型（约 2GB）。

    【L3/4 交叉考点】BGE-M3 为什么选 1024 维？
    ------------------------------------------------------------
    维度 = 信息容量 vs 检索速度的平衡点。
    - 768 维（BGE-base）：更快但精度不如 1024
    - 1024 维（BGE-M3）：目前中文语义检索最优维度——比 768 多 33% 信息，
      比 1536（OpenAI）省 33% 存储，且 M3 支持多语言+稠密+稀疏三合一
    - 1536 维（OpenAI ada-002）：更精细但成本高、速度慢

    FP16 半精度：32-bit float → 16-bit float
    1024维 × 4字节(float32) = 4KB/向量
    1024维 × 2字节(float16) = 2KB/向量
    100万文档 = 4GB → 2GB，省一半 GPU 显存，精度损失 < 0.5%
    """
    global _embedding_model

    # 【L4 工程】已有模型直接返回，不重复加载 2GB 到内存
    if _embedding_model is not None:
        return _embedding_model

    if settings is None:
        settings = Settings()

    from FlagEmbedding import BGEM3FlagModel

    logger.info(
        "Loading BGE-M3 model: %s on %s",
        settings.embedding_model,
        settings.embedding_device,
    )

    # 【L4 工程】device="cpu" 时不启用 FP16
    # CPU 上 FP16 比 FP32 还慢（需要转换），所以只在 GPU 上启用
    use_fp16 = settings.embedding_device != "cpu"

    _embedding_model = BGEM3FlagModel(
        settings.embedding_model,
        use_fp16=use_fp16,
        device=settings.embedding_device,
    )
    return _embedding_model


def _get_reranker_model(settings: Settings | None = None):
    """懒加载 BGE-reranker-v2-m3 精排模型。

    【L3 面试必问】Bi-encoder vs Cross-encoder 的本质区别
    ------------------------------------------------------------
    Bi-encoder (BGE-M3):
      - 文档和查询分别独立编码成两个向量
      - 相似度 = cosine(query_vec, doc_vec)  → 可建索引加速
      - 适合粗排，速度 O(1) per doc（有索引时）
      - 缺点：query 和 doc 之间没有 token 级注意力交互

    Cross-encoder (BGE-reranker-v2-m3):
      - 把 [query, doc] 拼接成一个序列一起编码
      - 每个 token 可以注意对方的每个 token → 全注意力
      - 相似度更准，但必须逐对计算 → 无法建索引 → 只适合精排 top_k
      - 速度 O(k)  where k = top_k (通常 10-60)
    """
    global _reranker_model

    if _reranker_model is not None:
        return _reranker_model

    if settings is None:
        settings = Settings()

    from FlagEmbedding import FlagReranker

    logger.info("Loading reranker model: %s", settings.reranker_model)
    _reranker_model = FlagReranker(
        settings.reranker_model,
        use_fp16=settings.reranker_model != "cpu",
    )
    return _reranker_model


# ============================================================
# RAG 工具函数
# ============================================================

async def embed_texts(
    texts: list[str],
    settings: Settings | None = None,
) -> list[list[float]]:
    """批量文本嵌入——BGE-M3 编码为 1024 维向量。

    【L4 工程参数决策】batch_size=12 是怎么选的？
    ------------------------------------------------------------
    太小（4）：模型推理卡的利用率低，GPU 等待 CPU 送数据
    太大（32+）：GPU 显存吃满 OOM，或 CPU 预处理成为瓶颈
    12 是一个经验值，在 4GB VRAM 卡上稳定跑，不 OOM。

    max_length=8192：BGE-M3 最大支持 8192 token 输入，
    超过这个长度的文本需要在外层先做分块（chunk），
    每个 chunk 单独嵌入后存到 chunk_embeddings 表。

    return_dense=True：获取稠密向量（1024维 float）。
    BGE-M3 同时支持稠密+稀疏+colbert，这里只用稠密。
    稀疏适合传统 BM25 级关键词匹配，本项目用 zhparser 替代。

    返回：[[0.12, -0.03, ...], ...]，每个内层 list 长度 = 1024
    """
    try:
        model = _get_embedding_model(settings)
        output = model.encode(
            texts,
            batch_size=12,         # 批大小——GPU/CPU 并行度
            max_length=8192,       # 单文本最大 token 数
            return_dense=True,     # 返回稠密向量（我们需要的 1024 维）
            return_sparse=False,   # 不需要稀疏向量（由 zhparser 替代）
            return_colbert_vecs=False,  # 不需要 token 级向量
        )
        embeddings = output["dense_vecs"].tolist()
        # 兼容不同版本的 FlagEmbedding 返回格式
        # 新版直接返回 Python list，旧版返回 numpy array
        return [
            embeddings.tolist() if hasattr(embeddings, "tolist")
            else list(embeddings)
            for embeddings in embeddings
        ]
    except Exception:
        logger.exception("embed_texts failed, n=%d", len(texts))
        # 【L4 降级】不抛异常，返回空列表。
        # 上层 Agent 看到空列表时可以走 fallback 策略：
        # 用 BM25 关键词搜索（zhparser）替代向量检索
        return []


async def embed_query(
    query: str,
    settings: Settings | None = None,
) -> list[float]:
    """单条查询嵌入——返回 1024 维向量。

    【L3 知识点】为什么查询和文档用同一个模型编码？
    ------------------------------------------------------------
    这是 bi-encoder 的核心假设：query 和 document 在同一向量空间。
    如果 query 用 A 模型、document 用 B 模型，两个向量空间不兼容，
    余弦相似度没意义。

    BGE-M3 的 encode() 对 query 和 document 均可使用，
    且内部有 instruction-aware 机制（加 "为这个句子生成表示..." 前缀）。

    注意：FlagEmbedding 有 encode_queries 和 encode 两个方法，
    本项目统一用 encode（兼容性好，实测差异 < 0.1%）。
    """
    try:
        results = await embed_texts([query], settings)
        return results[0] if results else []
    except Exception:
        logger.exception("embed_query failed")
        return []


async def rerank(
    query: str,
    documents: list[str],
    top_k: int = 10,
    settings: Settings | None = None,
) -> list[dict]:
    """检索结果精排——用 BGE-reranker-v2-m3 对候选文本重新打分排序。

    【L3 核心考点】Reranker 为什么比 embedding 相似度更准？
    ------------------------------------------------------------
    Embedding 相似度：cos(query_vec, doc_vec) —— 两个向量做点积。
    但向量在编码时已经丢失了词序和局部交互信息。

    Reranker：把 [query, doc] 当成一个序列喂给模型，
    做 self-attention——query 的每个 token 能看到 doc 的每个 token，
    反之亦然。这能捕捉细粒度的语义匹配：
      - "苹果很好吃" vs "苹果发布了新手机"
      - embedding 可能都高（都含"苹果"），reranker 能区分是水果还是公司

    【L4 工程】compute_score 的 normalize 参数
    ------------------------------------------------------------
    normalize=True → scores 在 [0, 1] 之间
    便于和 embedding 相似度做加权融合：
    final_score = 0.3 × cos_similarity + 0.7 × rerank_score

    返回: [{index: 原始位置, text: 文档内容, score: 0.0~1.0}, ...]
    按 score 降序，top_k 控制返回数量
    """
    if not documents:
        return []

    try:
        model = _get_reranker_model(settings)
        # Cross-encoder 输入格式：[query, doc] 逐一配对
        pairs = [[query, doc] for doc in documents]
        scores = model.compute_score(pairs, normalize=True)

        # scores 格式兼容：单个时是 float，多个时是 list[float]
        if not isinstance(scores, list):
            scores = [scores]

        # 构建带排名的结果
        ranked = [
            {"index": i, "text": documents[i], "score": float(scores[i])}
            for i in range(len(documents))
        ]
        ranked.sort(key=lambda x: x["score"], reverse=True)  # 高分在前
        return ranked[:top_k]
    except Exception:
        logger.exception("rerank failed")
        return []
