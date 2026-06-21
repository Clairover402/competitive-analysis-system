# Phase 3 实现总结 — Agent 实现层

**时间**: 2026-06-21
**作者**: AI工程师
**范围**: Phase 3（src/agents/）5 模块 + 4 Prompt 文件

---

## 一、Phase 3 是什么？

Phase 3 实现了竞品分析系统的四个专精 Agent，每个 Agent 负责分析流程的一个环节：

```
用户输入 ──→ Collector ──→ Analyzer ──→ Writer ──→ Quality ──→ 最终报告
 (task)      (数据采集)     (推理分析)     (报告撰写)   (质量门禁)    (pass/fail)
```

---

## 二、伪代码流程图

### 2.1 Collector Agent — 数据采集流水线

```
┌─────────────────────────────────────────────────────────────────────┐
│                    collector_agent(task, mcp_server, llm)           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  INPUT: task = {id, title, competitors: [C1,C2,...], dimensions: [D1,D2,...]} │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ STEP 1: 生成搜索关键词                                       │  │
│  │                                                              │  │
│  │ FOR each competitor IN competitors:                          │  │
│  │     keywords = await _generate_keywords(competitor, dims, llm)│  │
│  │     # 降级: LLM失败 → 模板拼接 "{竞品} {维度}"               │  │
│  │     FOR kw IN keywords × dim IN dimensions:                  │  │
│  │         all_queries.append((competitor, dim, kw))            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ STEP 2: 并发搜索 (asyncio.gather + Semaphore)                │  │
│  │                                                              │  │
│  │ semaphore = asyncio.Semaphore(max_concurrent_collectors)     │  │
│  │                                                              │  │
│  │ async def _search_one(c, d, q):                              │  │
│  │     async with semaphore:  # ← 控制并发上限                    │  │
│  │         resp = await mcp_server.call_tool("web_search", q)   │  │
│  │         return (c, parse_results(resp))                       │  │
│  │                                                              │  │
│  │ search_results = await asyncio.gather(*search_tasks)         │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 去重 + 分组                                                  │  │
│  │                                                              │  │
│  │ seen_urls = set()                                            │  │
│  │ competitor_urls = {c: [] for c in competitors}               │  │
│  │ FOR (competitor, results) IN search_results:                 │  │
│  │     FOR r IN results:                                        │  │
│  │         IF r.url NOT IN seen_urls:                           │  │
│  │             seen_urls.add(r.url)                             │  │
│  │             competitor_urls[competitor].append(r)            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ STEP 3: 并发抓取网页 (asyncio.gather + Semaphore)             │  │
│  │                                                              │  │
│  │ async def _fetch_one(c, page_info):                          │  │
│  │     async with semaphore:                                    │  │
│  │         resp = await mcp_server.call_tool("web_fetch", url)  │  │
│  │         return (c, {url, title, text})                       │  │
│  │                                                              │  │
│  │ FOR c, urls IN competitor_urls.items():                      │  │
│  │     FOR u IN urls[:5]:  # ← 每竞品上限5条URL                  │  │
│  │         fetch_tasks.append(_fetch_one(c, u))                 │  │
│  │                                                              │  │
│  │ fetch_results = await asyncio.gather(*fetch_tasks)           │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ STEP 4: 文本分块 (tiktoken, 800~1200 tokens/chunk)           │  │
│  │                                                              │  │
│  │ FOR each page_text IN fetch_results:                         │  │
│  │     chunks = _chunk_text(page_text)  # 句号断点优先           │  │
│  │     text_meta[i].chunk_texts = chunks                        │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ STEP 5: 批量嵌入向量化 (BGE-M3, batch_size=12)                │  │
│  │                                                              │  │
│  │ all_chunks = flatten(text_meta[].chunk_texts)                 │  │
│  │ FOR i IN range(0, len(all_chunks), 12):                      │  │
│  │     batch = all_chunks[i:i+12]                               │  │
│  │     embeddings.extend(await embed_texts(batch, settings))    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ STEP 6: 批量写入 + ID 回收                                    │  │
│  │                                                              │  │
│  │ await chunk_dao.batch_insert(task_id, chunk_records)         │  │
│  │ rows = await conn.fetch(                                     │  │
│  │     "SELECT id, source_url FROM chunk_embeddings              │  │
│  │      WHERE task_id=$1", task_id)                             │  │
│  │ # 按 source_url 分组，回填 result[competitor]["chunk_ids"]   │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  OUTPUT: {C1: {chunk_ids:[...], pages:[...]}, C2: ...}              │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Analyzer Agent — RAG 检索 + 多维分析

```
┌─────────────────────────────────────────────────────────────────────┐
│                     analyzer_agent(task, mcp_server, llm)           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  INPUT: task = {id, title, competitors, dimensions}                 │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 并行编排: 每个维度独立启动一个分析协程                        │  │
│  │                                                              │  │
│  │ tasks_coros = [                                              │  │
│  │     _retrieve_and_analyze_dimension(                         │  │
│  │         task_id, competitors, dim, chunk_dao, llm, settings  │  │
│  │     )                                                        │  │
│  │     FOR dim IN dimensions                                    │  │
│  │ ]                                                            │  │
│  │                                                              │  │
│  │ results = await asyncio.gather(*tasks_coros,                 │  │
│  │                                return_exceptions=True)  ← 关键│  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│           每个维度独立执行以下 6 步 ──────────────────────┐         │
│                                                          │         │
│  ┌───────────────────────────────────────────────────────▼──────┐  │
│  │ _retrieve_and_analyze_dimension(task_id, competitors, dim)   │  │
│  ├──────────────────────────────────────────────────────────────┤  │
│  │                                                              │  │
│  │  Step 1: 嵌入维度文本                                        │  │
│  │     query_vec = await embed_query(dim, settings)              │  │
│  │     # "定价策略" → [0.12, -0.34, ...] (1024-dim)            │  │
│  │                                                              │  │
│  │  Step 2: 向量检索 (语义召回)                                 │  │
│  │     vec_results = await chunk_dao.similarity_search(         │  │
│  │         task_id, query_vec, top_k=30                         │  │
│  │     )                                                        │  │
│  │     # pgvector HNSW 索引, <=> 余弦距离                       │  │
│  │                                                              │  │
│  │  Step 3: 关键词检索 (字面召回)                               │  │
│  │     kw_results = await chunk_dao.keyword_search(             │  │
│  │         task_id, dim, top_k=20                               │  │
│  │     )                                                        │  │
│  │     # zhparser 中文分词 + pg_bigm 2-gram                     │  │
│  │                                                              │  │
│  │  Step 4: 合并去重 (URL 级别)                                 │  │
│  │     seen = set()                                             │  │
│  │     merged = []                                              │  │
│  │     FOR r IN vec_results + kw_results:                       │  │
│  │         IF r.url NOT IN seen:                                │  │
│  │             seen.add(r.url)                                  │  │
│  │             merged.append(r)                                 │  │
│  │                                                              │  │
│  │  Step 5: Cross-encoder 精排                                  │  │
│  │     ranked = await rerank(                                   │  │
│  │         dim,                                                 │  │
│  │         [r.chunk_text for r in merged],  # 候选文档列表      │  │
│  │         top_k=15,                       # 精排取 Top15       │  │
│  │         settings=settings                                    │  │
│  │     )                                                        │  │
│  │     # BGE-reranker-v2-m3: [query, doc] 联合编码 → 精确排序   │  │
│  │                                                              │  │
│  │  Step 6: LLM 生成分析                                        │  │
│  │     chunks_text = format(top_docs, top_sources)              │  │
│  │     prompt = ANALYSIS_PROMPT % (dim, competitors, chunks)    │  │
│  │     resp = await llm.ainvoke(prompt)                         │  │
│  │     return json.loads(resp.content)                          │  │
│  │                                                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 后处理: 异常 → [分析失败] 标记                               │  │
│  │                                                              │  │
│  │ FOR (dim, result) IN zip(dimensions, results_list):          │  │
│  │     IF isinstance(result, Exception):                        │  │
│  │         analysis[dim] = {c: f"[分析失败] {result}" ...}      │  │
│  │     ELIF "error" in result:                                  │  │
│  │         analysis[dim] = {c: f"[分析失败] {result.error}"}    │  │
│  │     ELSE:                                                    │  │
│  │         analysis[dim] = result  # 正常                        │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  OUTPUT: {"定价策略": {"飞书":"...", "钉钉":"..."}, "功能对比": ...} │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 Writer Agent — 报告撰写

