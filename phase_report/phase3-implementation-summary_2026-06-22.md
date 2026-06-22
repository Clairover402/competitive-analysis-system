# Phase 3 实现总结 — Agent 实现层（面试导向版）

**时间**: 2026-06-22（优化版，基于 2026-06-21 原版）
**作者**: AI 工程师
**范围**: Phase 3（src/agents/）5 模块 + 4 Prompt 文件

---

## 一、Phase 3 是什么？

Phase 3 实现了竞品分析系统的**四个专精 Agent**，每个 Agent 负责分析流程的一个环节。这四个 Agent 被 Phase 4 的 Pipeline 编排层串联成自动化流水线。

```
用户 task ──→ Collector ──→ Analyzer ──→ Writer ──→ Quality ──→ 最终报告
             (数据采集)     (推理分析)     (报告撰写)   (质量门禁)   (pass/fail)
             温度 0.3       温度 0.1      温度 0.3      温度 0.0
             有工具          有工具(RAG)   无工具        无工具
```

**5 个交付物**:

| 文件 | 职责 | Agent 类型 | 有无工具 |
|------|------|-----------|---------|
| `__init__.py` | 公开导出 + LLM 客户端工厂 | — | — |
| `collector.py` | 搜索→抓取→分块→嵌入→存储（6 步） | Agent + Tool | web_search / web_fetch / embed_texts |
| `analyzer.py` | RAG 检索 + 多维 LLM 分析（6 步） | Agent + RAG | similarity_search / keyword_search / rerank |
| `writer.py` | Markdown 报告生成 | 纯 LLM | 无 |
| `quality.py` | LLM-as-Judge 五维评分 | 纯 LLM | 无 |

---

## 二、核心模块详解

### 2.1 `__init__.py` — LLM 客户端工厂 + Agent 导出

**唯一公开方法**:

| 方法 | 功能 | 参数 |
|------|------|------|
| `create_llm_client(settings, temperature)` | 创建 ChatDeepSeek 实例 | `settings: Settings`, `temperature: float = 0.3` |

**实现逻辑（3 步）**:
1. 从 `settings` 读取 `deepseek_api_key`、`deepseek_base_url`、`deepseek_model`
2. 组装为 `ChatDeepSeek` 构造参数
3. 注入 `temperature` 参数（调用方显式指定）

**为什么要用工厂模式包装**:
- 统一入口——所有 4 个 Agent 通过同一函数创建 LLM，不会出现"A 用 key1、B 用 key2"的混乱
- 可插拔——如果要切 OpenAI/Claude，只改这一个函数
- 默认值取最常见场景（t=0.3），特殊场景显式覆盖（Analyzer=0.1，Quality=0.0）

---

### 2.2 `collector.py` — 数据采集流水线

**公开方法**: `collector_agent(task, mcp_server, llm) -> dict`

**2 个内部函数**: `_generate_keywords()` + `_chunk_text()`

#### 内部函数 1: `_generate_keywords(competitor, dimensions, llm) -> list[str]`

**功能**: 用 LLM 将竞品名 + 维度列表转化为 3-5 条精准搜索词。

