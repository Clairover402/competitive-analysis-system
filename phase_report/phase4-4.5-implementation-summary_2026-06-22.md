# Phase 4 & 4.5 实现总结 — Pipeline 编排 + 记忆系统

**时间**: 2026-06-22
**作者**: AI 工程师
**范围**: Phase 4（src/pipeline/）4 文件 + Phase 4.5（src/memory/）6 文件

---

## 一、Phase 4 & 4.5 是什么？

Phase 4 用 LangGraph StateGraph 把四个 Agent 串联成自动化流水线，Phase 4.5 在流水线上增加了三层记忆体系。两者一起构成竞品分析系统的**编排骨架 + 记忆底座**。

```
Phase 4 Pipeline 编排                          Phase 4.5 记忆系统注入点
═════════════════════════                       ════════════════════════════

Collector → Analyzer → Writer → Quality ─┐        ┌─ analyze 节点注入记忆上下文
   ■          ★ ④       ■       ■ ②      │        │    (LongTermMemoryEngine.retrieve)
          检索长期记忆   写入摘要  评分    │        │
                        (跳过)           │        ├─ write 节点后无摘要钩子
                                     ┌───┘        │    (Summarizer 留给 Phase 5A)
                                     │            │
                                     ▼            └─ finalize 节点提取关键决策
                               score≥70?  YES        (LLM → add_memory → agent_memories)
                                     │  NO
                                     ▼
                               Writer 重写（≤2 次）
                                     │
                                     ▼
                               remaining_steps≤0?  → 强制 finalize
```

**交付物总览**:

| 阶段 | 文件数 | 核心模块 | 行数 |
|------|:--:|------|------|
| Phase 4 | 4 | State 定义 + 5 节点 StateGraph + PostgresSaver + 条件路由 | ~700 |
| Phase 4.5 | 6 | 长期记忆引擎 + 摘要记忆 + 冲突解决 + 遗忘策略 + 检索触发 + 包入口 | ~645 |
| **合计** | **10** | | **~1345** |

---

## 二、核心模块详解

### 2.1 Phase 4: Pipeline 编排

#### `state.py` — AgentState 共享状态（17 字段）

```
┌─────────────────────────────────────────────────────────────┐
│                    AgentState (TypedDict)                    │
├─────────────────┬─────────────────┬─────────────────────────┤
│ 任务元信息        │ 各 Agent 产出     │ 控制字段                │
│ task_id          │ collected_data   │ messages (Annotated)    │
│ user_id          │ analysis_results │ remaining_steps         │
│ title            │ report_content   │ final_report            │
│ competitors      │ report_version   │                         │
│ dimensions       │ quality_score    │                         │
│ pipeline_mode    │ quality_details  │                         │
│                  │ quality_passed   │                         │
│                  │ rewrite_suggest..│                         │
│                  │ retrieved_mem..  │                         │
└─────────────────┴─────────────────┴─────────────────────────┘
```

**核心设计**:
- `TypedDict` 而非 Pydantic——LangGraph 一等公民，零运行时开销
- `messages: Annotated[list, add_messages]`——累加 reducer，同名 ID 覆盖去重
- 其他字段默认覆盖语义——"以最新状态为准"
- 17 个字段中有 1 张读写矩阵表（谁写谁读），用于状态溯源

#### `graph.py` — StateGraph 5 节点编排

```python
# 核心 API 调用链
graph = StateGraph(AgentState)
graph.add_node("collect",  _make_node_collect(mcp_server, llm_0_3))
graph.add_node("analyze",  _make_node_analyze(mcp_server, llm_0_1, ltm_engine))
graph.add_node("write",    _make_node_write(mcp_server, llm_0_3))
graph.add_node("quality",  _make_node_quality(mcp_server, llm_0_0))
graph.add_node("finalize", _make_node_finalize(pool, ltm_engine, llm))

graph.set_entry_point("collect")
graph.add_edge("collect", "analyze")
graph.add_edge("analyze", "write")
graph.add_edge("write", "quality")
graph.add_conditional_edges("quality", route_after_quality, {
    "write": "write",
    "finalize": "finalize"
})

graph.add_edge("finalize", END)
compiled = graph.compile(checkpointer=PostgresSaver(pool))
```

**关键决策**:

| 决策 | 方案 | 原因 |
|------|------|------|
| Temperature | 4 个不同温度实例，`build_pipeline_graph` 时闭包创建 | 不进 State/Checkpoint，避免序列化问题 |
| `remaining_steps` 递减 | 在 `write` 节点递减，不在 `collect` | 递减必须在 write→quality 循环内，否则死循环 |
| `route_after_quality` | 条件边函数只读不写 | LangGraph 约束——条件边不能修改状态 |
| LLM 实例创建 | `_make_node_xxx` 工厂函数持有 llm 引用 | 每次节点执行复用同一实例，不在函数体内重复 new |

