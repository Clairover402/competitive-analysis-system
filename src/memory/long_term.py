"""LongTermMemoryEngine — 五步混合检索引擎 + 记忆写入。

═══════════════════════════════════════════════════════════════════════════════
                          【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 五步检索流水线: 重写 → 混合检索 → RRF 融合 → 元数据过滤 → 精排
  §2 RRF（Reciprocal Rank Fusion）: 排名融合解决跨检索器分数不可比问题
  §3 三因子加权排序: 语义相似度 + 重要性 + 时间衰减，SQL 层一次完成
  §4 检索精度分层: Bi-encoder 粗排（召回） → Cross-encoder 精排（精准）


═══════════════════════════════════════════════════════════════════════════════
                     【L4 工程 — 五步检索全链路详解】
═══════════════════════════════════════════════════════════════════════════════

Step 1: Query 重写（LLM 泛化 + 去口语化）
  ──────────────────────────────────────
  输入: "飞书上次调查的那个定价是不是改了"
  输出: "飞书 定价变化"
  原理: LLM 去掉填充词（"上次调查的那个"/"是不是"）→ 提取关键词组合
  价值: 口语化 query 直接做向量检索 → embedding 模型被干扰 → 召回质量下降
  重写后 query 更"像"训练数据中的搜索查询 → embedding 质量 ↑

  fallback: LLM 重写失败 → 返回原始 query（不失功能，仅降精度）

Step 2: 混合检索（双路并行）
  ──────────────────────────
  路 A: Bi-encoder 向量检索（BGE-M3 1024 维）
    — 将重写后 query → embed → pgvector <=> 余弦距离算子
    — top_k=60 → 返回最相似的 60 条记忆
    — 优点: 语义层面匹配（"定价" 能匹配 "收费"/"费用" 等近义词）
    — 限制: 对精确术语和数字的匹配一般

  路 B: 关键词检索（zhparser 分词 + pg_bigm N-gram）
    — zhparser 中文分词 → to_tsvector('zhparse', content)
    — 纯文本匹配: "飞书 定价" 只返回包含这两个词的内容
    — top_k=30 → 返回匹配度最高的 30 条
    — 优点: 精确匹配产品名/品牌/数字等
    — 限制: "飞书多少钱" 和 "飞书定价" 不匹配（词不同）

  双路并行的原因: 语义检索和精确检索是互补的——
    向量能召回 "定价策略" 但可能漏掉 "飞书2025Q1定价更新"（精确术语）
    关键词能命中 "飞书" 和 "定价" 但召回不了 "飞书收费模式"（词不同但语义相近）

Step 3: RRF 融合（排名级融合）
  ──────────────────────────────
  问题: 向量检索和关键词检索的分数不可比
    向量: 余弦相似度 0.95（范围 0~1）
    关键词: pg_bigm 匹配度 4.2（无固定范围）
  直接用分数加权 → 关键词会碾压或完全被忽略

  RRF 解法: 用排名代替分数
    RRF(doc) = Σ 1/(k + rank_i)
    k=60: 来自 Cormack 2009 原论文，使头部权重平滑
    效果: rank#1 和 rank#2 的 RRF 差距仅 ~1.02 倍

  RRF 对不对称召回的处理:
    向量返回 60 条，关键词返回 30 条
    → 关键词没返回的 doc 在关键词路的 RRF 得分 = 0
    → 不影响向量路的排名
    这就是 RRF 的优雅之处——不要求两个检索器返回同样数量的结果

  RRF 融合后取前 60 条 doc

Step 4: 元数据过滤（SQL 层）
  ─────────────────────────
  在 similarity_search 和 keyword_search 的 SQL 中已加入:
    WHERE is_active = true             — 排除已归档/删除的记忆
    AND user_id = $1                    — 多用户隔离
  不需要 Python 层再过滤——DB 层已经做了。

Step 5: Cross-encoder 精排
  ─────────────────────────
  将 RRF 融合后的 Top 60 → query + document 拼接 → Cross-encoder 模型
  → 重新排序 → 取 Top K（默认 10）
  
  Cross-encoder vs Bi-encoder 的本质区别:
  ┌─────────────────┬──────────────────────┬──────────────────────┐
  │                 │ Bi-encoder（粗排）     │ Cross-encoder（精排） │
  ├─────────────────┼──────────────────────┼──────────────────────┤
  │ 输入            │ query 和 doc 分别编码   │ query + doc 拼接编码 │
  │ query-doc 交互  │ 无（独立编码后算余弦）   │ 有（Attention 跨句） │
  │ 速度            │ 快（doc 可预计算缓存）   │ 慢（每次新 query 重算）│
  │ 精度            │ 中                      │ 高                   │
  │ 适用            │ 大规模候选（>1000 条）    │ 小规模精排（<100 条） │
  └─────────────────┴──────────────────────┴──────────────────────┘
  
  为什么不在 Step 2 中用 Cross-encoder 检索全部？
  全量 Cross-encoder: 1000 条 × 每条拼接编码 → 不可行（太慢 + 太贵）
  分层策略: Bi-encoder 快速捞 Top 60 → Cross-encoder 精准排 Top 10
  这就是"粗排 + 精排"的工程精髓——不追求一步到位，分两层逼近最优。


═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — 三因子加权排序公式】
═══════════════════════════════════════════════════════════════════════════════

三条记忆，同一次检索:

  记忆 A: "飞书企业版 ¥200/人/月"（创建 3 天前，importance=0.7，相似度=0.94）
  记忆 B: "飞书企业版 ¥180/人/月"（创建 30 天前，importance=0.8，相似度=0.89）
  记忆 C: "飞书标准版免费"（创建 90 天前，importance=0.5，相似度=0.72）

按原始相似度排序: A(0.94) > B(0.89) > C(0.72)

加入三因子加权后:
  记忆 A: 0.6×0.94 + 0.3×0.7 + 0.1×0.5^(3/90) = 0.564+0.21+0.098 = 0.872
  记忆 B: 0.6×0.89 + 0.3×0.8 + 0.1×0.5^(30/90) = 0.534+0.24+0.077 = 0.851
  记忆 C: 0.6×0.72 + 0.3×0.5 + 0.1×0.5^(90/90) = 0.432+0.15+0.05 = 0.632

最终排序: A(0.872) > B(0.851) > C(0.632)

记忆 A(0.872) 仍排第一（相似度 0.94 主导 60% 权重），但记忆 B(0.851)
凭借更高的 importance(0.8 vs 0.7) 大幅缩小差距。
对比乘法公式：A=0.614 vs B=0.356（差距 0.258，A 是 B 的 1.72 倍），
加法公式：A=0.872 vs B=0.853（差距 0.019，几乎平手）。
这才是正确行为——两条高相关近期记忆不应因 importance 微小差异被拉大差距。

为什么是 (0.6, 0.3, 0.1) 的权重分配？
  — 相似度 60%: 检索的核心是"找到最相关的内容"，因此权重最大（与旧版一致）
  — 重要性 30%: 从 20% 提升至 30%，加强 memory_type 对排序的影响
  — 时间 10%: 从 20% 降至 10%，减弱时间衰减的影响——旧但重要的记忆不再被过度打压

  为什么从乘法改为加法？
  — 乘法的问题: 任一因子接近 0 时整条记忆直接被淘汰
    例: sim=0.95 脳 imp=0.9 脳 decay=0.25 = 0.21（半年前的 decision，极其相关但被淘汰）
  — 加法的优势: 三因子独立贡献，不互相钳制
    例: 0.6脳0.95 + 0.3脳0.9 + 0.1脳0.25 = 0.87（同一条记忆，加法下排名正常）

SQL 层排序的优势:
  — 一次查询到结果，不在 Python 和 DB 之间来回传数据
  — PostgreSQL 的 ORDER BY 表达式可以包含算术运算、函数调用
  — 不需要把 100 条结果取到 Python、算加权分、再排序——这些在 DB 里全做完


═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — add_memory 的冲突检测流水线】
═══════════════════════════════════════════════════════════════════════════════

写入一条新记忆时:
  1. 映射 importance + half_life（从 _IMPORTANCE_MAP 查类型默认值）
  2. embed(content) → 1024 维向量
  3. 如果有 conflict_resolver → detect_conflict → resolve → soft_delete
  4. 插入 agent_memories（含 embedding + importance + half_life_days）

冲突检测的可选性:
  — 有 conflict_resolver → 写入前做冲突检测（标准流程）
  — conflict_resolver=None → 直接插入（不检测冲突，更快但可能有重复记忆）
  可选让 Pipeline 和单测用例可以复用同一个 engine——Pipeline 传 resolver，
  单测不传（避免 mock 额外的依赖）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.db.dao import AgentMemoryDAO
from src.mcp.tools_rag import embed_query, rerank

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.memory.conflict import MemoryConflictResolver

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# LLM Prompt — Query 重写
# ══════════════════════════════════════════════════════════════════════════════

_REWRITE_PROMPT = """你是查询改写器。将口语化问题转化为关键词检索查询。

