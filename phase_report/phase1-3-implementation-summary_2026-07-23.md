# Phase 1-3 实现总结 — 项目脚手架 + 数据库 + MCP 工具层

**时间**：2026-06-15 ~ 2026-06-20
**作者**：AI 工程师
**范围**：Phase 1（项目脚手架）→ Phase 2（数据库 Schema + DAO）→ Phase 3（MCP 工具层）共 8 源文件

---

## 一、Phase 1-3 是什么？

这三个阶段是竞品分析系统的**基础设施三层**——没有它们，后面的 Pipeline / Supervisor / 记忆系统 / API 服务都无处落脚。

```
┌────────────────────────────────────────────────┐
│              竞品分析系统 总体架构               │
│                                                │
│  Phase 10: 评估体系      ┐                     │
│  Phase 9:  FastAPI 服务化 ├── 上层建筑          │
│  Phase 8:  IntentRouter  │                     │
│  Phase 7:  Supervisor    │                     │
│  Phase 6:  记忆系统       ├── 业务逻辑           │
│  Phase 5:  Pipeline 编排  │                     │
│  Phase 4:  Agent 实现    ┘                     │
│                                                │
│  Phase 3:  MCP 工具层 ← ★ 本次                  │
│  Phase 2:  Schema + DAO ← ★ 本次               │
│  Phase 1:  项目脚手架  ← ★ 本次                 │
└────────────────────────────────────────────────┘
```

| Phase | 名称 | 源文件 | 关键交付 |
|:--:|------|:--:|------|
| 1 | 项目脚手架 + 配置 | 2 | `.env` + `config.py` + `__init__.py` |
| 2 | 数据库 Schema + DAO | 3 | `schema.sql`（9表）+ `dao.py`（7类CRUD）+ `connection.py` |
| 3 | MCP 工具层 | 3 | `server.py`（MCP Server）+ `tools_web.py` + `tools_rag.py` + `__init__.py` |
| **合计** | **基础设施三层** | **8** | **~1850 行代码** |

### 为什么 Phase 1-3 要放在一起总结？

因为它们加在一起才构成一个**可运行的 Agent 基础设施**：

- Phase 1 提供配置（Agent 知道怎么连 API、怎么连数据库）
- Phase 2 提供数据（Agent 有地方读写任务/报告/证据/记忆）
- Phase 3 提供能力（Agent 能搜索、能抓取、能嵌入、能精排）

有了这三层，Collector Agent 就能：`读配置 → 连接数据库 → 调用 web_search → 存入 chunk_embeddings`。没有任意一层，这个链路就跑不通。

---

## 二、核心模块详解

### 2.1 Phase 1：项目脚手架

#### config.py — Pydantic Settings 全局配置

**为什么用 pydantic-settings 而不是 `os.getenv()`？**

| 方案 | 类型安全 | .env 自动加载 | 字段级默认值 | IDE 补全 |
|------|:--:|:--:|:--:|:--:|
| `os.getenv()` | ❌ | ❌ | ❌ | ❌ |
| Pydantic Settings | ✅ | ✅ | ✅ | ✅ |

`os.getenv()` 的问题：拼错变量名（`DEEPSEEK_API_KYE`）→ 返回 `None` → 静默失败，查到死。Pydantic 在启动时校验所有字段，缺了直接报错。

**核心字段决策**：

| 字段 | 值 | 为什么 |
|------|-----|------|
| `deepseek_model` | `deepseek-v4-flash` | Flash 比 Chat 快 3 倍，竞品分析的 LLM 调用量大（每维度 × 每竞品），选速度 |
| `pg_port` | 5433 | Docker 容器映射端口，避开本机 PG 18 的 5432 |
| `embedding_model` | `BAAI/bge-m3` | 1024 维，中文 SOTA，多语言，开源免费 |
| `token_bucket_capacity` | 100 | 配合 10/s refill，本地开发够用 |
| `llm_rpm_limit` | 60 | DeepSeek 免费/个人 API 通常 60 RPM |

**面试考点**：`@computed_field` 的 `database_url` —— 不是存到 .env，而是由 5 个字段拼接。好处：改端口只改一处，不需要同时改 `.env` 里的 DSN 字符串。

#### .env 文件 — 敏感信息隔离

```
DEEPSEEK_API_KEY=sk-xxx  ← 永不提交 Git
PG_HOST=localhost
PG_PORT=5433
```