#### `checkpoint.py` — PostgresSaver 持久化

```python
class PostgresSaver(BaseCheckpointSaver):
    async def aget_tuple(config) -> Optional[CheckpointTuple]
    async def aput(config, checkpoint, metadata, new_versions)
    async def aput_writes(config, writes, task_id)
    async def setup()  # 建表（幂等 ON CONFLICT DO NOTHING）
```

**核心价值**: 任务中断后从 checkpoint 恢复，不用从头重跑。`thread_id = task_id` 保证任务隔离。

---

### 2.2 Phase 4.5: 记忆系统

#### `long_term.py` — LongTermMemoryEngine（五步检索引擎）

```
query ──→ Step 1: LLM 重写（泛化 + 去时间）
        → Step 2: 混合检索 ┬ Bi-encoder 向量检索（BGE-M3, top_k=30）
        │                   └ pg_bigm关键词检索（top_k=20）
        → Step 3: RRF 融合 ── RRF_score = Σ 1/(k+rank), k=60
        → Step 4: 元数据过滤（WHERE user_id=$1 AND is_active=true）
        → Step 5: Cross-encoder 精排 → Top 5
```

**三因子加权排序公式**（DAO 层 SQL 一次完成）:
```sql
ORDER BY (
  similarity * 0.6 +                     -- 语义相似度（权重最大）
  importance * 0.2 +                      -- 重要性
  EXP(-EXTRACT(EPOCH FROM NOW()-created_at) / half_life_days) * 0.2  -- 时间衰减
) DESC
```

**RRF 融合详解**:
- 向量检索 rank#1 和关键词检索 rank#1 可能指向不同文档
- 各自打分尺度不同（余弦相似度 vs pg_bigm 匹配度），无法直接比较
- RRF 用排名替代分数：`1/(60+rank)` 把两个排序统一到同一尺度
- k=60 使头部权重平滑——rank#1 vs rank#2 仅差 1.02 倍

#### `summarizer.py` — MemorySummarizer

```
新消息 ──→ round_num 判断
            │
            ├── <10 轮 → 增量合并（前次摘要 + 新消息 → LLM 生成新摘要）
            └── ≥10 轮 → 全量合并（前次摘要 + 最后 5 条消息 → LLM 重写）
```

**不集成 Pipeline 的原因**: Pipeline 最多 3 个 `report_version`，达不到全量合并阈值 10。留给 Phase 5A Supervisor（ReAct 循环有对话轮次概念）。

#### `conflict.py` — MemoryConflictResolver（三级策略）

| 冲突类型 | 策略 | 触发条件 |
|---------|------|---------|
| semantic_identical（ⅲ） | OVERWRITE | 新记忆与旧记忆语义完全相同 |
| semantic_update（⚡） | UPDATE | 语义相似 ≥ 0.85 但内容不同（如价格从 200→180） |
| semantic_unrelated（🆕） | KEEP_BOTH | 语义无关，各自留存 |

#### `forgetting.py` — MemoryForgetting（三层遗忘）

| 层级 | 策略 | 触发条件 |
|------|------|---------|
| 自然衰减 | 无操作 | importance 随半衰期自然降低（SQL 层 ORDER BY 自动体现） |
| 归档 | `SET is_active=false` | 创建超过 180 天 |
| 显示删除 | `soft_delete(id)` | 用户主动删除 |

**设计约束**: 不删除 `decision` 类型记忆（关键决策永久保留）。

---

## 三、架构决策全景图

```
决策分层:
  L5 架构决策 ──── 为什么用单一 StateGraph 而非多图
  L4 工程决策 ──── PostgresSaver 自建 vs LangGraph 内置、remaining_steps 递减位置
  L3 模式决策 ──── TypedDict vs Pydantic、add_messages reducer 语义
```

| # | 决策 | 选项 A | 选项 B | 选择 | 原因 |
|---|------|--------|--------|:--:|------|
| 1 | 状态容器 | TypedDict | Pydantic BaseModel | A | LangGraph 原生支持，零开销 |
| 2 | Checkpoint 存储 | 自建 PostgresSaver | LangGraph 内置 SqliteSaver | A | PG 一体化，不用额外依赖 |
| 3 | remaining_steps 递减位置 | write 节点 | route_after_quality | A | 条件边不能写状态 |
| 4 | Temperature 管理 | 闭包创建 4 个实例 | 放 AgentState | A | LLM 实例不可序列化 |
| 5 | Summarizer 集成 Pipeline | 集成 | 留给 Supervisor | B | Pipeline 无对话轮次 |
| 6 | RRF 融合 k 值 | 60 | 0（无平滑） | A | Cormack 2009 原论文推荐 |
| 7 | 冲突阈值 | 0.85 | 0.9 | A | 太低漏检，太高误报 |
| 8 | 遗忘策略 | 不删除 decision 类 | 统一归档 | A | 关键决策必须永久保留 |