**实现逻辑**:
1. 构造 prompt：`"你是一个搜索专家。根据竞品名称和分析维度，生成 3-5 个精准的搜索 query..."`，填入竞品名和维度列表
2. 调用 `llm.ainvoke(prompt)`，读取返回的 JSON 数组
3. 防御性清理：如果返回以 ` ``` ` 开头，去掉 markdown 代码块包裹
4. `json.loads()` 解析 → 返回关键词列表
5. 如果 LLM 失败（异常或返回格式不对） → 降级为模板拼接 `["飞书 定价策略", "飞书 功能对比", ...]`

**降级策略的意义**: LLM 挂了不能阻塞整个 Collector。模板拼接是最低保证——至少每个维度有一条 query，不会零搜索。

#### 内部函数 2: `_chunk_text(text, min_tokens=800, max_tokens=1200) -> list[str]`

**功能**: 用 tiktoken 按 token 数量将网页文本切分为固定大小的块。

**实现逻辑**:
1. `tiktoken.get_encoding("cl100k_base")` 获取与模型一致的分词器
2. 将文本按 `.`（句号）分割为句子行
3. 逐行拼接：当前累计 token 数 + 新行 token 数 ≤ 1200 → 延续当前 chunk；超过 → 将当前 chunk 装瓶，开启新 chunk
4. 超长行（>1200 tokens）强制按 max_tokens 切分
5. 返回 chunk 文本列表

**为什么不用 `len(text)` 而用 tiktoken**: 中文一个字 ≈ 1.5-2 tokens，英文一个词 ≈ 1 token。用字符数切分，中文实际 token 数会比英文多 50-100%。tiktoken 用 BPE 分词器，保证 "800 tokens" 就是模型实际读到的 800 个 token。

#### 主函数: `collector_agent(task, mcp_server, llm) -> dict`

**功能**: 搜索 + 抓取 + 分块 + 嵌入 + 存储，返回 `{竞品名: {chunk_ids: [...], pages: [...]}}`。

**六大步骤**:

| 步骤 | 操作 | 关键实现 |
|------|------|---------|
| 1: 关键词生成 | 对每个竞品调用 `_generate_keywords()` | LLM 方案为主，模板拼接兜底 |
| 2: 并发搜索 | `asyncio.gather(*search_tasks)` 并发执行所有 query | Semaphore 限制 `max_concurrent_collectors`（默认 3）|
| 3: 并发抓取 | `asyncio.gather(*fetch_tasks)` 并发抓取 HTML | 每竞品最多 5 条 URL，`web_fetch` max_chars=15000 |
| 4: 文本分块 | 每个页面文本调 `_chunk_text()` | 句号自然断点，800-1200 tokens/chunk |
| 5: 批量嵌入 | BGE-M3 分批向量化 | batch_size=12，显存适配 |
| 6: 批量写入 + ID 回收 | `chunk_dao.batch_insert()` → `SELECT id, source_url WHERE task_id` | 批量 INSERT + 一条索引查询回收 ID |

**为什么搜索和抓取分开两次 gather**:
搜索返回的 URL 需要先全局去重（不同 query 可能返回同一 URL），再去抓取。先收集 → 去重 → 再并发抓取，避免浪费带宽重复下载。

**chunk_ids 的设计用途**:
Collector 写入 DB 后 SELECT 回收 chunk_ids，存入 result dict。当前 Analyzer 不消费它（直接用 task_id 检索），但保留它用于：(1) 日志可观测性（监控每个任务采集了多少 chunk）；(2) 未来 Supervisor 编排（进度反馈，如"飞书已采集 15 个片段"）。

---

### 2.3 `analyzer.py` — RAG 检索 + 多维分析

**公开方法**: `analyzer_agent(task, mcp_server, llm) -> dict`

**1 个内部函数**: `_retrieve_and_analyze_dimension()`

#### 内部函数: `_retrieve_and_analyze_dimension(task_id, competitors, dimension, chunk_dao, llm, settings) -> dict[str, str]`

**功能**: 单个维度的 RAG 检索 + LLM 分析。被主函数并行调用。

**六大步骤**:

| 步骤 | 操作 | 关键数据 |
|------|------|---------|
| 1: 维度嵌入 | `embed_query(dimension, settings)` → 1024 维向量 | BGE-M3 bi-encoder |
| 2: 向量检索 | `chunk_dao.similarity_search(task_id, query_vec, top_k=30)` | pgvector HNSW，`<=>` 余弦距离 |
| 3: 关键词检索 | `chunk_dao.keyword_search(task_id, dimension, top_k=20)` | zhparser 分词 + pg_bigm 2-gram |
| 4: 合并去重 | 向量结果 + 关键词结果 → URL 去重（取并集） | Python `set()` |
| 5: Cross-encoder 精排 | `rerank(dimension, documents, top_k=15)` | BGE-reranker-v2-m3，从候选中选出最相关 15 个 |
| 6: LLM 分析 | 拼接 prompt + LLM 生成 → JSON 解析 | deepseek-v4-flash，t=0.1 |

**步骤 4 为什么按 URL 而不是 chunk 文本去重**:
同一页面（如 `feishu.cn/pricing`）可能切出 5 个 chunk，如果只按文本去重，会得到 5 个相似的 chunk 进入精排——浪费 LLM token。按 URL 去重，同一页面只出 1 个候选。

**步骤 5 Cross-encoder 与 Bi-encoder 的核心区别**:

| 维度 | Bi-encoder（步骤1-2） | Cross-encoder（步骤5） |
|------|----------------------|----------------------|
| 编码方式 | query 和 doc 独立编码 | `[query, doc]` 联合编码 |
| 相似度 | `cos(q_vec, d_vec)` | 模型直接输出分数 |
| 速度 | O(1) per doc（快） | O(N) per doc（慢） |
| 精度 | 粗排（召回） | 精排（精确排序） |
| 用途 | 从 10000 chunk 召 Top30 | 从 30-50 候选精排 Top15 |

**为什么不用 Cross-encoder 全量检索**: 10000 个 chunk × 每个一次模型调用 → 太慢。先 Bi-encoder 粗排（一次 batch inference 算完所有余弦相似度），再 Cross-encoder 精排前 30-50 候选。

#### 主函数: `analyzer_agent(task, mcp_server, llm) -> dict`

**功能**: 对每个维度并行执行 RAG 检索 + LLM 分析，返回 `{维度名: {竞品名: "分析结论"}}`。

**实现逻辑**:
1. 遍历 dimensions，对每个 dim 构造 `_retrieve_and_analyze_dimension` 协程
2. `asyncio.gather(*tasks, return_exceptions=True)` 并行执行所有维度
3. 后处理：三种失败分级
   - `isinstance(r, Exception)` → 未捕获异常 → `"[分析失败] {异常信息}"`
   - `"error" in r` → 内部标记错误 → `"[分析失败] {原因}"`
   - 正常 dict → 直接使用

**为什么 `return_exceptions=True`**: 单维失败不阻塞其他维度。假设 5 个维度中"定价策略"的检索超时了，其余 4 个维度照常返回——报告不会全丢，只会标记"定价策略: [分析失败] 检索超时"。

---

### 2.4 `writer.py` — 报告撰写

**公开方法**: `writer_agent(task, mcp_server, llm) -> dict`

**功能**: 将分析结果 dict 转化为结构化 Markdown 报告。**纯 LLM Agent，零工具调用。**

**实现逻辑（3 步）**:
1. **格式化输入**: `json.dumps(analysis_results, ensure_ascii=False, indent=2)` — 中文原样输出，带缩进方便 LLM 理解
2. **拼接改写建议**: 如果 task 中有 `rewrite_suggestions`（Quality 不通过时注入），拼接到 prompt 的占位中。初稿和改写用同一套 prompt，免维护两套模板
3. **LLM 生成**: `llm.ainvoke(prompt)` → 返回 Markdown 文本

**报告结构（由 prompt 约束）**:
```
# 报告标题
## 概述 (1-2段)
## 竞品对比总览 (表格，行=维度，列=竞品)
## 逐维度深度分析
## 关键发现 (3-5条)
## 风险与建议
```

**为什么 Writer 不调工具**: 它的输入是 Analyzer 已经分析好的结构化结果，工作是"翻译"成排版整洁的 Markdown。不需要上网查新数据，不需要做检索。如果 Writer 调了工具，反而说明前面的 Agent 完成度不够——架构 bug。

---

### 2.5 `quality.py` — 质量门禁

**公开方法**: `quality_agent(task, mcp_server, llm) -> dict`

**功能**: LLM-as-Judge 五维评分 + 写入 reports 表。**纯 LLM Agent。**

**实现逻辑（5 步）**:
1. **构造 prompt**: 将报告文本 + 五维评分标准（完整性/准确性/可追溯性/可读性/客观性）注入 prompt
2. **LLM 评分**: `llm.ainvoke(prompt)`，temperature=0.0（评分必须可复现）
3. **防御性解析**: 去除可能的 markdown 代码块包裹 → `json.loads()`
4. **代码重算加权分**: 遍历 `_WEIGHTS = {完整性:0.30, 准确性:0.30, 可追溯性:0.20, 可读性:0.10, 客观性:0.10}`，计算 `sum(各维度score × weight)`，**覆盖 LLM 声明的 overall_score**
5. **写入 reports 表**: `report_dao.create(task_id, content, quality_score, quality_details)`

**输出结构**:
```json
{
  "overall_score": 82.5,
  "passed": true,
  "dimensions": {"完整性": {"score": 85, "comment": "..."}, ...},
  "issues": ["企业微信定价缺少source_url"],
  "rewrite_suggestions": []
}
```

**为什么权重用代码算而不是 LLM 算**: LLM 不擅长算术。prompt 里写"按完整性×0.3 + 准确性×0.3 + ..."，但 LLM 经常给个大约的分数而非精确计算。所以代码重新算——取 LLM 的判断力，取代码的计算力。

**为什么 `overall_score >= 70`**:
- 五维等权重，每维 60 分及格 → 下限 60
- 5 × 60 = 300 → 平均 60，70 在 60 之上有 10 分质量缓冲区
- 太低（<60）→ 太宽松；太高（>80）→ 频繁 rewrite 浪费 LLM token
- 如果报告缺了 2-3 个 source_url，可追溯性（权重 20%）会跌到 50 左右，加权影响约 10 分，70 的阈值正好能抓住

**为什么在 Quality 写 reports 表而不是 Writer**: Writer 负责生成，Quality 负责评估 + 持久化。如果 Writer 不通过 → 重写 → 新报告 → Quality 覆盖旧记录。评分和报告在同一条记录里，方便查询"哪些任务通过了"。

---

## 三、核心设计决策（面试必问）

### 决策 1: 四种 Agent 类型的划分依据

| Agent | 类型 | 是否调工具 | 为什么 |
|-------|------|-----------|--------|
| Collector | Agent + Tool | 是 | 需要上网搜、抓取网页 |
| Analyzer | Agent + RAG | 是 | 需要从数据库检索，但不直接上网 |
| Writer | 纯 LLM | 否 | 输入已结构化，只需"翻译"成 Markdown |
| Quality | 纯 LLM | 否 | 读报告打分，不需要任何外部数据 |

**面试时可说**: 按"是否需要外部数据"划分 Agent 类型——需要上网的是 Agent+Tool，需要检索的是 Agent+RAG，输入已完备的用纯 LLM。

### 决策 2: Collector 为什么分两次 gather（搜索→去重→抓取）

搜索和抓取分开的原因:
- 不同 query 可能返回相同 URL → 需要先去重再抓取
- 如果不先去重，会重复下载同一个页面，浪费带宽 + 被目标站点视为爬虫攻击
- 工作流: 搜索(全部 query) → URL 去重 → 抓取(唯一 URL)

### 决策 3: Analyzer 为什么按维度遍历而非按竞品遍历

分析时外层循环是 `for dim in dimensions`，不是 `for competitor in competitors`。原因:
- 每个维度独立检索（"定价策略"的向量空间 vs "功能对比"的向量空间完全不同）
- 每个维度一次 LLM 调用分析该维度下所有竞品
- 不是笛卡尔积（N 竞品 × M 维度），而是 M 次 LLM 调用

### 决策 4: 温度的四级差异化

| Agent | 温度 | 理由 |
|-------|------|------|
| Collector | 0.3 | 搜索关键词需要多样性，覆盖不同角度 |
| Analyzer | 0.1 | 分析结论需要稳定可复现 |
| Writer | 0.3 | 报告措辞允许变化，但结构由 prompt 约束 |
| Quality | 0.0 | 评分必须可复现，重复调用应得相同分数 |

### 决策 5: Quality 不通过怎么回退

```
quality → passed=false → Supervisor 拿到 rewrite_suggestions
  → 把 rewrite_suggestions 塞进 task
  → writer_agent(task=含改写建议) → 生成修改后的报告
  → quality_agent(新报告) → 重新评分
  → 最多 2 次改写后强制终止（由 Pipeline 层的 remaining_steps 控制）
