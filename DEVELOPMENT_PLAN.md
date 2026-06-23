# AI驱动的竞品分析多Agent协作系统 — 阶段开发计划

> **状态**: Phase 0/1/2/3/4/4.5 ✅ 完成 → Phase 5A 待开发  
> **日期**: 2026-06-14  
> **最后更新**: 2026-06-22（Phase 4.5 验收通过）  
> **开发方式**: Codex (ACP Harness) 逐阶段执行  
> **项目路径**: `D:\AAAagent\projects\competitive-analysis-system\`

---

## 项目总览

### 系统架构（已完成理论设计）

```
                         请求进入
                            │
                   ┌────────▼────────┐
                   │  IntentRouter   │  ← LLM实体提取 → 代码路由决策
                   │  (代码路由)      │
                   └───┬────────┬────┘
                       │        │
           竞品列表明确  │        │  开放性探索
           (80% 流量)   │        │  (20% 流量)
                       │        │
              ┌────────▼───┐ ┌──▼──────────────┐
              │  Pipeline  │ │ Supervisor+ReAct │
              │            │ │                  │
              │ Collector  │ │ _think()→_act()  │
              │   ↓        │ │ →_observe()      │
              │ Analyzer   │ │                  │
              │   ↓        │ │ A2A 通信 +       │
              │ Writer     │ │ MCP 工具 +       │
              │   ↓        │ │ Harness 安全     │
              │ Quality    │ │                  │
              │   ↓        │ │ MAX_ROUNDS=10    │
              │ score≥70?  │ │                  │
              │ YES→完成    │ │                  │
              │ NO→回退     │ │                  │
              └────────────┘ └──────────────────┘
```

### 三层架构

| 层 | 职责 | 技术选型 |
|----|------|---------|
| MCP (工具层) | Agent 能力工具箱 | web_search, web_fetch, embed, rerank |
| A2A (通信层) | Agent 间通信协议 | AgentCard + Task 生命周期 |
| Harness (安全壳) | 五层安全检查 | 白名单/参数校验/频控/PII阻断/审计 |

### 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| 编排框架 | LangGraph 1.0 | StateGraph + Checkpoint |
| 数据库 | PostgreSQL + pgvector | 一体化存储 |
| 嵌入模型 | BGE-M3 | 1024维，~2GB懒加载 |
| 精排模型 | BGE-reranker-v2-m3 | 检索后精排 |
| LLM | DeepSeek Chat API | agent-study-key1 |
| Web 框架 | FastAPI + SSE | 异步服务 |
| 异步驱动 | asyncpg, httpx | 全链路异步 |
| 并行 | asyncio | Python 原生协程 |
| 程序入口 | Python 3.13 | `D:\AAAProgram Files\Python\python\python.exe` |

### 目录结构（目标）

```
D:\AAAagent\projects\competitive-analysis-system\
├── pyproject.toml          # 依赖管理
├── .env.example            # 环境变量模板
├── README.md               # 项目说明
│
├── src/
│   ├── __init__.py
│   ├── config.py           # 配置管理（环境变量 + pydantic）
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.sql      # PostgreSQL DDL
│   │   ├── connection.py   # asyncpg 连接池
│   │   └── dao.py          # 数据访问层
│   │
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── server.py       # MCP Server（tools/list + tools/call）
│   │   ├── tools_web.py    # web_search, web_fetch
│   │   └── tools_rag.py    # embed, rerank
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── collector.py    # 采集 Agent
│   │   ├── analyzer.py     # 分析 Agent（RAG + 五维分析）
│   │   ├── writer.py       # 撰写 Agent
│   │   ├── quality.py      # 质检 Agent（LLM-as-Judge）
│   │   └── prompts/        # System Prompt 文件
│   │       ├── collector.md
│   │       ├── analyzer.md
│   │       ├── writer.md
│   │       └── quality.md
│   │
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── state.py        # AgentState TypedDict
│   │   ├── graph.py        # LangGraph Pipeline 图
│   │   └── checkpoint.py   # PostgreSQL Checkpoint
│   │
│   ├── supervisor/
│   │   ├── __init__.py
│   │   ├── a2a.py          # A2A 协议（AgentCard + Task）
│   │   ├── router.py       # IntentRouter 路由决策
│   │   ├── supervisor.py   # Supervisor ReAct 循环
│   │   └── state.py        # SupervisorState
│   │
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── summarizer.py   # Summary memory (incremental + full merge every 10 rounds)
│   │   ├── long_term.py    # Long-term retrieval engine (5-step pipeline + time decay + importance)
│   │   ├── retrieval.py    # Retrieval strategy (hybrid trigger + 2-stage coarse60->fine10)
│   │   ├── conflict.py     # Conflict resolution (3-level: time/authority/keep-both)
│   │   └── forgetting.py   # Forgetting strategy (natural decay + 180d archive + soft delete)
│   │
│   ├── harness/
│   │   ├── __init__.py
│   │   ├── guard.py        # 五层检查中间件
│   │   └── audit.py        # 审计日志
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py       # FastAPI 路由
│   │   ├── sse.py          # SSE 推流
│   │   └── rate_limit.py   # 三层限流
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── logging.py      # 结构化日志
│   │   └── metrics.py      # Prometheus 指标
│   │
│   └── evaluation/
│       ├── __init__.py
│       ├── golden_dataset.py   # Golden Dataset
│       ├── ragas_eval.py       # RAGAS 评估
│       └── judge_eval.py       # LLM-as-Judge 评估
│
└── tests/
    ├── __init__.py
    ├── test_mcp.py
    ├── test_agents.py
    ├── test_pipeline.py
    ├── test_supervisor.py
    ├── test_harness.py
    ├── test_api.py
    └── test_e2e.py
```

---

## 阶段划分总览

| 阶段 | 名称 | Codex 任务数 | 预估工期 | 前置依赖 |
|------|------|-------------|---------|---------|
| Phase 0 | 项目脚手架 + 配置 | 1 | 1 会话 | 无 |
| Phase 1 | 数据库 Schema + DAO | 1 | 1 会话 | Phase 0 |
| Phase 2 | MCP 工具层 | 1 | 1~2 会话 | Phase 0 |
| Phase 3 | Agent 实现 | 1 | 1~2 会话 | Phase 1, Phase 2 |
| Phase 4 | Pipeline 编排 | 1 | 1~2 会话 | Phase 3 |
| Phase 4.5 | 记忆系统（Checkpoint+摘要+长期记忆+冲突/遗忘） | 1 | 1~2 会话 | Phase 4 |
| Phase 5A | Supervisor + A2A 通信协议 | 1 | 1~2 会话 | Phase 3 |
| Phase 5B | IntentRouter + Harness Engineering | 1 | 1 会话 | Phase 4, Phase 5A |
| Phase 6 | 服务化 + 可观测性 | 1 | 1~2 会话 | Phase 4, Phase 5B |
| Phase 7 | 评估体系 + 集成测试 | 1 | 1~2 会话 | Phase 6 |

> **总计**: 10 个 Codex 任务，预估 10~17 个 Codex 会话  
> **原则**: 每个阶段完成后验证再进入下一阶段，不跨阶段并行

---

## Phase 0：项目脚手架 + 配置

### 目标
创建项目目录结构、依赖配置、环境变量管理、数据库连接池基础代码。

### 前置条件
- 无。`D:\AAAagent\projects\competitive-analysis-system\` 目录尚不存在。

### 交付物
1. `pyproject.toml` — 完整依赖声明
2. `.env.example` — 环境变量模板
3. `src/__init__.py`
4. `src/config.py` — pydantic-settings 配置类
5. `src/db/connection.py` — asyncpg 连接池
6. `README.md` — 项目说明

### 提示词 (Phase 0)

`	ext
## 任务：创建竞品分析多Agent协作系统 — 项目脚手架

### 背景
我要开发一个 AI 驱动的竞品分析系统，架构是 LLM实体提取 + 代码路由双模：
- 80% Pipeline（Collector→Analyzer→Writer→Quality + 质检回退循环）
- 20% Supervisor+ReAct（LLM动态决策 + A2A通信 + MCP工具 + Harness安全壳）

技术栈：LangGraph 1.0 + FastAPI + PostgreSQL/pgvector + BGE-M3 + DeepSeek API

### 你的任务
创建项目根目录和基础脚手架代码。

### 具体要求

#### 1. 项目路径
`D:\AAAagent\projects\competitive-analysis-system\`

#### 2. pyproject.toml
生成一个完整的 pyproject.toml，包含以下依赖：
- langgraph >= 1.0.0
- langchain >= 0.3.0
- langchain_deepseek
- fastapi
- uvicorn[standard]
- asyncpg
- pgvector (asyncpg 扩展)
- pydantic >= 2.0
- pydantic-settings
- httpx
- sse-starlette
- prometheus-client
- tiktoken
- FlagEmbedding (BGE-M3)
- python-dotenv

Python 版本要求：>=3.11
项目名：competitive-analysis-system

#### 3. .env.example
```
# LLM
DEEPSEEK_API_KEY=your_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# Database
PG_HOST=localhost
PG_PORT=5432
PG_DATABASE=competitive_analysis
PG_USER=postgres
PG_PASSWORD=your_password

# Embedding
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DEVICE=cpu

# Reranker
RERANKER_MODEL=BAAI/bge-reranker-v2-m3

# Limits
MAX_CONCURRENT_COLLECTORS=3
MAX_ROUNDS_SUPERVISOR=10
TOKEN_BUCKET_CAPACITY=100
```

#### 4. src/config.py
用 pydantic-settings 读取环境变量，定义 Settings 类，包含：
- deepseek_api_key, deepseek_base_url, deepseek_model
- pg_host, pg_port, pg_database, pg_user, pg_password
- embedding_model, embedding_device
- reranker_model
- max_concurrent_collectors (int, default 3)
- max_rounds_supervisor (int, default 10)
- token_bucket_capacity (int, default 100)
- database_url (computed property: 拼接 PostgreSQL 连接字符串)
- 所有字段有合理的默认值

#### 5. src/db/connection.py
基于 asyncpg 实现数据库连接池：
- create_pool() 函数：从 Settings 读取配置创建 asyncpg Pool
- get_pool() 函数：返回全局连接池单例
- close_pool() 函数：关闭连接池
- 注意：使用 asyncpg 的 `async with` 模式
- 使用 `asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=10)`

#### 6. README.md
简洁的项目说明：项目名称、一句话定位、技术栈列表、快速启动步骤（3步）

```


