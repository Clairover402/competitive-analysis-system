# Phase 4 验收报告 — Pipeline 编排

**验收时间**: 2026-06-22
**验收人**: AI 工程师
**验收结论**: ⚠️ 有条件通过（1 个 Critical Bug，1 个 Schema 不一致）

---

## 1. 交付物清单

| # | 交付物 | 路径 | 状态 |
|---|--------|------|------|
| 1 | AgentState TypedDict | `src/pipeline/state.py` | ✅ |
| 2 | LangGraph 图定义 | `src/pipeline/graph.py` | ⚠️ |
| 3 | PostgreSQL Checkpoint 存储 | `src/pipeline/checkpoint.py` | ⚠️ |
| 4 | 模块导出 | `src/pipeline/__init__.py` | ✅ |

---

## 2. 验收标准逐项检查

### 2.1 StateGraph 可以编译不报错 ✅
- AST 解析：4 个文件全部通过
- import 验证：`AgentState`、`PostgresSaver`、`build_pipeline_graph`、`run_pipeline_task` 全部导入成功
- __all__ 导出：`['AgentState', 'build_pipeline_graph', 'run_pipeline_task', 'PostgresSaver']`

### 2.2 节点函数签名统一 ✅
```
async def node_collect(state: AgentState) -> dict
async def node_analyze(state: AgentState) -> dict
async def node_write(state: AgentState) -> dict
async def node_quality(state: AgentState) -> dict
async def node_finalize(state: AgentState) -> dict
```
5 个节点签名完全一致，通过闭包工厂函数 `_make_node_xxx()` 注入依赖。

### 2.3 条件边逻辑 ✅（路由决策正确）
```
route_after_quality:
  quality_passed=True         → "finalize"
  quality_passed=False, rem>0 → "write"
  quality_passed=False, rem≤0 → "finalize" (force)
```
路由逻辑本身无缺陷。

### 2.4 PostgresSaver 实现了核心方法 ✅
- `aget_tuple`: 支持指定 checkpoint_id 查询 + 最新 checkpoint 查询，含 pending writes 读取
- `aput`: INSERT ON CONFLICT UPDATE，写入 checkpoint_id/parent_checkpoint_id/metadata
- `aput_writes`: prepared statement 批量写入，带 idx 枚举
- `setup`: CREATE TABLE IF NOT EXISTS（幂等建表）

### 2.5 graph.ainvoke() 支持 thread_id ✅
```python
config = {"configurable": {"thread_id": task["id"]}}
final_state = await graph.ainvoke(initial_state, config)
```

---

## 3. 发现问题

### 🔴 Critical: remaining_steps 死循环 Bug

**位置**: `src/pipeline/graph.py`，`_make_node_collect` 函数

**问题**: `remaining_steps` 只在 `node_collect` 中减 1，write→quality→write 循环中永远不减。

**复现路径**:
```
Initial:  remaining_steps = 3
collect:  remaining_steps = 2  (唯一一次递减)
analyze:  remaining_steps = 2
write:    remaining_steps = 2  ← 永远不减！
quality:  score=58, passed=False
route:    rem=2>0 → "write"
write:    remaining_steps = 2  ← 还是 2
quality:  rem=2
route:    rem=2>0 → "write"
... 无限循环
```

**spec 预期**: "remaining_steps 初始值 = 3（最多质检→重写→质检 3 轮），每经一轮 remaining_steps -= 1"

**修复方案**:
```python
# graph.py, _make_node_write 的 return 中增加:
return {
    "report_content": ...,
    "report_version": ...,
    "remaining_steps": state["remaining_steps"] - 1,  # ← 加这一行
}
```
同时从 `_make_node_collect` 中移除 `remaining_steps` 递减，避免额外消耗一轮。

**修复后执行轨迹**:
```
Initial:  rem=3
collect:  rem=3
analyze:  rem=3
write:    rem=2  (第1次写入, v=1)
quality:  fail → back to write
write:    rem=1  (第2次写入/重写1, v=2)
quality:  fail → back to write
write:    rem=0  (第3次写入/重写2, v=3)
quality:  fail → rem=0 → force finalize
```
正好 3 轮重写。

### 🟡 Warning: checkpoint.py setup() 与 schema.sql 表结构不一致

