# AI驱动的竞品分析多Agent协作系统

> **Competitive Analysis Multi-Agent System**  
> 四个专职Agent + Supervisor双模架构，Pipeline确定性执行 + ReAct自适应探索

[![状态](https://img.shields.io/badge/状态-开发中-orange)](./DEVELOPMENT_PLAN.md)
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
                   ┌────────▼────────┐
                   │  IntentRouter   │  ← LLM实体提取 → 代码路由决策
                   └───┬────────┬────┘
                       │        │
           竞品列表明确  │        │  开放性探索
           (~80% 流量)  │        │  (~20% 流量)
                       │        │
              ┌────────▼───┐ ┌──▼──────────────┐
              │  Pipeline  │ │ Supervisor+ReAct │
              │            │ │                  │
              │ Collector  │ │ _think()→_act()  │
              │   ↓        │ │ →_observe()      │
              │ Analyzer   │ │                  │
              │   ↓        │ │ A2A 通信         │
              │ Writer     │ │ MCP 工具调用     │
              │   ↓        │ │ Harness 安全壳   │
              │ Quality    │ │                  │
              │   ↓        │ │ MAX_ROUNDS=10    │
              │ score≥70?  │ │                  │
              │ YES→完成    │ │                  │
              │ NO→回退重写  │ │                  │
              └────────────┘ └──────────────────┘
```

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
| **摘要记忆** | `memory_summaries` 表 | 跨轮压缩要点，递增+全量合并校准 |
| **长期记忆** | `agent_memories` 表 + pgvector | 用户偏好、历史决策、领域知识（时间衰减） |

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
# 提交竞品分析任务
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "competitors": ["飞书", "钉钉", "企业微信"],
    "dimensions": ["定价", "功能", "用户体验", "生态", "市场份额"]
  }'

# SSE 实时查看进度
curl -N http://localhost:8000/api/analyze/{task_id}/stream
```

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
│   ├── supervisor/             # Supervisor + A2A + IntentRouter
│   ├── memory/                 # 记忆系统（短期/摘要/长期/冲突/遗忘）
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
| Phase 0 | 项目脚手架 + 配置 | ⬜ 待开发 |
| Phase 1 | 数据库 Schema + DAO | ⬜ 待开发 |
| Phase 2 | MCP 工具层 | ⬜ 待开发 |
| Phase 3 | Agent 实现 | ⬜ 待开发 |
| Phase 4 | Pipeline 编排 | ⬜ 待开发 |
| Phase 4.5 | 记忆系统 | ⬜ 待开发 |
| Phase 5A | Supervisor + A2A | ⬜ 待开发 |
| Phase 5B | IntentRouter + Harness | ⬜ 待开发 |
| Phase 6 | 服务化 + 可观测性 | ⬜ 待开发 |
| Phase 7 | 评估体系 + 集成测试 | ⬜ 待开发 |

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

- **双模架构**：LLM 提取实体 → 代码路由决策——确定性任务 80% 走 Pipeline（快、稳、可预期），开放性探索 20% 走 Supervisor+ReAct（灵活、自适应）
- **LLM提取实体+代码路由**：LLM 负责语义理解和实体提取（competitors/dimensions），代码根据结构化规则判断路径。路由规则变了不动 prompt，改一行代码上线
- **PostgreSQL 一体存储**：业务数据、向量、Checkpoint、记忆、日志全部存 PostgreSQL，不引入 Redis/ES/向量数据库
- **质检回退循环**：Writer → Quality，score<70 时回退重写，直到通过或达到重试上限
- **全链路异步**：FastAPI + asyncpg + httpx，无同步阻塞
- **三层限流**：TokenBucket (100QPS) → Semaphore(3并发) → AsyncLimiter (60 RPM)
- **记忆双层校准**：递增摘要每轮丢失~2%细节，每10轮全量合并一次恢复保真度至95%+
- **软删除优先**：记忆冲突解决和遗忘策略全部软删除，不物理删除数据

---

## License

MIT