`

### 验收标准
1. pyproject.toml 可以直接 `pip install -e .` 安装（依赖解析通过即可，不需要实际安装）
2. config.py 可以单独 import 且正确读取 .env
3. connection.py 的 Pool 创建逻辑正确（不需要实际连接数据库）
4. .env.example 包含所有必要字段且有注释

### 注意事项
- Python 路径：`D:\AAAProgram Files\Python\python\python.exe`
- 所有文件编码 UTF-8
- 不要安装依赖或创建虚拟环境——只写代码
- 代码风格：type hints 全覆盖，每个函数有 docstring
```

---

## Phase 1：数据库 Schema + DAO

### 目标
创建 PostgreSQL + pgvector 表结构、数据库初始化脚本、数据访问层（DAO）。

### 前置条件
- Phase 0 已完成
- PostgreSQL 已安装并运行（用户自行确认）

### 交付物
1. `src/db/schema.sql` — 完整 DDL
2. `src/db/dao.py` — asyncpg 数据访问层
3. 更新 `src/db/__init__.py`

### 提示词 (Phase 1)

`	ext
## 任务：竞品分析系统 — 数据库 Schema 与 DAO 层

### 背景
项目已创建在 `D:\AAAagent\projects\competitive-analysis-system\`，已包含配置（src/config.py）和数据库连接池（src/db/connection.py）。

### 你的任务
创建数据库表结构和数据访问层。

### 具体要求

#### 1. src/db/schema.sql —— 完整 DDL
创建以下表（使用 IF NOT EXISTS）：

**tasks 表**（分析任务）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- title: VARCHAR(500) NOT NULL — 任务名称，如"协同办公软件竞品分析"
- competitors: JSONB NOT NULL — 竞品列表，如 ["飞书","钉钉","Notion"]
- dimensions: JSONB NOT NULL — 分析维度，如 ["功能","定价","市场"]
- status: VARCHAR(20) DEFAULT 'pending' — pending/running/completed/failed
- pipeline_mode: VARCHAR(20) DEFAULT 'pipeline' — pipeline 或 supervisor
- created_at: TIMESTAMPTZ DEFAULT NOW()
- updated_at: TIMESTAMPTZ DEFAULT NOW()

**reports 表**（分析报告）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- task_id: UUID REFERENCES tasks(id)
- content: TEXT — Markdown 格式报告内容
- quality_score: FLOAT — 质检分数 0-100
- quality_details: JSONB — 五维评分详情
- version: INT DEFAULT 1 — 重写版本号
- created_at: TIMESTAMPTZ DEFAULT NOW()

**evidence_map 表**（证据追溯）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- report_id: UUID REFERENCES reports(id)
- claim: TEXT NOT NULL — 分析结论
- source_url: TEXT NOT NULL — 来源 URL
- source_text: TEXT — 引用的原文片段
- dimension: VARCHAR(50) — 所属维度

**chunk_embeddings 表**（向量存储，需要 pgvector 扩展）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- task_id: UUID REFERENCES tasks(id)
- chunk_text: TEXT NOT NULL — 文档分块原文
- chunk_index: INT NOT NULL — 分块序号
- source_url: TEXT NOT NULL — 来源 URL
- embedding: vector(1024) — BGE-M3 1024维嵌入向量
- created_at: TIMESTAMPTZ DEFAULT NOW()

**agent_logs 表**（审计日志）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- task_id: UUID REFERENCES tasks(id)
- agent_name: VARCHAR(50) NOT NULL — collector/analyzer/writer/quality/supervisor
- action: VARCHAR(100) NOT NULL — 操作名称
- request: JSONB — 请求内容
- response: JSONB — 响应内容
- error: TEXT — 错误信息（如有）
- duration_ms: FLOAT — 耗时（毫秒）
- created_at: TIMESTAMPTZ DEFAULT NOW()

同时创建以下记忆系统专属表：

**memory_summaries 表**（摘要记忆——跨轮对话压缩缓存）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- task_id: UUID REFERENCES tasks(id)
- round_range: VARCHAR(50) — 如 "1-10"
- summary_text: TEXT NOT NULL — LLM 生成的摘要
- summary_type: VARCHAR(20) DEFAULT 'incremental' — incremental 或 full_merge
- created_at: TIMESTAMPTZ DEFAULT NOW()

**agent_memories 表**（长期记忆——跨会话知识库）：
- id: UUID PRIMARY KEY DEFAULT gen_random_uuid()
- user_id: VARCHAR(100) NOT NULL — 用户标识
- memory_type: VARCHAR(30) NOT NULL — decision/preference/fact/chat
- content: TEXT NOT NULL — 记忆正文
- importance: FLOAT DEFAULT 0.5 — 重要性评分 0.0-1.0（决策0.9/偏好0.7/事实0.5/闲聊0.1）
- embedding: vector(1024) — BGE-M3 嵌入向量
- source_task_id: UUID — 来源任务
- access_count: INT DEFAULT 0 — 被检索次数
- half_life_days: INT DEFAULT 30 — 半衰期（天）
- is_active: BOOLEAN DEFAULT true — 软删除标记
- created_at: TIMESTAMPTZ DEFAULT NOW()
- last_accessed: TIMESTAMPTZ

同时创建索引：
- tasks: status, created_at
- reports: task_id, quality_score
- evidence_map: report_id, dimension
- chunk_embeddings: task_id, embedding vector_cosine_ops (IVFFlat 或 HNSW)
- agent_memories: user_id, memory_type, is_active, embedding vector_cosine_ops (IVFFlat 或 HNSW)
- agent_logs: task_id, agent_name, created_at

使用 `CREATE EXTENSION IF NOT EXISTS vector;` 和 `CREATE EXTENSION IF NOT EXISTS "uuid-ossp";`

#### 2. src/db/dao.py —— 数据访问层
使用 asyncpg 实现以下类和方法：

**class TaskDAO**:
- async create(task_id, title, competitors, dimensions, pipeline_mode) -> UUID
- async get(task_id) -> dict | None
- async update_status(task_id, status) -> None

**class ReportDAO**:
- async create(task_id, content, quality_score, quality_details, version) -> UUID
- async get_latest(task_id) -> dict | None
- async get_all_versions(task_id) -> list[dict]
- async get_by_quality_threshold(task_id, min_score) -> list[dict]

**class EvidenceDAO**:
- async batch_insert(report_id, evidences: list[dict]) -> None
- async get_by_report(report_id) -> list[dict]
- async get_by_dimension(report_id, dimension) -> list[dict]

**class ChunkEmbeddingDAO**:
- async batch_insert(task_id, chunks: list[dict]) -> None
  (chunks 每个元素: {chunk_text, chunk_index, source_url, embedding: list[float]})
- async similarity_search(task_id, query_embedding: list[float], top_k: int = 10) -> list[dict]
  使用 pgvector 的 <=> 余弦距离操作符排序
- async keyword_search(task_id, keywords: str, top_k: int = 10) -> list[dict]
  使用 PostgreSQL tsvector/tsquery 全文检索

**class MemorySummaryDAO**:
- async save(task_id, round_range, summary_text, summary_type) -> UUID
- async get_latest(task_id) -> dict | None
- async get_by_round_range(task_id, round_range) -> list[dict]

**class AgentMemoryDAO**:
- async insert(user_id, memory_type, content, importance, embedding, source_task_id, half_life_days) -> UUID
- async similarity_search(user_id, query_embedding: list[float], top_k: int = 60) -> list[dict]
  使用 pgvector <=> 余弦距离 + 时间衰减加权排序：
  ORDER BY (1.0 - (embedding <=> $1)) * importance * POWER(0.5, EXTRACT(DAY FROM NOW() - created_at) / half_life_days) DESC
- async keyword_search(user_id, keywords: str, top_k: int = 30) -> list[dict]
- async soft_delete(memory_id: UUID) -> None  — 标记 is_active=false
- async archive_old(days: int = 180) -> int  — 归档超期记忆
- async get_conflicts(user_id, content: str, threshold: float = 0.85) -> list[dict]  — 语义相似度 >= 85% 视为冲突

**class AgentLogDAO**:
- async log(task_id, agent_name, action, request, response, error, duration_ms) -> None
- async get_by_task(task_id) -> list[dict]
- async get_recent_errors(task_id, limit=20) -> list[dict]

所有 DAO 方法：
- 接受 `pool: asyncpg.Pool` 作为第一个参数
- 使用 `async with pool.acquire() as conn:` 模式获取连接
- 返回 dict 使用 Record 的 dict() 方法
- 错误处理：记录异常但不吞掉，重新 raise

#### 3. src/db/__init__.py
导出 create_pool, get_pool, close_pool 以及所有 DAO 类。

```


`

### 验收标准
1. schema.sql 可以在 psql 中直接执行 `psql -f schema.sql` 创建所有表
2. DAO 方法签名正确，使用 async/await 模式
3. 向量搜索使用 pgvector 正确语法：`ORDER BY embedding <=> $1 LIMIT $2`
4. 全文检索使用 tsvector 正确语法：`to_tsvector('simple', chunk_text) @@ plainto_tsquery('simple', $1)`

### 注意事项
- 不要用 ORM（SQLAlchemy），直接用 asyncpg 原生 SQL
- 所有 SQL 用参数化查询（$1, $2...），防止注入
- embedding 字段写入时用 `$1::vector` 显式类型转换
```

---

## Phase 2：MCP 工具层

### 目标
实现 MCP Server（tools/list + tools/call 协议）、Web 搜索采集工具、向量嵌入与重排工具。

### 前置条件
- Phase 0 已完成（有 config.py 即可）