```

Writer 本身不需要知道"这是第几次改写"——它只需要处理 `rewrite_suggestions` 这个可选字段即可。

---

## 四、完整链路时序

```
用户创建 task {title, competitors: [C1,C2,...], dimensions: [D1,D2,...]}
  │
  ├── Collector: collector_agent(task, mcp, llm_t0.3)
  │     ├── _generate_keywords(每竞品) → [query1, query2, ...]
  │     ├── asyncio.gather(搜索全部query) → 去重 URL
  │     ├── asyncio.gather(抓取 ≤5 URL/竞品) → pages
  │     ├── _chunk_text(每页) → chunks
  │     ├── embed_texts(批量, batch=12) → 1024-dim 向量
  │     └── batch_insert + SELECT ID 回收 → {竞品: {chunk_ids, pages}}
  │
  ├── Analyzer: analyzer_agent(task, mcp, llm_t0.1)
  │     ├── asyncio.gather(每个维度) :
  │     │     └── _retrieve_and_analyze_dimension(dim):
  │     │           ├── embed_query → query_vec
  │     │           ├── similarity_search(向量, top30)
  │     │           ├── keyword_search(关键词, top20)
  │     │           ├── URL 去重合并
  │     │           ├── rerank(Cross-encoder, top15)
  │     │           └── llm.ainvoke(prompt) → JSON parse
  │     └── 后处理异常分级 → {维度: {竞品: "结论"}}
  │
  ├── Writer: writer_agent(task, mcp, llm_t0.3)
  │     ├── json.dumps(analysis_results, indent=2)
  │     ├── 拼接 rewrite_suggestions（如有）
  │     └── llm.ainvoke(prompt) → Markdown 报告
  │
  └── Quality: quality_agent(task, mcp, llm_t0.0)
        ├── llm.ainvoke(prompt) → 五维评分 JSON
        ├── 代码重算 weighted_sum → overall_score
        ├── passed = score >= 70
        └── report_dao.create() 写入 reports 表
