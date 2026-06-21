# AGENTS.md — 竞品分析多Agent协作系统

> 你读这个；人类读 README.md。完整开发计划在 DEVELOPMENT_PLAN.md。

## 我是谁

LangGraph 1.0 竞品分析系统。四个Agent（Collector/Analyzer/Writer/Quality）+ LLM实体提取后代码路由分流。

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
请求 → LLM实体提取(competitors/dimensions) → 代码路由
  ├── 竞品≥2且维度明确 → Pipeline: Collector→Analyzer→Writer→Quality, score<70 回退
  └── 参数不足或模糊 → Supervisor+ReAct: think→act→observe, MAX_ROUNDS=10
```

- MCP: Agent工具箱 (`src/mcp/`)
- A2A: Agent间P2P通信 (`src/supervisor/a2a.py`)
- Harness: 五层安全（白名单/参数校验/频控/PII阻断/审计） (`src/harness/`)
- 记忆: 短期(Checkpoint→PG) / 摘要(memory_summaries, 递增+10轮全量合并校准) / 长期(agent_memories+pgvector, 五步检索+时间衰减) (`src/memory/`)

## 编码规范

**必须**：全异步(no `time.sleep`/`requests`/`psycopg2`同步版) · Type hints + docstring全覆盖 · UTF-8 · `pathlib.Path` · pydantic-settings管理配置 ， 日志信息使用中文

**禁止**：创建虚拟环境 · pip install · git操作 · 硬编码密钥/密码 · 写C盘文件

**数据库**：连接池通过 `src/db/connection.py` 获取 · SQL写在DAO层 · pgvector用 `<=>` · 批量写不用逐条INSERT · 事务用 `async with conn.transaction()`

**全文检索**：zhparser（中文分词）+ pg_bigm（中文模糊匹配）。TS配置名 `zhparse`，仅索引 n/v/a/i/e/l 六种词性。查询用 `to_tsvector('zhparse', content) @@ to_tsquery('zhparse', keyword)`。模糊匹配用 `content % keyword`（pg_bigm 操作符）。Docker 镜像：`docker/Dockerfile` 预编译两个扩展。

**LangGraph**：State=TypedDict+Annotated(`operator.add`) · 节点 `async def f(state: AgentState) -> dict` · 条件边 `def f(state: AgentState) -> str` · Checkpoint=AsyncPostgresSaver，不删 · 循环控制=`remaining_steps`

## 阶段依赖

```text
0 → 1 → 3 → 4 → 4.5 → 6 → 7
  ↘ 2 ↗        ↘             
       5A → 5B ↗
```

Phase 1∥2 · 4∥5A · 红线：不跳依赖、不跨阶段并行互依赖组件。

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

## 记忆系统要点

长期记忆检索SQL（一次完成，不返Python二次排序）：
```sql
ORDER BY (1.0-(embedding<=>$1))*importance*POWER(0.5,EXTRACT(DAY FROM NOW()-created_at)/half_life_days) DESC
```

摘要Prompt必须限≤500字 · 所有记忆操作带 `user_id` · 软删除，不物理删

## 项目约束

根目录 `D:\AAAagent\projects\competitive-analysis-system\` · Windows PowerShell · 临时脚本用完即删


每一阶段都要我验收成功后才可以开始下一个Phase!!!