### 交付物
1. `src/mcp/server.py` — MCP 协议实现
2. `src/mcp/tools_web.py` — web_search, web_fetch
3. `src/mcp/tools_rag.py` — embed, rerank
4. `src/mcp/__init__.py`

### 提示词 (Phase 2)

`	ext
## 任务：竞品分析系统 — MCP 工具层

### 背景
项目在 `D:\AAAagent\projects\competitive-analysis-system\`，已有配置（src/config.py）和数据库连接池。

你需要实现 MCP (Model Context Protocol) 工具层——这是每个 Agent 的"能力工具箱"。

### MCP 协议说明
MCP 的核心是两个操作：
1. **tools/list**：声明可用工具及参数 schema
2. **tools/call**：执行指定工具并返回结果

N 个 Agent × M 个工具 → N+M 的关系（而非 N×M），因为工具集中管理、Agent 按需调用。

### 你的任务

#### 1. src/mcp/tools_web.py — Web 工具

### web_search(query: str, max_results: int = 10) -> list[dict]
- 使用 httpx 调用外部搜索 API
- 返回格式：[{title, url, snippet}, ...]
- 超时 15 秒，失败返回空列表不抛异常
- 如果 DeepSeek API 支持联网搜索，优先用它
- 否则实现一个基于 httpx 的通用搜索封装（接受 search_api_url 配置）

### web_fetch(url: str, max_chars: int = 10000) -> dict
- 使用 httpx 异步获取 URL 内容
- 返回格式：{url, title, text_content, status_code, error}
- 提取页面正文（去掉 HTML 标签、script、style）
- 用最简单的正则/字符串处理提取文本，不用 BeautifulSoup（减少依赖）
- 超时 20 秒，User-Agent 设为合理的浏览器标识
- 失败时 error 字段包含原因，text_content 为空字符串

#### 2. src/mcp/tools_rag.py — RAG 工具

### embed_texts(texts: list[str]) -> list[list[float]]
- 使用 FlagEmbedding 加载 BGE-M3 模型
- 懒加载：首次调用时加载模型到内存，后续复用
- 模型路径从 config 读取：settings.embedding_model
- GPU 不可用时自动退回到 CPU
- 返回 1024 维 float 列表的列表

### embed_query(query: str) -> list[float]
- 同上，单条查询嵌入
- 使用 BGE-M3 的 encode_queries 模式（如可用，否则用 encode）
- 返回 list[float]，1024 维

### rerank(query: str, documents: list[str], top_k: int = 10) -> list[dict]
- 使用 FlagEmbedding 加载 BGE-reranker-v2-m3
- 懒加载模式
- 返回格式：[{index, text, score}, ...] 按 score 降序
- top_k 参数控制返回数量

#### 3. src/mcp/server.py — MCP 服务器

实现 MCP 协议的 tools/list 和 tools/call：

```python
class MCPServer:
    """MCP Server 管理所有工具的注册和调用"""
    
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
    
    def register(self, name: str, description: str, 
                 parameters: dict, handler: callable):
        """注册一个工具"""
    
    async def list_tools(self) -> list[dict]:
        """返回所有已注册工具的 schema（tools/list 格式）"""
    
    async def call_tool(self, name: str, arguments: dict) -> dict:
        """执行工具调用（tools/call 格式）
        返回: {content: [...], isError: bool}
        """
```

注册的工具：
1. web_search — 搜索互联网
2. web_fetch — 抓取网页内容
3. embed_texts — 批量文本嵌入
4. embed_query — 单条查询嵌入
5. rerank — 搜索结果重排

每个工具的 parameters 按照 JSON Schema 格式定义（type, required, properties）。

**list_tools 返回格式示例**：
```python
{
    "tools": [
        {
            "name": "web_search",
            "description": "搜索互联网获取信息",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "最大返回数量", "default": 10}
                }
            }
        },
        # ... 其他工具
    ]
}
```

**call_tool 返回格式示例**：
```python
{
    "content": [
        {"type": "text", "text": json.dumps(results, ensure_ascii=False)}
    ],
    "isError": False
}
```

```


`

### 验收标准
1. MCPServer 可以正确注册 5 个工具
2. list_tools() 返回符合 MCP 规范的 JSON schema
3. call_tool("web_search", {"query": "飞书定价"}) 可以正确路由到 web_search
4. call_tool 对不存在的工具名返回 isError=True
5. embed_texts 第一次调用时加载模型，第二次不再加载（懒加载验证）
6. tools_web.py 中的 httpx 调用有完整的超时和错误处理

### 注意事项
- BGE-M3 模型 ~2GB，懒加载很重要——不要 import 时就加载
- 工具函数签名统一为 async def handler(arguments: dict) -> dict
- 所有工具调用失败时返回 {"error": "..."} 而非抛异常
- 代码注释：每个工具函数注明输入输出格式
```

---

## Phase 3：Agent 实现

### 目标
实现四个专职 Agent：Collector（采集）、Analyzer（分析+ RAG）、Writer（撰写）、Quality（质检）。每个 Agent 通过 MCP Server 调用工具。

### 前置条件
- Phase 1 已完成（DAO）
- Phase 2 已完成（MCP Server + 工具）

### 交付物
1. `src/agents/prompts/collector.md` — Collector System Prompt
2. `src/agents/prompts/analyzer.md` — Analyzer System Prompt
3. `src/agents/prompts/writer.md` — Writer System Prompt
4. `src/agents/prompts/quality.md` — Quality Judge Prompt
5. `src/agents/collector.py`
6. `src/agents/analyzer.py`
7. `src/agents/writer.py`
8. `src/agents/quality.py`
9. `src/agents/__init__.py`

### 提示词 (Phase 3)

`	ext
## 任务：竞品分析系统 — 四个 Agent 实现

### 背景
项目在 `D:\AAAagent\projects\competitive-analysis-system\`，已有：
- 数据库 DAO（src/db/）
- MCP 工具层（src/mcp/）— web_search, web_fetch, embed, rerank

### 四个 Agent 概述

| Agent | 职责 | 输入 | 输出 | 工具 |
|-------|------|------|------|------|
| Collector | 并发采集竞品信息 | task(competitors, dimensions) | 采集结果 {competitor: [urls+text]} | web_search, web_fetch |
| Analyzer | RAG检索 + 五维分析 | task + 采集结果 | 分析结果 {dimension: analysis} | embed_query, rerank |
| Writer | 结构化报告撰写 | task + 分析结果 | Markdown 报告 | 无（纯 LLM） |
| Quality | 五维质检评分 | task + 报告 | {score, details, passed} | 无（LLM-as-Judge） |

### 核心设计决策
- 每个 Agent 是一个 async 函数，不是 LangGraph 节点（节点在 Phase 4 中定义）
- Agent 接收 MCP Server 实例通过依赖注入
- 所有 LLM 调用走 langchain_deepseek.ChatDeepSeek
- 每个维度独立 try/except fail-fast——一个维度超时不拖垮其他

### 你的任务

#### 1. Agent System Prompts（src/agents/prompts/）

用 Markdown 文件定义，每个 Prompt 包含：
- 角色定义
- 输入格式
- 输出格式（JSON schema）
- 高质量示例（正例）
- 反例及避免方法
- 边界约束

**collector.md**：
```
你是竞品信息采集专家。根据给定的竞品列表和分析维度，搜索并采集相关网页内容。

输入：{"competitors": [...], "dimensions": [...]}
输出：{"competitor_name": {"url": "...", "pages": [{"url": "...", "title": "...", "text": "..."}]}}

规则：
- 每个竞品搜索 3-5 个相关 URL
- 抓取完整页面内容但去噪（去导航、广告、脚本）
- 采集中断不崩溃，缺失的数据标注 [信息未获取]
- 不要编造数据
```

**analyzer.md**：
```
你是竞品多维度分析专家。基于采集的网页内容，从指定维度逐一分析每个竞品。

输入：{"task": {...}, "collected_data": {...}, "dimensions": [...]}
输出：{"dimension_name": {"飞书": "分析...", "钉钉": "分析..."}}

规则：
- 每个结论必须有来源 URL（可追溯）
- 不确定的信息标注 [待验证]
- 一个维度分析失败不影响其他维度（独立 fail-fast）
- 分析要具体，避免"功能强大"这种空话
```

**writer.md**：
```
你是技术报告撰写专家。将分析结果组装成结构化的 Markdown 竞品分析报告。

输入：{"task": {...}, "analysis_results": {...}}
输出：Markdown 格式的完整报告

报告结构：
# {task.title}
### 概述
### 竞品对比总览（表格）
### 逐维度深度分析
### 关键发现
### 风险与建议

规则：
- 只使用给定的分析结果，不添加未经验证的信息
- 数据缺失部分标注 [数据不足]
- 保持客观中立，不使用情绪化语言
- 表格至少包含 3 列：维度、飞书、钉钉...
```

**quality.md**：
```
你是报告质量评审专家。对竞品分析报告进行五维度打分。

输入：{"task": {...}, "report": "..."}
输出：{
  "overall_score": 0-100,
  "passed": true/false,
  "dimensions": {
    "完整性": {"score": 0-100, "comment": "..."},
    "准确性": {"score": 0-100, "comment": "..."},
    "可追溯性": {"score": 0-100, "comment": "..."},
    "可读性": {"score": 0-100, "comment": "..."},
    "客观性": {"score": 0-100, "comment": "..."}
  },
  "issues": ["问题1", "问题2"],
  "rewrite_suggestions": ["建议1", "建议2"]
}

评分标准：
- 完整性(30%): 所有指定维度是否覆盖？所有竞品是否涉及？
- 准确性(30%): 数据是否来源可查？是否有编造成分？
- 可追溯性(20%): 结论是否能追溯到具体 URL？
- 可读性(10%): Markdown 结构是否清晰？表格是否完整？
- 客观性(10%): 是否有明显的倾向性语言？