```
┌─────────────────────────────────────────────────────────────────────┐
│                       writer_agent(task, mcp_server, llm)           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  INPUT: task = {id, title, competitors, dimensions,                 │
│                 analysis_results: {...},                            │
│                 rewrite_suggestions: [...] | None}                   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Step 1: 格式化输入                                           │  │
│  │                                                              │  │
│  │ results_str = json.dumps(analysis_results,                    │  │
│  │     ensure_ascii=False, indent=2)   # 中文原样输出            │  │
│  │                                                              │  │
│  │ suggestions_str = ""                                         │  │
│  │ IF rewrite_suggestions:  # ← Quality 的回退建议              │  │
│  │     suggestions_str = "改写作要求:\n" +                       │  │
│  │         "\n".join(f"- {s}" for s in rewrite_suggestions)     │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Step 2: 构造 prompt → LLM 生成                               │  │
│  │                                                              │  │
│  │ prompt = WRITER_PROMPT % (title, 竞品列表, 维度列表,          │  │
│  │                           results_str, suggestions_str)       │  │
│  │                                                              │  │
│  │ resp = await llm.ainvoke(prompt)  # temperature=0.3          │  │
│  │ report = resp.content.strip()                                │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Step 3: 记录日志                                             │  │
│  │                                                              │  │
│  │ await log_dao.log(task_id, "writer", "generate_report", ...) │  │
│  │ # 标记 rewrite=bool(rewrite_suggestions) 方便监控            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  OUTPUT: {report_markdown: "# 报告标题\n## 概述\n..."}               │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.4 Quality Agent — 质量门禁

```
┌─────────────────────────────────────────────────────────────────────┐
│                      quality_agent(task, mcp_server, llm)           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  INPUT: task = {id, title, competitors, dimensions,                 │
│                 report_markdown: "..."}                              │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Step 1: LLM-as-Judge 五维评分                                 │  │
│  │                                                              │  │
│  │ prompt = QUALITY_PROMPT % (title, 竞品, 维度, report)         │  │
│  │ resp = await llm.ainvoke(prompt)  # temperature=0.0          │  │
│  │ parsed = json.loads(resp.content)                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Step 2: 代码重算加权分数（防 LLM 算术错误）                  │  │
│  │                                                              │  │
│  │ WEIGHTS = {                                                  │  │
│  │     "完整性":   0.30,                                        │  │
│  │     "准确性":   0.30,                                        │  │
│  │     "可追溯性": 0.20,                                        │  │
│  │     "可读性":   0.10,                                        │  │
│  │     "客观性":   0.10                                         │  │
│  │ }                                                            │  │
│  │                                                              │  │
│  │ computed_score = 0.0                                         │  │
│  │ FOR dim_name, weight IN WEIGHTS.items():                     │  │
│  │     IF dim_name IN parsed.dimensions:                        │  │
│  │         computed_score += parsed.dimensions[name].score      │  │
│  │                          × weight                            │  │
│  │                                                              │  │
│  │ overall_score = round(computed_score, 1)  # 覆盖 LLM 的输出 │  │
│  │ passed = overall_score >= 70                                 │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Step 3: 写入 reports 表                                      │  │
│  │                                                              │  │
│  │ await report_dao.create(                                     │  │
│  │     task_id=task_id,                                         │  │
│  │     content=report,               # 报告文本                 │  │
│  │     quality_score=overall_score,   # 计算后的分数             │  │
│  │     quality_details=dim_scores,    # 各维度明细               │  │
│  │ )                                                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  OUTPUT: {overall_score, passed, dimensions, issues,                 │
│           rewrite_suggestions}                                       │
│                                                                     │
│  ┌─── 调用方 (Supervisor) 的处理逻辑 ──────────────────────────┐  │
│  │                                                              │  │
│  │ IF result.passed:                                            │  │
│  │     任务完成 ✓                                               │  │
│  │ ELSE:                                                        │  │
│  │     task["rewrite_suggestions"] = result.rewrite_suggestions │  │
│  │     task["report_markdown"] = ...  # (旧报告,供参考)          │  │
│  │     await writer_agent(task, mcp_server, llm)  # 重新撰写    │  │
│  │     await quality_agent(task, mcp_server, llm)  # 重新评分    │  │
│  │     # → 最多重试 2 次，超过则降级为人工审核                  │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 三、L3/L4/L5 知识点索引

