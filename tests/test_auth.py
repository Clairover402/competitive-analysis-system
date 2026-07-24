# -*- coding: utf-8 -*-
"""鉴权模块测试 — 密码哈希 / JWT 签发验证 / 路由鉴权。

═══════════════════════════════════════════════════════════════════════════════
                        【L2 鉴权 — 测试覆盖】
═══════════════════════════════════════════════════════════════════════════════

  6 个测试用例覆盖：
    1. test_hash_and_verify_password         — bcrypt 哈希与验证明文往返
    2. test_jwt_create_and_decode            — JWT 签发 → 解码往返 + payload 完整性
    3. test_jwt_expired_token                — 过期 token 解码抛 ExpiredSignatureError
    4. test_register_duplicate_username      — 注册重复用户名 → 409
    5. test_login_invalid_credentials        — 错误密码 → 401（信息不泄露）
    6. test_protected_endpoint_requires_jwt  — 未带 token 访问受保护端点 → 401

  测试策略：
    — security/dependencies 用纯单元测试（不依赖 DB/网络）
    — 路由用 ASGI transport（httpx.ASGITransport）内存启动 app
    — DB pool 用 FakeConn + FakeAcquireContext 模拟（不连真实 PG）
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from fastapi import FastAPI

from src.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from src.config import Settings


# ═════════════════════════════════════════════════════════════════════════════
# Test fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def event_loop():
    """创建模块级 event loop（pytest-asyncio 要求）。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings():
    """测试用 Settings——JWT secret 固定以便断言。"""
    return Settings(
        jwt_secret="test-secret-key-that-is-at-least-32-bytes-long",
        jwt_expire_hours=1,
        deepseek_api_key="test-key",
    )


# ── Fake DB 层（模拟 asyncpg.Pool）──

class _FakeConn:
    """模拟 asyncpg.Connection——控制 fetchrow/fetchval/execute 返回值。"""

    def __init__(self, user_rows: list[dict] | None = None):
        # fetchrow 返回单行（get_by_username 用）
        self.fetchrow = AsyncMock(side_effect=user_rows if user_rows else [None])
        self.fetchval = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value="OK")
        self.fetch = AsyncMock(return_value=[])


class _FakeAcquireContext:
    """模拟 async with pool.acquire() as conn: 的上下文管理器。"""

    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *args) -> None:
        pass


def _build_fake_pool(user_rows: dict | None = None) -> MagicMock:
    """构建带 _FakeAcquireContext 的模拟 pool。"""
    pool = MagicMock()
    conn = _FakeConn(user_rows=[user_rows] if user_rows else None)
    pool.acquire = MagicMock(return_value=_FakeAcquireContext(conn))
    return pool


# ═════════════════════════════════════════════════════════════════════════════
# §1 单元测试 — security.py
# ═════════════════════════════════════════════════════════════════════════════

class TestSecurity:
    """password_hash + JWT 纯函数测试（零外部依赖）。"""

    # ── Test 1: bcrypt 哈希往返 ──

    def test_hash_and_verify_password(self):
        """密码哈希 → 验证明文往返正确。"""
        plain = "mySecret123"
        hashed = hash_password(plain)

        # 哈希 != 明文
        assert hashed != plain
        # bcrypt 格式：$2b$...
        assert hashed.startswith("$2")

        # 验证正确密码
        assert verify_password(plain, hashed) is True
        # 验证错误密码
        assert verify_password("wrong", hashed) is False
        # 每个哈希独一无二（盐不同）
        another = hash_password(plain)
        assert hashed != another
        assert verify_password(plain, another) is True

    # ── Test 2: JWT 签发 → 解码往返 ──

    def test_jwt_create_and_decode(self, settings):
        """JWT 签发后能正确解码，payload 字段完整。"""
        token = create_access_token("user-abc", "testuser", settings)

        assert isinstance(token, str)
        assert token.count(".") == 2  # header.payload.signature

        payload = decode_access_token(token, settings)
        assert payload["sub"] == "user-abc"
        assert payload["username"] == "testuser"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] > payload["iat"]

    # ── Test 3: 过期 token ──

    def test_jwt_expired_token(self, settings):
        """过期 token 解码应抛出 ExpiredSignatureError。"""
        short_settings = Settings(
            jwt_secret="test-secret-key-that-is-at-least-32-bytes-long",
            jwt_expire_hours=-1,
            deepseek_api_key="test-key",
        )
        token = create_access_token("user-abc", "testuser", short_settings)

        with pytest.raises(jwt.ExpiredSignatureError):
            decode_access_token(token, short_settings)


