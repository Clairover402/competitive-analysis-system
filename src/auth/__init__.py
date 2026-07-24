"""认证鉴权模块 — JWT 签发/验证 + FastAPI 依赖注入。

组件：
    security.py      → bcrypt 密码哈希 + JWT 签发/验证
    dependencies.py  → get_current_user() FastAPI Depends 鉴权中间件
    UserDAO          → db/dao.py 中的用户数据访问

使用方式：
    from src.auth.dependencies import get_current_user

    @router.get("/api/tasks")
    async def list_tasks(current_user = Depends(get_current_user)):
        ...
"""

from src.auth.security import (
    create_access_token,
    verify_password,
    hash_password,
    decode_access_token,
)
from src.auth.dependencies import get_current_user

__all__ = [
    "create_access_token",
    "verify_password",
    "hash_password",
    "decode_access_token",
    "get_current_user",
]