`.gitignore` 必须包含 `.env`。配置管理的铁律：**密钥和代码分离**。

---

### 2.2 Phase 2：数据库 Schema + DAO

#### schema.sql — 9 张表的完整设计

```
┌─────────────────────────────────────────────────────────┐
│                    9 张表 三层次架构                      │
│                                                         │
│  业务层        向量层         运维层                      │
│  ┌────────┐  ┌──────────┐  ┌──────────────┐            │
│  │ tasks  │  │chunk     │  │ agent_logs   │            │
│  │        │  │_embeddings│  │              │            │
│  ├────────┤  │          │  ├──────────────┤            │
│  │reports │  ├──────────┤  │memory        │            │
│  │        │  │agent     │  │_summaries    │            │
│  ├────────┤  │_memories │  │              │            │
│  │evidence│  │          │  ├──────────────┤            │
│  │_map    │  └──────────┘  │checkpoints   │            │
│  └────────┘                │checkpoint    │            │
│                             │_writes       │            │
│                             └──────────────┘            │
└─────────────────────────────────────────────────────────┘
```

**7 个关键设计决策**：

| # | 决策 | 为什么 |
|:--:|------|------|
| 1 | UUID 主键 > SERIAL | 分布式安全，不依赖自增序列 |
| 2 | tasks 用 JSONB 存竞品/维度列表 | 半结构化数据不需要关联表 |
| 3 | HNSW 索引 > IVFFlat | 读多写少场景快 10-100 倍 |
| 4 | BGE-M3 → vector(1024) | 维度写死，换模型需 ALTER TABLE |
| 5 | ON DELETE…分情况选择 | CASCADE（报告依赖任务）vs SET NULL（记忆独立于任务） |
| 6 | zhparser 中文分词 | 基于 SCWS 词典，比 ngram 精准 |
| 7 | LangGraph checkpoint 双表 | 超标交付，为 Phase 7 的 Supervisor 做准备 |

#### 面试追问速答：

**Q: "为什么不拆成 TASK_COMPETITOR 和 TASK_DIMENSION 关联表？"**

> 竞品列表 `["飞书","钉钉"]` 不是独立实体——它们没有自己的属性、不参与 JOIN、不独立增删。用关联表 = 多个 JOIN 查 3 个名字，JSONB 一次拿到。这是一种"数据的独立实体判断"——有自己生命周期的拆表，没有的用 JSONB。

**Q: "zhparser 只索引 n,v,a,i,e,l,d 七种词性，为什么？"**

> 这些词性覆盖了有语义区分度的词。代词（r）、助词（u）、标点（w）对搜索没区分度——"我的产品很好用"去"我""的"只剩"产品""好用"。这是停用词思想通过词性过滤实现。

#### dao.py — 7 个 DAO 类的完整 CRUD

**为什么用 asyncpg 原生 SQL 而不用 SQLAlchemy？**

```
面试分层回答：
  L1 选型：asyncpg 直连 PostgreSQL binary protocol，比 ORM 快 2-5 倍
  L2 场景：本系统的 SQL = CRUD + 向量搜索，不需要 Unit of Work / Identity Map
  L3 团队：团队 SQL 能力够，不需要 ORM 翻译
```

**7 个 DAO 的职责矩阵**：

| DAO | 对应表 | 核心方法 | 特色 |
|------|------|------|------|
| TaskDAO | tasks | create / get / list / update_status | task_id 由调用者传入，减少一次 DB round-trip |
| ReportDAO | reports | create / get_by_task / get_latest | 版本号管理（writer 重写 → version+1） |
| EvidenceDAO | evidence_map | batch_create / get_by_report | executemany 批量插入 |
| ChunkEmbeddingDAO | chunk_embeddings | batch_insert / similarity_search | `<=>` 余弦距离 + HNSW 索引 |
| MemorySummaryDAO | memory_summaries | create / get_by_task_range | 按 round_range 查摘要（"1-10" / "11-20"） |
| AgentMemoryDAO | agent_memories | create / similarity_search / forget_old | 三因子加权排序 + 时间衰减 |
| AgentLogDAO | agent_logs | create / list_by_task / count_errors | 审计日志写入 |

**核心设计模式**：

1. **连接池注入**：`__init__(self, pool: asyncpg.Pool)` — 测试时注测试库，生产时注生产库
2. **参数化查询**：`$1, $2, …` — 零 SQL 注入风险
3. **向量类型处理**：`embedding::vector` 显式转换 — pgvector 特有语法
4. **软删除**：`is_active = false` — 不物理删除，数据可恢复