```

---

## 五、面试快速答题模板（2 分钟版）

> 问：你们项目的 Agent 层是怎么设计的？

**答**: 分了四个专精 Agent，按"是否需要外部数据"分类：需要上网的是 Collector、需要检索的是 Analyzer、数据已完备用纯 LLM 的是 Writer 和 Quality。

**Collector** 负责数据采集，六步流水线：LLM 生成搜索词 → 并发搜索 → 去重后并发抓取 → tiktoken 按 token 分块（800-1200 tokens）→ BGE-M3 批量嵌入 → PostgreSQL 批量写入。

**Analyzer** 负责推理分析，每个维度走六步 RAG 检索：BGE-M3 嵌入维度文本 → pgvector HNSW 向量检索 top30 → zhparser 关键词检索 top20 → URL 去重合并 → Cross-encoder 精排 top15 → LLM 生成分析结论。所有维度用 `asyncio.gather(return_exceptions=True)` 并行执行，单维失败不阻塞其他维度。

**Writer** 纯 LLM，把分析结果 dict 翻译成 Markdown 报告。**Quality** 纯 LLM，用 LLM-as-Judge 五维评分（完整性 30%、准确性 30%、可追溯性 20%、可读性 10%、客观性 10%），关键点是权重用代码重算，不信任 LLM 的算术能力。

温度策略：Collector 0.3（搜索需要多样性）、Analyzer 0.1（结论要稳定）、Writer 0.3（措辞可变化）、Quality 0.0（评分必须可复现）。

Quality 不通过时，`rewrite_suggestions` 回传给 Writer 重写，最多 2 次改写。

---

## 六、面试官追问：现场作答手册

### 追问 1: "为什么 tiktoken 分块 800-1200 而不是其他值？"

**快速答**: 三个约束。一是检索精度——太小丢上下文，太大检索噪音重。800-1200 是实践甜点区，够读完一个段落。二是 BGE-M3 输入上限 8192 tokens，保持在 1200 以内保证 embedding 质量。三是 Analyzer 的 LLM 上下文——5 维度 × 15 个 chunk × 1200 tokens ≈ 90K tokens，加上 prompt 在 128K 窗口内安全。

### 追问 2: "向量检索和关键词检索有什么区别？为什么要两个都用？"

**快速答**: 互补关系。向量检索找语义相似——搜"定价策略"能找到讨论"按功能模块收费"的段落，即使不含"定价"二字。关键词检索找字面匹配——精确匹配"¥180"、"SaaS 订阅"等具体数字术语。向量覆盖语义缺口，关键词覆盖精确匹配，取并集 URL 去重后送给 Cross-encoder 精排。

### 追问 3: "Cross-encoder 和 Bi-encoder 的本质区别是什么？"

**快速答**: Bi-encoder 是 query 和 doc 各自独立编码为向量，再算余弦相似度——速度快但精度低，适合大规模粗排。Cross-encoder 是把 `[query, doc]` 拼接为输入对，通过 self-attention 让 query 的每个 token 看到 doc 的每个 token——精度高但每对都要跑一次模型，适合精排少量候选。所以做法是：Bi-encoder 从 10000 chunk 召回 Top30 → Cross-encoder 精排 Top15 → LLM 分析。

### 追问 4: "如果 LLM 输出的 JSON 格式不对怎么办？"

**快速答**: 三层防御。第一层：prompt 里强调"只输出 JSON"、给正例反例。第二层：代码里预处理——去掉 ` ```json ``` ` 等 markdown 包裹。第三层：`json.loads()` 失败时在上层 catch，Collector 降级为模板拼接兜底，Analyzer 返回 `[分析失败]` 标记而非抛异常阻塞流程。

### 追问 5: "Quality 的 overall_score 为什么用代码重算而不是直接用 LLM 的？"

**快速答**: LLM 不擅长算术。prompt 里写"按完整性×0.3+准确性×0.3+..."，但 LLM 给的是"大约"的分数而非精确加权和。所以代码执行精确计算——取 LLM 的判断力（各维度评分），取代码的计算力（加权和），architecture 上叫"计算与推理分离"。

### 追问 6: "Collector 的 chunk_ids 字段 Analyzer 不用，为什么还存？"

**快速答**: 两条用途。一是可观测性——Agent 日志记录了每个任务采集的 chunk 总数，Supervisor 可以直接监控，不用再查 DB。二是未来编排——比如进度反馈"飞书已采集 15 个片段，钉钉 12 个"，需要知道每个竞品的 chunk 数量。当前 Analyzer 用 `task_id` 直接从 DB 检索，不依赖 chunk_ids。

### 追问 7: "如果 Collector 的 LLM 关键词生成失败了，搜索会停吗？"

**快速答**: 不会。代码里有 try/except + 降级策略。LLM 失败或返回格式不对 → 降级为模板拼接 `["飞书 定价策略", "飞书 功能对比", ...]`。这是最低保证——每个维度至少有一条 query，不会出现零搜索。设计理念是"LLM 提升质量上限，模板保证可用下限"。

### 追问 8: "Writer 和 Quality 都不调工具，为什么还叫 Agent？"

**快速答**: Agent 的定义是"能自主完成特定任务的 AI 单元"，不一定要调工具。Writer 和 Quality 是纯 LLM Agent——输入结构化数据，输出格式化文本，不需要任何外部工具。如果硬给 Writer 加 web_search，反而是架构 bug——说明前面的 Collector/Analyzer 完成度不够，Writer 需要自己补数据。这在面试时是一个加分回答——能说清楚"什么时候不该加工具"比"什么时候该加工具"更能体现架构判断力。

---

## 七、与 Phase 4 Pipeline 的接口约定

Phase 3 的四个 Agent 共享统一签名，这是它们被 Phase 4 Pipeline 串起来的基础：

```python
# 所有 Agent 的入口签名
async def xxx_agent(task: dict, mcp_server: MCPServer, llm: ChatDeepSeek) -> dict:
```

| Agent | 消费的 task key | 产出的 state key（Phase 4 写入 AgentState） |
|-------|----------------|------------------------------------------|
| Collector | id, title, competitors, dimensions | collected_data |
| Analyzer | id, title, competitors, dimensions | analysis_results |
| Writer | analysis_results, rewrite_suggestions | report_content, report_version |
| Quality | report_content | quality_score, quality_passed, rewrite_suggestions |

Phase 4 的 `graph.py` 通过闭包工厂函数 `_make_node_xxx()` 将每个 Agent 包装为 LangGraph 节点，节点返回的 dict 被 LangGraph 自动 merge 入 AgentState。

---

## 八、验收结果

| 验收项 | 结果 |
|--------|------|
| 4 个 Agent 统一签名 | ✅ `async def xxx_agent(task, mcp, llm) -> dict` |
| Collector 6 步流水线 | ✅ 生成→搜索→抓取→分块→嵌入→存储 |
| Analyzer RAG 6 步全链路 | ✅ 嵌入→向量检索→关键词检索→去重→精排→分析 |
| Writer 纯 LLM | ✅ 零工具调用，支持 rewrite_suggestions |
| Quality 代码重算 | ✅ weighted_sum 覆盖 LLM 的 overall_score |
| 温度差异化 | ✅ 0.3/0.1/0.3/0.0 |
| LLM 输出防御 | ✅ markdown 代码块清理 + JSON parse 容错 |
| 降级策略 | ✅ Collector 关键词 LLM→模板，Analyzer 异常→标记 |
| API key 统一入口 | ✅ create_llm_client 工厂 |
| 批量操作 | ✅ batch_insert + embed batch_size=12 |
| **总评** | **✅ 10/10 通过** |