---

## 四、关键技术亮点 🏆

### 🏆 亮点一：温度分离的闭包工厂模式

**问题**: LangGraph 的 State/Checkpoint 存储序列化数据，LLM 实例（含 API key、连接池）不可序列化。

**方案**: 在 `build_pipeline_graph` 时创建 4 个不同温度的 `ChatDeepSeek` 实例，通过 `_make_node_xxx(llm)` 工厂函数将引用闭包进节点函数。节点函数签名是 `async def node_xxx(state: AgentState)`——LLM 在闭包中，不在 state 里。

```python
# 工厂函数持有 llm 引用，但不暴露在节点签名中
def _make_node_analyze(mcp_server, llm, ltm_engine):
    async def node_analyze(state: AgentState):
        # llm 和 ltm_engine 在闭包中，state 只管业务数据
        memories = await ltm_engine.retrieve(state["user_id"], query)
        result = await analyzer_agent(task, mcp_server, llm, memory_context=memories)
        return {"analysis_results": result}
    return node_analyze
```

### 🏆 亮点二：PostgresSaver 自建——只实现 4 个方法

继承 `BaseCheckpointSaver`，只需要重写 `aget_tuple` + `aput` + `aput_writes` + `setup`。不需要实现 `get_tuple`（同步版）、`list`、`delete` 等方法——LangGraph 在异步上下文中只调这 4 个。

`ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO NOTHING` 保证 `setup()` 幂等——多次调用不会重复建表。

### 🏆 亮点三：三因子加权检索公式在 SQL 层一次完成

不返 Python 再排序——DAO 的 `similarity_search` 在 SQL `ORDER BY` 中直接完成三因子加权计算。一次 SQL 查询到结果，避免"查 100 条 → Python 排序 → 取 Top 10"的多余网络传输。

### 🏆 亮点四：remaining_steps 的精确递减位置

第一版在 `collect` 节点递减——导致 `remaining_steps` 在第一个 Agent 就跑掉 1 步，write→quality 循环内 steps 不够。修复后只放在 `write` 节点递减：每次重写消耗 1 步，初稿不耗步。最终效果：initial=3 → 最多 2 次改写机会。

---

## 五、Bug 记录与修复

| # | Bug | 现象 | 根因 | 修复 | 严重度 |
|---|-----|------|------|------|:--:|
| 1 | remaining_steps 死循环 | 无论评分多少都只改写 1 次 | collect 节点提前递减 | 递减移至 write 节点 | 🔴 |
| 2 | checkpoint 表结构不一致 | PostgresSaver.setup() 报错 | schema.sql 缺 4 列 | 补齐 type/metadata/channel/value | 🔴 |
| 3 | `\\n` 转义 ×4 | LLM prompt 中出现字面 `\n` 文本 | Codex 写入时多转一次 | `\\\\n` → `\\n` | 🟡 |
| 4 | user_id 兜底 `"default"` | 所有记忆落到同一用户 | initial_state 漏写 user_id | 补 1 行 + 改 2 行 state["user_id"] | 🟡 |

---

## 六、面试追问手册

### Q1: 为什么用 TypedDict 而不是 Pydantic BaseModel？

TypedDict 是 LangGraph 的"一等公民"——`StateGraph(AgentState)` 直接接受 TypedDict，对 `Annotated` reducer 的支持更原生。TypedDict 零运行时开销（纯类型标注），序列化到 checkpoint 时就是纯 dict。Pydantic 也可以但 LangGraph 对它的 `add_messages` reducer 支持需要额外配置。这个项目不需要 Pydantic 的验证能力——字段值由 Agent 产出，不在 State 层做校验。

### Q2: add_messages 的 reducer 是怎么工作的？

`add_messages` 是 LangGraph 内置的 reducer 函数，行为分两种情况：
- 同 ID 消息 → 覆盖旧消息（去重）
- 不同 ID 消息 → 追加到列表末尾（累加）

本质是"追加为主，覆盖为辅"。用在 messages 字段上保证 LLM 看到完整对话历史，同时防止同一条消息重复出现。其他字段（如 `report_content`）用覆盖语义——"以最新状态为准"。

### Q3: PostgresSaver 为什么不用 LangGraph 自带的 AsyncPostgresSaver？