### L3 核心考点（面试必问）

| 知识点 | 所在文件 | 关键概念 |
|--------|---------|---------|
| RAG 检索全链路 | analyzer.py | 向量检索 → 关键词检索 → 合并去重 → C-E重排 → LLM |
| Bi-encoder vs Cross-encoder | analyzer.py | 独立编码 vs 联合编码；粗排 vs 精排；O(1) vs O(N) |
| 向量语义检索 vs 关键词字面检索 | analyzer.py | 互补关系：语义覆盖 + 精确匹配 |
| tiktoken 分块原理 | collector.py | BPE 分词器；字符 vs token；中文 token 膨胀 |
| LLM-as-Judge 范式 | quality.py | 评分标准→prompt→结构化JSON；五大维度加权 |
| 纯 LLM Agent 模式 | writer.py | Agent+Tool / Agent+RAG / 纯LLM 三种模式对比 |
| asyncio.gather 并发语义 | collector.py | 协程并发 vs 多线程；事件循环调度 |

### L4 工程实践

| 工程点 | 所在文件 | 决策逻辑 |
|--------|---------|---------|
| Semaphore 限流 | collector.py | 保护目标站点；配置化 max_concurrent_collectors |
| LLM 输出清理（markdown代码块） | collector/analyzer/quality | 模型不一定遵守 "只输出 JSON"，预清理防御 |
| 降级策略（LLM关键词→模板拼接） | collector.py | LLM 失败不阻塞流程；最低保证（模板兜底） |
| URL 去重（不是 chunk 文本去重） | analyzer.py | 避免同页面多 chunk 浪费 LLM token |
| 批量插入 vs 逐条插入 | collector.py | batch INSERT 性能优于逐条 10~50 倍 |
| ID 回收（INSERT 后 SELECT） | collector.py | asyncpg 批量插入不返回 ID；索引查询替代 |
| return_exceptions=True | analyzer.py | 单维失败不阻塞其他维度 |
| 代码重算分数（防 LLM 算术错误） | quality.py | LLM 产出子分数 → 代码执行加权和 |
| 工厂模式统一 LLM 配置 | __init__.py | 防止 api_key 散落；统一插拔 |