通过阈值：overall_score >= 70
不通过时给出具体的 rewrite_suggestions
```

#### 2. Agent 实现代码

每个 Agent 遵循统一模式：

```python
async def collector_agent(
    task: dict,
    mcp_server: MCPServer,
    llm: ChatDeepSeek
) -> dict:
    """
    采集竞品信息。
    
    Args:
        task: {id, title, competitors, dimensions}
        mcp_server: MCP 工具服务器
        llm: LLM 客户端
    
    Returns:
        {competitor_name: {url, pages: [{url, title, text}]}}
    """
```

**collector.py**：
1. 用 LLM 为每个竞品生成搜索关键词（结合 dimensions）
2. 调用 mcp_server.call_tool("web_search", ...)  搜索
3. 对搜索结果调用 mcp_server.call_tool("web_fetch", ...) 抓取页面
4. 使用 asyncio.gather() 并发采集（受 max_concurrent_collectors 限制）
5. 每个竞品独立 try/except——一个失败不影响其他
6. 结果写入 agent_logs 表

**analyzer.py**：
1. 对采集的文本做语义分块（800-1200 token/chunk，用 tiktoken 估算）
2. 调用 embed_texts 嵌入所有 chunk
3. 调用 ChunkEmbeddingDAO.batch_insert 保存
4. 对每个维度：
   a. 调用 embed_query(维度关键词) 生成查询向量
   b. 调用 ChunkEmbeddingDAO.similarity_search 向量检索（top_k=30）
   c. 调用 ChunkEmbeddingDAO.keyword_search 关键词检索（top_k=20）
   d. 合并去重后调用 rerank 精排（最终取 top_k=15）
   e. 用 LLM 分析该维度的所有竞品
5. 每个维度独立 try/except——失败标注 [分析失败]

**writer.py**：
1. 读取 analyzer 的分析结果
2. 用 LLM（带 system prompt）组装 Markdown 报告
3. 输出格式包含完整的报告结构
4. 如果有上一轮质量反馈（rewrite_suggestions），纳入改写

**quality.py**：
1. 读取报告内容
2. 用 LLM-as-Judge（带五维评分 prompt）打分
3. 解析 JSON 输出，提取 overall_score 和 details
4. 判断 passed: overall_score >= 70
5. 结果写入 reports 表

#### 3. src/agents/__init__.py
导出四个 agent 函数。

```


`

### 验收标准
1. 每个 Agent 的 System Prompt 包含正例+反例+边界约束
2. Agent 函数签名统一为 async def xxx_agent(task, mcp_server, llm) -> dict
3. Collector 使用 asyncio.gather 并发采集，有 Semaphore 限流
4. Analyzer 的 RAG 流程完整：分块→嵌入→向量检索→BM25检索→合并→重排→LLM分析
5. Analyzer 五个维度独立 fail-fast
6. Quality 正确计算 weighted score 并判断 passed

### 注意事项
- 每个 Agent 的输出必须能 JSON 序列化（方便后续 LangGraph 状态传递）
- LLM 调用使用 ChatDeepSeek(model=settings.deepseek_model)
- 温度设置：Collector=0.3, Analyzer=0.1, Writer=0.3, Quality=0.0（打分不要创意）
- API Key 从 settings.deepseek_api_key 读取
- 每个维度分析前检查是否有可用数据，数据不足直接标注不要强行编造
```

---

## Phase 4：Pipeline 编排

### 目标
用 LangGraph StateGraph 实现 Pipeline 主干编排，包含质检回退循环和 PostgreSQL Checkpoint 持久化。

### 前置条件
- Phase 3 已完成（四个 Agent 函数可用）

### 交付物
1. `src/pipeline/state.py` — AgentState TypedDict
2. `src/pipeline/graph.py` — LangGraph 图定义（Pipeline 主干）
3. `src/pipeline/checkpoint.py` — PostgreSQL Checkpoint 存储

### 提示词 (Phase 4)

`	ext
## 任务：竞品分析系统 — Pipeline 编排（LangGraph）

### 背景
四个 Agent 已完成（src/agents/），需要 LangGraph StateGraph 将其串联为 Pipeline。

### Pipeline 流程

```
Collector → Analyzer → Writer → Quality
                                ↓
                        score >= 70? ──YES──→ 完成
                                │
                               NO
                                │
                              Writer（重写，带 quality 反馈）
```

### 你的任务

#### 1. src/pipeline/state.py — 状态定义

用 TypedDict + Annotated 定义 AgentState：

```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    task_id: str
    title: str
    competitors: list[str]
    dimensions: list[str]
    pipeline_mode: str  # "pipeline"
    
    # 采集阶段
    collected_data: dict  # {competitor: {url, pages: [...]}}
    
    # 分析阶段
    analysis_results: dict  # {dimension: {competitor: "分析内容"}}
    
    # 撰写阶段
    report_content: str  # Markdown 报告
    report_version: int  # 重写版本号
    
    # 质检阶段
    quality_score: float
    quality_details: dict
    quality_passed: bool
    rewrite_suggestions: list[str]
    
    # 控制
    messages: Annotated[list, add_messages]
    remaining_steps: int  # 防止死循环
    final_report: str  # 最终输出
```

#### 2. src/pipeline/graph.py — 图定义

创建 LangGraph StateGraph，节点函数如下：

**node_collect(state: AgentState) -> dict**：
- 调用 collector_agent(task, mcp_server, llm)
- 写入 collected_data
- 写入 agent_logs

**node_analyze(state: AgentState) -> dict**：
- 调用 analyzer_agent(task, mcp_server, llm)
- 写入 analysis_results
- 写入 agent_logs

**node_write(state: AgentState) -> dict**：
- 调用 writer_agent(task, llm)
- 如果存在 rewrite_suggestions，传给 writer
- 写入 report_content, report_version += 1

**node_quality(state: AgentState) -> dict**：
- 调用 quality_agent(task, llm)
- 写入 quality_score, quality_details, quality_passed, rewrite_suggestions

**node_finalize(state: AgentState) -> dict**：
- 写入 final_report = report_content
- 更新 task status 为 completed

**路由函数**：

```python
def route_after_quality(state: AgentState) -> str:
    """条件边：质检通过 → finalize，不通过 → 回退 write"""
    if state["quality_passed"]:
        return "finalize"
    if state["remaining_steps"] <= 0:
        return "finalize"  # 强制结束
    return "write"
```

**图构建结构**：
```
collect → analyze → write → quality
                              ↓
                     route_after_quality
                       ↙        ↘
                   finalize     write (循环)
```

使用 `add_conditional_edges("quality", route_after_quality, {...})`。

**图配置**：
- remaining_steps 初始值 = 3（最多质检→重写→质检 3 轮）
- 每经一轮 remaining_steps -= 1（防止无限循环）

#### 3. src/pipeline/checkpoint.py — PostgreSQL Checkpoint

参考 LangGraph 的 SqliteSaver，实现 PostgreSQL Checkpoint 存储：

```python
class PostgresSaver:
    """LangGraph Checkpoint 的 PostgreSQL 实现"""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
    
    async def setup(self):
        """创建 checkpoints 和 checkpoint_writes 表"""
        # 需要创建 LangGraph 标准的 checkpoints 表结构
        # thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id
        # type, checkpoint (JSONB), metadata (JSONB)
    
    async def get_tuple(self, config: dict) -> CheckpointTuple | None:
        """获取最近的 checkpoint"""
    
    async def put(self, config: dict, checkpoint, metadata, new_versions):
        """保存 checkpoint"""
    
    async def put_writes(self, config: dict, writes, task_id):
        """保存 pending writes"""
```

#### 4. 导出函数

在 graph.py 中导出：

```python
async def build_pipeline_graph(
    llm: ChatDeepSeek,
    mcp_server: MCPServer,
    pool: asyncpg.Pool
) -> StateGraph:
    """构建并编译 Pipeline 图"""
    # 1. 创建 StateGraph(AgentState)
    # 2. 添加节点
    # 3. 添加边
    # 4. 设置 checkpoint (PostgresSaver)
    # 5. 返回编译后的图

async def run_pipeline_task(task: dict) -> dict:
    """入口：运行 Pipeline 模式竞品分析"""
    # 1. 获取依赖（llm, mcp_server, pool）
    # 2. build_pipeline_graph()
    # 3. graph.ainvoke(initial_state, config)
    # 4. 返回 final_report
```

```


`

### 验收标准
1. StateGraph 可以编译不报错
2. 节点函数签名统一：async def node_xxx(state: AgentState) -> dict
3. 条件边 route_after_quality 的逻辑正确（≥70分→结束，<70分→重写，步数耗尽→强制结束）
4. PostgresSaver 实现了 get_tuple/put/put_writes
5. graph.ainvoke() 可以传入 thread_id 支持对话恢复

### 注意事项
- 使用 LangGraph 1.0 API（StateGraph, add_node, add_edge, add_conditional_edges）
- 不要用已废弃的 add_sequence_nodes
- Checkpoint 的 JSONB 字段用 json.dumps 序列化
- 图编译使用 graph.compile(checkpointer=postgres_saver)
```

---

## Phase 4.5：记忆系统（Checkpoint + 摘要 + 长期记忆 + 冲突/遗忘）

### 目标
实现 Agent 三层记忆体系，覆盖短期（Checkpoint，Phase 4 已落地）、摘要记忆（分层压缩）、长期记忆（五步检索引擎 + 时间衰减 + 重要性评分）、冲突解决和遗忘策略。

### 前置条件
- Phase 4 已完成（Checkpoint + DAO 可用）
- Phase 1 的 agent_memories / memory_summaries 表已创建
- MCP 工具层的 embed_query / embed_texts / rerank 可用

