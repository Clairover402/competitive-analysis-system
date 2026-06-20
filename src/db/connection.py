"""asyncpg 数据库连接池管理。

全局单例模式——整个应用生命周期共享同一个连接池。
实际数据库连接在首次 create_pool() 调用时建立。
"""

from __future__ import annotations

import asyncpg

from src.config import Settings

_pool: asyncpg.Pool | None = None


async def create_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """创建 PostgreSQL 连接池。

    首次调用时建立连接池并缓存为全局单例；
    后续调用直接返回已有的池。

    Args:
        settings: 配置实例，为 None 时使用默认 Settings()。

    Returns:
        asyncpg.Pool: 已建立的连接池。
    """
    global _pool

    if _pool is not None:
        return _pool

    if settings is None:
        settings = Settings()

    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    """获取全局连接池单例。

    Returns:
        asyncpg.Pool: 已建立的连接池。

    Raises:
        RuntimeError: 连接池尚未初始化——请先调用 create_pool()。
    """
    if _pool is None:
        raise RuntimeError(
            "连接池尚未初始化，请先调用 await create_pool()"
        )
    return _pool


async def close_pool() -> None:
    """关闭全局连接池，释放所有连接。"""
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None