# -*- coding: utf-8 -*-
r"""
Phase 6 集成测试 — FastAPI 6 端点 + 限流 + DB 连通性。

运行方式：
  cd D:\\AAAagent\\projects\\competitive-analysis-system
  python test_phase6.py

前提：Docker PG 容器 competitive-analysis-pg 运行在端口 5433
"""

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import asyncpg
import httpx

PROJECT_ROOT = r"D:\AAAagent\projects\competitive-analysis-system"
sys.path.insert(0, PROJECT_ROOT)

# Mock _execute_task —— 不真正执行 Agent 引擎
mock_execute = AsyncMock(return_value=None)
patcher_exec = patch("src.api.routes._execute_task", mock_execute)
patcher_exec.start()

from src.api.routes import app  # noqa: E402
from src.db.connection import create_pool, close_pool  # noqa: E402
from src.config import Settings  # noqa: E402
from src.observability.logging import BoundLogger  # noqa: E402
import logging  # noqa: E402


# ---- 初始化 app.state（ASGI transport 不触发 lifespan） ----
async def init_app_state():
    settings = Settings()
    pool = await create_pool(settings)
    app.state.pool = pool
    app.state.settings = settings
    app.state.token_bucket = None    # 限流测试自己创建
    app.state.agent_semaphore = None
    app.state.llm_limiter = None
    app.state.log = BoundLogger(logging.getLogger("test"))
    return pool


def ok(msg): print(f"  [PASS] {msg}")
def fail(msg, detail=""):
    print(f"  [FAIL] {msg}")
    if detail: print(f"         {detail}")


async def main():
    print("=" * 60)
    print("  Phase 6 集成测试")
    print("=" * 60)

    pool = await init_app_state()
    transport = httpx.ASGITransport(app=app)
    passed = 0
    total = 0

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # ---- GET /health ----
        total += 1
        try:
            r = await client.get("/health")
            assert r.status_code == 200, f"status={r.status_code}"
            body = r.json()
            assert body["status"] in ("ok", "degraded")
            assert isinstance(body["db_connected"], bool)
            ok(f"GET /health -> 200 ({body['status']}, db={body['db_connected']})")
            passed += 1
        except Exception as e:
            fail("GET /health", str(e))

        # ---- GET /metrics ----
        total += 1
        try:
            r = await client.get("/metrics")
            assert r.status_code == 200
            assert "text/plain" in r.headers.get("content-type", "")
            assert len(r.text) > 0
            ok(f"GET /metrics -> 200 ({len(r.text)} bytes)")
            passed += 1
        except Exception as e:
            fail("GET /metrics", str(e))

        # ---- POST /api/tasks (空输入 -> 422, FastAPI 标准校验码) ----
        total += 1
        try:
            r = await client.post("/api/tasks", json={"title": "空"})
            assert r.status_code == 422, f"期望 422 实际 {r.status_code}"
            ok("POST /api/tasks empty -> 422 (Pydantic 校验: 缺 competitors+dimensions 或 query)")
            passed += 1
        except Exception as e:
            fail("POST /api/tasks empty", str(e))

        # ---- POST /api/tasks (缺 title -> 422) ----
        total += 1
        try:
            r = await client.post("/api/tasks", json={"competitors": ["A"], "dimensions": ["B"]})
            assert r.status_code == 422
            ok("POST /api/tasks no-title -> 422 (Pydantic 校验)")
            passed += 1
        except Exception as e:
            fail("POST /api/tasks no-title", str(e))

        # ---- POST /api/tasks (competitors+dimensions) ----
        total += 1
        task_id = None
        try:
            r = await client.post("/api/tasks", json={
                "title": "协同办公软件竞品分析",
                "competitors": ["飞书", "钉钉", "Notion"],
                "dimensions": ["功能", "定价", "市场"],
            })
            import traceback
            if r.status_code != 202:
                detail = r.text[:500]
                # 如果是 500，dump 完整 traceback
                if r.status_code == 500:
                    print(f"  [DEBUG] 500 body: {detail}")
                raise AssertionError(f"status={r.status_code} body={detail}")
            body = r.json()
            task_id = body["task_id"]
            # UUID 格式含连字符: 36 字符（8-4-4-4-12）
            assert len(task_id) == 36, f"UUID 期望 36 字符，实际 {len(task_id)}"
            assert body["status"] == "pending"
            ok(f"POST /api/tasks (structured) -> 202 tid={task_id[:8]}...")
            passed += 1
        except Exception as e:
            import traceback
            fail("POST /api/tasks structured", str(e)[:200])
            print(f"  [TRACE] {traceback.format_exc()[:500]}")

        # ---- POST /api/tasks (query 模式) ----
        total += 1
        try:
            r = await client.post("/api/tasks", json={
                "title": "飞书竞争力分析",
                "query": "飞书和钉钉在功能定价方面谁更强？",
            })
            assert r.status_code == 202
            ok(f"POST /api/tasks (query) -> 202 tid={r.json()['task_id'][:8]}...")
            passed += 1
        except Exception as e:
            fail("POST /api/tasks query", str(e))

        # ---- GET /api/tasks/{id} ----
        if task_id:
            total += 1
            try:
                r = await client.get(f"/api/tasks/{task_id}")
                assert r.status_code == 200
                body = r.json()
                assert body["task_id"] == task_id
                assert body["status"] == "pending"
                ok(f"GET /api/tasks/{task_id[:8]}... -> 200 ({body['status']})")
                passed += 1
            except Exception as e:
                fail(f"GET /api/tasks/{task_id[:8]}", str(e))

            # ---- GET /api/tasks/{id}/reports (empty) ----
            total += 1
            try:
                r = await client.get(f"/api/tasks/{task_id}/reports")
                assert r.status_code == 404
                ok(f"GET /api/tasks/{task_id[:8]}.../reports -> 404 (empty expected)")
                passed += 1
            except Exception as e:
                fail(f"GET /api/tasks/{task_id[:8]}.../reports", str(e))

        # ---- GET /api/tasks/{id} (不存在的任务) ----
        total += 1
        try:
            r = await client.get("/api/tasks/ffffffff-ffff-ffff-ffff-ffffffffffff")
            assert r.status_code == 404
            ok("GET /api/tasks/fff... -> 404 (task not found)")
            passed += 1
        except Exception as e:
            fail("GET /api/tasks not-found", str(e))

    # ---- 清理 ----
    await close_pool()

    # ---- 汇总 ----
    print(f"\n{'='*60}")
    print(f"  {passed}/{total} 通过", end="")
    if passed < total:
        print(f"  ({total - passed} 失败)")
    else:
        print()
    print(f"{'='*60}")

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
