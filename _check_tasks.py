"""查任务状态（一次性）"""
import asyncio, sys
sys.path.insert(0, ".")
from src.config import Settings

async def main():
    import asyncpg
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    # 最近5个任务
    rows = await conn.fetch(
        "SELECT id, title, status, error_message, created_at "
        "FROM tasks ORDER BY created_at DESC LIMIT 5"
    )
    for r in rows:
        err = (r['error_message'] or '')[:100]
        print(f"  [{r['status']}] {r['title']} | {r['id'][:8]}... | {r['created_at']}")
        if err:
            print(f"    错误: {err}")
    await conn.close()

asyncio.run(main())
