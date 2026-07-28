import asyncio, asyncpg

async def main():
    pool = await asyncpg.create_pool(host='localhost', port=5432, database='cas_db', user='postgres', password='123456')
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT agent_name, action, count(*) as cnt
            FROM agent_logs
            GROUP BY agent_name, action
            ORDER BY cnt DESC
        """)
        for r in rows:
            an = r['agent_name']
            ac = r['action']
            c = r['cnt']
            print(f'{an:30s} / {ac:50s} x{c}')
    await pool.close()

asyncio.run(main())
