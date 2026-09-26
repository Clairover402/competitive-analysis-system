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

# 【2026-07-29】内容质量门禁阈值：正文中文字数（CJK Unified Ideographs）低于此值视为 SPA 骨架页，丢弃
# 【2026-09-26】提升为模块级常量——_fetch_one（步骤3）需在分块前用它判断 Tavily 预取正文是否达标
_MIN_CJK_CHARS = 200

# ═════════════════════════════════════════════════════════════════════════════
# §1 搜索关键词生成
# ═════════════════════════════════════════════════════════════════════════════

_KEYWORD_PROMPT = """你是一个搜索专家。根据竞品名称和分析维度，生成 3-5 个精准的搜索 query。
要求：每个 query 要具体，结合竞品名+维度+时间限定，不同 query 覆盖不同角度。

输入: {competitor: "%s", dimensions: %s}
输出: ["query1", "query2", ...]"""


# 【L4 工程】错误信息提取——降级日志只打根因，不打完整堆栈
# exc_info=True 的堆栈会刷屏，用 traceback 手动提取最后一行作为摘要
import traceback as _traceback

def _extract_error_root_cause() -> str:
    """提取当前异常的最后一帧信息作为根因摘要。"""
    tb = _traceback.format_exc()
    lines = tb.strip().split("\n")
    # 找最后一行非空的 traceback 文本（通常是异常类名 + 信息）
    for line in reversed(lines):
        line = line.strip()
        if line:
            # 截断到 120 字符，避免超长错误信息
            return line[:120]
    return "unknown error"


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

    # ── 以空响应重试来容错模型临时的返回格式异常 ──
    max_attempts = 2
    last_error = None
    for attempt in range(max_attempts):
        try:
            resp = await llm.ainvoke(prompt)
            text = (resp.content or "").strip()

            # 【L4 工程】防御空响应：LLM 有时返回空内容
            if not text:
                raise ValueError("LLM returned empty response (attempt %d)" % (attempt + 1))

            # 清理可能的 markdown 代码块包裹
            # 【2026-07-29 强化】兼容更多格式变体：```json\n...```、```list\n...```、
            # 甚至直接 ```[...]```（无语言标识）
            if text.startswith("```"):
                # 去掉开头的 ``` 及可选的语言标识行
                # 格式: ```json\n...\n``` 或 ```\n...\n``` 或 ```[...]```
                cleaned = text[3:]  # 去掉前3个反引号
                newline_idx = cleaned.find("\n")
                if newline_idx >= 0:
                    cleaned = cleaned[newline_idx:].strip()
                else:
                    # 无换行：结尾是 ``` → 去掉
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3].strip()
                    else:
                        cleaned = cleaned.strip()
                # 去掉结尾的 ```
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].strip()
                text = cleaned

            # 【2026-07-29 强化】兼容 "前面解释+JSON" 格式
            # 在 LLM 不遵守"只输出 JSON"时，从文本中提取 [ 到 ] 部分
            if not text.startswith("["):
                bracket_idx = text.find("[")
                if bracket_idx >= 0:
                    bracket_end = text.rfind("]")
                    if bracket_end > bracket_idx:
                        text = text[bracket_idx:bracket_end + 1]

            # 【2026-07-29 强化】空值二次防御 + JSONDecodeError 重试
            if not text or not text.strip():
                raise ValueError("LLM output became empty after extraction (attempt %d)" % (attempt + 1))

            keywords = json.loads(text)
            if isinstance(keywords, list) and len(keywords) > 0:
                logger.info("日志：LLM为 %r 生成了 %d 个关键词", competitor, len(keywords))
                return keywords
            else:
                raise ValueError("LLM returned non-list or empty list (attempt %d)" % (attempt + 1))

        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            if attempt < max_attempts - 1:
                logger.warning(
                    "关键词生成第%d次尝试失败: %s，重试中...",
                    attempt + 1, str(e)[:120],
                )
                continue  # 重试

    # ── 2 次尝试均失败 → 记录根因 + 降级到模板 ──
    logger.warning(
        "为 %r 生成关键词失败(%d次尝试)，将使用模板兜底（根因: %s）",
        competitor,
        max_attempts,
        _extract_error_root_cause() if last_error else "unknown",
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

    # ── ✏️ 输入日志 ──
    logger.info(
        "【Collector】开始 task=%s 竞品=%d个(%s) 维度=%d个(%s)",
        task_id,
        len(competitors), ",".join(competitors),
        len(dimensions), ",".join(dimensions),
    )

    for competitor in competitors:
        result[competitor] = {"chunk_ids": [], "pages": []}

    # ══════════════════════════════════════════════════════════
    # 步骤1: LLM 生成搜索关键词
    # ══════════════════════════════════════════════════════════
    t_generate = time.perf_counter()
    # 【L4 工程】每个竞品独立调用 LLM 生成 query
    # 不并行化（llm.ainvoke 不支持同时多请求，除非用多 key 池）
    all_queries: list[tuple[str, str]] = []  # (competitor, query)
    per_competitor_keywords: dict[str, list[str]] = {}  # 记录每个竞品的关键词生成结果
    for competitor in competitors:
        keywords = await _generate_keywords(competitor, dimensions, llm)
        per_competitor_keywords[competitor] = keywords
        # 每个关键词搜索一次（不去重，留给后面的 seen_urls 处理）
        for kw in keywords:
            all_queries.append((competitor, kw))
    
    # ── ✏️ 日志：关键词生成完成（含每个竞品的详细结果）──
    await log_dao.log(
        task_id=task_id, agent_name="collector", action="generate_keywords",
        request={"competitors": competitors, "dimensions": dimensions},
        response={
            "total_queries": len(all_queries),
            "per_competitor": {
                c: {"keyword_count": len(kws), "keywords": kws}
                for c, kws in per_competitor_keywords.items()
            },
        },
        duration_ms=round((time.perf_counter() - t_generate) * 1000, 1),
    )

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

    async def _search_one(competitor: str, query: str):
        async with semaphore:
            # 【2026-09-26】max_results 由 3 → 10：原硬编码 3 导致候选池过小
            # （5 关键词 × 3 = 15 候选，再被相关性过滤砍到个位数），采集覆盖不足。
            resp = await mcp_server.call_tool("web_search", {"query": query, "max_results": 10})
            # 【L4 工程】MCP 错误处理：不因为单条搜索失败而阻塞整个 gather
            if resp.get("isError"):
                return (competitor, [])
            try:
                results = json.loads(resp["content"][0]["text"])
                return (competitor, results)
            except Exception as e:
                logger.warning("搜索结果 JSON 解析失败 competitor=%s query=%s: %s",
                                competitor, query[:50], e)
                return (competitor, [])

    search_tasks = [_search_one(c, q) for (c, q) in all_queries]
    t_search = time.perf_counter()
    search_results = await asyncio.gather(*search_tasks)

    # ── ✏️ 日志：搜索完成（含每个竞品的搜索结果明细）──
    search_detail: dict[str, dict] = {}
    total_results = 0
    for competitor, results in search_results:
        if competitor not in search_detail:
            search_detail[competitor] = {"total": 0, "sample_urls": []}
        search_detail[competitor]["total"] += len(results)
        total_results += len(results)
        if results:
            # 取首条结果的标题和 URL 作为样本
            first = results[0]
            search_detail[competitor]["sample_urls"].append(
                {"title": first.get("title", "")[:50], "url": first.get("url", "")}
            )
    await log_dao.log(
        task_id=task_id, agent_name="collector", action="search_complete",
        request={"queries": len(search_results)},
        response={"total_results": total_results, "per_competitor": search_detail},
        duration_ms=round((time.perf_counter() - t_search) * 1000, 1),
    )

    # ──────────── 搜索结果去重 + 分组 ────────────
    # 【L4 工程】URL 全局去重：不同 query 可能返回相同的 URL
    # 如果不去重，会重复抓取同一个页面——浪费带宽 + 被目标站点视为爬虫攻击
    # 【L4 工程】反爬/死链域名黑名单——这些站在当前部署环境（中国网络 + 无登录态）
    # 下，httpx 抓不到正文：要么 403 反爬，要么境外超时/SSL 失败。
    # 【2026-09-26 实测】王者荣耀/英雄联盟任务里，13 条 URL 有 6 条（46%）落在
    # 这些域名的死链上（zhihu 403、pinterest SSL、google/reddit 超时），浪费
    # 抓取名额 + 70 秒任务里有大量时间耗在等超时。提前过滤，把名额让给能抓的站。
    # 注意：仅当 Tavily 未预取到正文（raw_content 空）时才过滤——raw_content 非空
    # 说明 Tavily 服务端已渲染好正文，直接走直通，无需 httpx，故不跳过。
    _ANTI_SCRAPE_DOMAINS = {
        "baike.baidu.com",   # 百度百科——双重反爬（TLS fingerprint + cookie）
        "zhihu.com",         # 知乎——需登录态，httpx 无登录态 403
        "pinterest.com",     # Pinterest——境外 + 本机 SSL 证书链缺失
        "google.com",        # Google（含 sites.google.com）——境外，超时
        "reddit.com",        # Reddit——境外，超时
    }

    def _is_dead_domain(host: str) -> bool:
        """判断域名是否落在反爬/境外死链黑名单（含子域匹配）。"""
        return any(host == d or host.endswith("." + d) for d in _ANTI_SCRAPE_DOMAINS)

    # 【2026-07-29 修复】搜索结果相关性过滤
    # 问题：搜索 "英雄联盟 玩法设计" → Bing 返回 "英雄（张艺谋电影）"、
    # "英雄_CCTV节目" → Collector 抓这些页面存为 chunk → Analyzer 面对
    # 电影/电视剧的内容无法做竞品分析 → [数据不足]。
    # 这不是"英雄联盟"的个例——任何模糊匹配的搜索词都可能被同名词条污染。
    #
    # 【2026-09-26 两处修复】
    #   1. 别名映射落地：原注释声称支持 "英雄联盟→LOL/League"，但代码从未实现，
    #      导致英文优质结果（标题写 League of Legends 不含"英雄联盟"中文）被误杀。
    #   2. max_results 3→10：候选池从 5×3=15 提到 5×10=50，覆盖度显著提升。
    _COMPETITOR_ALIASES: dict[str, set[str]] = {
        "英雄联盟": {"lol", "league of legends", "league", "英雄联盟手游"},
        "王者荣耀": {"honor of kings", "王者荣耀世界", "王者"},
        # 可继续按需扩充，未列出的竞品只做全名+分词子串匹配
    }

    def _build_relevance_filter(competitor_name: str) -> set[str]:
        """从竞品名构建相关性关键词集合（含已知别名）。
        例："英雄联盟" → {"英雄联盟", "lol", "league of legends", "league"}
        例："王者荣耀" → {"王者荣耀", "honor of kings", "王者"}
        例："Notion" → {"notion"}
        """
        terms: set[str] = set()
        # 完整名必保留
        terms.add(competitor_name.strip().lower())
        # 分词子串（按空格/tab分词，过滤单字）
        for token in competitor_name.split():
            token = token.strip().lower()
            if len(token) >= 2:
                terms.add(token)
        # 已知别名（英文缩写等，中文无空格分词取不到）
        for alias in _COMPETITOR_ALIASES.get(competitor_name.strip(), ()):
            terms.add(alias)
        return terms

    def _result_relevant(title: str, snippet: str, filter_terms: set[str]) -> bool:
        """检查搜索结果是否与竞品相关。

        【2026-09-26】匹配面已通过别名映射扩宽（竞品名 + 英文缩写/别名）。
        不引入维度词匹配：维度词可能为"功能/价格"等超通用词，会引入大量噪音。
        """
        combined = (title + " " + snippet).lower()
        return any(term in combined for term in filter_terms)

    seen_urls: set[str] = set()
    competitor_urls: dict[str, list[dict]] = {c: [] for c in competitors}
    skipped_dead: int = 0  # 跳过的死链域名计数（反爬/境外双抓不到）
    skipped_irrelevant: int = 0  # 跳过的无关搜索结果计数
    for competitor, results in search_results:
        filter_terms = _build_relevance_filter(competitor)
        for r in results:
            url = r.get("url", "")
            raw = r.get("raw_content", "")
            if url and url not in seen_urls:
                # 死链过滤：仅当 Tavily 未预取到正文（raw_content 空）且域名在黑名单时跳过
                try:
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                except Exception:
                    host = ""
                if not raw and _is_dead_domain(host):
                    skipped_dead += 1
                    continue
                # 结果相关性过滤：标题+snippet不包含竞品关键词 → 跳过
                if not _result_relevant(
                    r.get("title", ""), r.get("snippet", ""), filter_terms
                ):
                    skipped_irrelevant += 1
                    continue
                seen_urls.add(url)
                competitor_urls[competitor].append({
                    "url": url,
                    "title": r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                    "raw_content": r.get("raw_content", ""),  # Tavily 预取正文（SPA 已渲染）
                })
    if skipped_dead:
        logger.info("日志：跳过 %d 个死链域名URL（zhihu/pinterest/google/reddit等，双抓不到）", skipped_dead)
    if skipped_irrelevant:
        logger.info("日志：跳过 %d 个不相关搜索结果（标题不包含竞品关键词）", skipped_irrelevant)

    # ── ✏️ 日志：最终保留待抓取的 URL ──
    # 这些 URL 已通过完整过滤链：全局去重 → 反爬域名黑名单 → 相关性过滤 → 每竞品截断8条
    final_url_lines: list[str] = []
    final_total: int = 0
    for c in competitors:
        kept = [u["url"] for u in competitor_urls[c][:8]]
        final_total += len(kept)
        final_url_lines.append(
            f"  {c}({len(kept)}条): " + (" | ".join(kept) if kept else "无")
        )
    logger.info(
        "【Collector】最终保留URL task=%s 共%d条\n%s",
        task_id, final_total, "\n".join(final_url_lines),
    )

    # ──────────── 步骤3: 并发抓取网页内容 ────────────
    # 【L4 工程】每个竞品最多抓取 8 条 URL（urls[:8]）
    #   — 8 是经验值：超过 8 个页面通常是重复内容或低质量内容
    #   — 对 5 个竞品 × 8 条 URL = 40 个页面，每个 15000 字符
    #   — 总共 ~600KB 文本，chunk 化后约 80~120 个 chunk
    #   — 【2026-09-26】由 5 上调至 8：为多产品线竞品（如英雄联盟端游/手游）留足余量，
    #     避免有效来源被截断；代价是 chunk 数增多，后续 Analyzer 检索成本略增

    async def _fetch_one(competitor: str, page_info: dict):
        """抓取单个页面内容，返回 (竞品名, 页面数据)。

        【L4 工程】Semaphore 控制并发连接数。
        web_fetch max_chars=15000：取足够的分析上下文（约 3000~5000 中文字），
        但避免整站下载（可能 100KB+ 的 HTML/JSS/CSS 混合）。

        【2026-09-26】Tavily 预取正文直通：search 阶段 Tavily 已用
        include_raw_content 服务端渲染好正文，此时跳过 web_fetch，
        既省一次请求，又绕开“httpx 抓不到 SPA 正文”的死结。
        """
        url = page_info["url"]

        # ── Tavily 预取正文：CJK 足够时直接使用，不调 web_fetch ──
        raw = page_info.get("raw_content", "")
        if raw:
            cjk = sum(1 for ch in raw if '\u4e00' <= ch <= '\u9fff')
            if cjk >= _MIN_CJK_CHARS:
                return (competitor, {
                    "url": url,
                    "title": page_info["title"],
                    "text": raw[:15000],
                })

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
        for u in urls[:8]:  # 每竞品最多 8 条 URL
            fetch_tasks.append(_fetch_one(competitor, u))

    t_fetch = time.perf_counter()
    fetch_results = await asyncio.gather(*fetch_tasks)

    # ── ✏️ 日志：抓取完成（含每页文字量明细）──
    fetch_detail: dict[str, dict] = {}
    fetched_pages = 0
    fetched_chars = 0
    for competitor, page in fetch_results:
        if competitor not in fetch_detail:
            fetch_detail[competitor] = {"pages": 0, "empty_pages": 0, "total_chars": 0}
        fetch_detail[competitor]["pages"] += 1
        text = page.get("text", "")
        if text:
            fetched_pages += 1
            fetched_chars += len(text)
            fetch_detail[competitor]["total_chars"] += len(text)
        else:
            fetch_detail[competitor]["empty_pages"] += 1
    await log_dao.log(
        task_id=task_id, agent_name="collector", action="fetch_complete",
        request={"pages": len(fetch_results), "with_text": fetched_pages},
        response={
            "total_chars": fetched_chars,
            "per_competitor": fetch_detail,
        },
        duration_ms=round((time.perf_counter() - t_fetch) * 1000, 1),
    )

    # ──────────── 步骤4: 文本分块 ────────────
    # 收集所有有文本的页面 → 逐个分块
    # 【2026-07-29 修复】内容质量门禁：剔除无效的 SPA 骨架页面
    #  httpx 只能拿到 HTML 源码，不能执行 JS → 现代 SPA 网站（pvp.qq.com、douban）
    #  返回的 text 只有导航栏、页脚、loading 文案等 20~100 字符的骨架。
    #  这些页面嵌入后变成噪音向量，污染 RAG 检索结果 → Analyzer [数据不足]。
    #  修复：统计中文汉字密度（CJK Unified Ideographs），< 200 汉字 → 丢弃。
    all_texts: list[str] = []
    text_meta: list[dict] = []  # {competitor, url, title, chunk_texts: [...]}
    discarded_pages: int = 0
    for competitor, page in fetch_results:
        result[competitor]["pages"].append(page)
        if page["text"]:
            # ── 内容质量门禁：统计有效汉字数 ──
            cjk_count = sum(1 for ch in page["text"] if '\u4e00' <= ch <= '\u9fff')
            if cjk_count < _MIN_CJK_CHARS:
                discarded_pages += 1
                logger.info(
                    "日志：丢弃低质量页面 competitor=%r url=%s CJK=%d（阈值=%d）",
                    competitor, page["url"], cjk_count, _MIN_CJK_CHARS,
                )
                continue  # 跳过 SPA 骨架页面，不进入分块和嵌入
            all_texts.append(page["text"])
            text_meta.append({
                "competitor": competitor,
                "url": page["url"],
                "title": page["title"],
                "chunk_texts": [],
            })
    if discarded_pages:
        logger.info("日志：内容门禁丢弃 %d 个低质量页面，保留 %d 个", discarded_pages, len(text_meta))

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
    # ── 构建 per-competitor 详情（与控制台日志对齐）──
    per_competitor = {}
    for comp, data in result.items():
        per_competitor[comp] = {
            "pages": len(data["pages"]),
            "chunks": len(data["chunk_ids"]),
            "urls": [p["url"] for p in data["pages"]],
        }
    await log_dao.log(
        task_id=task_id,
        agent_name="collector",
        action="collect_and_store",
        request={"competitors": competitors, "dimensions": dimensions},
        response={
            "total_pages": sum(len(v["pages"]) for v in result.values()),
            "total_chunks": len(all_chunks),
            "competitors_count": len(competitors),
            "per_competitor": per_competitor,
        },
        duration_ms=round(duration_ms, 1),
    )

    # ── ✏️ 输出日志：按竞品分维度统计 ──
    output_lines = []
    for comp, data in result.items():
        page_count = len(data["pages"])
        chunk_count = len(data["chunk_ids"])
        urls = [p["url"] for p in data["pages"]]
        output_lines.append(f"  {comp}: {page_count}页面 {chunk_count}chunk → {urls}")
    logger.info(
        "【Collector】完成 task=%s 共%d竞品 %d页面 %dchunk 耗时%.0fms\n%s",
        task_id,
        len(competitors),
        sum(len(v["pages"]) for v in result.values()),
        len(all_chunks),
        duration_ms,
        "\n".join(output_lines),
    )

    return result