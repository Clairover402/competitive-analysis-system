"""Collector Agent — 搜索关键词生成 + 并发网页抓取 + 文本分块 + 嵌入 + 存储。

═══════════════════════════════════════════════════════════════════════════════
                            【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

Collector 是整个竞品分析系统的"数据层入口"。它负责把用户输入的竞品名+维度
转化为可被 Analyzer 检索的结构化 chunk 数据。

六大步骤串联（Pipeline 模式，确定性序列）：
  步骤1 ──→ 步骤2 ──→ 步骤3 ──→ 步骤4 ──→ 步骤5 ──→ 步骤6
  LLM生成    并发搜索    并发抓取    tiktoken    BGE-M3     batch insert
  关键词                 HTML下载    分块        嵌入向量化   + ID回收

【L5 决策】为什么 Collector 不做 LLM 提取？
─────────────────────────────────────────
在"拆分 Agent vs 单体 Agent"的设计决策中，选择拆分的原因是：
  ① 职责单一：Collector 只需关心"数据是否到场"，不关心"数据怎么分析"
  ② 故障隔离：搜索抓取失败 ≠ 无法出报告（Analyzer 可标注[数据不足]）
  ③ 并行编排：Collector 和 Analyzer 可以在 Supervisor 层调度为流水线
     (Collector 产出 N chunk 后，Analyzer 立即开始第一批检索，不必等全部完成)

【L5 决策】搜索阶段的两级并发模型
─────────────────────────────────
  asyncio.gather(search_tasks)   ← 并发搜索（所有 query 同时发出）
       │
       ├── [每 query] Semaphore 限制 max_concurrent_collectors 个实际并发
       │
  asyncio.gather(fetch_tasks)    ← 并发抓取（所有 URL 同时发出）
       │
       ├── [每 URL]   Semaphore 同 limits 限制连接数

为什么搜索和抓取分开两次 gather？
  — 搜索返回的 URL 需要去重后再抓取（不同 query 可能返回相同 URL）
  — 先收集所有候选 URL → 去重 → 再并发抓取（避免浪费带宽）


═══════════════════════════════════════════════════════════════════════════════
                            【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════
  §1 搜索策略：LLM关键词 vs 模板拼装（降级设计）
  §2 并发模型：asyncio.gather + Semaphore（L4 工程必备）
  §3 文本分块：tiktoken + 自然断点（chunk 工程化）
  §4 批量嵌入：BGE-M3 batch_size=12 经验值
  §5 chunk_id 回收：batch INSERT 后 SELECT 回填 ID
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import tiktoken

from src.db.connection import create_pool
from src.db.dao import ChunkEmbeddingDAO, AgentLogDAO
from src.mcp.tools_rag import embed_texts

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# §1 搜索关键词生成
# ═════════════════════════════════════════════════════════════════════════════

_KEYWORD_PROMPT = """你是一个搜索专家。根据竞品名称和分析维度，生成 3-5 个精准的搜索 query。
要求：每个 query 要具体，结合竞品名+维度+时间限定，不同 query 覆盖不同角度。