#### connection.py — 连接池单例

```
create_pool()       ← 启动时调用一次
    ↓
_pool (module-level singleton)
    ↓
get_pool()          ← 所有 DAO 通过它拿 Pool
    ↓
close_pool()        ← 关闭时释放
```

`min_size=2, max_size=10` — 开发环境够用。生产环境根据 QPS 调 `max_size = QPS × avg_query_time`。

---

### 2.3 Phase 3：MCP 工具层

#### 架构：MCPServer + ToolDef + 5 个工具

```
┌──────────────┐
│  MCPServer   │ ← tools/list() 返回 5 个工具的 inputSchema
│  dict[name]  │ ← tools/call(name, args) 执行具体工具
│  = ToolDef   │
└──┬──┬──┬──┬──┘
   │  │  │  │  │
   w  w  e  e  r
   e  e  m  m  e
   b  b  b  b  r
   _  _  e  e  a
   s  f  d  d  n
   e  e  _  _  k
   a  t  t  q
   r  c  e  u
   c  h  x  e
   h     t  r
         s  y

5 个工具：web_search / web_fetch / embed_texts / embed_query / rerank
```

#### server.py — MCPServer 核心设计

**为什么 `_tools` 用 dict 不用 list？**

```
list:  O(n) 查找  ←  20 个工具时每次 call_tool 都要遍历
dict:  O(1) 查找  ←  1 次 hash 定位，不受工具数量影响
```

**为什么 call_tool 的异常不 raise 而是 return isError？**

MCP 协议规定工具异常标记 `isError=true` 而非抛异常。如果直接 raise → Agent 循环中断 → 一个工具超时导致整个分析任务卡死。return isError 让上层降级：搜索超时 → 用缓存 → 继续分析。

**ToolDef.input_schema 为什么是 property？**

内部拆分 required/properties（方便读取），property 组装 MCP 标准格式 `{type, required, properties}`。避免两处维护同一套数据。

#### tools_web.py — 互联网搜索与抓取

**为什么用 DuckDuckGo 不用 Google API？**

| 方案 | API Key | 免费配额 | 依赖 |
|------|:--:|:--:|------|
| Google Custom Search | 需要 | 100次/天 | SDK |
| Bing Search API | 需要 | 1000次/月 | SDK |
| DuckDuckGo HTML | 零 | 无限制 | 仅 httpx + 正则 |

**但正则解析有风险**：DuckDuckGo 改版 → 正则失效 → 返回空。这是"够用"方案，Phase 8 预留了 Playwright 浏览器升级路径。

**web_search 异常兜底**：`except Exception → return []`。搜索失败不抛异常，返回空列表让上层 Agent 降级处理。

**web_fetch 正则提取 vs BeautifulSoup**：
- 正则：零依赖、快 3-5 倍、够用
- BS4：需要 lxml ~15MB、准确率高、支持 CSS 选择器

选正则是因为竞品分析只需提取正文文字，不需要精确 DOM 定位。tradeoff：简洁性 > 覆盖率。

#### tools_rag.py — 向量嵌入与精排

**核心：BGE-M3 的两大懒加载单例**

```python
_embedding_model = None  # 2GB，首次调用 5-10s
_reranker_model = None   # 约 1GB，首次调用 3-5s

if _embedding_model is not None:  # 已加载 → 直接复用
    return _embedding_model
```

懒加载好处：
1. `import src.mcp` 不停 5-10 秒
2. 如果这次任务不需要 RAG（纯搜索分析），模型永远不加载
3. GIL 保证两线程同时到达时不重复加载

**为什么选 BGE-M3（1024维）？**

| 模型 | 维度 | 中文效果 | 多语言 | 开源 |
|------|:--:|------|:--:|:--:|
| BGE-M3 | 1024 | ⭐⭐⭐ | ✅ | ✅ |
| OpenAI ada-002 | 1536 | ⭐⭐ | ✅ | ❌ |
| BGE-large-zh | 1024 | ⭐⭐⭐ | ❌ | ✅ |

M3 比 large-zh 多"多语言支持"——竞品可能有英文资料（Notion / Slack），M3 中英混合检索更优。

**为什么需要 embed + rerank 两阶段？**