### 交付物
1. `src/memory/__init__.py`
2. `src/memory/summarizer.py` — 摘要记忆（递增摘要 + 每 10 轮全量合并校准累积偏差）
3. `src/memory/long_term.py` — 长期记忆检索引擎（五步流水线：Query 重写→混合检索→RRF 融合→元数据过滤→Reranker 精排；三因索排序：语义相似度 × 重要性 × 时间衰减）
4. `src/memory/retrieval.py` — 检索策略（混合触发：关键词预判覆盖 80% + LLM 兜底覆盖 20%；两阶段范式：粗排 k=60 → 精排 top_k=10）
5. `src/memory/conflict.py` — 冲突解决（三级策略：事实更新→覆盖，偏好变化→保留历史+改标签，矛盾信息→双留+标记；软删除优于硬删除）
6. `src/memory/forgetting.py` — 遗忘策略（自然衰减在 SQL ORDER BY 层、180 天归档、显式软删除；决策类不归档）
7. 更新 `src/pipeline/graph.py` — 集成记忆钩子到 Pipeline 节点

### 提示词 (Phase 4.5)

```text
## 任务：竞品分析系统 — Agent 记忆系统

### 背景
项目在 `D:\AAAagent\projects\competitive-analysis-system\`，已有：
- Phase 1: agent_memories / memory_summaries 表（含 pgvector 向量索引）
- Phase 4: LangGraph Pipeline + PostgreSQL Checkpoint（短期记忆已落地）
- MCP 工具层：embed_query, embed_texts, rerank 可用
- DAO 层：MemorySummaryDAO, AgentMemoryDAO, ChunkEmbeddingDAO 可用

### 记忆系统三层架构回顾

```
+--------------------------------------------------+
|  第三层：长期记忆（Long-term）                      |
|  agent_memories 表 -> pgvector 向量检索            |
|  用户偏好、历史决策、领域知识                       |
|  持久 + 时间衰减 + 重要性评分                       |
+--------------------------------------------------+
|  第二层：摘要记忆（Summary）                        |
|  memory_summaries 表 -> JSON 字段                  |
|  过去 N 轮的压缩要点                                |
|  分层策略：递增(每轮) + 全量合并(每10轮校准)          |
+--------------------------------------------------+
|  第一层：短期记忆（Short-term）                     |
|  LangGraph Checkpoint -> PostgreSQL                |
|  当前会话完整对话历史 + Agent 状态                   |
|  控制维度：20 轮 / 100K Token / 决策优先            |
+--------------------------------------------------+
```

三层协作流程：
1. 短期 = 工作区，每轮直接读写
2. Token 预算将满 -> LLM 摘要 -> 写入 memory_summaries，清空旧轮
3. 会话结束 -> 摘要中的关键事实沉淀到 agent_memories（长期）
4. 新会话启动 -> 长期记忆检索 -> 注入短期初始上下文

### 你的任务

#### 1. src/memory/summarizer.py — 摘要记忆

核心：分层策略 = 递增摘要（日常）+ 每 N 轮全量合并（校准偏差）

```python
class MemorySummarizer:
    '''Memory summarizer: layered strategy to avoid incremental summary drift'''
    
    def __init__(self, llm: ChatDeepSeek, summary_dao: MemorySummaryDAO):
        self.llm = llm
        self.dao = summary_dao
        self.FULL_MERGE_INTERVAL = 10  # full merge every 10 rounds
    
    async def incremental_summary(
        self, task_id: str, prev_summary: str | None, 
        new_rounds: list[dict]
    ) -> str:
        '''Incremental: old summary + new conversation -> LLM -> new summary
        Prompt requirements:
        - Preserve all key decisions and user preferences
        - Discard chit-chat and repeated info
        - Mark uncertain info as [unverified]
        - Output <= 500 characters
        '''
    
    async def full_merge_summary(
        self, task_id: str, rounds: list[dict]
    ) -> str:
        '''Full merge: feed all 10 rounds of raw dialogue to LLM for fresh summary
        Used to calibrate cumulative drift of incremental summaries.
        Cost 10x higher than incremental, but fidelity 82%% -> 95%%+
        '''
    
    async def summarize_round(
        self, task_id: str, messages: list[dict], round_num: int
    ) -> str:
        '''Unified entry: decide incremental or full merge'''
        if await self.should_do_full_merge(task_id):
            rounds = await self._get_recent_rounds(task_id, 10)
            summary = await self.full_merge_summary(task_id, rounds)
            await self.dao.save(task_id, f"{round_num-9}-{round_num}", 
                               summary, "full_merge")
        else:
            prev = await self.dao.get_latest(task_id)
            recent = self._extract_recent_messages(messages)
            summary = await self.incremental_summary(task_id, prev, recent)
            await self.dao.save(task_id, str(round_num), 
                               summary, "incremental")
        return summary
```

**关键设计决策**：
- 递增摘要每轮丢 ~2% 细节，100 轮后保真度只剩 82%
- 全量合并每 10 轮校准一次，维持保真度 95%+
- 成本：递增 ~$0.000056/次，全量合并 ~$0.00056/次（10x）
- 竞品分析典型 7-10 轮，全量合并最多触发 1 次，月成本增量 < $2

#### 2. src/memory/long_term.py — 长期记忆检索引擎

五步流水线实现：

```python
class LongTermMemoryEngine:
    '''Long-term memory: pgvector retrieval + multi-factor ranking'''
    
    async def retrieve(
        self, user_id: str, query: str, top_k: int = 10
    ) -> list[dict]:
        '''5-step retrieval pipeline
        Step 1: Query rewrite (de-colloquialize + add synonyms)
        Step 2: Hybrid search (vector pgvector + BM25 tsvector)
        Step 3: RRF fusion (k=60, dedup + merge -> Top-60)
        Step 4: Metadata filter (WHERE user_id + is_active=true + within 180d)
        Step 5: Reranker fine rank (BGE-reranker-v2-m3 -> Top-10)
        '''
    
    async def _rewrite_query(self, query: str) -> str:
        '''Step 1: LLM query rewrite
        Input: '花月之前对 BGE-M3 有什么评价？'
        Output: 'BGE-M3 embedding 模型 评价 优缺点'
        '''
    
    def _compute_weight(
        self, similarity: float, importance: float, 
        created_at, half_life_days: int
    ) -> float:
        '''Final weight = semantic_similarity x importance x time_decay
        Time decay = 0.5 ^ (days / half_life_days)
        Half-life: decision=90d, preference=60d, fact=30d, chat=7d
        '''
    
    async def add_memory(
        self, user_id: str, content: str, memory_type: str,
        importance: float | None = None, half_life_days: int = 30
    ) -> UUID:
        '''Write long-term memory + auto conflict detection
        1. embed_query(content) -> vector
        2. conflict detection: semantic similarity >= 85%% -> mark conflict
        3. auto importance: decision=0.9, preference=0.7, fact=0.5, chat=0.1
        4. INSERT INTO agent_memories
        '''
```

**关键设计决策**：
- 粗排 60（召回优先），精排 10（精度优先）——两阶段范式
- 时间衰减用指数公式：weight x 0.5^(days/half_life)，不是线性
- 不同记忆类型有不同重要性权重和半衰期——决策记忆更持久
- pgvector ORDER BY 公式在 SQL 层一次完成三因索融合

#### 3. src/memory/retrieval.py — 检索触发策略

```python
class MemoryRetrievalStrategy:
    '''Decide: when to query memory, what to query'''
    
    # Keyword triggers (free, code-based, covers ~80%% of cases)
    RECALL_KEYWORDS = [
        '还记得', '上次', '之前', '以前', '回顾',
        '历史', '以前说过', '你记得', '之前聊过'
    ]
    SKIP_KEYWORDS = ['你好', '谢谢', '再见', '今天天气']
    
    async def should_retrieve(self, user_message: str) -> tuple[bool, str]:
        '''Hybrid trigger: keyword pre-check (80%%) + LLM fallback (20%%)
        1. Keyword match -> fast decision (most cases)
        2. Ambiguous case -> LLM judgment (rare)
        3. Default: don't query (conservative: query less rather than more)
        '''
    
    async def retrieve_if_needed(
        self, user_id: str, message: str, engine: LongTermMemoryEngine
    ) -> list[dict]:
        '''Unified entry: judge -> retrieve or skip'''
```

#### 4. src/memory/conflict.py — 冲突解决

```python
class MemoryConflictResolver:
    '''3-level conflict resolution'''
    
    async def detect_conflict(
        self, new_content: str, user_id: str, threshold: float = 0.85
    ) -> list[dict]:
        '''Detect: old memories with semantic similarity >= 85%%'''
    
    async def resolve(self, new_content: str, conflict: dict) -> str:
        '''Choose strategy by conflict type:
        - Fact update -> OVERWRITE (new overwrites old, old archived)
        - Preference change -> UPDATE (keep history + old marked expired)
        - Contradictory info -> KEEP_BOTH (both kept + marked conflict_id)
        Why no physical delete?
        1) Audit trail; 2) Transitional preferences need both visible; 3) Conflict is information
        '''
```

#### 5. src/memory/forgetting.py — 遗忘策略

```python
class MemoryForgetting:
    '''3 strategies: natural decay + periodic archive + explicit eviction
    JVM GC analogy: natural_decay=Minor GC, archive=Survivor->Old Gen, explicit=Full GC
    '''
    
    async def natural_decay(self, user_id: str) -> None:
        '''Strategy 1: Do nothing - time decay applies at SQL ORDER BY layer
        POWER(0.5, days/half_life_days) sinks old memories naturally
        '''
    
    async def archive_old_memories(self, user_id: str, days: int = 180) -> int:
        '''Strategy 2: Archive >180d + no access
        UPDATE agent_memories SET is_active = false
        WHERE last_accessed < NOW() - INTERVAL '180 days'
          AND memory_type != 'decision'  # decisions never archived
        '''
    
    async def explicit_forget(self, memory_id: UUID) -> None:
        '''Strategy 3: User explicit delete -> soft delete only'''
    
    async def run_maintenance(self, user_id: str) -> dict:
        '''Periodic maintenance (cron-triggerable)'''
```

#### 6. 集成到 Pipeline

```python
# node_analyze: retrieve long-term memory before analysis
async def node_analyze(state: AgentState) -> dict:
    memories = await retrieval_strategy.retrieve_if_needed(
        user_id=state.get('user_id', 'default'),
        message=f"分析 {state['competitors']} 维度 {state['current_dimension']}",
        engine=long_term_engine
    )
    state['retrieved_memories'] = memories  # inject to Analyzer context
    # ... original analysis logic ...

