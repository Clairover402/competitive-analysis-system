"""Analyzer Agent — RAG检索 + 多维度 LLM 分析。

═══════════════════════════════════════════════════════════════════════════════
                         【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Analyzer 是系统的"推理引擎"。它不直接上网，不直接读数据库，只通过
ChunkEmbeddingDAO 和 tools_rag 获取检索结果，再用 LLM 做维度分析。

【L5 决策】为什么每个维度独立检索？
──────────────────────────────────
在"一个 query 搜全部维度"vs"每个维度独立检索"的选择中，选后者的原因：

  ① 检索精度
     "飞书定价策略" → 向量空间接近"定价"语义
     "飞书功能对比" → 向量空间接近"功能"语义
     → 合并成一个 query 会稀释两个维度的检索精度

  ② 故障隔离
     5 个维度用 asyncio.gather(*, return_exceptions=True)
     一维失败（如检索 0 结果、LLM 超时）不影响其他维度

  ③ 并行化
     5 个维度完全独立 → 可以并行执行 → 总时间 = max(各维度时间)


═══════════════════════════════════════════════════════════════════════════════
                     【L3 核心考点 — RAG 检索全链路】
═══════════════════════════════════════════════════════════════════════════════

  Step 1  Step 2      Step 3      Step 4      Step 5        Step 6
  ┌─────┐ ┌────────┐ ┌─────────┐ ┌─────────┐ ┌───────────┐ ┌──────────────┐
  │Query│→│向量检索│→│关键词检索│→│合并去重  │→│Cross-Enc   │→│LLM 生成分析  │
  │嵌入 │ │TopK=60 │ │TopK=30  │ │(URL去重)│ │Reranker   ││(逐维度独立)  │
  └─────┘ └────────┘ └─────────┘ └─────────┘ └───────────┘ └──────────────┘
   BGE-M3   pgvector    PostgreSQL   Python       BGE-        ChatDeepSeek
   bi-enc   HNSW索引    zhparser+   set()去重    reranker     temperature
   oder                 pg_bigm                 v2-m3        0.1

【L3 面试必问】向量检索 + 关键词检索的互补性
────────────────────────────────────────────
两次检索返回不同的结果集 → 合并后覆盖更全面：

 向量检索（语义）                    关键词检索（字面）
─────────────────                   ─────────────────
搜"定价策略"                        搜"定价策略" → zhparser 分词为
                                    定价 & 策略
    │                                   │
    ▼                                   ▼
找到讨论"企业版收费模式             找到包含"定价"或"策略"
从按人头改为按功能模块"             这两个词的段落
的段落                                   │
（即使不包含"定价"二字）                  │
    │                                   ▼
    │                              但这些段落可能只是顺带
    │                              提到，不深入讨论定价
    │                                   │
    └──────────────┬────────────────────┘
                   │
           合并 + URL去重
           （取并集）
                  │
                  ▼
        Cross-Encoder Reranker
        （重新排序，选出最相关的 Top15）

【L3 面试必问】Bi-encoder（粗排） vs Cross-encoder（精排） 的核心区别
─────────────────────────────────────────────────────
                                    Bi-encoder            Cross-encoder
  ───────────────────────────────  ────────────────────  ──────────────────
  代表模型                          BGE-M3                BGE-reranker-v2-m3
  编码方式                          query 和 doc 独立编码   [query, doc] 联合编码
  相似度计算                        cos(q_vec, d_vec)     模型直接输出分数
  速度                             O(1) per doc（快）     O(N) per doc（慢）
  精度                             粗排（召回）           精排（精确）
  典型用法                         从 10000 个 chunk       从 50 个候选
                                   召回 Top30              精排 Top15

  为什么不用 Cross-encoder 做全量检索？
    对 10000 个 chunk 逐个做 Cross-encoder → 10000 次模型调用 → 太慢
    → 先 Bi-encoder 粗排（一次 batch inference 就能算完 10000 个余弦相似度）
    → 再 Cross-encoder 精排（只对 Top30~50 候选做精确排序）


═══════════════════════════════════════════════════════════════════════════════
                        【L5 决策 — 异常处理策略】
═══════════════════════════════════════════════════════════════════════════════

每个维度分析返回的是 dict[str,str]（竞品→结论），而不是抛异常。
这样做的好处：
  ① Supervisor 不需要 try/except 包裹维度结果
  ② 报告可以标注 "[数据不足]" 而不是空白
  ③ 日志可以看到具体哪个维度失败了

asyncio.gather(return_exceptions=True) 保证即使某个维度抛了未捕获的异常，
其他维度也能正常返回。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from src.db.connection import create_pool
from src.db.dao import ChunkEmbeddingDAO, AgentLogDAO, RetrievalSnapshotDAO
from src.observability.token_usage import extract_usage, add_usage
from src.agents.content_quality import (
    clean_chunk_text,
    is_boilerplate_chunk,
)
from src.agents.analysis_shape import (
    cell_source_url,
    conclusion_text,
    placeholder_cell,
    summarize_per_dimension,
)

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# Prompt 设计
# ═════════════════════════════════════════════════════════════════════════════

_ANALYSIS_PROMPT = """你是一个竞品分析专家。基于给出的网页文本片段，按指定维度分析【单个竞品】。

