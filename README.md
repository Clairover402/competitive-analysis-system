# AI驱动的竞品分析多Agent协作系统

> **Competitive Analysis Multi-Agent System**  
> 四个专职Agent + Supervisor双模架构，Pipeline确定性执行 + ReAct自适应探索  
> 双入口：结构化 API 直接给参数；对话入口一句话，由意图漏斗判断是分析、查历史还是查证据

[![状态](https://img.shields.io/badge/状态-已完成-brightgreen)](./DEVELOPMENT_PLAN.md)
[![Python](https://img.shields.io/badge/Python-3.13-blue)](https://www.python.org/)
[![框架](https://img.shields.io/badge/LangGraph-1.0-green)](https://langchain-ai.github.io/langgraph/)
[![数据库](https://img.shields.io/badge/PostgreSQL-pgvector-336791)](https://github.com/pgvector/pgvector)

---

## 项目简介

本系统利用多个AI Agent协作完成竞品分析任务——从竞品信息采集、多维度分析、结构化撰写到质量评审，全链路自动化。

**核心场景**：输入竞品名称列表 → 系统自动采集、分析、撰写 → 输出结构化竞品分析报告（含质检打分）。

### 为什么用多Agent？

单Agent面对竞品分析任务有三大硬伤：
- **上下文不够**：5个竞品 × 5个维度 = 大量上下文，单Agent处理不过来
- **职责耦合**：采集、分析、撰写、质检混在一个提示词里，拆东墙补西墙
- **无法自纠**：写完不能自我评审，质量无保障

多Agent拆分后，每个Agent只干一件事，通过Pipeline串联或Supervisor动态调度，质量可闭环。

---

## 系统架构

```
                       请求进入
                          │
        ┌─────────────────┴──────────────────┐
        │                                    │
  POST /api/tasks                     POST /api/chat
  竞品 + 维度（参数明确）               一句话（自由文本）
        │                                    │
        │                          ┌─────────▼──────────┐
        │                          │      意图漏斗       │
        │                          │ L2 向量最近邻       │
        │                          │ L3 LLM（仅兜底）    │
        │                          │ L4 澄清反问         │
        │                          └────┬───────────────┘
        │                               │ 四个出口：
        │                               │  analysis / lookup / trace / 拒绝类
        │                               │  （只有 analysis 往下走，其余当场作答）
        ▼                               │
  ┌─────────────────┐                   │
  │  IntentRouter   │ ◄─────────────────┘
  │    （零 LLM）    │
  │ 只判：参数够不够？│
  └──┬───────────┬──┘
     │           │
  参数齐全    参数不足
     │           │
┌────▼─────┐ ┌───▼──────────────┐
│ Pipeline │ │ Supervisor+ReAct │
│Collector │ │ _think→_act→     │
│    ↓     │ │ _observe         │
│Analyzer  │ │ A2A 通信         │
│    ↓     │ │ MCP 工具调用     │
│ Writer   │ │ Harness 安全壳   │
│    ↓     │ │ MAX_ROUNDS=10    │
│ Quality  │ └──────────────────┘
│ score≥70?│
│ NO→重写   │
└──────────┘
```

意图漏斗本身是三层，且**只在 L2 判不准时才花钱**：

```
一句话
  │
  ▼
┌──────────────────────────────────────────────────┐
│ L2  向量最近邻（零 LLM，接管大部分明确问法）        │
└──┬───────────────────────────────────────────┬───┘
   │ 命中且 top1-top2 差距够大                   │ 差距太小 / 类别不明
   │                                            ▼
   │                                  ┌──────────────────┐
   │                                  │ L3  LLM 分类      │
   │                                  └──┬───────────────┘
   │                                     │ 仍判不出
   │                                     ▼
   │                            L4 澄清反问（给可点选项）
   ▼
analysis / lookup / trace / 拒绝类
```

> 没有 L1。历史上第一层是"正则/关键词"兜底，2026-10-29 删除——
> 它的输入是"参数够不够启动分析"，而它要回答的是"这是哪一类问题"，
> 参数是分析对象、不是意图，所以那层在生产路径上永不成立。

### 三层架构

| 层 | 职责 | 技术 |
|----|------|------|
| **MCP** (工具层) | Agent 的能力工具箱 | web_search, web_fetch, embed, rerank |
| **A2A** (通信层) | Agent 间 P2P 通信协议 | AgentCard + Task 生命周期 |
| **Harness** (安全壳) | 五层安全检查 | 白名单/参数校验/频控/PII阻断/审计 |

### 记忆系统

| 层 | 存储 | 用途 |
|----|------|------|
| **短期记忆** | LangGraph Checkpoint → PostgreSQL | 当前会话完整对话历史 |
| **摘要记忆** | `memory_summaries` 表 | 跨轮压缩要点，递增 + 每 10 轮全量合并校准 |
| **语义事实** | `agent_memories` 表 + pgvector | 从报告抽出的结论：观测时间、来源 URL、取代链、保鲜期档位 |
| **用户偏好** | `preferences` 表 | `inject=True` 的键注入 writer prompt |

> 语义事实目前**只写不读**。唯一需要它的意图（"这条数据现在还准吗"）已作为
> 不提供的产品能力下线，读取口随之删除；写入仍在跑，因为它是以后做时效判定的原料。
> 判据记在代码里：**列还在 ≠ 能力还在**——`SELECT ttl_days` 拿到的只是写入那刻的快照。

---

## 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| 编排框架 | LangGraph 1.0 | StateGraph + Checkpoint 持久化 |
| 数据库 | PostgreSQL + pgvector | 一体化存储（业务数据 + 向量 + Checkpoint + 记忆） |
| 嵌入模型 | BGE-M3 | 1024维，~2GB懒加载 |
| 精排模型 | BGE-reranker-v2-m3 | 检索后重排序 |
| LLM | DeepSeek Chat API | 通用对话与推理 |
| Web框架 | FastAPI + SSE | 异步REST API + 流式推送 |
| 异步驱动 | asyncpg, httpx | 全链路异步 |
| Python | 3.13 | 原生协程 asyncio |

---

## 快速开始

### 环境要求

- Python 3.13+
- PostgreSQL 15+（需安装 pgvector 扩展）
- DeepSeek API Key

### 安装

```bash
# 1. 克隆/进入项目
cd D:\AAAagent\projects\competitive-analysis-system\

# 2. 安装依赖
pip install -e .

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env：填入 DEEPSEEK_API_KEY、DATABASE_URL 等

# 4. 初始化数据库
psql -U postgres -f src/db/schema.sql

# 5. 启动服务
python -m src.main
```

### 使用示例

```bash
# ① 结构化入口：直接给竞品与维度，返回 202 + task_id
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "competitors": ["飞书", "钉钉", "企业微信"],
    "dimensions": ["定价", "功能", "用户体验", "生态"]
  }'

# ② 对话入口：一句话，由意图漏斗决定要做什么
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "对比一下飞书和钉钉的定价"}'
#   → analysis：复用 ① 的整条链路（已抽好的槽位用 prefill 带进去，不再重复调模型）
#   → lookup / trace：直接返回结果
#   → 意图不明：返回 type=clarify，带可点选项

# ③ SSE 实时查看进度
curl -N http://localhost:8000/api/tasks/{task_id}/stream
```

> **规模上限：单次任务最多 4 个竞品 × 4 个维度**（`MAX_COMPETITORS` / `MAX_DIMENSIONS`，
> 见 `src/config.py`）。超出时返回 400，并在提示里写明「实际几个 / 上限几个」；
> **两条入口都校验**——显式参数查一次，对话抽出来的参数再查一次。
> 不是成本考虑——检索是单任务混池取 60 条 chunk，竞品一多，每格分到的配额被摊薄，
> 会出现某竞品在某维度整块「数据不足」，而最终报告看起来仍是完整的。
> 调上限要同步调大 `VECTOR_SEARCH_TOP_K`（每竞品至少留 15 条）。

---

## 项目结构

```
competitive-analysis-system/
├── README.md                   # 本文件
├── AGENTS.md                   # AI Agent 开发指南
├── DEVELOPMENT_PLAN.md         # 阶段开发计划（Codex 提示词）
├── pyproject.toml
├── .env.example
│
├── src/
│   ├── config.py               # 配置管理
│   ├── db/                     # 数据库（schema / 连接池 / DAO）
│   ├── mcp/                    # MCP 工具层（web_search, web_fetch, embed, rerank）
│   ├── agents/                 # 4 个专职 Agent + System Prompts
│   ├── pipeline/               # Pipeline 编排（StateGraph + Checkpoint）
│   ├── supervisor/             # Supervisor + A2A + IntentRouter + intent/（意图漏斗）
│   ├── memory/                 # 记忆系统（短期/摘要/语义事实/偏好/冲突/遗忘）
│   ├── harness/                # 安全壳（五层检查 + 审计）
│   ├── api/                    # FastAPI 路由 + SSE + 限流
│   ├── observability/          # 日志 + Prometheus 指标
│   └── evaluation/             # Golden Dataset + RAGAS + LLM-as-Judge
│
└── tests/                      # 单元测试 + 集成测试 + E2E
```

---

## 开发阶段

| 阶段 | 名称 | 状态 |
|------|------|------|
| Phase 1 | 项目脚手架 + 配置 | ✅ 已完成 |
| Phase 2 | 数据库 Schema + DAO | ✅ 已完成 |
| Phase 3 | MCP 工具层 | ✅ 已完成 |
| Phase 4 | Agent 实现 | ✅ 已完成 |
| Phase 5 | Pipeline 编排 | ✅ 已完成 |
| Phase 6 | 记忆系统 | ✅ 已完成 |
| Phase 7 | Supervisor + A2A | ✅ 已完成 |
| Phase 8 | IntentRouter + Harness | ✅ 已完成 |
| Phase 9 | 服务化 + 可观测性 | ✅ 已完成 |
| Phase 10 | 评估体系 + 集成测试 | ✅ 已完成 |

> 详细开发计划见 [DEVELOPMENT_PLAN.md](./DEVELOPMENT_PLAN.md)

---

## 四大Agent

| Agent | 职责 | 输入 | 输出 |
|-------|------|------|------|
| **Collector** | 采集竞品原始信息 | 竞品名称列表 | 结构化原始数据 |
| **Analyzer** | 多维度深度分析 | 原始数据 + 维度定义 | 分析结果（含引用来源） |
| **Writer** | 撰写结构化报告 | 分析结果 + 模板 | Markdown 报告 |
| **Quality** | 质检打分 | 报告 + 评分标准 | 分数 + 修改建议 |

---

## 关键设计决策

- **双模架构**：确定性任务走 Pipeline（快、稳、可预期），开放性探索走 Supervisor+ReAct（灵活、自适应）。分流的判据是"参数够不够启动"，由 `IntentRouter` 零 LLM 判断——**"够不够启动"是能算出来的，不该再付一次模型调用**
- **推断与匹配分开**：`/api/chat` 要判断"这是哪一类问题"（只能推断，走向量最近邻 + LLM 兜底）；`/api/tasks` 收到的请求参数已经齐了（无需判断）。所以 `IntentRouter` 不是意图识别，是参数校验——两者共用一个类，判的却是两件事
- **参数抽取以值池为锚**：竞品与维度先拿库里的真实取值做匹配（零 LLM），词表覆盖不了的才交给 LLM 兜底。读取侧（查历史 / 查证据）池外一律丢弃——编造的名字本来就查不到；写入侧（发起分析）池外必须放行——用户比库更早知道新竞品
- **"不提供"要明说，不能装作答了**：核实数据时效（"这价格现在还准吗"）不是本系统能力，这类问题会收到明确说明 + 指到证据溯源，而不是拿一份旧报告冒充答案
- **PostgreSQL 一体存储**：业务数据、向量、Checkpoint、记忆、日志全部存 PostgreSQL，不引入 Redis/ES/向量数据库
- **质检回退循环**：Writer → Quality，score<70 时回退重写，直到通过或达到重试上限
- **全链路异步**：FastAPI + asyncpg + httpx，无同步阻塞
- **三层限流**：TokenBucket (100QPS) → Semaphore(3并发) → AsyncLimiter (60 RPM)
- **记忆双层校准**：递增摘要每轮丢失~2%细节，每10轮全量合并一次恢复保真度至95%+
- **软删除优先**：记忆冲突解决和遗忘策略全部软删除，不物理删除数据

---

## License

MIT