# After node_write: trigger summary
async def after_write(state: AgentState) -> None:
    await summarizer.summarize_round(
        task_id=state['task_id'], messages=state['messages'],
        round_num=state.get('report_version', 1)
    )

# node_finalize: persist key decisions to long-term memory
async def node_finalize(state: AgentState) -> dict:
    await long_term_engine.add_memory(
        user_id=state.get('user_id', 'default'),
        content=f"竞品分析完成: {state['title']}, "
                f"质检分 {state['quality_score']}, "
                f"竞品 {state['competitors']}",
        memory_type='decision', importance=0.8
    )
    # ... original finalize logic ...
```

```


### 验收标准
1. `MemorySummarizer.summarize_round()` 自动判断递增 or 全量合并
2. `LongTermMemoryEngine.retrieve()` 五步流水线串通，返回带权重的 Top-10
3. `MemoryRetrievalStrategy.should_retrieve()` 关键词预判覆盖常用触发词
4. `MemoryConflictResolver.resolve()` 按冲突类型选择正确策略
5. `MemoryForgetting.archive_old_memories()` 正确归档且不删决策类记忆
6. Pipeline 集成：analyze 节点可检索长期记忆，finalize 节点可写入长期记忆

### 实现状态：✅ 已完成（2026-06-22）

**交付物**（全部通过 AST 验证）：
| 文件 | 模块 | 行数 |
|------|------|------|
| `src/memory/__init__.py` | 包入口，导出 5 个类 | 25 |
| `src/memory/summarizer.py` | MemorySummarizer（递增 + 全量合并） | ~140 |
| `src/memory/long_term.py` | LongTermMemoryEngine（五步检索 + RRF 融合） | ~180 |
| `src/memory/retrieval.py` | MemoryRetrievalStrategy（混合触发） | ~100 |
| `src/memory/conflict.py` | MemoryConflictResolver（三级冲突） | ~120 |
| `src/memory/forgetting.py` | MemoryForgetting（三层遗忘） | ~80 |

**Pipeline 集成**（`src/pipeline/graph.py` 已更新）：
- analyze 节点：检索长期记忆 → 格式化为 memory_context → 注入 Analyzer LLM Prompt
- finalize 节点：LLM 提取 3-5 条关键决策/偏好/事实 → `engine.add_memory()` 写入 agent_memories
- Summarizer 不在 Pipeline 集成（Pipeline 无对话轮次，留给 Phase 5A Supervisor ReAct 循环）

**验收中修复的 Bug**：
- 🔧 summarizer.py / retrieval.py / conflict.py 共 4 处 `\\n` 转义错误（Codex 写入时多转了一次）→ 全部改为 `\n`

**实施时补充的设计决策**：
- round_num 在 Pipeline 中无自然来源（最多 3 个 report_version，达不到全量合并阈值 10）→ Summarizer 跳过 Pipeline，留给 Supervisor
- user_id 在 Pipeline 集成中暂用 `state.get("user_id", "default")` 兜底——待 AgentState 补 user_id 字段后消除兜底值
- Finalize 节点的记忆提取用内联 prompt（非 `_KEY_DECISIONS_PROMPT` 常量），格式 `type|content`
- 三个钩子中 Pipeline 只集成了两个（analyze + finalize），write 后摘要钩子不集成

### 注意事项
- 长期记忆检索的 ORDER BY 在 SQL 层完成（一次查询，不返 Python 再排序）
- 摘要记忆的 LLM Prompt 必须约束输出长度（<=500 字）——否则摘要比原文还长
- 冲突检测的语义相似度阈值 0.85 可调——太高导致漏检，太低导致误报
- 遗忘策略不加 cron 调度（Phase 4.5 只写逻辑，调度由用户自行配置）
- 所有记忆操作必须带 user_id——多用户隔离
- embedding 向量用 BGE-M3 1024 维，和 RAG 检索共用同一模型（避免重复加载）
- **Pipeline 只集成长记忆检索和写入两个钩子，Summarizer 留给 Phase 5A Supervisor**

---

## Phase 5A：Supervisor + A2A 通信协议

### 目标
实现 Agent Card 定义、A2A 通信协议、Supervisor ReAct 循环。

### 前置条件
- Phase 3 已完成（Agent 函数可用）
- Phase 4 不是硬依赖（Supervisor 独立使用 Agent 函数）
- Phase 4.5 不是硬依赖（记忆系统可后续接入）

### 交付物
1. `src/supervisor/a2a.py` — A2A 协议实现
2. `src/supervisor/state.py` — SupervisorState
3. `src/supervisor/supervisor.py` — Supervisor ReAct 循环

### 提示词 (Phase 5A)

`	ext
## 任务：竞品分析系统 — Supervisor + A2A 通信协议

### 背景
四个 Agent 函数已实现。需要在 Pipeline 之外构建 Supervisor+ReAct 探索模式。

### A2A 协议说明
A2A (Agent-to-Agent) 是 Google 提出的 Agent 通信协议。核心概念：
- **Agent Card**：声明 Agent 的能力、输入输出 schema、URL
- **Task**：send → pending → running → completed/failed 生命周期
- 协议是 P2P 设计，但我们采用集中式（全走 Supervisor 中转）

### 你的任务

#### 1. src/supervisor/a2a.py — A2A 协议

```python
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class AgentCard:
    """A2A Agent Card：声明 Agent 的能力"""
    name: str
    description: str
    capabilities: list[str]  # ["collect", "web_search", "web_fetch"]
    input_schema: dict       # JSON Schema
    output_schema: dict      # JSON Schema
    endpoint: str            # 逻辑端点（用于 A2A 路由）

@dataclass  
class A2ATask:
    """A2A Task：一次 Agent 调用的生命周期"""
    id: str = field(default_factory=lambda: str(uuid4()))
    agent_name: str
    action: str              # 调用的具体 action
    arguments: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: dict | None = None
    error: str | None = None
    created_at: float = 0.0
    completed_at: float | None = None

class A2ARouter:
    """A2A 路由器：管理 Agent Card 注册和 Task 分发"""
    
    def __init__(self):
        self._cards: dict[str, AgentCard] = {}
    
    def register(self, card: AgentCard):
        """注册 Agent Card"""
        self._cards[card.name] = card
    
    def get_card(self, agent_name: str) -> AgentCard | None:
        """获取 Agent Card"""
        return self._cards.get(agent_name)
    
    def list_agents(self) -> list[AgentCard]:
        """列出所有可用 Agent"""
        return list(self._cards.values())
    
    async def send_task(self, task: A2ATask) -> A2ATask:
        """发送 Task 到目标 Agent 并等待结果
        返回更新后的 Task（带 result）
        """
        # 1. 查找 AgentCard
        # 2. 校验 arguments 符合 input_schema
        # 3. 更新 task.status = RUNNING
        # 4. 调用 Agent 函数
        # 5. 更新 task.status = COMPLETED/FAILED, task.result
        # 6. 返回 task
```

注册四个 Agent Card：
- Collector: capabilities=["collect", "web_search", "web_fetch"]
- Analyzer: capabilities=["analyze", "embed", "rerank"]
- Writer: capabilities=["write", "compose_report"]
- Quality: capabilities=["evaluate", "score_report"]

#### 2. src/supervisor/state.py — SupervisorState

```python
class SupervisorState(TypedDict):
    task_id: str
    title: str
    user_query: str  # 用户原始问题（开放性探索）
    
    # 探索结果
    found_competitors: list[str]  # 探索发现的竞品
    collected_data: dict
    analysis_results: dict
    report_content: str
    
    # 控制
    current_round: int
    max_rounds: int  # 10
    last_action: str
    reasoning_trace: list[dict]  # [{round, thought, action, args, observation}]
    
    # 终结状态
    final_output: str
    is_complete: bool