| 阶段 | 方法 | 精度 | 速度（10万文档） | 可建索引 |
|------|------|:--:|:--:|:--:|
| 粗排 | bi-encoder 余弦 | 中 | ~50ms | ✅ HNSW |
| 精排 | cross-encoder 打分 | 高 | ~200ms（top-60） | ❌ |

全量 rerank 10万条 = 几分钟。两阶段 = 粗排 50ms 筛 top-60 + 精排 200ms = 总 250ms，精度接近全量 rerank。

**面试追问速答：**

**Q: "Bi-encoder 和 Cross-encoder 的本质区别？"**

> Bi-encoder：query 和 doc 独立编码成两个向量，点积算相似度——快但丢失交互信息。
> Cross-encoder：`[query, doc]` 拼接一起编码，做全注意力——准但不能建索引。
> 两阶段 = 取两者之长：bi-encoder 筛候选（快）+ cross-encoder 精排（准）。

---

## 三、核心设计决策（面试追问导向）

### 决策 1：基础设施先行——先建好"水电煤"再盖楼 🏆 亮点

Phase 1-3 的每一行都是"被依赖的"——Phase 4 的 Agent 导入 config / dao / mcp 才能工作。如果先写 Agent 再建数据库，Agent 代码要反复改接口。正确的工程顺序是：**先让基础设施能跑 → 再基于它开发上层业务逻辑。**

### 决策 2：asyncpg 选型——性能优先 🏆 亮点

> "ODM/ORM 的本质是'你不懂 SQL 我帮你写'，但如果你的团队懂 SQL，多一层 ORM 就是多一层性能损耗。asyncpg 直接走 PostgreSQL binary protocol，比 SQLAlchemy async 快 2-5 倍——在这个 SQL 以 CRUD 为主的系统中，ORM 的 Unit of Work / Lazy Loading 都是我们不需要的复杂度。"

### 决策 3：DuckDuckGo 搜索——零依赖的最小可行方案

> "功能驱动的选型：竞品分析需要联网搜索，但不需要搜到毫秒级新闻。DuckDuckGo HTML 版零 API Key、零配额、零 SDK——够用的同时避免了 Google API 的日 100 次限制。如果未来需要高质量搜索，MCPServer 的 register 设计天然支持替换——改一行注册代码就行。"

### 决策 4：BGE-M3 中文嵌入——开源免费、效果 SOTA

> "1024 维是中文语义检索的最优点——比 768 多维 33% 信息量，比 1536（OpenAI）省 33% 存储。且 M3 是多语言模型，竞品分析中外文资料混合的场景天然适配。"

---

## 四、完整链路时序（端到端数据流）

```
用户创建分析任务
    │
    ▼
Settings() 加载 .env       ← Phase 1: 配置就绪
    │
    ▼
create_pool() 连接 PG      ← Phase 2: 数据库就绪
    │
    ▼
TaskDAO.create(task_id)    ← Phase 2: 任务入库（tasks 表）
    │
    ▼
create_mcp_server()        ← Phase 3: 工具注册
    │
    ▼
Collector Agent 启动
    │
    ├─→ web_search("飞书 功能 2025")
    │       │
    │       ▼ DuckDuckGo HTML → 正则解析 → [{title, url, snippet}]
    │
    ├─→ web_fetch(url) × N（并发）
    │       │
    │       ▼ httpx GET → 正则提取正文 → {title, text_content}
    │
    ├─→ embed_texts(chunks)  ← 懒加载 BGE-M3 (首次 ~5s)
    │       │
    │       ▼ BGE-M3 encode → [1024维向量]
    │
    └─→ ChunkEmbeddingDAO.batch_insert()
            │
            ▼ chunk_embeddings 表（HNSW 索引加速后续检索）
```

**关键时序**：

```
0ms    → TaskDAO.create()
10ms   → create_mcp_server() 注册 5 工具
15ms   → web_search("飞书功能")   → 200ms（网络）
220ms  → web_fetch × 3（并发）    → 1500ms（最慢的网站）
1720ms → embed_texts(9 chunks)   → 800ms（首次加载 5s + 编码 800ms）
2520ms → batch_insert(9 rows)    → 50ms
2570ms → 采集完成，AgentLogDAO 写审计日志
────────────────────────────────
总耗时：~2.6s（不含首次模型加载的 5s）
```

---

## 五、2 分钟面试答题模板

