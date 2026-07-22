# Phase 6 集成测试报告 — 2026-07-22

## 测试覆盖
9 个测试用例，覆盖 FastAPI 6 个端点 + 参数校验 + DB 读写：
- GET /health → 200 (DB 连通校验)
- GET /metrics → 200 (Prometheus 文本格式)
- POST /api/tasks 空输入 → 422 (Pydantic 校验)
- POST /api/tasks 缺 title → 422 (Pydantic 校验)
- POST /api/tasks structured → 202 (competitors+dimensions 模式)
- POST /api/tasks query → 202 (自然语言入口)
- GET /api/tasks/{id} → 200 (完整 TaskResponse)
- GET /api/tasks/{id} 不存在 → 404
- GET /api/tasks/{id}/reports 空 → 404

## 结果：9/9 全部通过

## 发现并修复的 3 个 Bug

1. **TokenBucket None 崩溃**（routes.py L302）
   - 根因：ASGI transport 不触发 lifespan，app.state.token_bucket=None
   - 修复：`if tb is not None and not await tb.acquire()` 守卫

2. **asyncpg list → jsonb 类型错误**（dao.py L85-93）
   - 根因：Python list 直接传 `$3::jsonb` 被 asyncpg 拒绝
   - 修复：`json.dumps(competitors, ensure_ascii=False)` 先序列化

3. **jsonb 读回仍是字符串**（dao.py L105-117）
   - 根因：asyncpg 返回 JSONB 为 JSON 字符串，Pydantic 期望 list
   - 修复：`json.loads(val)` 反序列化，兼容 str 和已解析的 list

## 关键设计决策
- 测试用 ASGI transport（内存中），不启动 uvicorn，验证闭环快
- mock 掉 `_execute_task`，不调用真实 LLM
- app.state 手动初始化（比 lifespan 更显式，适合测试）

## 文件变更
- 修改：src/api/routes.py（TokenBucket None 守卫）
- 修改：src/db/dao.py（json.dumps/json.loads 序列化/反序列化）
- 新增：test_phase6.py（集成测试脚本）
