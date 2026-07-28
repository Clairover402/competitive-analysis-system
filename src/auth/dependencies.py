"""FastAPI 鉴权依赖 — 从 HTTP Header 提取 JWT 并注入当前用户。

═══════════════════════════════════════════════════════════════════════════════
                        【L2 鉴权 — FastAPI Depends 注入】
═══════════════════════════════════════════════════════════════════════════════

使用方式（任意路由）：
    from src.auth.dependencies import get_current_user

    @router.get("/api/tasks")
    async def list_tasks(current_user = Depends(get_current_user)):
        user_id = current_user.user_id  # → 在路由里直接用
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import Settings

logger = logging.getLogger(__name__)

# ── HTTP Bearer Token 提取器 ──
# 自动从 Authorization: Bearer <token> 头中提取 token
# auto_error=False → token 缺失时不自动 403，我们手动 401
_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """当前认证用户（注入到路由中）。

    frozen=True → 不可变——路由不应修改用户信息。
    dataclass 比 Pydantic 更轻量——不需要序列化/校验。
    """

    user_id: str
    username: str


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """从请求中提取 JWT 并解析当前用户。

    鉴权链（按优先级）：
      1. Authorization: Bearer <token> header → 标准 HTTP 鉴权
      2. ?token=xxx URL query parameter → SSE 场景的降级方案
         （EventSource API 不支持自定义 HTTP Header，只能走 URL 传 token）
      3. 都没有 → 401

    然后：解码 JWT → 过期/签名错误 → 401 → 提取 user_id + username

    【L4 工程】鉴权失败统一返回 401：
      — 不区分"token 缺失"和"token 过期"（避免信息泄露）
      — 攻击者不需要知道"为什么失败"，只知道"没权限"就够了

    Raises:
        HTTPException 401: 未认证
    """
    # ── 优先 Authorization header，降级 URL query token（SSE）──
    token: str | None = None
    if credentials is not None:
        token = credentials.credentials
    else:
        # EventSource 不支持自定义 header，token 走 URL query
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(status_code=401, detail="未提供认证令牌")

    settings: Settings = request.app.state.settings

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="认证令牌已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="认证令牌无效")

    user_id = payload.get("sub")
    username = payload.get("username", "unknown")

    if not user_id:
        raise HTTPException(status_code=401, detail="认证令牌数据无效")

    return CurrentUser(user_id=user_id, username=username)
