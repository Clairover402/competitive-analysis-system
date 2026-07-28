# 架构债务根治 — 每任务重复初始化 → 启动时一次性构建

## 问题

`POST /api/tasks` 每次创建任务都会：
- `IntentRouter.route()` → `_setup_dependencies()` → 重建整个依赖树
- `build_pipeline_graph()` / `build_supervisor_graph()` → 编译 StateGraph
- `create_llm_client()` × 9（每次创建 9 个 HTTP Client）
- `PostgresSaver.setup()`（每次查 PG 执行 CREATE TABLE IF NOT EXISTS）
- `A2ARouter.register()` × 4（每次注册 AgentCard）

实际上注释里早已写了正确意图：
```python
# 【L5 决策】为什么 build 和 invoke 分开？
# build 是一次性操作（应用启动时执行一次），invoke 是每次请求执行。
```
但代码实现没跟上。

## 根治方案

把所有一次性构建全部拉到 FastAPI `lifespan` 启动阶段，任务创建时直接从 `app.state` 拿预编译图 → `graph.ainvoke()`。

### 改动文件：`src/api/routes.py`

1. **`lifespan` 启动时构建全部共享依赖：**
   - `create_mcp_server()` — MCP Server 全局单例
   - `build_pipeline_graph()` — Pipeline 预编译图（LLM ×4 + PostgresSaver + 记忆引擎）
   - `HarnessGuard + A2ARouter` — 注册 4 个 AgentCard + handler + 专属温度 LLM
   - `build_supervisor_graph()` — Supervisor 预编译图
   - `IntentRouter` — 纯路由决策器（只读）

2. **`_execute_task()` 重写：**
   - 旧：`router.route()` → `_setup_dependencies()` → `build_*_graph()` × 每次
   - 新：`IntentRouter.classify()`（纯函数 0ms）→ `app.state.pipeline_graph.ainvoke()` 或 `app.state.supervisor_graph.ainvoke()`
   - 不再调用 `router.route()`，不再经过 `_setup_dependencies()`

3. **删除 `_get_router()`** — 不再需要从请求上下文拿 IntentRouter

## 效果

| 指标 | 根治前 | 根治后 |
|------|--------|--------|
| LLM Client 创建 | 9次/任务 | 0次/任务（启动时 5 个，全局复用） |
| StateGraph 编译 | 2次/任务 | 0次/任务 |
| PG 建表检查 | 2次/任务 | 1次/启动 |
| AgentCard 注册 | 4次/任务 | 4次/启动 |
| 任务创建路径 | lifespan → route → _setup_deps → build → invoke | lifespan（一次性）→ invoke |

## 共享安全

- **LLM Client**：无状态 HTTP Client，实例级线程安全
- **MCP Server**：只读工具能力，不持有任务状态
- **A2ARouter**：注册表是启动时静态快照
- **CompiledStateGraph**：任务隔离靠 `config["thread_id"]`，不是图实例
