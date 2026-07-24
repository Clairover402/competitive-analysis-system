"""认证安全 — bcrypt 密码哈希 + JWT 签发/验证。

═══════════════════════════════════════════════════════════════════════════════
                        【L2 鉴权 — 核心实现】
═══════════════════════════════════════════════════════════════════════════════

本模块做两件事：
  1. bcrypt 哈希密码 → 注册时哈希存入 users 表，登录时比对
  2. JWT 签发/验证 → 登录成功返回 token，API 请求通过 token 识别用户

为什么选 PyJWT 不选 python-jose？
  — python-jose 已停止维护（最后更新 2022），PyJWT 是最活跃的 JWT 库
  — 本项目不需要 JWE（加密），只需要 JWS（签名），PyJWT 完全够用

为什么选 bcrypt 不选 scrypt/argon2？
  — bcrypt 是 passlib 的一等公民，开箱即用
  — scrypt 内存消耗更大但对抗 GPU 并行攻击更好——但内部工具场景不需要
  — argon2 是最新的 Password Hashing Competition 冠军，但需要额外装 argon2-cffi
  — 选择：够用 + 简单 > 最好
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from src.config import Settings

logger = logging.getLogger(__name__)

# ── bcrypt 密码哈希上下文 ──
# schemes=["bcrypt"]: 只用 bcrypt，不 auto-upgrade 到新算法
# deprecated="auto": 如果 bcrypt 标准升级会自动迁移（passlib 特性）
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """对明文密码 bcrypt 哈希。

    【L4 工程】绝不要自己写哈希循环——
    用 passlib 是最佳实践：
      — 自动加盐（每次哈希随机生成 16 字节 salt）
      — rounds=12（2^12 次迭代，约 0.3s/次，够慢以对抗暴力破解）
      — 格式: $2b$12$<salt><hash>，salt 嵌在哈希字符串里

    Args:
        password: 用户明文密码

    Returns:
        bcrypt 哈希字符串，如 $2b$12$...
    """
    return _pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证明文密码是否与哈希匹配。

    passlib 内部从哈希字符串中提取 salt 和 rounds，重新计算比对。
    我们不需要手动传 salt——这就是用库的好处。

    Args:
        plain_password: 用户输入的明文密码
        hashed_password: 数据库中存储的 bcrypt 哈希

    Returns:
        True 如果匹配
    """
    return _pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    user_id: str,
    username: str,
    settings: Settings,
) -> str:
    """签发 JWT access token。

    【L4 工程】JWT payload 设计
    ─────────────────────────
    sub (Subject):      user_id — 唯一标识，后续 Depends(get_current_user) 从中取
    username:           用于日志和前端显示，不查库
    exp (Expiration):   过期时间，当前时间 + jwt_expire_hours
    iat (Issued At):    签发时间，用于判断 token 年龄

    为什么不把更多用户信息放在 JWT 里？
      — JWT 每次 HTTP 请求都带，payload 越大带宽开销越大
      — 只放最小必要信息（user_id + username），其他信息需要时再查库
      — JWT 是无状态的——不放可能变的数据（如用户状态），否则 token 失效要等过期

    Args:
        user_id:   用户 UUID 字符串
        username:  用户名
        settings:  Settings 实例（取 jwt_secret + jwt_expire_hours）

    Returns:
        JWT 字符串，格式: header.payload.signature
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token


def decode_access_token(token: str, settings: Settings) -> dict:
    """验证并解码 JWT token。

    解码失败（过期/签名不匹配/格式错误）→ 抛出 jwt.PyJWTError，
    调用方（dependencies.py）会捕获并转 HTTP 401。

    【L4 工程】算法锁定为 HS256
    ─────────────────────────
    jwt.decode 如果 algorithms=None 会接受 token header 中声明的算法——
    这是安全漏洞：攻击者可以伪造 token 把算法设为 "none" 绕过验证。
    显式指定 algorithms=["HS256"] 锁死算法。

    Args:
        token:    HTTP Authorization header 中提取的 JWT 字符串
        settings: Settings 实例

    Returns:
        解码后的 payload dict: {"sub": user_id, "username": "...", ...}

    Raises:
        jwt.PyJWTError: token 无效、过期或签名不匹配
    """
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
    )