```

#### 3. src/supervisor/supervisor.py — Supervisor ReAct 循环

```python
class Supervisor:
    """Supervisor Agent：LLM 动态决策的 ReAct 循环"""
    
    def __init__(self, llm: ChatDeepSeek, a2a_router: A2ARouter):
        self.llm = llm
        self.router = a2a_router
    
    async def _think(self, state: SupervisorState) -> dict:
        """LLM 决策下一步行动
        输出：{action, agent, arguments, reason}
        """
        # 1. 构建 Prompt（包含当前状态、历史 reasoning_trace）
        # 2. LLM 输出下一步决策
        # 3. 解析 JSON
    
    async def _act(self, action: dict) -> A2ATask:
        """执行决策：通过 A2A 发 Task"""
        task = A2ATask(
            agent_name=action["agent"],
            action=action["action"],
            arguments=action["arguments"]
        )
        return await self.router.send_task(task)
    
    async def _observe(self, state: SupervisorState, task: A2ATask):
        """观察结果，写入状态"""
        # 根据 task.agent_name 将结果写入对应状态字段
    
    async def run(self, state: SupervisorState) -> str:
        """ReAct 主循环
        while state.current_round < state.max_rounds:
            action = await self._think(state)
            if action["action"] == "finish":
                break
            task = await self._act(action)
            await self._observe(state, task)
            state.current_round += 1
        return state.final_output
```

**Supervisor System Prompt 核心要点**：
```
你是竞品分析协调者（Supervisor）。根据用户的问题和当前状态，决策下一步行动。

可用 Agent：
1. collector — 搜索并采集竞品信息
2. analyzer — 分析竞品数据的特定维度
3. writer — 撰写分析报告
4. quality — 评估报告质量

输出格式（JSON）：
{
  "thought": "当前情况分析...",
  "action": "collect|analyze|write|quality|finish",
  "agent": "collector|analyzer|writer|quality",
  "arguments": {},
  "reason": "为什么选择这一步"
}

规则：
- collect 必须先于 analyze（没数据不能分析）
- analyze 必须先于 write（没分析不能写报告）
- 如果所有必要步骤已完成，action=finish
- 每步都要有明确理由
```

```


`

### 验收标准
1. A2ARouter.register() 可以注册 Agent Card
2. A2ARouter.send_task() 正确路由到对应 Agent 函数
3. Supervisor._think() 返回符合格式的 JSON 决策
4. Supervisor.run() 的 ReAct 循环有 MAX_ROUNDS=10 硬上限
5. 每个循环轮的 reasoning_trace 都有完整记录

### 注意事项
- A2A 的 send_task 要持有 Agent 函数引用（通过依赖注入）
- Supervisor._think() 的 temperature=0.3（决策需要一定灵活性但不要太多创意）
- reasoning_trace 是面试展示的重点——记录要详细
- 如果 LLM 返回的 JSON 解析失败，记录错误并 finish
```

---

## Phase 5B：IntentRouter + Harness Engineering

### 目标
实现 LLM 实体提取后的代码路由和 Harness 五层安全检查。

### 前置条件
- Phase 4 已完成（Pipeline graph）
- Phase 5A 已完成（Supervisor + A2A）

### 交付物
1. `src/supervisor/router.py` — 代码路由决策
2. `src/harness/guard.py` — 五层安全检查
3. `src/harness/audit.py` — 审计日志

### 提示词 (Phase 5B)

`	ext
## 任务：竞品分析系统 — IntentRouter + Harness Engineering

### 背景
- Pipeline 图已完成（src/pipeline/graph.py）
- Supervisor+A2A 已完成（src/supervisor/）
- 需要分流器把两者串起来，加上安全壳

### 你的任务

#### 1. src/supervisor/router.py — 代码路由决策

```python
class IntentRouter:
    """代码路由：LLM 已提取实体(competitors/dimensions)，代码判断走哪条路"""
    
    def __init__(self):
        self.route_history: list[dict] = []
    
    def classify(self, parsed: dict) -> str:
        """根据 LLM 提取的结构化数据判断路径
        
        LLM 已输出: {"competitors": [...], "dimensions": [...], "intent_is_clear": bool}
        代码只负责: 读这些字段，做确定性路由决策
        
        Returns: "pipeline" | "supervisor"
        """
        # 决策逻辑（代码，读 LLM 输出）：
        # if not parsed.competitors or len(parsed.competitors) < 2:
        #     return "supervisor"
        # if not parsed.dimensions or len(parsed.dimensions) == 0:
        #     return "supervisor"
        # if not parsed.intent_is_clear:
        #     return "supervisor"
        # return "pipeline"
    
    async def route(self, task: dict) -> dict:
        """分流入口"""
        route_type = self.classify(task)
        self.route_history.append({
            "task_id": task["id"],
            "route": route_type,
            "reason": "竞品和维度明确" if route_type == "pipeline" else "开放性探索"
        })
        
        if route_type == "pipeline":
            return await run_pipeline_task(task)
        else:
            supervisor = Supervisor(llm, a2a_router)
            state = SupervisorState(
                task_id=task["id"],
                title=task.get("title", "探索性分析"),
                user_query=task.get("query", ""),
                current_round=0,
                max_rounds=10,
                ...
            )
            return await supervisor.run(state)
```

**路由逻辑核心**：
- LLM 提取出 `{"competitors":["飞书","钉钉","Notion"],"dimensions":["功能","定价","市场"],"intent_is_clear":true}` → 代码判定 pipeline
- LLM 提取出 `{"competitors":[],"dimensions":[],"intent_is_clear":false}` → 代码判定 supervisor（先探索澄清）

#### 2. src/harness/guard.py — 五层安全检查

```python
class HarnessGuard:
    """Harness Engineering 五层安全检查中间件"""
    
    # 第一层：白名单
    ASYNC def check_whitelist(self, agent_name: str, action: str) -> bool:
        """检查该 Agent 是否有权执行此 action"""
        WHITELIST = {
            "collector": ["collect", "web_search", "web_fetch"],
            "analyzer": ["analyze", "embed", "rerank"],
            "writer": ["write", "compose_report"],
            "quality": ["evaluate", "score_report"],
        }
        return action in WHITELIST.get(agent_name, [])
    
    # 第二层：参数校验
    async def validate_params(self, action: str, arguments: dict, schema: dict) -> tuple[bool, str]:
        """校验参数类型、必填字段、值范围"""
        # 检查 required 字段都存在
        # 检查参数类型匹配
        # 返回 (is_valid, error_message)
    
    # 第三层：频控
    async def check_rate_limit(self, agent_name: str) -> bool:
        """检查该 Agent 是否超过频控阈值"""
        # 全局 TokenBucket: 100 QPS
        # 单 Agent 独立窗口: 10 req/s
        # 返回 True=放行, False=限流
    
    # 第四层：安全断点（PII 检测）
    async def scan_for_pii(self, content: str) -> tuple[bool, str]:
        """检测内容中是否包含敏感信息（手机号、身份证、邮箱等）"""
        # 正则匹配常见的 PII 模式
        # 手机号：1[3-9]\d{9}
        # 身份证：\d{17}[\dXx]
        # 返回 (has_pii, detail)
    
    # 第五层：审计记录
    async def audit(self, event: dict):
        """记录所有通过 Harness 的请求"""
        # event: {timestamp, agent, action, arguments, result, 
        #         whitelist_pass, param_pass, rate_limit_pass, pii_pass}
    
    # 统一入口
    async def guard(self, agent_name: str, action: str, 
                    arguments: dict, schema: dict) -> dict:
        """五层检查统一入口，阻断不崩溃"""
        checks = {
            "whitelist": False,
            "param_valid": False,
            "rate_limit": False,
            "pii_clean": True,
        }
        
        # 1. 白名单
        if not await self.check_whitelist(agent_name, action):
            await self.audit({...})
            return {"error": "WHITELIST_DENIED", "degraded": True}
        checks["whitelist"] = True
        
        # 2. 参数校验
        valid, msg = await self.validate_params(action, arguments, schema)
        if not valid:
            await self.audit({...})
            return {"error": f"PARAM_INVALID: {msg}", "degraded": True}
        checks["param_valid"] = True
        
        # 3. 频控
        if not await self.check_rate_limit(agent_name):
            await self.audit({...})
            return {"error": "RATE_LIMITED", "degraded": True}
        checks["rate_limit"] = True
        
        # 4. PII 检测（只告警不阻断——但记录）
        has_pii, detail = await self.scan_for_pii(str(arguments))
        if has_pii:
            checks["pii_clean"] = False
            # 注意：PII 检测失败只记录，不阻断（阻断太激进）
        
        # 5. 审计
        await self.audit({...})
        
        return {"passed": True, "checks": checks}
```

### 关键设计：阻断不崩溃
- 检查失败返回 `{"error": "...", "degraded": true}`，不抛异常
- Supervisor 在 _observe() 中看到 degraded 标记
- 下一轮 Think 决定跳过或重试

#### 3. src/harness/audit.py — 审计日志

```python
class AuditLogger:
    """结构化审计日志"""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
    
    async def log(self, event: dict):
        """写入 agent_logs 表
        包含：task_id, agent_name, action, request(JSON), 
              response(JSON), error, duration_ms
        """
        await AgentLogDAO.log(self.pool, **event)
    
    async def get_task_trail(self, task_id: str) -> list[dict]:
        """获取完整的调用链日志（按时间排序）"""
        return await AgentLogDAO.get_by_task(self.pool, task_id)
```

```


`

### 验收标准
1. IntentRouter.classify({"competitors":["飞书"],"dimensions":["功能"],"intent_is_clear":true}) → "pipeline"
2. IntentRouter.classify({"competitors":[],"dimensions":[],"intent_is_clear":false}) → "supervisor"
3. Harness 白名单拦截：collector 不能调 "embed" action
4. Harness 返回 {"error": "WHITELIST_DENIED", "degraded": true}
5. PII 检测能捕获手机号和身份证号
6. 审计日志每次 check 都写入 agent_logs

### 注意事项
- Harness 集成到 A2ARouter.send_task() 中——所有跨 Agent 调用必经 Harness
- PII 检测不阻断——只记录告警。PII 阻断太激进，可能误杀正常数据
- 频控 TokenBucket 用简单的内存实现即可（时间窗口 + 计数器），不需要 Redis
- 路由历史记录到 agent_logs
```

---

## Phase 6：服务化 + 可观测性

### 目标
FastAPI 服务 + SSE 进度推送 + 三层限流 + Prometheus 指标 + 结构化日志。

### 前置条件
- Phase 4 + Phase 5B 已完成

### 交付物
1. `src/api/routes.py` — FastAPI 路由
2. `src/api/sse.py` — SSE 进度推送
3. `src/api/rate_limit.py` — 三层限流
4. `src/observability/logging.py` — 结构化日志
5. `src/observability/metrics.py` — Prometheus 指标

### 提示词 (Phase 6)

`	ext
## 任务：竞品分析系统 — FastAPI 服务化 + 可观测性

### 背景
Pipeline + Supervisor + Harness 已完成，需要封装为 HTTP 服务。

### 你的任务

#### 1. src/api/routes.py — API 路由

```python
from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="Competitive Analysis Agent System")

@app.post("/api/tasks")
async def create_task(request: TaskRequest):
    """创建竞品分析任务"""
    # 1. 生成 task_id
    # 2. 写入 tasks 表
    # 3. IntentRouter.route() 异步执行
    # 4. 返回 {task_id, status: "pending"}

@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """查询任务状态和结果"""
    # 返回任务状态 + 最新报告

@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    """SSE 进度推送"""
    # EventSourceResponse 推送进度事件
    # 事件类型：collect_progress, analyze_progress, 
    #           write_progress, quality_result, complete

@app.get("/api/tasks/{task_id}/reports")
async def get_reports(task_id: str):
    """获取所有版本的报告"""

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点"""
    return Response(content=generate_latest(), media_type="text/plain")
```

**TaskRequest 模型**：
```python
class TaskRequest(BaseModel):
    title: str  # "协同办公软件竞品分析"
    competitors: list[str] | None = None
    dimensions: list[str] | None = None
    query: str | None = None  # 开放性探索用