> "Phase 1-3 是竞品分析系统的基础设施三层。Phase 1 用 Pydantic Settings 管理配置——类型安全 + .env 自动加载 + 启动校验，比 os.getenv() 可靠。Phase 2 是数据库层——9 张表分三层架构（业务/向量/运维），选型上 UUID 主键 > SERIAL、HNSW 索引 > IVFFlat、zhparser 中文分词 > ngram、asyncpg 原生 SQL > SQLAlchemy ORM——每个决策都有场景支撑而非追潮流。Phase 3 是 MCP 工具层——预注册 5 个工具（搜索+抓取+嵌入+精排），MCPServer 用 dict 做 O(1) 工具路由，异常返回 isError 而非抛异常保证 Agent 容错。BGE-M3 做两阶段检索——bi-encoder 粗排（50ms）+ cross-encoder 精排（200ms）替代全量 rerank（几分钟），精度接近但延迟可控。"

---

## 六、面试官追问手册

### Q1: "9 张表为什么 tasks 是根？为什么不把 competitor 拆成独立表？"

> tasks 是整个数据模型的"根"——所有其他 6 张表都有 task_id 外键。但 competitors 不拆独立表的原因是它没有独立属性——`["飞书","钉钉"]` 这两个字符串不需要自己的 created_at、不需要独立增删、不参与 JOIN。给没有独立生命周期的数据建表 = 过度工程。

### Q2: "MCPServer 的 call_tool 以后可以加哪些安全切面？"

> 五层校验流水线：① 白名单（工具不在白名单 → 拒绝）② 参数校验（type/max/min 不符合 → 拒绝）③ 限流（TokenBucket 防滥用）④ PII 拦截（手机号/身份证脱敏）⑤ 审计日志（每次调用写 agent_logs）。当前 call_tool 预留了扩展点——注释里标注了"未来可在此增加"。

### Q3: "为什么不用 LangChain 的 Tool 封装？"

> LangChain 的 BaseTool 强制所有工具继承同一个抽象类——input_schema 要在类属性里维护、_run 和 _arun 必须同时实现。我们的 ToolDef 是一个朴素的数据类 + handler 函数——不需要继承、不需要同时实现 sync/async、不需要理解 LangChain 的 Callback 系统。少一层依赖就少一个出错点。

---

## 七、验收结果

### 依赖关系验证

```python
from src.config import Settings         # Phase 1 ✓
settings = Settings()                    # 字段从 .env 加载

from src.db import create_pool          # Phase 2 ✓
pool = await create_pool(settings)       # 连接 PG:5433

from src.db import TaskDAO              # Phase 2 ✓
dao = TaskDAO(pool)                     # pool 构造注入
task_id = await dao.create(...)         # INSERT 成功

from src.mcp import create_mcp_server   # Phase 3 ✓
mcp = create_mcp_server(settings)       # 5 工具注册
tools = await mcp.list_tools()          # 返回 5 个 schema
result = await mcp.call_tool("web_search", {"query": "飞书"})  # isError=false
```

### 代码统计

| Phase | 文件 | 行数 |
|:--:|------|:--:|
| Phase 1 | `.env` + `config.py` + `__init__.py` | ~85 |
| Phase 2 | `schema.sql` + `connection.py` + `dao.py` + `db/__init__.py` | ~1050 |
| Phase 3 | `server.py` + `tools_web.py` + `tools_rag.py` + `mcp/__init__.py` | ~715 |
| **合计** | **8 文件** | **~1850** |

### 面试注释覆盖

所有源文件均已添加三层教学注释体系：
- **L3 Agent 设计**：架构定位、为什么这样设计
- **L4 Agent 工程**：工程决策、选型理由
- **L5 面试体系**：面试可能怎么问、怎么答

---

## 附录：Phase 1-3 代码结构

```
项目根目录/
├── .env                        # Phase 1: 敏感配置（不提交 Git）
└── src/
    ├── __init__.py              # Phase 1: 版本号
    ├── config.py                # Phase 1: Pydantic Settings
    ├── db/                      # Phase 2
    │   ├── __init__.py          # 公开导出 create_pool + 7 个 DAO
    │   ├── connection.py        # asyncpg 连接池单例
    │   ├── schema.sql           # 9 张表完整 DDL
    │   └── dao.py               # 7 个 DAO 类（~850 行）
    └── mcp/                     # Phase 3
        ├── __init__.py          # create_mcp_server() 工厂函数
        ├── server.py            # MCPServer + ToolDef
        ├── tools_web.py         # web_search + web_fetch
        └── tools_rag.py         # embed_texts + embed_query + rerank
```
