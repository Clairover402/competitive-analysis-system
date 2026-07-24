"""认证路由 — POST /api/auth/register + POST /api/auth/login。

═══════════════════════════════════════════════════════════════════════════════
                        【L2 鉴权 — HTTP API】
═══════════════════════════════════════════════════════════════════════════════

两条公开路由（无需 JWT）：
  POST /api/auth/register → 注册新用户，返回 JWT
  POST /api/auth/login    → 验证密码，返回 JWT

JWT 过期时间由 Settings.jwt_expire_hours 控制（默认 24h）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import time

from src.auth.dependencies import CurrentUser, get_current_user
from src.auth.security import create_access_token, hash_password, verify_password
from src.db.dao import UserDAO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── 登录/注册频控：按 IP 滑动窗口 ──
# 每 IP 每分钟最多 5 次登录尝试 + 3 次注册尝试
# 防止暴力破解和批量注册
_auth_attempts: dict[str, list[float]] = {}  # ip → [timestamp, ...]
_AUTH_MAX_LOGIN_PER_MINUTE = 5
_AUTH_MAX_REGISTER_PER_MINUTE = 3
_AUTH_WINDOW_SECONDS = 60


# ═════════════════════════════════════════════════════════════════════════════
# §0 鉴权频控 — 按 IP 滑动窗口限流
# ═════════════════════════════════════════════════════════════════════════════

def _check_auth_rate_limit(request: Request, max_attempts: int) -> None:
    """按客户端 IP 做滑动窗口频控。

    【L4 工程】为什么不用 Redis？
      — 内部工具场景，内存足够。Redis 增加运维成本。
      — 内存方案重启丢失计数——但重启时攻击窗口也重新计算，可接受。
      — 如果未来多副本部署，再迁移到 Redis 方案。

    滑动窗口而非固定窗口：防跨窗口突刺。
    内存无上限风险：单 IP 只存 60s 的时间戳列表，<100 条。
    """
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - _AUTH_WINDOW_SECONDS

    # ── 清理过期记录 ──
    attempts = _auth_attempts.get(ip, [])
    attempts = [t for t in attempts if t > cutoff]
    _auth_attempts[ip] = attempts

    # ── 判断频控 ──
    if len(attempts) >= max_attempts:
        raise HTTPException(429, "操作过于频繁，请 1 分钟后再试")

    # ── 记录本次尝试 ──
    attempts.append(now)


# ═════════════════════════════════════════════════════════════════════════════
# §1 Pydantic 数据模型
# ═════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    """注册请求体。

    用户名规则：字母、数字、下划线、中文，2~50 字符。
    邮箱规则：标准邮箱格式，选填（None 时不校验格式）。
    """
    username: str = Field(
        ...,
        min_length=2,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\u4e00-\u9fff]{2,50}$",
        description="用户名（字母/数字/下划线/中文，2~50字符）",
    )
    password: str = Field(..., min_length=6, max_length=128, description="密码（≥6位）")
    email: str | None = Field(
        None,
        max_length=200,
        pattern=r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$",
        description="邮箱（可选，需合法格式）",
    )


class LoginRequest(BaseModel):
    """登录请求体。"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class AuthResponse(BaseModel):
    """认证成功响应。"""
    user_id: str
    username: str
    token: str
    token_type: str = "bearer"


# ═════════════════════════════════════════════════════════════════════════════
# §2 POST /api/auth/register
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/register", status_code=201)
async def register(body: RegisterRequest, request: Request) -> AuthResponse:
    """注册新用户。

    流程:
      0. 频控检查（每 IP 每分钟 ≤3 次）
      1. 检查用户名/邮箱唯一性
      2. bcrypt 哈希密码
      3. 写入 users 表 → 冲突返回 409
      4. 签发 JWT → 返回（注册即登录）
    """
    # ── 0. 频控 ──
    _check_auth_rate_limit(request, _AUTH_MAX_REGISTER_PER_MINUTE)

    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(500, "数据库连接池未初始化")

    settings = request.app.state.settings
    user_dao = UserDAO(pool)

    # ── 1. 检查用户名是否已存在 ──
    existing_user = await user_dao.get_by_username(body.username)
    if existing_user:
        raise HTTPException(409, f"用户名已被注册: {body.username}")

    # ── 2. 检查邮箱是否已存在（仅当提供了邮箱）──
    if body.email:
        existing_email = await user_dao.get_by_email(body.email)
        if existing_email:
            raise HTTPException(409, f"邮箱已被注册: {body.email}")

    # ── 3. 哈希密码 ──
    pwd_hash = hash_password(body.password)

    # ── 4. 写入 users 表 ──
    user = await user_dao.create(
        username=body.username,
        password_hash=pwd_hash,
        email=body.email,
    )
    if user is None:
        # 理论上不会走到这里（上面已检查），但保留兜底
        raise HTTPException(500, "用户创建失败")

    # ── 3. 签发 JWT ──
    user_id = str(user["id"])
    token = create_access_token(user_id, body.username, settings)

    logger.info("用户注册成功", extra={"username": body.username, "user_id": user_id[:8]})
    return AuthResponse(user_id=user_id, username=body.username, token=token)


# ═════════════════════════════════════════════════════════════════════════════
# §3 POST /api/auth/login
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/login")
async def login(body: LoginRequest, request: Request) -> AuthResponse:
    """用户登录。

    流程:
      0. 频控检查（每 IP 每分钟 ≤5 次）
      1. 按 username 查 users 表 → 不存在 → 401
      2. 检查账号是否被禁用
      3. bcrypt 验证密码 → 不匹配 → 401
      4. 签发 JWT → 返回
    """
    # ── 0. 频控 ──
    _check_auth_rate_limit(request, _AUTH_MAX_LOGIN_PER_MINUTE)

    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(500, "数据库连接池未初始化")

    settings = request.app.state.settings
    user_dao = UserDAO(pool)

    # ── 1. 查用户 ──
    user = await user_dao.get_by_username(body.username)
    if user is None:
        raise HTTPException(401, "用户名或密码错误")

    # ── 2. 检查账号是否被禁用 ──
    if not user.get("is_active", True):
        raise HTTPException(403, "账号已被禁用，请联系管理员")

    # ── 3. 验证密码 ──
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")

    # ── 3. 签发 JWT ──
    user_id = str(user["id"])
    token = create_access_token(user_id, body.username, settings)

    logger.info("用户登录成功", extra={"username": body.username, "user_id": user_id[:8]})
    return AuthResponse(user_id=user_id, username=body.username, token=token)


# ═════════════════════════════════════════════════════════════════════════════
# §4 GET /api/auth/me
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/me")
async def get_me(current_user: CurrentUser = Depends(get_current_user)):
    """查看当前用户信息（需 JWT）。

    前端登录后验证 token 是否有效用。
    """
    return {"user_id": current_user.user_id, "username": current_user.username}