LangGraph 自带的 `AsyncPostgresSaver` 需要安装 `langgraph-checkpoint-postgres` 包，对表结构有固定要求（`checkpoint` 列是 `bytea` 序列化）。自建版本直接控制 JSONB 存储格式，可读性更好（`SELECT * FROM checkpoints` 直接看到 JSON），并且和项目的 `checkpoints`/`checkpoint_writes` 表设计完全一致。只需要 4 个方法就能跑——LangGraph 异步路径不调 `list`/`delete` 等。

### Q4: RRF 融合中 k=60 是怎么定的？

来自 Cormack 2009 年原论文。k=60 使 rank#1（1/61≈0.0164）与 rank#2（1/62≈0.0161）差距仅 1.02 倍，头部权重平滑。如果 k=0，rank#1 得分是 rank#2 的两倍——对排名过于敏感。搜索引擎评测中 k=60 是标准值。

### Q5: 为什么 Summarizer 不集成 Pipeline？

Summarizer 的 `round_num` 判断全量合并阈值（10 轮）依赖"对话轮次"概念。Pipeline 中最多只有 3 个 `report_version`（Writer 重写次数），达不到阈值——Summarizer 会永远运行在增量模式下，全量合并路径永远不会触发。Phase 5A Supervisor 有 ReAct 循环（`_think→_act→_observe`），天然有对话轮次，是 Summarizer 的正确归属。

### Q6: remaining_steps 为什么在 write 节点递减而不在 collect？

历史 Bug：第一版放在 collect 节点递减，导致 initial=3 进入 collect 后变 2，进入 write→quality 循环时只剩 1 步——只能改写 1 次。修复后只在 write 节点递减：初稿（write v1）不耗步，每次重写（write v2、v3）耗 1 步。语义对齐：remaining_steps 表示"剩余改写步数"，而非"剩余节点执行步数"。

---

## 七、验收标准对照

### Phase 4 验收

| # | 标准 | 结果 |
|---|------|:--:|
| 1 | AgentState 完整定义（17 字段含 user_id） | ✅ |
| 2 | StateGraph 5 节点正确注册 | ✅ |
| 3 | 条件边 `route_after_quality` 逻辑正确 | ✅ |
| 4 | remaining_steps ≤ 0 → 强制 finalize | ✅ |
| 5 | PostgresSaver 4 方法实现且表结构对齐 | ✅ |
| 6 | 4 个不同温度 LLM 实例闭包创建 | ✅ |
| 7 | AST 解析通过 | ✅ |

### Phase 4.5 验收

| # | 标准 | 结果 |
|---|------|:--:|
| 1 | 6/6 文件 AST 通过 | ✅ |
| 2 | DAO 12 个被调方法完全对照 | ✅ |
| 3 | Pipeline 集成 2 个钩子（analyze + finalize） | ✅ |
| 4 | Summarizer 不集成 Pipeline（架构决策） | ✅ |
| 5 | LLM Prompt ≤500 字约束 | ✅ |
| 6 | 冲突阈值 0.85 参数化 | ✅ |
| 7 | 归档排除 decision 类记忆 | ✅ |
| 8 | embedding 共用 BGE-M3 1024 维 | ✅ |
| 9 | user_id 隔离链路打通（无兜底值） | ✅ |

---

## 八、代码文件索引

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `src/pipeline/__init__.py` | ~30 | 包导出 + `run_pipeline_task` 入口函数 |
| `src/pipeline/state.py` | ~220 | AgentState 17 字段定义 + 读写矩阵 |
| `src/pipeline/graph.py` | ~650 | 5 节点 StateGraph + 条件路由 + 闭包工厂 |
| `src/pipeline/checkpoint.py` | ~95 | PostgresSaver 自建实现 |
| `src/memory/__init__.py` | ~25 | 记忆包导出（5 个类） |
| `src/memory/long_term.py` | ~180 | 五步检索 + RRF 融合 + 三因子排序 |
| `src/memory/summarizer.py` | ~140 | 递增/全量摘要合并（留给 Phase 5A） |
| `src/memory/retrieval.py` | ~100 | 检索触发策略（关键词预检 + LLM 重写） |
| `src/memory/conflict.py` | ~120 | 三级冲突策略（OVERWRITE/UPDATE/KEEP_BOTH） |
| `src/memory/forgetting.py` | ~80 | 三层遗忘（衰减/归档/软删除） |

---

## 九、下一步

| Phase | 内容 | 前置 | 状态 |
|-------|------|------|:--:|
| Phase 5A | Supervisor + A2A 通信协议 | Phase 3 ✅ | ⬚ 待开发 |
| Phase 5B | IntentRouter + Harness Engineering | Phase 4 + 5A | ⬚ 待开发 |
| Phase 6 | FastAPI 服务化 + 可观测性 | Phase 4 + 5B | ⬚ 待开发 |
| Phase 7 | 评估体系 + 集成测试 | Phase 6 | ⬚ 待开发 |
