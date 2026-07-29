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
  │嵌入 │ │TopK=30 │ │TopK=20  │ │(URL去重)│ │Reranker   ││(逐维度独立)  │
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
from src.db.dao import ChunkEmbeddingDAO, AgentLogDAO
from src.mcp.tools_rag import embed_query, rerank

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# Prompt 设计
# ═════════════════════════════════════════════════════════════════════════════

_ANALYSIS_PROMPT = """你是一个竞品分析专家。基于给出的网页文本片段，按指定维度分析各竞品。

当前分析维度: %s
竞品列表: %s

参考文本片段:
%s

请按 JSON 格式返回分析结果：
{
  "竞品名": "分析结论（必须附带 source_url），不确定信息标注[待验证]，数据缺失标注[数据不足]",
  ...
}

规则：
- 每个结论必须引用原文的 source_url
- 不确定的信息标注 [待验证]
- 无法找到相关信息标注 [数据不足]
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
    llm: ChatDeepSeek,
    settings,  # 显式传递 settings，避免隐式依赖全局配置
) -> dict[str, str]:
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
        成功: {竞品名: "分析结论", ...}
        失败: {"error": "失败原因"}
    """
    try:
        # ─── Step 1: 维度文本嵌入 ───
        # 【L3 原理】embed_query 调用 BGE-M3 bi-encoder 将维度名转为 1024 维向量。
        # "定价策略" → [0.12, -0.34, 0.56, ...] （1024个float32）
        # 这个向量在 1024 维空间中靠近所有与"定价"相关的 chunk 向量。
        query_vec = await embed_query(dimension, settings)
        if not query_vec:
            return {"error": f"文本向量化失败，维度不匹配 {dimension!r}"}

        # ─── Step 2: 向量检索（语义召回） ───
        # 【L3 原理】pgvector HNSW 索引在 1024 维空间做近似最近邻搜索。
        # 使用 <=> 算符（余弦距离），距离越小 = 语义越接近。
        # TopK=30：不是越多越好。30~50 是粗排的实践最优区间。
        #   <20：可能漏掉相关 chunk
        #   >50：Cross-encoder 精排成本太高
        vec_results = await chunk_dao.similarity_search(task_id, query_vec, top_k=30)

        # ─── Step 3: 关键词检索（字面召回） ───
        # 【L3 原理】zhparser 中文分词 + pg_bigm 2-gram 索引，
        # 能精确匹配到包含"¥180"、"SaaS订阅"等具体数字/术语的 chunk。
        # 这些词通常不会被向量检索召回（它们在语义空间中特征不明显）。
        # TopK=20：字面匹配的召回范围比语义窄，20 足够。
        kw_results = await chunk_dao.keyword_search(task_id, dimension, top_k=20)

        # ─── Step 4: 合并去重 ───
        # 【L4 工程】为什么用 URL 而不是 chunk 文本去重？
        #   同一页面可能产生多个 chunk（如 5 个 chunk 都来自
        #   https://feishu.cn/pricing），如果只按文本去重，
        #   会得到 5 个相似的 chunk（浪费 LLM token）。
        #   按 URL 去重 → 每个页面最多保留一个 chunk 进入精排。
        #   同时保留向量和关键词两个来源的覆盖（取并集）。
        seen: set[str] = set()
        merged: list[dict] = []
        for r in vec_results + kw_results:
            url = r.get("source_url", "")
            if url not in seen:
                seen.add(url)
                merged.append(r)

        if not merged:
            return {c: "[数据不足] 未检索到相关网页内容" for c in competitors}

        # ─── Step 5: Cross-encoder 精排 ───
        # 【L3 核心考点】重排（Reranking）做的是什么？
        #
        # Bi-encoder 粗排：cos(query_vec, doc_vec)
        #   → 只比较两个独立向量的夹角，无法捕捉 query-doc 之间的细粒度交互
        #
        # Cross-encoder 精排：把 [query, doc] 拼成一对输入模型
        #   → Self-attention 让 query 的每个 token 看到 doc 的每个 token
        #   → 能捕捉"虽然都有'定价'，但这个 doc 讲的是竞品定价，那个讲的是内部定价策略"
        #   → 排序质量明显优于余弦相似度
        #
        # 【L4 工程】top_k=15：精排在 30~50 个候选里选出最相关的 15 个。
        # LLM 分析 15 个 chunk 是性价比最优的输入量。
        documents = [r["chunk_text"] for r in merged]
        ranked = await rerank(dimension, documents, top_k=15, settings=settings)

        # 取最终检索文本（保留排序信息）
        if ranked:
            top_docs = [
                merged[r["index"]].get("chunk_text", "")
                for r in ranked if r["index"] < len(merged)
            ]
            top_sources = [
                merged[r["index"]].get("source_url", "")
                for r in ranked if r["index"] < len(merged)
            ]
        else:
            # 重排失败 → 退回到原始合并结果
            top_docs = documents[:15]
            top_sources = [r["source_url"] for r in merged[:15]]

        # ─── Step 6: LLM 分析 ───
        # 【L4 工程】智能截取：跳过页面导航栏/页眉垃圾，只取正文
        # 问题：web_fetch 提取的文本前 200 字符通常是导航栏
        #   ("首页 搜索 登录 注册 我的 购物车 联系我们")，
        #   直接 doc[:1500] 会把这段垃圾喂给 LLM → 判无有效信息 → [数据不足]。
        # 修复：跳过前导短行（<50字符的导航栏文本），从第一个真正段落开始截取。
        # 15 chunks × 1500 chars = 22500 chars ≈ 8000~12000 tokens
        # + prompt ≈ 500 tokens → 总共 ~12K tokens
        # 在 DeepSeek 128K 窗口内完全安全
        chunks_text = ""
        for i, (doc, src) in enumerate(zip(top_docs, top_sources)):
            sliced = _smart_slice(doc, max_chars=1500)
            chunks_text += f"[片段{i+1}] (来源: {src})\n{sliced}\n\n"

        prompt = _ANALYSIS_PROMPT % (dimension, competitors, chunks_text)
        resp = await llm.ainvoke(prompt)
        text = resp.content.strip()

        # 【L4 工程】LLM 输出防御：模型不一定遵守"只输出 JSON"
        # 有时会包裹 ```json ... ```，预清理防 JSON parse 失败
        if text.startswith("```"):
            parts = text.split("```", 2)
            if len(parts) >= 3:
                text = parts[2].strip()
            else:
                text = parts[1].strip()

        if not text:
            logger.error("【Analyzer】LLM 返回空响应 dimension=%r", dimension)
            return {c: "[分析失败] LLM返回空响应" for c in competitors}

        # 如果没以 { 开头，尝试提取 JSON 片段
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start:end + 1]

        result = json.loads(text)
        if not isinstance(result, dict):
            return {c: "[分析失败] LLM返回格式异常" for c in competitors}
        return result

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
):
    """执行单个维度分析，完成后立即写入 agent_logs → SSE 实时推送。

    【L4 工程】关键设计：不改变并行语义
    原本是 asyncio.gather(*coros)，gather 的行为是所有 coro 并行跑、
    全部完成后才返回。现在用这个包装函数保持并行但"每完成一个就写一条日志"。
    核心技巧：gather 本身不改变——只是每个 coro 内部多了一步 log。
    并行度完全不变（Semaphore 约束在内部）。
    """
    result = await coro
    # 检查结果类型，决定日志内容
    if isinstance(result, Exception):
        await log_dao.log(
            task_id=task_id, agent_name="analyzer", action="dim_analysis",
            request={"dimension": dim_name},
            response={"dimension": dim_name, "error": str(result)},
        )
    elif isinstance(result, dict) and "error" in result:
        await log_dao.log(
            task_id=task_id, agent_name="analyzer", action="dim_analysis",
            request={"dimension": dim_name},
            response={"dimension": dim_name, "error": result["error"]},
        )
    else:
        # 正常完成：统计竞品数量
        comp_count = len(result) if isinstance(result, dict) else 0
        await log_dao.log(
            task_id=task_id, agent_name="analyzer", action="dim_analysis",
            request={"dimension": dim_name},
            response={"dimension": dim_name, "competitors": comp_count},
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

    【L5 架构】Analyzer 仅依赖 ChunkEmbeddingDAO + tools_rag，
    不调用 mcp_server 的 web_search/web_fetch（那是 Collector 的事）。

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

    # 【L4 工程】中间进度：逐维度打日志，让 SSE 实时推送到前端
    # 因为 gather 是并行的，我们不要等全部完成——改用 asyncio.as_completed
    tasks_coros = [
        _retrieve_and_analyze_dimension(task_id, competitors, dim, chunk_dao, llm, settings)
        for dim in dimensions
    ]
    # 包装：给每个 coro 打上标签 (dim_name, coro)
    tagged = [
        _track_dim_completion(task_id, dim, coro, log_dao)
        for dim, coro in zip(dimensions, tasks_coros)
    ]
    results_list = await asyncio.gather(*tagged, return_exceptions=True)

    # ─── 后处理：异常 → [分析失败] + 错误信息 ───
    # 【L5 决策】三种失败处理的分级：
    #   1. 未捕获异常（isinstance(result, Exception)）
    #      → "[分析失败] {异常信息}"  ← 把异常信息暴露给报告，方便定位
    #   2. 内部标记错误（result["error"]）
    #      → "[分析失败] {错误原因}"  ← 如 embed_query 失败
    #   3. 正常返回（dict[str, str]）
    #      → 直接使用
    analysis_results: dict[str, dict[str, str]] = {}
    for dim, result in zip(dimensions, results_list):
        if isinstance(result, Exception):
            logger.exception("Dimension %r raised exception", dim)
            analysis_results[dim] = {c: f"[分析失败] {result}" for c in competitors}
        elif isinstance(result, dict) and "error" in result:
            analysis_results[dim] = {c: f"[分析失败] {result['error']}" for c in competitors}
        elif isinstance(result, dict):
            analysis_results[dim] = result
        else:
            analysis_results[dim] = {c: "[分析失败] 未知错误" for c in competitors}

    duration_ms = (time.perf_counter() - t0) * 1000
    # ── 构建 per-dimension 详情（与控制台日志对齐）──
    per_dimension = {}
    for dim_name, comp_analyses in analysis_results.items():
        analyzed_comps = [c for c, text in comp_analyses.items() if not str(text).startswith("[分析失败]") and not str(text).startswith("[数据不足]")]
        per_dimension[dim_name] = {
            "competitors_analyzed": len(analyzed_comps),
            "has_data": len(analyzed_comps) > 0,
        }
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
        for c, text in comp_analyses.items():
            # LLM 返回的值可能是字符串、dict 或非字符串
            # 统一转为 str 再截取，避免切片 dict 引发的 KeyError
            text_str = str(text)
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