# ═════════════════════════════════════════════════════════════════════════════
# §2 集成测试 — auth_routes（ASGI transport + fake pool）
# ═════════════════════════════════════════════════════════════════════════════

class TestAuthRoutes:
    """鉴权路由集成测试——ASGI transport 内存启动 app，fake DB pool。"""

    @pytest.fixture
    def client(self, settings):
        """构建带 fake pool 的 FastAPI test client。"""
        from src.api.routes import app

        # 保存原始 state
        original_pool = getattr(app.state, "pool", None)
        original_settings = getattr(app.state, "settings", None)

        # 注入 fake pool + test settings
        app.state.pool = _build_fake_pool()
        app.state.settings = settings
        app.state.token_bucket = None  # 跳过限流

        transport = httpx.ASGITransport(app=app)
        c = httpx.AsyncClient(transport=transport, base_url="http://test")

        yield c

        # 恢复
        app.state.pool = original_pool
        app.state.settings = original_settings

    # ── 辅助：生成合法 JWT header ──

    def _token_header(self, user_id: str = "user-abc", username: str = "testuser") -> dict:
        settings = Settings(
            jwt_secret="test-secret-key-that-is-at-least-32-bytes-long",
            jwt_expire_hours=1,
            deepseek_api_key="test-key",
        )
        token = create_access_token(user_id, username, settings)
        return {"Authorization": f"Bearer {token}"}

    # ── Test 4: 注册冲突 ──

    @pytest.mark.asyncio
    async def test_register_duplicate_username(self, client):
        """注册已存在的用户名 → 409 Conflict。"""
        user = {"id": "existing-id", "username": "taken", "email": None}
        conn = _FakeConn(user_rows=[user])
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_FakeAcquireContext(conn))
        client._transport.app.state.pool = pool

        resp = await client.post("/api/auth/register", json={
            "username": "taken",
            "password": "123456",
        })
        assert resp.status_code == 409
        assert "已被注册" in resp.json()["detail"]

    # ── Test 5: 错误密码 → 401 ──

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self, client):
        """错误密码 → 401，且不泄露是"用户不存在"还是"密码错"。"""
        from src.auth.security import hash_password

        user = {
            "id": "user-xyz",
            "username": "normal",
            "password_hash": hash_password("correct"),
            "email": None,
            "is_active": True,
            "created_at": "2026-01-01",
        }
        conn = _FakeConn(user_rows=[user])
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_FakeAcquireContext(conn))
        client._transport.app.state.pool = pool

        resp = await client.post("/api/auth/login", json={
            "username": "normal",
            "password": "wrong_password",
        })
        assert resp.status_code == 401
        assert "用户名或密码错误" in resp.json()["detail"]

    # ── Test 6: 未认证 → 401 ──

    @pytest.mark.asyncio
    async def test_protected_endpoint_requires_jwt(self, client):
        """不带 JWT 访问受保护端点 → 401。"""
        resp = await client.get("/api/tasks/some-id")
        assert resp.status_code == 401
        assert "未提供认证令牌" in resp.json()["detail"]

        resp = await client.post("/api/tasks", json={
            "title": "test",
            "competitors": ["A"],
            "dimensions": ["功能"],
        })
        assert resp.status_code == 401