输入: {competitor: "%s", dimensions: %s}
输出: ["query1", "query2", ...]"""


async def _generate_keywords(
    competitor: str,
    dimensions: list[str],
    llm: ChatDeepSeek,
) -> list[str]:
    """LLM 生成多样化搜索关键词（伪布尔检索问题 → 语义化 query）。

    【L3 核心考点】为什么不用简单的 "{竞品} {维度}" 模板？
    ─────────────────────────────────────────────────────
    以 "飞书" × "定价策略" 为例：

      模板拼接: "飞书 定价策略"
        → 搜索结果太宽泛，可能返回官网首页、新闻稿等无关内容

      LLM 生成: "飞书 2025 企业版 价格调整 SaaS 订阅费率"
        → 包含同义词（企业版/SaaS）、时间限定（2025），
          更可能命中具体的定价页面

    LLM 的优势在于理解语义等价关系：
      "定价策略" ≈ "定价" ≈ "价格" ≈ "费率" ≈ "订阅费用" ≈ "license 费用"
      这种同义词扩展靠规则写不穷举，但 LLM 天然具备。

    【L4 工程】降级策略（LLM 不可用时的兜底）
    ─────────────────────────────────────────
    不要因为 LLM 挂了就阻塞整个 Collector。
    try/except 捕获一切异常 → 模板拼接兜底：
      "{竞品} {维度1}", "{竞品} {维度2}", ...
    保证至少每个维度有一条 query，不会出现零搜索结果。
    """
    dims_str = json.dumps(dimensions, ensure_ascii=False)
    prompt = _KEYWORD_PROMPT % (competitor, dims_str)
    try:
        resp = await llm.ainvoke(prompt)
        text = resp.content.strip()
        # 清理可能的 markdown 代码块包裹（模型输出防御）
        # 【L4 工程】模型不一定严格遵守"只输出 JSON"的指令，
        # 有时会给 ```json\n...\n```，所以统一预处理
        if text.startswith("```"):
            text = text.split("`", 2)[2].split("```", 1)[0].strip()
        keywords = json.loads(text)
        if isinstance(keywords, list) and len(keywords) > 0:
            logger.info("日志：LLM为 %r 生成了 %d 个关键词", len(keywords), competitor)
            return keywords
    except Exception:
        logger.warning(
            "日志：为 %r 生成关键词失败，将使用模板兜底",
            competitor,
            exc_info=True,
        )
    # 兜底：模板拼接 {竞品} {维度}
    # 【L4 工程】这是设计契约的最低保证——无论如何不会返回空列表


    """
    用户创建任务
    │
    ├── title:      "2025年企业协作工具竞品分析"
    ├── competitors: ["飞书", "钉钉", "企业微信"]
    └── dimensions:  ["定价策略", "功能对比", "技术架构"]   ← 用户输入"""

    """
    [
    "飞书 定价策略",
    "飞书 功能对比", 
    "飞书 技术架构"
    ]
    """
    return [f"{competitor} {dim}" for dim in dimensions]


# ═════════════════════════════════════════════════════════════════════════════
# §2 文本分块
# ═════════════════════════════════════════════════════════════════════════════

# 【L3 原理】为什么用 tiktoken 而不是 str.__len__()？
# LLM 看到的是 token，不是字符。中文一个字 ≈ 1.5~2 tokens，英文一个词 ≈ 1~2 tokens。
# 如果用字符数切分，中文文本的实际 token 数会比英文多 50-100%，导致 LLM 上下文溢出。
# tiktoken 使用与模型一致的 BPE 分词器（cl100k_base），保证 "800 tokens" 就是模型实际读到的 800 个 token。

_ENCODING = tiktoken.get_encoding("cl100k_base")


def _chunk_text(text: str, min_tokens: int = 800, max_tokens: int = 1200) -> list[str]:
    """按 token 数将文本切分为固定大小的块（自然断点优先）。

    【L3 核心考点】chunk_size 怎么定？
    ────────────────────────────
    这是 RAG 工程中被讨论最多但最没有标准答案的问题。三个约束条件：

      ① 检索精度 vs 召回率权衡
         小 chunk（200~500t）：检索更精准，但可能丢失上下文
         大 chunk（2000~4000t）：上下文完整，但检索噪音增加
         → 800~1200t 是实践甜点区：够读完一个段落，又不会太宽泛

      ② Embedding 模型的输入上限
         BGE-M3 最大输入 8192 tokens，但 token 数越大，embedding 质量下降
         → 保持在 1200t 以内，保证 embedding 质量

      ③ LLM 上下文窗口（Analyzer 会拼接多个 chunk 一起分析）
         5 个维度 × 15 个候选 chunk × 1200 tokens = 90000 tokens
         → 加上 prompt，需要在 128K 窗口内放下

    【L4 工程】为什么以句号 ". " 为断点，而不是固定 token 切割？
    ─────────────────────────────────────────────────────────
    固定 token 切割会从句子中间截断：
      "飞书在2024年Q4将企业版价格从¥200/人/月调整为¥..."
      → 后半段变成 "180/人/月" ，丢失 "飞书" 和 "调整" 的上下文

    自然断点（. // \\n // 段落结束）保证每个 chunk 是语义完整的句子。
    超长句子（>1200t）强制按 max_tokens 切分——不完美，但不会阻塞流程。

    【L5 决策】为什么没有用 LangChain 的 RecursiveCharacterTextSplitter？
    不需要。这个项目的 chunk 策略足够简单（句号断点 + token 上限），
    自建 20 行代码比引入 LangChain 依赖更可控。
    """
    tokens = _ENCODING.encode(text)
    if len(tokens) <= max_tokens:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current: list[int] = []

    for line in text.split(". "):
        line_tokens = _ENCODING.encode(line)
        if len(current) + len(line_tokens) > max_tokens:
            if current:
                chunks.append(_ENCODING.decode(current))
                current = []
            # 超长行（图表数据、长URL等）→ 强制按 max_tokens 切分
            if len(line_tokens) > max_tokens:
                for i in range(0, len(line_tokens), max_tokens):
                    chunks.append(_ENCODING.decode(line_tokens[i:i + max_tokens]))
            else:
                current = line_tokens
        else:
            current.extend(line_tokens)

    if current:
        chunks.append(_ENCODING.decode(current))
    return chunks


# ═════════════════════════════════════════════════════════════════════════════
# Collector Agent 主函数
# ═════════════════════════════════════════════════════════════════════════════

async def collector_agent(
    task: dict,
    mcp_server: MCPServer,
    llm: ChatDeepSeek,
) -> dict:
    """Collector Agent — 搜索 + 抓取 + 分块 + 嵌入 + 存储。

    【L5 架构】Agent 统一签名
    ────────────────────────
    async def xxx_agent(task, mcp_server, llm) -> dict
    所有 Agent 共享此签名。task 是 Supervisor 注入的状态字典，
    mcp_server 是工具能力（含 settings），llm 是推理引擎。

    Args:
        task: {id, title, competitors: [str], dimensions: [str]}
        mcp_server: MCP 工具服务器（含 settings 配置入口）
        llm: ChatDeepSeek 客户端（temperature=0.3）

    Returns:
        {competitor_name: {chunk_ids: [str], pages: [{url, title, text}]}}
    """
    settings = mcp_server.settings
    task_id = task["id"]
    competitors = task["competitors"]
    dimensions = task["dimensions"]

    # 【L4 工程】每次 Agent 调用新建连接池（短期连接模式）
    # 因为 Agent 可能在不同服务器上运行，不适合跨进程复用连接
    pool = await create_pool(settings)
    chunk_dao = ChunkEmbeddingDAO(pool)
    log_dao = AgentLogDAO(pool)

    # 【L4 工程】Semaphore 值来自配置（默认3），可以根据目标站点的反爬力度调
    semaphore = asyncio.Semaphore(settings.max_concurrent_collectors)
    result: dict[str, dict] = {}
    t0 = time.perf_counter()

    for competitor in competitors:
        result[competitor] = {"chunk_ids": [], "pages": []}

    # ──────────── 步骤1: LLM 生成搜索关键词 ────────────
    # 【L4 工程】每个竞品独立调用 LLM 生成 query
    # 不并行化（llm.ainvoke 不支持同时多请求，除非用多 key 池）
    all_queries: list[tuple[str, str, str]] = []  # (competitor, dimension, query)
    for competitor in competitors:
        keywords = await _generate_keywords(competitor, dimensions, llm)
        # keyword × dimension 笛卡尔积 → 每个 query 对照每个维度搜索
        for kw in keywords:
            for dim in dimensions:
                all_queries.append((competitor, dim, kw))

    """
    
    输入: （来自用户填写的表单）{"competitor": "飞书", "dimensions": ["定价", "功能"]}
    输出: all_queries 如下：
    [
     "飞书企业版定价 2025", 
     "飞书收费方案 对比",
     "飞书专业版 企业版 价格区别", 
     "飞书最新功能 更新", 
     "飞书 vs 钉钉 功能对比"
    ]
    
    """



    # ──────────── 步骤2: 并发搜索 ────────────
    # 【L3 核心考点】asyncio.gather 的并发语义
    # gather 把 N 个协程同时提交到事件循环，全部完成后再返回。
    # 不是多线程/多进程——所有协程在单线程中交替执行，IO 等待时让出。
    # 【L4 工程】Semaphore 限制实际并发数，保护目标服务不被 DDoS

    async def _search_one(competitor: str, _dim: str, query: str):
        async with semaphore:
            resp = await mcp_server.call_tool("web_search", {"query": query, "max_results": 3})
            # 【L4 工程】MCP 错误处理：不因为单条搜索失败而阻塞整个 gather
            if resp.get("isError"):
                return (competitor, [])
            try:
                results = json.loads(resp["content"][0]["text"])
                return (competitor, results)
            except Exception:
                return (competitor, [])

    search_tasks = [_search_one(c, d, q) for (c, d, q) in all_queries]
    search_results = await asyncio.gather(*search_tasks)

    # ──────────── 搜索结果去重 + 分组 ────────────
    # 【L4 工程】URL 全局去重：不同 query 可能返回相同的 URL
    # 如果不去重，会重复抓取同一个页面——浪费带宽 + 被目标站点视为爬虫攻击
    seen_urls: set[str] = set()
    competitor_urls: dict[str, list[dict]] = {c: [] for c in competitors}
    for competitor, results in search_results:
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                competitor_urls[competitor].append({
                    "url": url,
                    "title": r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                })

    # ──────────── 步骤3: 并发抓取网页内容 ────────────
    # 【L4 工程】每个竞品最多抓取 5 条 URL（urls[:5]）
    #   — 5 是经验值：超过 5 个页面通常是重复内容或低质量内容
    #   — 对 5 个竞品 × 5 条 URL = 25 个页面，每个 15000 字符
    #   — 总共 ~375KB 文本，chunk 化后约 50~80 个 chunk

    async def _fetch_one(competitor: str, page_info: dict):
        """抓取单个页面内容，返回 (竞品名, 页面数据)。

        【L4 工程】Semaphore 控制并发连接数。
        web_fetch max_chars=15000：取足够的分析上下文（约 3000~5000 中文字），
        但避免整站下载（可能 100KB+ 的 HTML/JSS/CSS 混合）。
        """
        url = page_info["url"]
        async with semaphore:
            resp = await mcp_server.call_tool("web_fetch", {"url": url, "max_chars": 15000})
            if resp.get("isError"):
                logger.warning("Fetch failed for %s", url)
                return (competitor, {
                    "url": url,
                    "title": page_info["title"],
                    "text": "",
                    "error": resp["content"][0]["text"],
                })
            try:
                data = json.loads(resp["content"][0]["text"])
                return (competitor, {
                    "url": url,
                    "title": data.get("title", page_info["title"]),
                    "text": data.get("text_content", ""),
                })
            except Exception:
                return (competitor, {"url": url, "title": page_info["title"], "text": ""})

    fetch_tasks = []
    for competitor, urls in competitor_urls.items():
        for u in urls[:5]:  # 每竞品最多 5 条 URL
            fetch_tasks.append(_fetch_one(competitor, u))

    fetch_results = await asyncio.gather(*fetch_tasks)

    # ──────────── 步骤4: 文本分块 ────────────
    # 收集所有有文本的页面 → 逐个分块
    all_texts: list[str] = []
    text_meta: list[dict] = []  # {competitor, url, title, chunk_texts: [...]}
    for competitor, page in fetch_results:
        result[competitor]["pages"].append(page)
        if page["text"]:
            all_texts.append(page["text"])
            text_meta.append({
                "competitor": competitor,
                "url": page["url"],
                "title": page["title"],
                "chunk_texts": [],
            })

    for i, text in enumerate(all_texts):
        chunks = _chunk_text(text)
        if chunks:
            text_meta[i]["chunk_texts"] = chunks

    # ──────────── 步骤5+6: 批量嵌入 + 批量写入 ────────────
    # 【L4 工程】为什么分两步（embed + insert）而不是边 embed 边 insert？
    #   ① GPU 利用率：BGE-M3 模型推理时 GPU 利用率最高在 batch 推理
    #   ② DB 事务开销：一次 INSERT 100 行 vs 100 次 INSERT 1 行 → 前者快 10-50 倍
    #   ③ 错误恢复：如果 INSERT 失败，整个批次可以重试（不需要重新 embed）

    # 展平所有 chunk 为列表
    all_chunks: list[str] = []
    chunk_index_map: list[int] = []  # chunk_idx → text_meta_idx
    for i, meta in enumerate(text_meta):
        for ch in meta["chunk_texts"]:
            all_chunks.append(ch)
            chunk_index_map.append(i)

    if all_chunks:
        # 【L4 工程】BATCH = 12 的来历
        # BGE-M3 1024-dim 向量 → 每个 float32 × 1024 = 4KB
        # 12 × 4KB = 48KB 嵌入数据 + 模型中间激活 ≈ 8GB VRAM 以内
        # 如果显存更大，可以调到 32~64
        embeddings = []
        BATCH = 12
        for i in range(0, len(all_chunks), BATCH):
            batch = all_chunks[i:i + BATCH]
            emb = await embed_texts(batch, settings)
            embeddings.extend(emb)

        # 构造批量写入记录
        chunk_records: list[dict] = []
        for idx, (chunk_text, embedding) in enumerate(zip(all_chunks, embeddings)):
            meta_idx = chunk_index_map[idx]
            chunk_records.append({
                "chunk_text": chunk_text,
                "chunk_index": idx,
                "source_url": text_meta[meta_idx]["url"],
                "embedding": embedding,
            })

        await chunk_dao.batch_insert(task_id, chunk_records)

        # ──────────── chunk_id 回收 ────────────
        # 【L4 工程】为什么 batch_insert 后再 SELECT 而不是依赖 INSERT RETURNING？
        #   asyncpg 的 executemany 不返回结果，Copy 协议也不返回结果。
        #   如果要逐条 RETURNING，只能用单条 INSERT（性能差 10 倍以上）。
        #   与其损失性能，不如批量 INSERT + 一条 SELECT 回收 ID。
        #   这条 SELECT 走了 task_id 索引，成本可以忽略。

        """
        chunk_ids 是 Collector 写完库后 SELECT 回收的，存进 result dict，
        但 Analyzer 完全不看这个字段——它直接用 task_id 从 DB 检索。

        那 chunk_ids 现在有什么用？
        两条实际用途 + 一条事实：
        
        1. 日志可观测性 — AgentLogDAO.log 记录了 total_chunks，Supervisor 可以监控"这个任务采集了多少 chunk"，不需要查 DB
        2. 未来 Supervisor 编排 — 比如做进度反馈：“飞书已采集 15 个片段，钉钉已采集 12 个片段”——需要知道每个竞品有多少 chunk
        事实：当前 Analyzer 不消费它，删了不影响检索流程
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, source_url FROM chunk_embeddings WHERE task_id = $1",
                task_id,
            )
            url_to_ids: dict[str, list[str]] = {}
            for row in rows:
                url = row["source_url"]
                url_to_ids.setdefault(url, []).append(str(row["id"]))

        for meta in text_meta:
            competitor = meta["competitor"]
            url = meta["url"]
            if url in url_to_ids:
                result[competitor]["chunk_ids"].extend(url_to_ids[url])

    # ──────────── Agent 日志 ────────────
    # 【L4 工程】每个 Agent 执行完毕后记录日志
    # supervisor 可以根据日志判断每个阶段的耗时、成功率
    duration_ms = (time.perf_counter() - t0) * 1000
    await log_dao.log(
        task_id=task_id,
        agent_name="collector",
        action="collect_and_store",
        request={"competitors": competitors, "dimensions": dimensions},
        response={
            "total_pages": sum(len(v["pages"]) for v in result.values()),
            "total_chunks": len(all_chunks),
        },
        duration_ms=round(duration_ms, 1),
    )

    logger.info(
        "日志：collector执行完成：共 %d 个页面，拆分 %d 个文本块，耗时 %.0f 毫秒",
        sum(len(v["pages"]) for v in result.values()),
        len(all_chunks),
        duration_ms,
    )


    """
    # collector 的 return result
    {
        "飞书": {
            "chunk_ids": ["uuid-1", "uuid-2", "uuid-3", ...],
            "pages": [
                {"url": "https://feishu.cn/pricing", "title": "...", "text": "飞书企业版..."},
                ...
            ]
        },
        "钉钉": {...}
    }
    """
    return result