用户问题: %s

只返回改写后的检索关键词（不要其他文字）。去掉填充词和口语化表达。"""

# ══════════════════════════════════════════════════════════════════════════════
# 记忆类型 → (默认 importance, 默认 half_life_days)
# ══════════════════════════════════════════════════════════════════════════════

# 【L5 决策】为什么每种类型有不同默认值？
# ──────────────────────────────────
# decision: 高重要性(0.9) + 长半衰期(90天) → 用户的关键决策必须长期保留
# preference: 中高重要性(0.7) + 中等半衰期(60天) → 偏好会变化但变化不快
# fact: 中等重要性(0.5) + 中等半衰期(30天) → 产品定价/功能等事实信息
# chat: 低重要性(0.1) + 短半衰期(7天) → 闲聊记忆一周内衰减到几乎为零

_IMPORTANCE_MAP = {
    "decision":   (0.9, 90),    # 关键决策 — 长保留 + 高权重
    "preference": (0.7, 60),    # 用户偏好 — 中保留 + 中高权重
    "fact":       (0.5, 30),    # 事实信息 — 中保留 + 中权重
    "chat":       (0.1, 7),     # 闲聊 — 短保留 + 低权重
}


# ══════════════════════════════════════════════════════════════════════════════
# RRF（Reciprocal Rank Fusion）— 排名级跨检索器融合
# ══════════════════════════════════════════════════════════════════════════════

def _rrf_fusion(
    vec_results: list[dict],
    kw_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """双向检索结果 RRF 融合: RRF(doc) = Σ 1/(k + rank_i)。

    【L3 核心考点】RRF 的三个核心问题

    问题 1: 为什么不能用分数直接加权？
    ─────────────────────────────
      向量检索分数: 余弦相似度 0.0 ~ 1.0（有界）
      关键词检索分数: pg_bigm 匹配度 0 ~ ∞（无界）
      两个尺度完全不同 → 直接 0.6×vec + 0.4×kw → 关键词可能碾压一切

    问题 2: 为什么用排名而不是分数？
    ─────────────────────────────
      "排名" 是统一尺度——向量检索的 #1 和关键词检索的 #1 都是 #1。
      不关心分数大不大，只关心谁排前面。

    问题 3: k=60 的作用？
    ──────────────────
      RRF 公式: 1/(k + rank)
      k=0:  rank#1=1.0, rank#2=0.5, rank#3=0.33 → 头部极陡
      k=60: rank#1≈0.0164, rank#2≈0.0161, rank#3≈0.0159 → 头部平滑
      大 k 使排名差异的影响变小——不会因为某个检索器把 A 排第一、
      另一个把 A 排第五而导致 A 直接掉出 Top 10。

    【L4 工程】去重键为什么用 content[:80]？
    ──────────────────────────────────────
      不能用 memory_id 去重——两条不同检索器返回的同一 doc 有不同 ID。
      content[:80] 是一个"近似文档指纹"——前面 80 个字符几乎不可能
      出现在两篇不相关的文档中。长了浪费内存，短了可能碰撞。

    实现细节:
      — 两个检索器各贡献一轮 1/(k+rank) 加分
      — 同一文档在两边都出现 → 两次加分累加 → 排名提升
      — 只在关键词路出现（不在向量路）→ RRF = 1/(k+rank_kw)（正常，不被惩罚）
    """
    # ── 向量路贡献 ──
    rrf_scores: dict[str, float] = {}
    rrf_docs: dict[str, dict] = {}

    for rank, doc in enumerate(vec_results):
        doc_id = doc.get("content", "")[:80]
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        rrf_docs[doc_id] = doc

    # ── 关键词路贡献 ──
    for rank, doc in enumerate(kw_results):
        doc_id = doc.get("content", "")[:80]
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        if doc_id not in rrf_docs:
            rrf_docs[doc_id] = doc

    # ── 按 RRF 分数降序排列，取前 k 条 ──
    merged = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return [rrf_docs[doc_id] for doc_id, _ in merged[:k]]


# ══════════════════════════════════════════════════════════════════════════════
# LongTermMemoryEngine — 五步检索引擎
# ══════════════════════════════════════════════════════════════════════════════

class LongTermMemoryEngine:
    """长期记忆检索引擎——五步流水线 + 记忆写入。

    【L3 核心概念】五步流水线
    ──────────────────────
    Step 1: Query 重写 — LLM 去口语化 + 提取关键词
    Step 2: 混合检索 — Bi-encoder 向量(top_k=60) + pg_bigm 关键词(top_k=30)
    Step 3: RRF 融合 — 排名级双路合并，取前 60
    Step 4: 元数据过滤 — SQL 层 WHERE is_active + user_id（已在 DAO 中）
    Step 5: Cross-encoder 精排 — top_k 条精准排序

    【L3 核心概念】精度分层（Two-Stage Retrieval）
    ─────────────────────────────────────────
      粗排（Step 2-3）: Bi-encoder → 高召回低精度，海量文档中快速筛选
      精排（Step 5）: Cross-encoder → 高精度低速度，少量候选精准排序
      两层合起来: 既不掉召回（粗排覆盖全），又不失精度（精排筛精准）

    用法:
        engine = LongTermMemoryEngine(llm, dao, conflict_resolver)
        results = await engine.retrieve(user_id, "定价策略", top_k=10)
        memory_id = await engine.add_memory(user_id, "飞书企业版 ¥200/人/月", "fact")
    """

    def __init__(
        self,
        llm: ChatDeepSeek,
        dao: AgentMemoryDAO,
        conflict_resolver: MemoryConflictResolver | None = None,
    ) -> None:
        """初始化检索引擎。

        Args:
            llm: ChatDeepSeek 实例（温度 0.1，检索需要精准）
            dao: agent_memories 表的 DAO 层
            conflict_resolver: 冲突解决器（可选——不传则跳过冲突检测）

        【L5 决策】为什么 conflict_resolver 是可选的？
        ─────────────────────────────────────────
          冲突检测是写入链路的事，检索链路不需要。
          单测时可以只创建 engine 不传 resolver → 减少 mock 负担。
          这是"分层依赖"策略: 基础功能只需要 llm + dao，
          冲突检测是增强功能，加在 resolver 参数上。
        """
        self._llm = llm
        self._dao = dao
        self._conflict = conflict_resolver

    # ══════════════════════════════════════════════════════════════════════
    # 检索 — 五步流水线
    # ══════════════════════════════════════════════════════════════════════

    async def retrieve(
        self, user_id: str, query: str, top_k: int = 10
    ) -> list[dict]:
        """五步检索流水线入口。

        【L4 工程】两条 early return 路径
        ─────────────────────────────
        1. embed_query() 失败 → 返回空列表（无法做向量检索，关键词也不够精确）
        2. RRF 融合后为空 → 返回空列表（两种检索器都没命中）

        early return 不是 bug——它防止空数据流入 Cross-encoder 精排层。

        【L3 核心考点】top_k 的三层含义
        ───────────────────────────
        Step 2 向量检索: top_k=60（固定——需要足够大的候选池让 RRF 有意义）
        Step 2 关键词检索: top_k=30（固定——关键词命中比向量少，设少点合理）
        Step 5 精排: top_k 由调用方指定（默认 10）——这是最终返回数量
        """
        # ── Step 1: Query 重写 ──
        rewritten = await self._rewrite_query(query)

        # ── Step 2: 混合检索 ──
        query_vec = await embed_query(rewritten)
        if not query_vec:
            logger.warning("embed_query 失败: %r", rewritten)
            return []  # early return 1: 向量检索不可用

        # 【L5 决策】向量和关键词的 top_k 不对称的原因
        # 向量检索（top_k=60）: 语义层面匹配更广，需要更大的候选池
        # 关键词检索（top_k=30）: 精确匹配，通常命中少于向量
        vec_results = await self._dao.similarity_search(user_id, query_vec, top_k=60)
        kw_results = await self._dao.keyword_search(user_id, rewritten, top_k=30)

        # ── Step 3: RRF 融合 ──
        merged = _rrf_fusion(vec_results, kw_results, k=60)

        # ── Step 4: 元数据过滤（SQL 层已做，Python 层无需额外操作） ──
        if not merged:
            return []  # early return 2: 两路都没命中

        # ── Step 5: Cross-encoder 精排 ──
        documents = [d.get("content", "") for d in merged]
        if len(documents) <= top_k:
            # 【L4 工程】如果 RRF 候选数 ≤ 需要的 top_k，跳过精排
            # 精排 5 条里取 5 条是无意义的——就是换个顺序
            return merged

        ranked = await rerank(query, documents, top_k=top_k)
        if not ranked:
            # 【L4 工程】rerank 失败 → 降级返回 RRF 融合的 top_k 结果
            # 宁可没有精排，也不能没有结果
            return merged[:top_k]

        # ── 构造最终结果（附加 rerank score） ──
        result = []
        for r in ranked:
            idx = r.get("index", 0)
            if idx < len(merged):
                doc = dict(merged[idx])
                doc["rerank_score"] = r.get("score", 0)
                result.append(doc)

        logger.info("检索完成: 查询=%r, 候选=%d, 返回=%d",
                     query, len(merged), len(result))
        return result

    # ══════════════════════════════════════════════════════════════════════
    # Query 重写（内部方法）
    # ══════════════════════════════════════════════════════════════════════

    async def _rewrite_query(self, query: str) -> str:
        """Step 1: LLM Query 重写（去口语化 + 泛化）。

        【L3 核心概念】为什么需要 query 重写？
        ──────────────────────────────────
        embedding 模型（BGE-M3）在训练时看到的 query 是搜索风格的:
          "飞书定价"  → embedding 精准
          "飞书上次调研的那个定价是不是改了" → embedding 被填充词干扰 → 失真

        LLM 重写的作用: 去掉填充词，提取核心关键词组合。
        就像把"那个什么来着，上次说的那个地方的天气" → "XX 天气"——
        embedding 模型只看到那 2-3 个核心词。

        【L4 工程】重写失败的 fallback
        ──────────────────────────
        LLM 异常 → 返回原始 query（不优雅但可用）
        原始 query 直接给 embedding 模型 → 精度降低但不会零召回
        """
        try:
            prompt = _REWRITE_PROMPT % query
            resp = await self._llm.ainvoke(prompt)
            rewritten = resp.content.strip()
            if len(rewritten) > 3:          # 重写结果至少 3 个字符才有效
                logger.debug("Query 重写: %r → %r", query, rewritten)
                return rewritten
        except Exception:
            logger.debug("Query 重写失败: %r", query)
        return query  # fallback: 用原始 query

    # ══════════════════════════════════════════════════════════════════════
    # 记忆写入 — 含冲突检测
    # ══════════════════════════════════════════════════════════════════════

    async def add_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str = "fact",
        importance: float | None = None,
        half_life_days: int | None = None,
        source_task_id: str | None = None,
    ) -> str:
        """写入长期记忆——含冲突检测。

        全流程:
          1. 查 _IMPORTANCE_MAP 获取类型默认值
          2. 调用方传了自定义 importance/half_life → 覆盖默认值
          3. 调用 embed_query(content) 生成 1024 维向量
          4. 如果有 conflict_resolver → detect → resolve → soft_delete
          5. DAO.insert() 写入 agent_memories 表

        Args:
            user_id: 用户 ID（多用户隔离）
            content: 记忆内容文本
            memory_type: 记忆类型（decision/preference/fact/chat）
            importance: 重要性评分（0~1，None 则从 _IMPORTANCE_MAP 取）
            half_life_days: 半衰期天数（None 则从 _IMPORTANCE_MAP 取）
            source_task_id: 来源任务 ID（追溯记忆由哪个分析任务产生）

        Returns:
            新记忆的 UUID

        【L5 决策】importance 和 half_life 的参数设计
        ──────────────────────────────────────────
        _IMPORTANCE_MAP 定义了每种类型的"默认值"。
        但调用方可以通过 importance=0.95 覆盖（例如: 用户明确标记为"非常重要"）。
        两层设计:
          — 类型层: decision=0.9（大部分 decision 都是用这个值）
          — 调用层: 可以覆盖（特定几笔 decision 可能重要性为 1.0）

        【L4 工程】source_task_id 的追溯价值
        ─────────────────────────────────
        从分析报告中提取的记忆携带 source_task_id——知道"这句话来自哪次分析"。
        出问题时可以溯源: 这次分析的结论提取出了什么记忆？
        这是一个可观测性字段——不必需但你不想没有。
        """
        # ── 1. 取默认值 + 调用方覆盖 ──
        imp, half = _IMPORTANCE_MAP.get(
            memory_type,
            (importance or 0.5, half_life_days or 30)
        )
        if importance is not None:
            imp = importance
        if half_life_days is not None:
            half = half_life_days

        # ── 2. 生成 embedding ──
        embedding = await embed_query(content)

        # ── 3. 冲突检测（如果启用了 resolver） ──
        if embedding and self._conflict:
            conflicts = await self._conflict.detect_conflict(user_id, content)
            if conflicts:
                # 检测到冲突 → LLM 仲裁 → 软删除应删除的旧记忆
                await self._conflict.resolve(content, conflicts)

        # ── 4. 写入 DB ──
        memory_id = await self._dao.insert(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            importance=imp,
            embedding=embedding if embedding else None,
            source_task_id=source_task_id,
            half_life_days=half,
        )

        logger.info("长期记忆写入成功: id=%s, 类型=%s, 重要性=%.1f",
                     memory_id[:8], memory_type, imp)
        return memory_id