```

#### 2. src/api/sse.py — SSE 进度推送

```python
async def event_generator(task_id: str, pool: asyncpg.Pool):
    """从 agent_logs 读取进度并推送 SSE"""
    last_log_id = 0
    while True:
        # 1. 查询 agent_logs WHERE id > last_log_id
        # 2. 每条新日志 → yield SSE event
        # 3. 更新 last_log_id
        # 4. 检查任务是否完成
        # 5. 完成则发送 complete 事件 + break
        # 6. 等待 1 秒再检查
        await asyncio.sleep(1)
```

SSE 事件类型：
- `progress`: {"agent": "collector", "message": "正在采集飞书数据..."}
- `progress`: {"agent": "analyzer", "message": "正在分析定价维度..."}
- `quality_result`: {"score": 82, "passed": true}
- `complete`: {"task_id": "...", "report_id": "..."}
- `error`: {"agent": "...", "message": "..."}

#### 3. src/api/rate_limit.py — 三层限流

```python
class TokenBucket:
    """第一层：入口限流 — TokenBucket 100 QPS"""
    def __init__(self, capacity: int = 100, refill_rate: float = 10.0):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate  # tokens per second
    
    async def acquire(self) -> bool:
        """获取 token，成功返回 True"""

class AgentSemaphore:
    """第二层：Agent 并发控制 — asyncio.Semaphore(3)"""
    def __init__(self, max_concurrent: int = 3):
        self.semaphore = asyncio.Semaphore(max_concurrent)

class LLMRateLimiter:
    """第三层：LLM API 限流 — 60 RPM"""
    def __init__(self, max_rpm: int = 60):
        self.requests = []
    
    async def wait_if_needed(self):
        """检查滑动窗口，必要时等待"""
```

#### 4. src/observability/logging.py — 结构化日志

```python
import structlog

def setup_logging():
    """配置结构化日志"""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

logger = structlog.get_logger()
```

日志格式：`[timestamp] [level] [task_id] [agent] [action] message`

#### 5. src/observability/metrics.py — Prometheus 指标

定义以下指标：

| 指标名 | 类型 | 标签 | 说明 |
|--------|------|------|------|
| ca_tasks_total | Counter | status | 任务总数 |
| ca_task_duration_seconds | Histogram | route | 任务耗时 |
| ca_agent_calls_total | Counter | agent, action, status | Agent 调用次数 |
| ca_quality_score | Histogram | — | 质检分数分布 |
| ca_rag_recall | Gauge | dimension | RAG 召回率 |
| ca_rate_limit_hits | Counter | layer, agent | 限流触发次数 |
| ca_harness_blocks | Counter | check_type | Harness 阻断次数 |

```


`

### 验收标准
1. POST /api/tasks 创建任务并返回 task_id
2. GET /api/tasks/{id}/stream 推送 SSE 进度事件
3. TokenBucket 正确实现 100 QPS 限流
4. /metrics 端点输出 Prometheus 格式
5. 结构化日志包含 task_id 和 agent 字段

### 注意事项
- FastAPI 使用 lifespan 管理资源（数据库 pool、模型加载）
- SSE 推送需要有超时和断连重试机制
- TokenBucket 不用 Redis——内存实现即可
- LLM Rate Limiter 用滑动窗口，不是固定窗口
```

---

## Phase 7：评估体系 + 集成测试

### 目标
Golden Dataset + RAGAS 评估 + LLM-as-Judge 离轨评估 + 回归测试 + E2E 测试。

### 前置条件
- Phase 6 已完成

### 交付物
1. `src/evaluation/golden_dataset.py`
2. `src/evaluation/ragas_eval.py`
3. `src/evaluation/judge_eval.py`
4. `tests/test_e2e.py` — 端到端测试

### 提示词 (Phase 7)

`	ext
## 任务：竞品分析系统 — 评估体系 + 集成测试

### 背景
全系统已完成，需要构建评估体系和服务健康检查。

### 你的任务

#### 1. src/evaluation/golden_dataset.py

构建 10 个竞品的 Golden Dataset（easy/medium/hard 各 3-4 个）：

```python
GOLDEN_DATASET = [
    {
        "task_id": "golden_001",
        "title": "飞书基础功能分析",
        "competitors": ["飞书", "钉钉"],
        "dimensions": ["功能", "定价"],
        "difficulty": "easy",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 2,
            "required_keywords": ["飞书", "钉钉", "定价", "功能"],
            "min_evidence_count": 4,
            "quality_threshold": {
                "完整性": 60,
                "准确性": 60
            }
        }
    },
    # ... 更多测试用例
]
```

#### 2. src/evaluation/ragas_eval.py

基于 RAGAS 的四维评估：

```python
async def evaluate_rag(task_id: str) -> dict:
    """RAGAS 评估：context_relevancy, faithfulness, 
       answer_relevancy, context_precision"""
```

关键阈值：
- context_relevancy >= 0.75
- faithfulness >= 0.80
- answer_relevancy >= 0.70

#### 3. src/evaluation/judge_eval.py

LLM-as-Judge 离轨评估：

```python
async def evaluate_with_judge(task_id: str) -> dict:
    """用不同模型做评估（模型隔离原则）"""
    # 评估模型用 deepseek-chat（与质检 Agent 同模型）
    # 五维评分：完整性30 准确性30 可追溯性20 可读性10 客观性10
```

#### 4. tests/test_e2e.py

端到端测试：`POST /api/tasks → 等待完成 → 验证报告`

```python
async def test_e2e_pipeline():
    """E2E: Pipeline 模式测试"""
    # 1. 创建任务 {"competitors": ["飞书"], "dimensions": ["功能"]}
    # 2. 轮询 GET /api/tasks/{id} 直到 status=completed
    # 3. 验证 report_content 非空
    # 4. 验证 quality_score >= 0
    # 5. 验证 evidence_map 有引用来源

async def test_quality_loop():
    """质检回退循环测试"""
    # 验证 quality_score < 70 时触发重写

async def test_supervisor_explore():
    """Supervisor 探索模式测试"""
    # 创建无 competitors 的任务
    # 验证 IntentRouter 走 supervisor 模式
    # 验证探索结果非空

async def test_harness_whitelist():
    """Harness 白名单拦截测试"""
    # 验证非法 action 被拦截

async def test_rate_limit():
    """频控测试"""
    # 高并发请求，验证 429 返回
```

### 门禁标准

| 档位 | 条件 | 操作 |
|------|------|------|
| PASS | overall_score >= 0.85 AND all RAGAS metrics above threshold | 合并 |
| WARN | overall_score >= 0.70 | 人工 Review |
| BLOCK | overall_score < 0.70 | 阻断合并 |

```


`

### 验收标准
1. Golden Dataset 覆盖 easy/medium/hard 三类场景
2. RAGAS 评估输出四维指标
3. E2E 测试覆盖 Pipeline + Supervisor 两条路径
4. Harness 安全性测试通过
5. 所有测试函数可独立运行

### 注意事项
- 评估不要依赖真实 API（用 mock 数据或仅验证结构）
- E2E 测试要有 timeout（单个测试不超过 60 秒）
- Golden Dataset 的 expected 字段用于自动化断言
```

---

## 开发顺序与依赖图

```
Phase 0 ──→ Phase 1 ──→ Phase 3 ──→ Phase 4 ──→ Phase 4.5 ──→ Phase 6 ──→ Phase 7
    │   ✅       ✅         ✅         ✅          ✅
    └──→ Phase 2 ─────────┘              │
          ✅                              │
                        Phase 5A ──→ Phase 5B ──┘

✅ = 已完成    ⏳ = 进行中    ⬚ = 待开发
```

- Phase 0（脚手架）是所有阶段的前置
- Phase 1（数据库）和 Phase 2（MCP工具）可并行
- Phase 3（Agent）依赖 Phase 1 + Phase 2
- Phase 4（Pipeline）→ Phase 4.5（记忆系统）依赖 Phase 4
- Phase 5A（Supervisor）依赖 Phase 3
- Phase 5B（Router+Harness）依赖 Phase 4 + Phase 5A
- Phase 6（服务化）依赖 Phase 4 + Phase 5B
- Phase 7（评估）依赖 Phase 6

### 开发进度总览

| Phase | 名称 | 状态 | 完成时间 | 审计报告 |
|-------|------|:--:|---------|---------|
| 0 | 项目脚手架 + 配置 | ✅ | 2026-06-20 | phase_report/phase0-audit_2026-06-20.md |
| 1 | 数据库 Schema + DAO | ✅ | 2026-06-20 | phase_report/phase1-audit_2026-06-20.md |
| 2 | MCP 工具层 | ✅ | 2026-06-20 | phase_report/phase2-audit_2026-06-20.md |
| 3 | Agent 实现 | ✅ | 2026-06-21 | phase_report/phase3-audit_2026-06-21.md |
| 4 | Pipeline 编排 | ✅ | 2026-06-22 | phase_report/phase4-audit_2026-06-22.md |
| 4.5 | 记忆系统 | ✅ | 2026-06-22 | 本文档（内联验收） |
| 5A | Supervisor + A2A | ⬚ | — | — |
| 5B | IntentRouter + Harness | ⬚ | — | — |
| 6 | 服务化 + 可观测性 | ⬚ | — | — |
| 7 | 评估体系 + 集成测试 | ⬚ | — | — |

---

## 给 Codex 的通用约束（每次任务都需包含）

```
### 环境约束
- Python: D:\AAAProgram Files\Python\python\python.exe (3.13)
- 项目根目录: D:\AAAagent\projects\competitive-analysis-system\
- 所有文件编码: UTF-8
- 不创建虚拟环境，不安装依赖（pip install -e . 由用户手动执行）
- 不做 git 操作
- Type hints 全覆盖，每个函数有 docstring
- 异步代码使用 async/await，数据库用 asyncpg

### 代码风格
- 每个函数注释说明"为什么这样做"（不是"做了什么"）
- 关键设计决策加 # DESIGN: 前缀注释
- 错误处理：不吞异常，对外返回 dict 带 error 字段
- 日志：用 structlog，不是 print
```