**问题**: `checkpoint.py` 的 `setup()` 方法创建的表包含 `schema.sql` 中不存在的列。

| 表 | checkpoint.py 有 | schema.sql 无 |
|----|-----------------|---------------|
| checkpoints | `type TEXT` | ❌ 缺失 |
| checkpoints | `metadata JSONB` | ❌ 缺失 |
| checkpoint_writes | `channel TEXT` | ❌ 缺失 |
| checkpoint_writes | `type TEXT` | ❌ 缺失 |
| checkpoint_writes | `value JSONB` | ❌ 缺失 |

**风险**: 如果 `schema.sql` 先执行建表，`setup()` 的 `CREATE TABLE IF NOT EXISTS` 不会修改已存在的表结构，后续 `aput`/`aput_writes` 写入 `type`/`metadata`/`channel`/`value` 列时报 SQL 错误。

**修复方案**: 将 `checkpoint.py setup()` 中的完整列清单同步到 `schema.sql` 第 8-9 节。

---

## 4. AgentState 字段审查（16 字段）

| 分类 | 字段 | 类型 | 用途 |
|------|------|------|------|
| 任务元信息 | `task_id` | str | 任务唯一 ID |
| | `title` | str | 任务标题 |
| | `competitors` | list[str] | 竞品列表 |
| | `dimensions` | list[str] | 分析维度 |
| | `pipeline_mode` | str | 固定 "pipeline" |
| Collector | `collected_data` | dict | 采集结果 |
| Analyzer | `analysis_results` | dict | 分析结果 |
| Writer | `report_content` | str | Markdown 报告 |
| | `report_version` | int | 重写版本号 |
| Quality | `quality_score` | float | 总分 |
| | `quality_details` | dict | 五维评分 |
| | `quality_passed` | bool | 是否通过 |
| | `rewrite_suggestions` | list[str] | 修改建议 |
| 控制 | `messages` | Annotated[list, add_messages] | 消息历史 |
| | `remaining_steps` | int | 防死循环 |
| | `final_report` | str | 最终输出 |

---

## 5. 架构审查

### 温度差异方案 ✅
通过闭包注入，4 个 LLM 实例不进 State：
```python
llm_collector = create_llm_client(settings, temperature=0.3)
llm_analyzer  = create_llm_client(settings, temperature=0.1)
llm_writer    = create_llm_client(settings, temperature=0.3)
llm_quality   = create_llm_client(settings, temperature=0.0)
```

### PostgresSaver 连接池复用 ✅
`build_pipeline_graph` 接收 `pool` 参数，直传 `PostgresSaver(pool)`，零包装。

### 图结构 ✅
```
collect → analyze → write → quality
                              ↓
                     route_after_quality
                       ↙        ↘
                   finalize     write (loop)
```

---

## 6. 不属于 Bug 但值得注意

- **First-write 也算一轮**: 如果 remaining_steps 只在 write 中递减，第一次 write 就消耗 1 步，实际最多 2 轮重写。建议初始值设为 4 或第一次不计入。**按 spec 要求 3 轮重写的话，移除 collect 递减后初始值 = 3 正好（第一次 write→quality 不计入回合）。**

- **node_finalize 用 TaskDAO.update_status**: `TaskDAO.update_status` 方法存在（dao.py L106），无问题。

- **checkpoint.py 中的 type 列值**: `aput` 硬编码写入 `"checkpoint"`，这在 LangGraph 内部用于区分 checkpoint 类型。BaseCheckpointSaver 本身携带 `serde` 序列化器，未被显式使用（JSONB 用 `json.dumps(default=str)` 兜底），当前够用。

---

## 7. 总结

| 维度 | 结果 |
|------|------|
| 文件完整性 | ✅ 4/4 |
| AST 解析 | ✅ 4/4 |
| import 验证 | ✅ |
| 节点签名 | ✅ 5/5 统一 |
| 路由逻辑 | ✅ |
| Checkpoint 方法 | ✅ aget_tuple+aput+aput_writes+setup |
| remaining_steps Bug | 🔴 1 个必修复 |
| Schema 不一致 | 🟡 1 个需同步 |
| **总评** | **⚠️ 修复 Bug 后通过** |
