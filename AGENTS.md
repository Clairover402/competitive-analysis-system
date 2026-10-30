# AGENTS.md — 竞品分析多Agent协作系统

> 你读这个；人类读 README.md。完整开发计划在 DEVELOPMENT_PLAN.md。

## 我是谁

LangGraph 1.0 竞品分析系统。四个Agent（Collector/Analyzer/Writer/Quality）+ 双入口：结构化 API 直接给参数，对话入口靠意图漏斗分流。

## 必须先读

1. `DEVELOPMENT_PLAN.md` — 当前Phase的完整提示词
2. `pyproject.toml` / `src/db/schema.sql` — 如果已存在，读它而非猜测

## 技术栈速查

| 组件 | 选型 |
|------|------|
| 编排 | LangGraph 1.0（StateGraph + AsyncPostgresSaver） |
| DB | PostgreSQL + pgvector + asyncpg |
| 嵌入 | BGE-M3 1024维（MCP embed工具调用） |
| LLM | DeepSeek Chat API（`deepseek-v4-flash`，60RPM） |
| Web | FastAPI + SSE + uvicorn |
| 异步 | asyncio（Python 3.13，全链路 async/await） |

## 架构

```
请求进入
  ├── POST /api/tasks   竞品+维度已给 ────┐
  └── POST /api/chat    一句话 → 意图漏斗 ┤（只有 analysis 出口并入下行）
                                          ▼
                     IntentRouter.classify(llm_parsed)   ← 零 LLM，只判"参数够不够启动"
                       ├── 参数齐全   → Pipeline: Collector→Analyzer→Writer→Quality, score<70 回退
                       └── 参数不足/模糊 → Supervisor+ReAct: think→act→observe, MAX_ROUNDS=10
```

- MCP: Agent工具箱 (`src/mcp/`)
- A2A: Agent间P2P通信 (`src/supervisor/a2a.py`)
- Harness: 五层安全（白名单/参数校验/频控/PII阻断/审计） (`src/harness/`)
- 意图漏斗: L2向量→L3 LLM→L4澄清 (`src/supervisor/intent/`)，详见下面「意图识别要点」
- 记忆: 短期(Checkpoint→PG) / 摘要(memory_summaries, 递增+10轮全量合并校准) / 语义事实(agent_memories+pgvector, **只写不读**) / 偏好(preferences, inject 键进 writer prompt) (`src/memory/`)

## 编码规范

**必须**：全异步(no `time.sleep`/`requests`/`psycopg2`同步版) · Type hints + docstring全覆盖 · UTF-8 · `pathlib.Path` · pydantic-settings管理配置 ， 日志信息使用中文

**禁止**：创建虚拟环境 · pip install · git操作 · 硬编码密钥/密码 · 写C盘文件

**改文件**：用 `apply_patch` 工具，不要通过 shell 执行 apply_patch（Codex 会往历史里插一条 warning，DeepSeek 直接 400 `No tool output found for tool call`，整条会话作废）

**数据库**：连接池通过 `src/db/connection.py` 获取 · SQL写在DAO层 · pgvector用 `<=>` · 批量写不用逐条INSERT · 事务用 `async with conn.transaction()`

**全文检索**：zhparser（中文分词）+ pg_bigm（中文模糊匹配）。TS配置名 `zhparse`，仅索引 n/v/a/i/e/l 六种词性。查询用 `to_tsvector('zhparse', content) @@ to_tsquery('zhparse', keyword)`。模糊匹配用 `content % keyword`（pg_bigm 操作符）。Docker 镜像：`docker/Dockerfile` 预编译两个扩展。

**LangGraph**：State=TypedDict+Annotated(`operator.add`) · 节点 `async def f(state: AgentState) -> dict` · 条件边 `def f(state: AgentState) -> str` · Checkpoint=AsyncPostgresSaver，不删 · 循环控制=`remaining_steps`

## 阶段依赖

```text
0 → 1 → 3 → 4 → 4.5 → 6 → 7
  ↘ 2 ↗        ↘             
       5A → 5B ↗
```

Phase 3∥2 · 4∥5A · 红线：不跳依赖、不跨阶段并行互依赖组件。

每个Phase开始前：读当前Phase提示词 → 确认前置交付物+验收通过 → 读 dao.py/mcp/server.py 确认签名。

## 常见坑

```powershell
# Python路径（注意 Program Files 空格反引号转义）
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
D:\AAAProgram` Files\Python\python\python.exe script.py
```

**DeepSeek API**：`competitive-analysis-system-key` · tool_calls 1.0格式 `tc["name"]`/`tc["args"]`（非 `tc["function"]["name"]`）

**BGE-M3**：首次调用懒加载~2GB到 `~/.cache/huggingface/`，MCP Server启动时预热。

**Checkpoint**：`AsyncPostgresSaver.from_conn_string(url)` 后必须 `await checkpointer.setup()`。

**pgvector**：`CREATE EXTENSION IF NOT EXISTS vector;`

## 意图识别要点

`src/supervisor/intent/`：L2 向量最近邻 → L3 LLM → L4 澄清反问。**没有 L1**（2026-10-29 删）。

- ★ **L2 是最近邻，删一个类别会把它的话术「吸」给邻居**：删 verify 时必须把它的原型句
  降级成 `UNSUPPORTED_EXEMPLARS`（拒绝类，仍参与排名但返回 unknown + 专用说明），
  否则「这结论还能信吗」会被 trace 接走、答成"引用了哪些网站"——不报错，只答非所问。
- ⚠️ 删类别时**同步删 `clarify.CLARIFY_CHOICES` 里的选项**，否则用户点完选项下一轮
  又判回 unknown，死循环反问。
- **两个 `classify` 不是一回事**：`supervisor/router.IntentRouter.classify(llm_parsed)`
  判「参数够不够启动分析」（零 LLM，`/api/tasks` 与 `/api/chat` 都走）；
  `intent/classifier.classify()` 判「问题属于哪一类」（漏斗）。别合并。
- **槽位**：`fill_slots` 按 label 抽，值池匹配优先、LLM 只兜底。闸门方向相反——
  读取侧（lookup/trace）池外 `gate_llm` 丢弃，写入侧（analysis）`normalize_to_pool` 只改名不丢弃。
- 没有 keyword 槽位（2026-10-30 删）：它曾拿维度名去 `claim ILIKE` 正文，漏 11 条真定价。
  维度走 `e.dimension = ANY($3)` 精确列。

## 记忆系统要点

**★ 语义事实（`agent_memories`）只写不读**：verify 下线后读取路径（`retrieve` /
`_effective_ttl`）一并删除，零调用方；写入仍在跑（`ttl_days_for` 定档 + 同 subject
余弦 ≥0.90 取代）。看到这张表别以为有检索能力——**列还在 ≠ 能力还在**。

摘要Prompt必须限≤500字 · 所有记忆操作带 `user_id` · 软删除，不物理删

## 项目约束

根目录 `D:\AAAagent\projects\competitive-analysis-system\` · Windows PowerShell · 临时脚本用完即删


每一阶段都要我验收成功后才可以开始下一个Phase!!!