### L5 架构决策

| 决策 | 所在文件 | 理由 |
|------|---------|------|
| Collector 不做 LLM 提取 | collector.py | 职责分离 + 故障隔离 + 流水线编排 |
| 搜索与抓取分开两次 gather | collector.py | 搜索→去重→抓取，避免重复抓取 |
| 每竞品最多 5 条 URL | collector.py | 5条足以覆盖定价/功能等核心信息 |
| 维度并行分析（非串行） | analyzer.py | 总时间 = max(各维度)，显著快于串行 |
| 失败返回 dict 而不抛异常 | analyzer.py | Supervisor 不需要 try/except；报告可标注 |
| temperature 按 Agent 选值 | __init__.py | Collector/W=0.3, Analyzer=0.1, Quality=0.0 |
| Writer 不调工具 | writer.py | 纯 LLM Agent；输入已结构化 |
| Quality 写 reports 表 | quality.py | Writer 负责生成，Quality 负责评估+持久化 |
| overall >= 70 阈值 | quality.py | 60分及格 + 10分缓冲区；实际 source_url 缺失影响 |

---

## 四、验收 Bug 修复记录

### Bug #1: collector.py `_fetch_one` 函数缺失 (Critical)

**根因**: 函数头丢失，body 错位嵌入 `if url and url not in seen_urls:` 循环体内部。
**表现**: 
- 第 164-175 行代码在 dedup 循环中串行执行（未并发）
- 第 180 行 `fetch_tasks.append(_fetch_one(...))` → 运行时 `NameError`
**修复**: 正确声明 `async def _fetch_one(competitor, page_info)`，移除重复的串行执行代码

### Bug #2: analyzer.py settings 传递不一致 (Minor)

**根因**: `embed_query(dimension)` 和 `rerank(dimension, docs, top_k=15)` 不传 settings
**影响**: 虽可 fallback 到默认 `Settings()`，但与 collector.py 风格不一致
**修复**: `_retrieve_and_analyze_dimension` 新增 `settings` 参数，全链路显式传递

---

## 五、文件清单

| 文件 | 大小 | 函数数 | 注释风格 |
|------|------|--------|---------|
| `src/agents/__init__.py` | 4.0KB | 1 | L4工程 + L5决策 |
| `src/agents/collector.py` | 21.7KB | 5 | L3+L4+L5 全标注 |
| `src/agents/analyzer.py` | 18.1KB | 2 | L3+L4+L5 全标注 |
| `src/agents/writer.py` | 7.2KB | 1 | L3+L4+L5 全标注 |
| `src/agents/quality.py` | 9.2KB | 1 | L3+L4+L5 全标注 |
| `prompts/collector.md` | — | — | 正例+反例+兜底规则 |
| `prompts/analyzer.md` | — | — | 正例+反例+source_url约束 |
| `prompts/writer.md` | — | — | 正例+反例+报告模板 |
| `prompts/quality.md` | — | — | 正例+反例+评分标准表 |

---

## 六、下一步：Phase 4 — 记忆系统

按 `DEVELOPMENT_PLAN.md`，Phase 4 交付:
- `src/memory/__init__.py` — 模块导出
- `src/memory/short_term.py` — 滑动窗口短期记忆
- `src/memory/summarizer.py` — LLM 摘要压缩
- `src/memory/long_term.py` — 语义检索长期记忆
- `src/memory/retriever.py` — 三因子排序（语义 × 重要性 × 时间衰减）
- `src/memory/forgetting.py` — 遗忘策略（半衰期: decision=90d, chat=7d）