当前分析维度: %s
目标竞品: %s

参考文本片段（这些片段已按竞品过滤，都属于目标竞品）:
%s

请按 JSON 格式返回分析结果，只分析这一个竞品：
{
  "conclusion": "分析结论（禁止在 conclusion 里写 source_url 或 URL 字样）",
  "source_url": "该结论所依据的原文来源链接（即参考文本片段里 (来源: xxx) 的 xxx，原样复制，不要写 source_url 占位符）"
}

规则：
- conclusion 里只写分析结论本身，不要把 URL 或 source_url 字样混进去
- source_url 必须是对应结论依据的原文链接，原样来自参考文本片段里的 (来源: xxx)，若无来源填 ""
- 不确定的信息在 conclusion 里标注 [待验证]
- 无法找到相关信息时，conclusion 填 "[数据不足] 未找到相关信息"，source_url 填 ""
- 禁止空话："功能强大"→"支持100人视频会议+文档实时协作"
- 只输出 JSON，不要其他文字
"""


# ═════════════════════════════════════════════════════════════════════════════
# 正文智能截取工具
# ═════════════════════════════════════════════════════════════════════════════

# 【L4 工程】导航栏噪音行特征库
# web_fetch 提取的页面文本前 200 字符通常是导航栏/页眉，
# 这些行短（<50字符）、多为功能词（首页/登录/注册/搜索）。
# LLM 读到"首页 搜索 登录 产品 价格 联系我们" → 判无有效信息 → [数据不足]。
_NAV_NOISE_PATTERNS: list[str] = [
    "首页", "搜索", "登录", "注册", "退出", "我的", "购物车",
    "联系我们", "关于我们", "服务条款", "隐私政策", "网站地图",
    "导航", "菜单", "目录", "回到顶部", "分享到", "扫描二维码",
    "下载APP", "打开APP", "关注我们", "微信公众号", "微博", "收藏本站",
    "切换语言", "English", "Language", "Menu", "Search", "Login",
    "热搜", "热门推荐", "推荐文章", "最新文章", "热门标签",
    "上一篇", "下一篇", "设为首页", "加入收藏", "会员中心",
]


def _smart_slice(text: str, max_chars: int = 1500) -> str:
    """智能截取：跳过页面导航栏/页眉噪音，从第一个正文段落开始截取。

    【L4 工程】核心思路
    ------------------------------------------------------------
    不改变截取长度（1500 字符），只改变"从哪里截"。

    原文（从 web_fetch 出来）：
      "首页 搜索 登录 注册 我的 \n \n \n
       飞书企业版定价策略深度解析 \n \n
       2024年Q4飞书将企业版价格从¥200/人/月..."

    doc[:1500] 的结果：
      "首页 搜索 登录 注册 我的 飞书企业版定价策..."
      → LLM 看到导航栏 + 截断的标题 → "无有效信息"

    _smart_slice(doc, 1500) 的结果：
      "飞书企业版定价策略深度解析 \n \n
       2024年Q4飞书将企业版价格从¥200/人/月..."
      → LLM 直接看到正文 → 完整分析

    算法：
      ① 按行分割文本
      ② 跳过前导短行（<50 字符 且 匹配导航噪音词）
      ③ 找到第一个正文行（>50 字符 或 不含噪音词）
      ④ 从该行开始截取 max_chars

    Args:
        text: web_fetch 提取的原始文本
        max_chars: 最大截取字符数（默认 1500）

    Returns:
        截取后的正文文本，保证有内容（至少 1 字符）
    """
    if not text:
        return ""

    lines = text.split("\n")
    first_content_idx = 0

    # 扫描跳过前导噪音行
    for i, line in enumerate(lines):
        stripped = line.strip()
        # 空行 → 继续
        if not stripped:
            continue
        # 短行 + 匹配噪音词 → 是导航栏，跳过
        if len(stripped) < 50 and _is_nav_noise(stripped):
            continue
        # 找到第一个正文行
        first_content_idx = i
        break

    # 从正文行开始拼接
    sliced = "\n".join(lines[first_content_idx:])
    result = sliced[:max_chars].strip()
    # 极端情况：全部被过滤 → 返还原文（兜底）
    return result if result else text[:max_chars].strip()


def _is_nav_noise(line: str) -> bool:
    """判断一行文本是否为导航栏噪音（匹配特征库）。"""
    for pattern in _NAV_NOISE_PATTERNS:
        if pattern in line:
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# 单维度 RAG 检索 + 分析
async def _retrieve_and_analyze_dimension(
    task_id: str,
    competitors: list[str],
    dimension: str,
    chunk_dao: ChunkEmbeddingDAO,
    mcp_server: MCPServer,
    llm: ChatDeepSeek,
    settings,  # 显式传递 settings，避免隐式依赖全局配置
    usage_acc: dict[str, int] | None = None,
) -> dict[str, dict[str, str]]:
    """单个维度的 RAG 检索 + LLM 分析（内部函数，不抛异常）。

    【L5 决策】为什么用 dimension 字符串而不是向量？
    ─────────────────────────────────────────────
    调用方不应该知道"这个维度需要先嵌入"——那是本函数的职责。
    传入 dimension: str，内部调用 embed_query(dimension, settings)，
    调用方只需要传维度名（如"定价策略"），不用管怎么嵌入。

    【L4 工程】返回 dict 而不是抛异常
    失败时返回 {"error": str} —— 这不是 bug，是设计契约。
    analyzer_agent 的上层会检查 "error" key 并生成 [数据不足] 标记。

    Returns:
        成功: {竞品名: {"conclusion": "分析结论", "source_url": "原文链接"}, ...}
        失败: {"error": "失败原因"}   ← 整个维度失败，由上层展开成逐竞品的占位标记
    """
    try:
        # ─── Step 1: 维度文本嵌入 ───
        # 【L3 原理】embed_query 调用 BGE-M3 bi-encoder 将维度名转为 1024 维向量。
        # "定价策略" → [0.12, -0.34, 0.56, ...] （1024个float32）
        # 这个向量在 1024 维空间中靠近所有与"定价"相关的 chunk 向量。
        # 【2026-10-19】改走 invoke：embed_query 此前直接 import 调函数，
        #   绕过了 MCP 的白名单/参数校验/审计（3/5 工具漏管之一）。
        query_vec = await mcp_server.invoke(
            "embed_query",
            {"query": dimension},
            task_id=task_id,
            agent_name="analyzer",
        )
        if not query_vec:
            return {"error": f"文本向量化失败，维度不匹配 {dimension!r}"}

        # ─── Step 2: 向量检索（语义召回） ───
        # 【L3 原理】pgvector HNSW 索引在 1024 维空间做近似最近邻搜索。
        # 使用 <=> 算符（余弦距离），距离越小 = 语义越接近。
        # 【竞品配额决策，2026-10-12 修正注释】top_k 提到 60：两条检索 SQL 都
        # 只按 task_id 过滤（WHERE 里没有 competitor 谓词），召回的是全竞品混池，
        # 所以配额要按「所有竞品加起来」给足，再在内存按 competitor 分组。
        # 【2026-10-16】提升为配置（settings.vector_search_top_k，默认仍 60）——
        # 它是**绝对值**，池子小时会大于池子本身，直接触发下面那条退化告警。
        vec_top_k = settings.vector_search_top_k
        vec_results = await chunk_dao.similarity_search(task_id, query_vec, top_k=vec_top_k)

        # 【2026-10-16 新增】检索退化告警 —— 让「静默全量返回」可见
        #  ── 为什么必须告警 ──
        #  similarity_search 至多返回 top_k 条，故 len(vec_results) < top_k
        #  ⟺ **池子本身小于 top_k**。此时该层检索等于「把池子全取回来」，
        #  维度名对召回结果**零影响**（实测 golden_001：池子 32 < 60，
        #  4 个快照的 recalled_urls 是同一个 12 URL 集合，只有顺序不同）。
        #  ⚠️ 那句观测本身也是 Step 5.5 那个口径 bug 的证据：当时 recalled_urls
        #  落的是维度级混池，所以各竞品快照天然相同。已改落 group，仅历史快照仍如此。
        #  这个退化此前完全静默，外部只表现为「评估分数全 0」，极易被误判为
        #  「判据形态问题」或「模型不听话」。所以在最上游主动 WARN，暴露根因。
        if len(vec_results) < vec_top_k:
            logger.warning(
                "【Analyzer】检索退化：检索池(%d) < vector top_k(%d) → 本次全量返回，"
                "维度区分度不可用（dimension=%r）",
                len(vec_results), vec_top_k, dimension,
            )

        # ─── Step 3: 关键词检索（字面召回） ───
        # 【L3 原理】zhparser 中文分词 + pg_bigm 2-gram 索引，
        # 能精确匹配到包含"¥180"、"SaaS订阅"等具体数字/术语的 chunk。
        kw_results = await chunk_dao.keyword_search(task_id, dimension, top_k=30)

        # ─── Step 4: 合并去重（每 URL 最多保留 chunks_per_url 条） ───
        # 【L4 工程】为什么按 URL 去重而不是按 chunk 文本？
        #   同一页面会产出多个 chunk，不限制的话单个页面能霸占整个候选池。
        # 【2026-10-16 修正】原实现是「每 URL 只留 1 条」，有两个缺陷：
        #   ① 信息浪费：实测每 URL 平均产出 2.7 个 chunk（32 chunk / 12 URL），
        #      只留 1 条等于丢掉约 2/3 的页面内容，而候选池本就偏小；
        #   ② 不可复现：「留哪一条」取决于 `vec_results + kw_results` 的返回顺序，
        #      而该顺序由 query 向量决定 → 同一页面在不同维度下留下的 chunk 不同，
        #      给快照引入了与维度无关的随机性（实测两维度仅 4/7 文本重叠）。
        # 【改写后的取法】按「出现顺序」取前 k 条 —— 不额外计算相似度：
        #   vec_results 已按余弦距离升序（最相似在前）、kw_results 已按匹配度排序，
        #   拼接后同一 URL 的 chunk 天然就是「与维度相关性从高到低」。
        #   这样取 top-k 既与维度相关、又可复现，且**不产生任何额外 embedding 调用**。
        # 【兼容性】k = settings.chunks_per_url，**默认 1**：
        #   此时 [:1] 与旧的「保留首个」等价 → 默认行为与 token 预算均不变。
        url_keep: dict[str, list[dict]] = {}
        url_order: list[str] = []
        for r in vec_results + kw_results:
            url = r.get("source_url", "")
            if url not in url_keep:
                url_keep[url] = []
                url_order.append(url)
            url_keep[url].append(r)

        keep_per_url = max(1, int(getattr(settings, "chunks_per_url", 1)))
        merged: list[dict] = []
        for url in url_order:
            merged.extend(url_keep[url][:keep_per_url])

        if not merged:
            # 【2026-10-13 补漏】此处此前返回**裸字符串**，是 analysis_results 里
            #   唯一的 str 单元格来源——破坏了「单元格恒为 {conclusion, source_url}」
            #   不变量（因消费侧 analysis_shape 兜底才没暴露成故障）。
            #   统一走产出侧口径 placeholder_cell。
            return {
                c: placeholder_cell("[数据不足] 未检索到相关网页内容")
                for c in competitors
            }

        # ─── Step 4.5: 按竞品分组（维度检索 + 竞品分组） ───
        # 【L3 面试核心考点】为什么「维度检索 + 内存分组」而不是「竞品×维度检索」？
        #   竞品×维度 = N×M 次 embedding + N×M 次 SQL 检索（贵操作，不可接受）。
        #   维度检索 + 内存分组 = M 个维度 × (1 向量 + 1 关键词) = 2M 次 SQL + 纯内存分组。
        #   竞品名是专有名词，拼进 embed_query 对向量方向贡献极小，
        #   且分组是纯 CPU 的 dict 操作（免费），所以「贵调用只做 M 次、免费操作随便做」。
        # 【2026-10-12 复核事实】两条检索 SQL 的 WHERE 只有 task_id，
        #   competitor 只是被 SELECT 出来的一个列，**从未进入 WHERE**；
        #   所以 SQL 侧是零过滤的，真正的归属判定全在这里的内存分组。
        #   schema 里声明的 idx_chunk_task_competitor(task_id, competitor) 因此
        #   当前没有任何查询命中（死索引）—— 只有改成 SQL 下推过滤时才有价值。
        by_competitor: dict[str, list[dict]] = {}
        for r in merged:
            comp = r.get("competitor")
            # competitor 为 None（历史数据/未知归属）→ 归入一个共享兜底组，
            # 该组的 chunk 会分发给所有竞品分析（保守策略，保证不丢信息）。
            if comp:
                by_competitor.setdefault(comp, []).append(r)
            else:
                by_competitor.setdefault("__unattributed__", []).append(r)
        unattributed = by_competitor.pop("__unattributed__", [])

        # ─── Step 5: 逐竞品精排 + 分析 ───
        # 每个竞品在其专属 chunk 子集内做 Cross-encoder 精排，再单独喂 LLM。
        final_result: dict[str, dict] = {}
        snapshot_dao = RetrievalSnapshotDAO(chunk_dao._pool)

        for competitor in competitors:
            # 该竞品专属 chunk + 无归属兜底 chunk（合并，保证兜底信息不丢）
            group = by_competitor.get(competitor, []) + unattributed
            if not group:
                final_result[competitor] = placeholder_cell("[数据不足] 未找到相关信息")
                continue

            # ─── Step 4.6: 精排前清洗（2026-10-14 新增）───
            # 【L3 面试考点】为什么清洗要放在「精排之前」而不是之后？
            #   cross-encoder 是**逐对打分**的：把样板 chunk 一起喂进去，
            #   它不仅不会把样板排到后面，反而会因为「导航里竞品名重复密度高、
            #   与 query『竞品 维度』字面重合」把样板排到头部（实测精排后样板占比升高）。
            #   → 必须在打分**之前**把候选池洗干净，而不是指望 reranker 自己识别。
            # 【L5 设计】为什么这里做双保险（Collector 已过滤一次）？
            #   Collector 的过滤只对**新采集**的数据生效；库里已有的历史 chunk
            #   （旧任务/未重跑的任务）仍然是脏的。检索侧这里是「零成本兜底」：
            #   规整 + 剔除都不花钱，且保证无论数据来自哪一批，喂给 reranker 的都干净。
            cleaned_group: list[dict] = []
            for r in group:
                raw_text = r.get("chunk_text", "") or ""
                if is_boilerplate_chunk(raw_text):
                    continue
                cleaned_group.append({**r, "chunk_text": clean_chunk_text(raw_text)})
            # 兜底：若清洗后一条不剩（极端情况：整页都是样板），退回「仅规整、不剔除」，
            # 宁可喂稍脏的内容，也不要直接判 [数据不足]。
            if not cleaned_group:
                cleaned_group = [
                    {**r, "chunk_text": clean_chunk_text(r.get("chunk_text", "") or "")}
                    for r in group
                ]
            group = cleaned_group

            # 【命名约定 2026-10-16】这一段的三个名字：
            #   rerank_query       = **不是** RAGAS 的 user_input。本项目里没有 user、
            #                        也没有人提问，这只是一个给 cross-encoder 用的
            #                        查询串（拼竞品名，让 reranker 区分「苹果是水果
            #                        还是公司」）。它要等到**评估阶段**才被当作
            #                        RAGAS 的 user_input 消费（见 ragas_core.
            #                        build_samples：user_input = snap.get("query")）。
            #                        ⇒ 领域侧保留领域名，只在「进入 RAGAS 的边界」
            #                          做名词转换，别把 RAGAS 的词污染到生产链路。
            #   candidate_contexts = 精排**前**的候选 chunk（还没排过序，只是候选池）
            #   reranked_texts     = 精排**后**的 chunk 原文 —— 才是 RAGAS
            #                        retrieved_contexts 该吃的东西
            # ⚠️ 区分 candidate vs reranked 很重要：评估只认 reranked，
            #    把 candidate 当 retrieved_contexts 喂进去会高估检索质量。
            rerank_query = f"{competitor} {dimension}"
            candidate_contexts = [r["chunk_text"] for r in group]
            # 【2026-10-16】top_k 提升为配置（settings.rerank_top_k，默认仍 15）。
            rerank_top_k = settings.rerank_top_k
            # 【2026-10-19】改走 invoke：rerank 此前绕过统一入口，
            #   精排是检索层最贵的一步（cross-encoder 逐对打分），必须纳入审计。
            reranked = await mcp_server.invoke(
                "rerank",
                {
                    "query": rerank_query,
                    "documents": candidate_contexts,
                    "top_k": rerank_top_k,
                },
                task_id=task_id,
                agent_name="analyzer",
            )
            # 【2026-10-16 新增】精排「无筛选」告警
            #  候选池 ≤ top_k 时，精排只改变**顺序**、不改变**集合**，
            #  而落盘的 reranked_texts 是整个集合 → 同一竞品在不同维度下的
            #  contexts 集合会完全相同（实测 golden_001：候选 7/5 ≤ 15）。
            #  此时维度区分只能体现在排序上，而评估侧拿的是集合，
            #  于是「维度区分度」在整条链路上彻底消失。
            if reranked and len(candidate_contexts) <= rerank_top_k:
                logger.warning(
                    "【Analyzer】精排无筛选：候选池(%d) ≤ rerank top_k(%d) → "
                    "只排序不截断，维度区分度无法体现在 contexts 集合上"
                    "（competitor=%r dimension=%r）",
                    len(candidate_contexts), rerank_top_k, competitor, dimension,
                )
            if reranked:
                reranked_texts = [
                    group[r["index"]].get("chunk_text", "")
                    for r in reranked if r["index"] < len(group)
                ]
                reranked_urls = [
                    group[r["index"]].get("source_url", "")
                    for r in reranked if r["index"] < len(group)
                ]
            else:
                # reranker 失败兜底：退回「未精排」的候选前 15 条
                # （此时 reranked_texts 名不副实，但它仍会被当作 retrieved_contexts 落盘，
                #   评估侧读到的分母/排序语义与正常路径不同 —— 靠 reranked 为空来识别。）
                reranked_texts = candidate_contexts[:15]
                reranked_urls = [r["source_url"] for r in group[:15]]

            # ─── Step 5.5: 落竞品×维度级检索快照（RAGAS 检索质量评估输入） ───
            # 每条快照 = 一个竞品在一个维度下的精排头部，query="竞品 维度"。
            # 【为什么要落这条 query】让评估阶段的 user_input（= 快照 query）与
            # 线上精排 rerank("竞品 维度") 用的是**同一把尺子**：评估不是事后另造
            # 一个问法去考检索，而是考「线上真正拿去排序的那个串」。
            # 【名词边界】DB 列名仍是 query / reranked_texts（历史名，未做迁移）；
            # 「query → user_input」的转换只在评估侧边界做（ragas_core.build_samples）。
            # 【2026-10-20 修 recalled_urls 口径】原先落的是 `merged`——**该维度的混池全集**，
            #   同一维度下每个竞品拿到的是同一份 URL 集合，于是：
            #   ⚠️ coverage_skew 只看得见「整块没料、连快照都没落」（上面 continue 掉的那种格），
            #      「被大竞品挤到只剩 1 条」完全看不见 → 指标恒偏健康，检索粒度改造无法验证。
            #   改落 `group`（该竞品实际进精排的候选），三项指标才成立：
            #     recall_avg    = 每格真召回几条（不再是混池大小）
            #     coverage_skew = 竞品间召回均衡度（不再是同维度内的固定值）
            #   ★ 代价：2026-10-20 之前的历史快照仍是混池口径，**新旧对比必须分段**。
            snapshot_id: str | None = None
            try:
                snapshot_id = await snapshot_dao.insert_snapshot(
                    task_id=task_id,
                    dimension=dimension,
                    competitor=competitor,
                    query=rerank_query,
                    recalled_urls=[r.get("source_url", "") for r in group],
                    reranked_urls=reranked_urls,
                    reranked_texts=reranked_texts,
                )
            except Exception:
                logger.exception(
                    "【Analyzer】检索快照落盘失败 task_id=%s dimension=%r competitor=%r",
                    task_id, dimension, competitor,
                )

            # 智能截取 + 组装该竞品的分析上下文
            chunks_text = ""
            for i, (doc, src) in enumerate(zip(reranked_texts, reranked_urls)):
                sliced = _smart_slice(doc, max_chars=1500)
                chunks_text += f"[片段{i+1}] (来源: {src})\n{sliced}\n\n"

            # 单竞品 LLM 分析
            prompt = _ANALYSIS_PROMPT % (dimension, competitor, chunks_text)
            resp = await llm.ainvoke(prompt)
            if usage_acc is not None:
                add_usage(usage_acc, extract_usage(resp))
            text = resp.content.strip()

            # LLM 输出防御：剥离 ```json 包裹 / 提取 JSON 片段
            if text.startswith("```"):
                parts = text.split("```", 2)
                text = parts[2].strip() if len(parts) >= 3 else parts[1].strip()
            if not text:
                final_result[competitor] = placeholder_cell("[分析失败] LLM返回空响应")
                continue
            if not text.startswith("{"):
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    text = text[start:end + 1]
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                final_result[competitor] = placeholder_cell("[分析失败] LLM返回格式异常")
                continue

            if isinstance(parsed, dict) and "conclusion" in parsed:
                # 成功路径也走 analysis_shape 的取值口径（取正文 / 取来源各一次），
                # 调用点不再自己写 .get("conclusion")——形态变更只需改模块一处。
                conclusion = conclusion_text(parsed)
                final_result[competitor] = {
                    "conclusion": conclusion,
                    "source_url": cell_source_url(parsed),
                }
                # 回填快照 response_text：供四指标评估的生成层（faithfulness/
                # answer_relevancy）使用；失败不阻断分析流程。
                if snapshot_id:
                    try:
                        await snapshot_dao.set_response_text(snapshot_id, conclusion)
                    except Exception:
                        logger.exception(
                            "【Analyzer】快照 response_text 回填失败 task_id=%s key=%s@%s",
                            task_id, competitor, dimension,
                        )
            else:
                final_result[competitor] = placeholder_cell("[分析失败] LLM返回格式异常")

        return final_result

    except Exception as e:
        # 【L4 工程】全面捕获，返回错误标记而不是抛异常
        logger.exception("Dimension %r analysis failed", dimension)
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
# Analyzer 中间进度辅助函数
# ═════════════════════════════════════════════════════════════════════════════

async def _track_dim_completion(
    task_id: str,
    dim_name: str,
    coro,
    log_dao,
    usage_acc: dict[str, int] | None = None,
):
    """执行单个维度分析，完成后立即写入 agent_logs → SSE 实时推送。

    【L4 工程】关键设计：不改变并行语义
    原本是 asyncio.gather(*coros)，gather 的行为是所有 coro 并行跑、
    全部完成后才返回。现在用这个包装函数保持并行但"每完成一个就写一条日志"。
    核心技巧：gather 本身不改变——只是每个 coro 内部多了一步 log。
    并行度完全不变（Semaphore 约束在内部）。
    """
    result = await coro
    # 【2026-10-01 可观测性】从维度级累加器取 token（None → 空 dict）
    _u = usage_acc or {}
    _tk = dict(
        prompt_tokens=_u.get("prompt_tokens"),
        completion_tokens=_u.get("completion_tokens"),
        total_tokens=_u.get("total_tokens"),
    )
    # 检查结果类型，决定日志内容
    if isinstance(result, Exception):
        await log_dao.log(
            task_id=task_id, agent_name="analyzer", action="dim_analysis",
            request={"dimension": dim_name},
            response={"dimension": dim_name, "error": str(result)},
            **_tk,
        )
    elif isinstance(result, dict) and "error" in result:
        await log_dao.log(
            task_id=task_id, agent_name="analyzer", action="dim_analysis",
            request={"dimension": dim_name},
            response={"dimension": dim_name, "error": result["error"]},
            **_tk,
        )
    else:
        # 正常完成：统计竞品数量
        comp_count = len(result) if isinstance(result, dict) else 0
        await log_dao.log(
            task_id=task_id, agent_name="analyzer", action="dim_analysis",
            request={"dimension": dim_name},
            response={"dimension": dim_name, "competitors": comp_count},
            **_tk,
        )
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Analyzer Agent 主函数
# ═════════════════════════════════════════════════════════════════════════════

async def analyzer_agent(
    task: dict,
    mcp_server: MCPServer,
    llm: ChatDeepSeek,
) -> dict:
    """Analyzer Agent — 对每个维度执行 RAG 检索 + LLM 分析。

    【L5 架构】Analyzer 依赖 ChunkEmbeddingDAO + MCP 的 embed_query/rerank。
    不调用 web_search/web_fetch（那是 Collector 的事）；检索类工具同样走
    mcp_server.invoke 统一管控入口，而不是直接 import 函数（2026-10-19 修复）。

    【L5 决策】温度 temperature=0.1（不是默认的 0.3）
    分析任务需要稳定、可复现的结论，不是创意写作。
    temperature=0.1 让输出更确定性，避免同一份数据产生不同结论，

    Args:
        task: {id, title, competitors: [str], dimensions: [str]}
        mcp_server: MCP 工具服务器（用于获取 settings，不调用工具）
        llm: ChatDeepSeek 客户端（temperature=0.1，低温度保证分析一致性）

    Returns:
        {dimension_name: {competitor_name: "分析结论", ...}, ...}
    """
    settings = mcp_server.settings
    task_id = task["id"]
    competitors = task["competitors"]
    dimensions = task["dimensions"]

    pool = await create_pool(settings)
    chunk_dao = ChunkEmbeddingDAO(pool)
    log_dao = AgentLogDAO(pool)

    t0 = time.perf_counter()

    # ── ✏️ 输入日志 ──
    logger.info(
        "【Analyzer】开始 task=%s 竞品=%d个(%s) 维度=%d个(%s)",
        task_id,
        len(competitors), ",".join(competitors),
        len(dimensions), ",".join(dimensions),
    )

    # ─── 所有维度并行分析 ───
    # 【L4 工程】asyncio.gather(*tasks, return_exceptions=True)
    # 关键参数 return_exceptions=True：
    #   — 如果某个维度抛了未捕获异常，gather 不会中断整个批次
    #   — 异常作为 Exception 对象出现在 results_list 中
    #   — 由下面的 for 循环处理为 "[分析失败]" 标记

    # 【2026-10-01 可观测性】每个维度一个独立累加器，避免并行写入竞争
    dim_usage_accs: list[dict[str, int]] = [{} for _ in dimensions]
    tasks_coros = [
        _retrieve_and_analyze_dimension(
            task_id, competitors, dim, chunk_dao, mcp_server, llm, settings,
            dim_usage_accs[i],
        )
        for i, dim in enumerate(dimensions)
    ]
    # 包装：给每个 coro 打上标签 (dim_name, coro)
    tagged = [
        _track_dim_completion(task_id, dim, coro, log_dao, dim_usage_accs[i])
        for i, (dim, coro) in enumerate(zip(dimensions, tasks_coros))
    ]
    results_list = await asyncio.gather(*tagged, return_exceptions=True)

    # ─── 后处理：异常 → [分析失败] + 错误信息 ───
    # 【L5 决策】三种失败处理的分级：
    #   1. 未捕获异常（isinstance(result, Exception)）
    #      → "[分析失败] {异常信息}"  ← 把异常信息暴露给报告，方便定位
    #   2. 内部标记错误（result["error"]）
    #      → "[分析失败] {错误原因}"  ← 如 embed_query 失败
    #   3. 正常返回（{竞品: {conclusion, source_url}}）
    #      → 直接使用
    # 【2026-10-12 加固】三条失败分支也产出与成功分支**完全一致的单元格形态**
    #   （{conclusion, source_url}）。此前失败分支写成裸字符串，导致
    #   analysis_results 里同时存在 str 和 dict 两种单元格 →
    #   消费方必须处处 isinstance 兼容，漏一处就静默失真
    #   （analyzer 的 per_dimension.has_data 就是这么恒为 True 的）。
    def _fail_cell(reason: str) -> dict[str, str]:
        # 标记前缀在这里拼，格子本身由产出侧唯一口径 placeholder_cell 造
        return placeholder_cell(f"[分析失败] {reason}")

    analysis_results: dict[str, dict] = {}
    for dim, result in zip(dimensions, results_list):
        if isinstance(result, Exception):
            logger.exception("Dimension %r raised exception", dim)
            analysis_results[dim] = {c: _fail_cell(str(result)) for c in competitors}
        elif isinstance(result, dict) and "error" in result:
            analysis_results[dim] = {c: _fail_cell(str(result["error"])) for c in competitors}
        elif isinstance(result, dict):
            analysis_results[dim] = result
        else:
            analysis_results[dim] = {c: _fail_cell("未知错误") for c in competitors}

    duration_ms = (time.perf_counter() - t0) * 1000
    # ── 构建 per-dimension 详情（与控制台日志对齐）──
    # 【2026-10-12 修复】原实现直接 `str(text).startswith("[分析失败]")`，
    #   而 text 此时已是 dict，str(dict) 以 "{'conclusion'" 开头 → 两个 startswith
    #   恒 False → 过滤条件恒 True → analyzed_comps 恒等于全部竞品 → has_data 恒 True。
    #   现统一走 analyze_shape.summarize_per_dimension（口径唯一，且有单测）。
    per_dimension = summarize_per_dimension(analysis_results)
    await log_dao.log(
        task_id=task_id,
        agent_name="analyzer",
        action="multi_dimension_analysis",
        request={"competitors": competitors, "dimensions": dimensions},
        response={
            "dimensions_analyzed": list(analysis_results.keys()),
            "dimensions_count": len(dimensions),
            "dimension_names": dimensions,
            "per_dimension": per_dimension,
        },
        duration_ms=round(duration_ms, 1),
    )

    # ── ✏️ 输出日志：每个维度的分析长度 ──
    output_lines = []
    for dim_name, comp_analyses in analysis_results.items():
        snippet = {}
        for c, val in comp_analyses.items():
            # 【2026-10-12 修复】走 conclusion_text 取正文再统计，
            # 此前直接 str(val)，打印的是 dict repr（含键名/source_url），
            # 长度和"前30字"都不是结论本身，日志完全失真。
            text_str = conclusion_text(val)
            snippet[c] = f"{len(text_str)}字符 / 前30字: {text_str[:30]}..."
        output_lines.append(f"  {dim_name}: {snippet}")
    logger.info(
        "【Analyzer】完成 task=%s %d维度 耗时%.0fms\n%s",
        task_id,
        len(dimensions),
        duration_ms,
        "\n".join(output_lines),
    )

    return analysis_results
