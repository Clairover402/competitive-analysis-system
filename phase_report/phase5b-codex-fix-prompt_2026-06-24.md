# Phase 5B Codex 修复提示词

**生成时间**: 2026-06-24 15:15
**基于**: Phase 5B 验收报告，3 个缺陷

---

## 缺陷 #1：HarnessGuard 死代码（阻断级）

### 问题
`src/supervisor/router.py` 的 `_setup_dependencies()` 中：
```python
router = A2ARouter(mcp_server)  # ← harness 参数 None
```
导致 `src/supervisor/a2a.py` 的 `send_task()` 中 `if self._harness is not None` 永远为 False——五层安全检查从未执行。

### 修复方案

#### 步骤 1：`router.py` — 创建 HarnessGuard 并注入 A2ARouter

在 `_setup_dependencies()` 中：
- 在创建 router 之前，先 `from src.harness import HarnessGuard; guard = HarnessGuard(pool)`
- 修改 A2ARouter 构造函数调用为 `router = A2ARouter(mcp_server, harness=guard)`

注意：HarnessGuard 需要 pool 参数（用于审计日志写入），所以创建顺序是 pool → guard → router。

#### 步骤 2：`a2a.py` — 修正 `send_task()` 的 Step 编号

当前注释写的是 `# Step ?.5: Harness ????Phase 5B?`（乱码），改为正确的步骤编号。参照前面的 Step ①~⑥ 顺序，Harness 检查应放在 Step ② 和 Step ③ 之间（查完注册信息后、标记 RUNNING 前），改为 Step ②.5。

具体改动行：
```python
# 改前:
# Step ?.5: Harness ????Phase 5B?

# 改后:
# Step ②.5: Harness 五层安全检查（Phase 5B 集成）
```

#### 步骤 3：验证 Harness 在 send_task 中的 3 步逻辑正确

确认 `a2a.py` 的 `send_task()` 中 Harness 集成逻辑（已存在，只需确认）：
1. `guard_result = await self._harness.guard(...)` → 调用五层检查
2. `if not guard_result["passed"]: task.status = FAILED + task.error = ...` → 阻断返回
3. `logger.warning(...)` → 记录拦截日志

---

## 缺陷 #2：classify 门槛与验收标准矛盾

### 问题
`src/supervisor/router.py` 的 `classify()` 方法：
```python
if not competitors or len(competitors) < 2:
    return "supervisor"
```
验收标准期望：单竞品（如 `["飞书"]`）+ 维度 + 意图明确 → pipeline。

### 决策
验收标准代表正确的业务逻辑——用户说"分析飞书"时，虽然竞品只有一个，但用户明确知道要分析谁，不需要探索模式。真正的探索模式触发条件是"用户根本没提竞品名字"。

### 修复方案

`router.py` 的 `classify()` 中将 `len(competitors) < 2` 改为 `len(competitors) == 0`：

```python
# 改前:
if not competitors or len(competitors) < 2:
    logger.info("路由决策: supervisor（竞品数量不足: %d）", len(competitors))
    return "supervisor"

# 改后:
if not competitors or len(competitors) == 0:
    logger.info("路由决策: supervisor（未指定竞品，需探索发现）")
    return "supervisor"
```

同时更新类 docstring 中的规则说明和 route() 中 history_entry 的记录文案。

---

## 缺陷 #3：依赖重复创建（技术债，低优先级）

### 问题
`IntentRouter._setup_dependencies()` 每次都 new 全量依赖，而 `run_pipeline_task` 的 `if mcp_server is None` 分支也会重复创建——虽然当前不会触发（因为 router 已经把 pool/mcp_server 传进去了），但每次 route() 创建了两套依赖，浪费资源。

### 修复方案（低优先级，本次不做）

将来引入 AppContext 统一管理：`IntentRouter` 构造时接收一个 `AppContext`，`_setup_dependencies()` 改为读 ctx 的预创建实例。本次不修，Phase 6 服务化时一并处理。

---

## 执行顺序

1. 先修缺陷 #2（最简，classify 一行改）
2. 再修缺陷 #1（两步：router.py 注入 + a2a.py 注释）
3. 不修缺陷 #3
