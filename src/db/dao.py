"""数据访问层（DAO）—— asyncpg 原生 SQL，全异步。
============================================================

【L4 工程必问】为什么用 asyncpg 不用 SQLAlchemy？
------------------------------------------------------------
面试官："你们为什么选 asyncpg，不用 ORM？"

回答分三个层次：
  Level 1（选型）：SQLAlchemy 2.0 支持 async，但底层仍通过
    asyncpg 或 aiopg 驱动。多一层抽象 = 多一层性能损耗。
    asyncpg 直接调 PostgreSQL binary protocol，比 ORM 快 2-5 倍。

  Level 2（场景）：竞品分析系统的 SQL 以 CRUD + 向量搜索为主，
    不需要 ORM 的 Unit of Work / Identity Map / Lazy Loading。
    这些企业级 ORM 特性对我们反而是负担——增加复杂度、引入 N+1 陷阱。

  Level 3（团队）：团队是 Python + PostgreSQL 栈，SQL 能力够，
    不需要 ORM 来"翻译"——写 SQL 比写 ORM 查询更可控。
    对面试官说："我们选工具是按场景选，不是按潮流选。"

【L4 工程模式总结】（本文件涉及的所有模式）
  - 连接池模式 (asyncpg.Pool 单例, acquire/release)
  - 参数化查询 ($1, $2, ...) 防注入
  - 批量操作 (executemany, 减少 DB round-trip)
  - 向量类型转换 (::vector, pgvector 特有)
  - 软删除 (is_active=false, 不物理删除)
  - 时间衰减检索 (POWER(0.5, age/half_life))
  - 异常不吞掉 (记录后重新 raise)

【L3 架构定位】
DAO 是 Supervisor 架构中所有 Agent 的"记忆读写层"。
Collector 通过 ChunkEmbeddingDAO 存入采集结果，
Analyzer 通过 similarity_search 检索相关内容，
Supervisor 通过 AgentMemoryDAO 读写长期记忆，
Quality 通过 AgentLogDAO 记录审计日志。
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


class TaskDAO:
    """分析任务数据访问。

    【L3 设计】tasks 表是整个系统的"任务主干"——
    一个 task 串联 reports / evidence_map / chunk_embeddings /
    agent_logs / memory_summaries / agent_memories 六张子表。
    这体现"以任务为中心的数据组织"——所有数据都有 task_id 外键，
    查询时按 task_id 过滤就能拿到该任务的全量上下文。
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        # 【L4 工程】pool 通过构造函数注入，而非全局变量
        # 好处：测试时注入测试库 pool，生产时注入生产库 pool
        self._pool = pool

    async def create(
        self,
        task_id: str,
        title: str,
        competitors: list[str],
        dimensions: list[str],
        pipeline_mode: str = "pipeline",
    ) -> str:
        """创建新任务。

        【L4 工程】为什么 task_id 由调用者传入而非数据库自动生成？
        ------------------------------------------------------------
        竞品分析系统的 task_id 由 Supervisor Agent 在启动时生成，
        这样 Agent 拿到 task_id 后立即开始写入日志/记忆——
        不需要等 INSERT 返回。减少一次 DB round-trip。
        这在 Supervisor 架构中尤其重要——task_id 贯穿整个分析生命周期。

        【L4 工程】$3::jsonb 为什么需要显式类型转换？
        ------------------------------------------------------------
        asyncpg 传 Python list 时默认映射为 PostgreSQL 的 TEXT[]，
        但我们的列是 JSONB 类型。不加 ::jsonb，PG 会报类型不匹配。
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tasks (id, title, competitors, dimensions, pipeline_mode)
                   VALUES ($1, $2, $3::jsonb, $4::jsonb, $5)
                   RETURNING id""",
                task_id, title, competitors, dimensions, pipeline_mode,
            )
            return str(row["id"])

    async def get(self, task_id: str) -> dict | None:
        """按 ID 获取任务。

        返回 None 而非抛异常——让调用方自己决定"不存在"怎么处理。
        常见用法：task = await dao.get(tid); if task is None: raise HTTPException(404)
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1", task_id
            )
            return dict(row) if row else None

    async def update_status(self, task_id: str, status: str) -> None:
        """更新任务状态。

        【L4 工程】为什么只更新 status 不更新其他字段？
        ------------------------------------------------------------
        单一职责：每个方法只改一个字段，避免"大 UPDATE"误改数据。
        如果需要同时改多个字段，加新方法（如 update_all）。
        这是 CQRS 思想在 DAO 层的简化应用。

        status 状态机: pending → running → completed/failed
        updated_at 自动更新为 NOW()——用于监控"任务卡了多久"。
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET status = $2, updated_at = NOW()
                   WHERE id = $1""",
                task_id, status,
            )


class ReportDAO:
    """分析报告数据访问。

    reports 表支持多版本——每次 Writer Agent 重写都会创建新版本，
    version 字段递增。质检系统根据 quality_score 选择最优版本。
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        task_id: str,
        content: str,
        quality_score: float | None = None,
        quality_details: dict | None = None,
        version: int = 1,
    ) -> str:
        """创建新报告版本。

        quality_score 为 None 表示尚未质检——Writer 先写入，

        这体现异步解耦：Writer 写报告Quality Agent 稍后更新分数。和 Quality 打分是独立的 Agent 操作。
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO reports (task_id, content, quality_score,
                                        quality_details, version)
                   VALUES ($1, $2, $3, $4::jsonb, $5)
                   RETURNING id""",
                task_id, content, quality_score, quality_details, version,
            )
            return str(row["id"])

    async def get_latest(self, task_id: str) -> dict | None:
        """获取某任务的最新版本报告。

        ORDER BY version DESC LIMIT 1——最高版本号 = 最新版本。
        如果没有任何报告（刚创建任务），返回 None。
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM reports
                   WHERE task_id = $1
                   ORDER BY version DESC
                   LIMIT 1""",
                task_id,
            )
            return dict(row) if row else None

    async def get_all_versions(self, task_id: str) -> list[dict]:
        """获取某任务的所有版本报告（按版本升序）。

        审计场景：追溯"第一版报告长什么样、质检发现了什么问题、
        重写后哪些地方改了"。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM reports
                   WHERE task_id = $1
                   ORDER BY version ASC""",
                task_id,
            )
            return [dict(r) for r in rows]

    async def get_by_quality_threshold(
        self, task_id: str, min_score: float
    ) -> list[dict]:
        """获取某任务中质检分数 >= min_score 的报告。

        典型用法：get_by_quality_threshold(task_id, 80.0)
        → 只返回"合格"报告，低于 80 分的被视为不合格需要重写。

        【L4 工程】threshold 为什么是参数不是常量？
        → 不同分析维度可能有不同阈值：功能分析 70 分就够，
          定价分析需要 85 分（涉及金额，要更谨慎）。
          参数化让调用方灵活控制。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM reports
                   WHERE task_id = $1 AND quality_score >= $2
                   ORDER BY version DESC""",
                task_id, min_score,
            )
            return [dict(r) for r in rows]


class EvidenceDAO:
    """证据追溯数据访问。

    【L3 面试必问】为什么竞品分析系统需要 evidence_map？
    ------------------------------------------------------------
    面试官："分析报告不就行了，为什么还要存证据？"

    答："三个理由：
      1) 可信度——报告中每个结论都必须能追溯到来源 URL + 原文片段，
         否则 AI 可能幻觉（编造一个看起来很真的分析）。
      2) 合规性——竞品分析可能用于商业决策，必须有据可查。
      3) 可复核——质检 Agent 通过 evidence 验证报告的准确性。"
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def batch_insert(
        self, report_id: str, evidences: list[dict]
    ) -> None:
        """批量插入证据记录。

        【L4 工程】为什么用 executemany 而不是循环 INSERT？
        ------------------------------------------------------------
        一次分析可能有 20-50 条证据。逐条 INSERT = 20-50 次
        DB round-trip（每次 ~1ms 网络延迟 × 50 = 50ms）。
        executemany 打包成一次 TCP 往返 → 一次 5ms 搞定。
        性能差距：50ms vs 5ms，差 10 倍。

        对面试官说："我们用 executemany 把 batch 操作压到一次
        round-trip，这是 asyncpg 比 psycopg2 快的核心原因之一——
        asyncpg 支持 binary protocol 批量传输。"
        """
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO evidence_map (report_id, claim, source_url,
                                             source_text, dimension)
                   VALUES ($1, $2, $3, $4, $5)""",
                [
                    (
                        report_id,
                        e["claim"],
                        e["source_url"],
                        e.get("source_text"),
                        e.get("dimension"),
                    )
                    for e in evidences
                ],
            )

    async def get_by_report(self, report_id: str) -> list[dict]:
        """按报告 ID 获取所有证据。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM evidence_map WHERE report_id = $1", report_id
            )
            return [dict(r) for r in rows]

    async def get_by_dimension(
        self, report_id: str, dimension: str
    ) -> list[dict]:
        """按报告 ID + 维度获取证据。

        质检 Agent 逐个维度检查——"定价分析的 5 条证据来源可靠吗？"
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM evidence_map
                   WHERE report_id = $1 AND dimension = $2""",
                report_id, dimension,
            )
            return [dict(r) for r in rows]


class ChunkEmbeddingDAO:
    """文档分块向量存储数据访问。

    【L3 面试必问】chunk_embedding 和 agent_memories 都用向量检索，
    为什么分两张表？
    ------------------------------------------------------------
    面试官："两张表都存向量，为什么不合并？"

    答："三个原因：
      1) 生命周期不同——chunk 随 task 删，memory 跨 task 保留。
         chunk 的 ON DELETE CASCADE vs memory 的 ON DELETE SET NULL
         就说明了它们的依附关系不同。
      2) 检索语义不同——chunk 按 task_id 过滤（"在这个任务里找相近的"），
         memory 按 user_id 过滤（"这个用户的长期记忆里有类似内容吗"）。
      3) 索引策略不同——chunk 量大（每任务数百条），需要 HNSW 索引；
         memory 量小（每用户数千条），HNSW 也够但权重排序公式不同。
      合并成一张表 → 用 type 字段区分 → 查询必须多加一个 WHERE type=...
      条件，索引选择性下降，且语义耦合。分表更干净。"

    这是数据库设计中的"按查询模式分表"原则——
    不以实体相似性建表，以查询模式建表。
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def batch_insert(
        self, task_id: str, chunks: list[dict]
    ) -> None:
        """批量插入文档分块及嵌入向量。

        【L4 工程】$5::vector 为什么显式转换？
        ------------------------------------------------------------
        asyncpg 不认识 PG 的 vector 类型，传 list[float] 时
        不知道该映射到哪个 PG 类型。加 ::vector 显式告诉 PG：
        "把这个 float[] cast 成 vector(1024)"。

        面试官反问："不用 ::vector，改用 register_adapter 注册自定义类型呢？"
        → "可以，但 register_adapter 是全局注册，影响整个进程。
          显式转换更局部、更可控，不污染全局。"
        """
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO chunk_embeddings (task_id, chunk_text,
                                                 chunk_index, source_url,
                                                 embedding)
                   VALUES ($1, $2, $3, $4, $5::vector)""",
                [
                    (
                        task_id,
                        c["chunk_text"],
                        c["chunk_index"],
                        c["source_url"],
                        c["embedding"],
                    )
                    for c in chunks
                ],
            )

    async def similarity_search(
        self,
        task_id: str,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[dict]:
        """向量相似度检索（余弦距离）。

        【L3 面试必问】为什么用 <=>（余弦距离）不用 <->（欧氏距离）？
        ------------------------------------------------------------
        面试官："<=> 和 <-> 有什么区别？为什么选 <=>？"

        答："核心区别：归一化后的向量，余弦距离比欧氏距离更稳定。

        例子：两个语义相似的文本 A="飞书定价 25 元/人/月" 和
        B="飞书每人每月 25 元"——换了词序但语义相同。
        - 欧氏距离可能因为词序不同把向量推远（词序影响位置）
        - 余弦距离看方向不看大小——两句话的语义方向一致，余弦距离很小

        BGE-M3 的输出是归一化向量（||v||=1），此时：
        余弦距离 = 1 - cos_sim，范围 [0, 2]，越小越相似。
        <=> 是 PG 对余弦距离的原生实现，配合 HNSW 索引极快。

        pgvector 三种距离操作符对比：
          <->  欧氏距离（L2）：适合坐标/图像特征
          <#>  内积（负值）：适合非归一化向量
          <=>  余弦距离：适合归一化语义向量 ← 我们用的"
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, task_id, chunk_text, chunk_index, source_url,
                          embedding <=> $2::vector AS distance,
                          created_at
                   FROM chunk_embeddings
                   WHERE task_id = $1
                   ORDER BY embedding <=> $2::vector
                   LIMIT $3""",
                task_id, query_embedding, top_k,
            )
            return [dict(r) for r in rows]

    async def keyword_search(
        self,
        task_id: str,
        keywords: str,
        top_k: int = 10,
    ) -> list[dict]:
        """中文全文检索（zhparser 分词）。

        【L3 面试必问】向量检索和关键词检索，什么时候用哪个？
        ------------------------------------------------------------
        向量检索(<=>)：找"意思相近但不一定含相同词"的内容
          → "飞书定价" 找到 "字节跳动旗下飞书服务费用标准"
          适合：开放性探索、语义发散搜索

        关键词检索(@@)：找"包含这些词"的内容
          → "飞书定价" 精确匹配含"飞书"和"定价"的文档
          适合：精确查找、证据定位

        两者互补——竞品分析场景通常先用向量检索找相关文档，
        再用关键词检索确认某个具体信息是否存在。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, task_id, chunk_text, chunk_index, source_url,
                          created_at
                   FROM chunk_embeddings
                   WHERE task_id = $1
                     AND to_tsvector('zhparse', chunk_text)
                         @@ plainto_tsquery('zhparse', $2)
                   ORDER BY created_at DESC
                   LIMIT $3""",
                task_id, keywords, top_k,
            )
            return [dict(r) for r in rows]


class MemorySummaryDAO:
    """摘要记忆数据访问——跨轮对话压缩缓存。

    【L3 面试必问】memory_summaries 和 agent_memories 有什么区别？
    ------------------------------------------------------------
    这是 Agent 记忆系统的两个层级：

    memory_summaries（摘要记忆 = 短期/工作记忆）：
      - 粒度：按 round_range（如 "1-10"）组织
      - 生命周期：随 task，task 结束后可归档或丢弃
      - 作用：压缩前 10 轮对话 → 避免上下文窗口溢出
      - 类比：Supervisor ReAct 循环的"盘前笔记"

    agent_memories（长期记忆 = 知识库）：
      - 粒度：按条组织，含重要性 + 时间衰减
      - 生命周期：跨 task，跨会话
      - 作用：积累跨会话的知识 → 下次分析同一竞品时更快
      - 类比：你的"行业经验"，不会因为换了项目就忘

    面试官追问："为什么不用一个表 + type 字段？"
    → 同 ChunkEmbeddingDAO 的理由——查询模式不同，
      分表比 type 字段更干净。而且摘要记忆可能被整个清除
      （task 完成），长期记忆不应被清除——分表让 DROP/TRUNCATE 更安全。
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def save(
        self,
        task_id: str,
        round_range: str,
        summary_text: str,
        summary_type: str = "incremental",
    ) -> str:
        """保存摘要。

        【L3 知识点】incremental vs full_merge 两种摘要策略
        ------------------------------------------------------------
        incremental（递增摘要）：
          每轮对话后立即生成，只描述本轮新信息。
          快，但可能丢失跨轮上下文。

        full_merge（全量合并）：
          每 10 轮生成一次，对前 10 轮做全量合并再摘要。
          慢（需传 10 轮对话给 LLM），但保留跨轮关系更准。

        本系统：每轮 incremental，每 10 轮 full_merge 校准。
        面试时可以画这个模式——"递增+定期全量合并"是工程实践中
        最常用的对话压缩策略。
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO memory_summaries (task_id, round_range,
                                                 summary_text, summary_type)
                   VALUES ($1, $2, $3, $4)
                   RETURNING id""",
                task_id, round_range, summary_text, summary_type,
            )
            return str(row["id"])

    async def get_latest(self, task_id: str) -> dict | None:
        """获取某任务最新的摘要。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM memory_summaries
                   WHERE task_id = $1
                   ORDER BY created_at DESC
                   LIMIT 1""",
                task_id,
            )
            return dict(row) if row else None

    async def get_by_round_range(
        self, task_id: str, round_range: str
    ) -> list[dict]:
        """按轮次范围获取摘要。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM memory_summaries
                   WHERE task_id = $1 AND round_range = $2
                   ORDER BY created_at DESC""",
                task_id, round_range,
            )
            return [dict(r) for r in rows]


class AgentMemoryDAO:
    """长期记忆数据访问。

    【L5 面试核心】三因子加权检索公式——本系统最核心的工程创新之一
    ============================================================
    排序公式：
      score = (1.0 - cosine_distance) × importance × 0.5^(age_days / half_life_days)

    三个因子拆解：

    1) 语义相似度 (1.0 - cosine_distance)，范围 0~1：
       query 和 memory 的语义匹配程度。cosine_distance 越小越相似，
       所以用 1.0 - distance 转为相似度（越大越相关）。

    2) 重要性 (importance)，范围 0~1：
       不同 memory_type 有不同权重：
         decision   = 0.9 — "我们决定用 DuckDuckGo 而非 Google API"
         preference = 0.7 — "用户偏好飞书文档格式"
         fact        = 0.5 — "钉钉 2024 Q4 用户数 7 亿"
         chat        = 0.1 — 闲聊内容

    3) 时间衰减 0.5^(age_days / half_life_days)：
       半衰期决定记忆"遗忘"速度：
         decision (90天半衰期) → 3个月后才衰减到 50%
         chat     (7天半衰期)  → 1周后就衰减到 50%
       类比：人的记忆——重要决策记很久，闲聊很快忘。

    【面试官追问】"为什么用指数衰减不用线性衰减？"
    → 指数衰减更符合认知科学中的 Ebbinghaus 遗忘曲线——
      遗忘速度先快后慢，而不是匀速遗忘。
      POWER(0.5, age/half_life) = e^(-λt) where λ = ln(2)/half_life
      这是标准的一阶指数衰减模型。

    【面试官追问】"如果用户需要永久记住某条记忆怎么办？"
    → 设置 half_life_days 为一个极大值（如 36500 = 100年），
      或把 importance 设到 1.0。两者配合可让衰减几乎为 0。
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert(
        self,
        user_id: str,
        memory_type: str,
        content: str,
        importance: float = 0.5,
        embedding: list[float] | None = None,
        source_task_id: str | None = None,
        half_life_days: int = 30,
    ) -> str:
        """插入长期记忆。

        【L4 工程】embedding 为什么是可选的（None）？
        ------------------------------------------------------------
        不是所有记忆都需要向量检索。例如：
          "用户偏好 Markdown 格式" —— 这是一条规则，不需要向量匹配。
          存 embedding=None 省 2KB 存储。
        但无 embedding 的记忆只能用 keyword_search（zhparser），
        不能用 similarity_search。这是"按需求选择检索方式"的体现。
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO agent_memories (user_id, memory_type, content,
                                               importance, embedding,
                                               source_task_id, half_life_days)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7)
                   RETURNING id""",
                user_id, memory_type, content, importance,
                embedding, source_task_id, half_life_days,
            )
            return str(row["id"])

    async def similarity_search(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 60,
    ) -> list[dict]:
        """向量相似度检索，含时间衰减加权。

        【L4 工程】top_k=60 不是魔数——
        ------------------------------------------------------------
        这是"粗排数量"，后续交给 rerank 模型精排到 10 条。
        60 = 召回足够多的候选（覆盖 99% 的相关记忆）
           + 精排延迟可接受（60 对 cross-encoder ≈ 300ms）
        如果设 100+，精排延迟超 500ms，用户体验变差。
        如果设 20，可能漏掉相关内容。

        返回结果含 weighted_score 字段，便于调试记忆排序效果。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, user_id, memory_type, content, importance,
                          access_count, half_life_days, created_at,
                          last_accessed,
                          (1.0 - (embedding <=> $2::vector))
                          * importance
                          * POWER(0.5,
                                  EXTRACT(DAY FROM NOW() - created_at)
                                  / half_life_days
                            ) AS weighted_score
                   FROM agent_memories
                   WHERE user_id = $1 AND is_active = true
                   ORDER BY weighted_score DESC
                   LIMIT $3""",
                user_id, query_embedding, top_k,
            )
            return [dict(r) for r in rows]

    async def keyword_search(
        self,
        user_id: str,
        keywords: str,
        top_k: int = 30,
    ) -> list[dict]:
        """中文全文检索长期记忆（zhparser）。

        用于精确匹配场景："我记得之前分析过飞书的定价策略"
        → keyword_search(user_id, "飞书 定价")。
        不需要向量——关键词足够定位。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, user_id, memory_type, content, importance,
                          access_count, half_life_days, created_at,
                          last_accessed
                   FROM agent_memories
                   WHERE user_id = $1
                     AND is_active = true
                     AND to_tsvector('zhparse', content)
                         @@ plainto_tsquery('zhparse', $2)
                   ORDER BY created_at DESC
                   LIMIT $3""",
                user_id, keywords, top_k,
            )
            return [dict(r) for r in rows]

    async def soft_delete(self, memory_id: str) -> None:
        """软删除记忆——标记 is_active=false，不物理删除数据。

        【L4 工程】为什么软删除不硬删除？
        ------------------------------------------------------------
        1) 可恢复——用户误删可以 undo
        2) 审计——硬删除后不知道"什么被删了"，软删除保留全量
        3) 训练——被软删除的记忆可能是负样本，可用来训练冲突检测模型
        4) 法规——部分行业要求数据保留 N 年，即使标记"无效"也不能物理删

        参考：JVM GC 的"标记-清除"——先标记不可达，再统一清除。
        我们把这个逻辑分两步：soft_delete 只标记，archive_old 做批量清除。
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE agent_memories
                   SET is_active = false
                   WHERE id = $1""",
                memory_id,
            )

    async def archive_old(self, days: int = 180) -> int:
        """归档超期记忆（标记 is_active=false）。

        【L4 工程】archive_old 为什么只标记不物理删除？
        ------------------------------------------------------------
        同上——软删除模式。但 archive 和 soft_delete 的区别：
        - soft_delete: 用户主动删某条记忆（精确，按 ID）
        - archive_old: 按时间批量归档（宽泛，按创建时间）
        两条路径最终都设 is_active=false。

        days 默认 180（半年）—— 半年没访问的记忆视为"冷数据"。
        返回受影响行数，供上层监控"这次归档了多少条"。
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE agent_memories
                   SET is_active = false
                   WHERE is_active = true
                     AND created_at < NOW() - ($1 || ' days')::INTERVAL""",
                str(days),
            )
            # asyncpg 返回 command tag 如 "UPDATE 23"
            count = int(result.split()[-1]) if result else 0
            return count

    async def get_conflicts(
        self,
        user_id: str,
        content_embedding: list[float],
        threshold: float = 0.85,
    ) -> list[dict]:
        """检测语义冲突——相似度 >= threshold 视为冲突。

        【L3 面试追问】记忆冲突是什么？为什么要检测？
        ------------------------------------------------------------
        场景：用户第一次问"飞书的价格"，系统记录 memory "飞书 25元/人/月"。
              三个月后飞书涨价到 30 元，用户再次问——
              旧记忆和新事实冲突。

        检测：新信息的 embedding 与旧记忆的 embedding 相似度 >= 0.85，
              但内容含义矛盾（如价格数字不同）。
              标记为冲突 → Supervisor 决定：更新旧记忆 / 保留两条并注明时间。

        阈值 0.85 意味"语义几乎相同但可能细节不同"——
        0.7 太宽（把不相关的也拉进来），0.95 太窄（漏掉真实冲突）。
        0.85 是经验值，可根据场景调整。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, user_id, memory_type, content, importance,
                          1.0 - (embedding <=> $2::vector) AS similarity
                   FROM agent_memories
                   WHERE user_id = $1
                     AND is_active = true
                     AND embedding IS NOT NULL
                     AND (1.0 - (embedding <=> $2::vector)) >= $3
                   ORDER BY similarity DESC""",
                user_id, content_embedding, threshold,
            )
            return [dict(r) for r in rows]


class AgentLogDAO:
    """Agent 审计日志数据访问。

    【L5 面试必问】Harness Engineering 的审计模块怎么落地？
    ------------------------------------------------------------
    审计日志是整个系统的"黑匣子"——
    每条日志记录：谁(agent_name) + 做了什么(action) +
    入参(request) + 出参(response) + 是否出错(error) +
    耗时多少(duration_ms)。

    面试官："日志量大了怎么办？"
    → "三条策略：
      1) 按 task_id 分区——每个任务的日志独立查询，不需要全表扫描
      2) 定期归档——超期日志转移到 agent_logs_archive 表或对象存储
      3) 采样——非错误日志按 10% 采样（可配置），错误日志 100% 保留"
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def log(
        self,
        task_id: str,
        agent_name: str,
        action: str,
        request: dict | None = None,
        response: dict | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """记录一次 Agent 操作日志。

        【L4 工程】log 方法为什么是"fire and forget"——不返回值？
        ------------------------------------------------------------
        日志写入不应阻塞主流程。async 已经让写入不阻塞事件循环，
        不需要 await 返回值（无返回值 → 不需要赋值）。
        如果日志写入失败，exceptions 会被 asyncpg 抛出，
        但调用方通常不处理——日志是辅助链路，不能阻断主链路。
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_logs (task_id, agent_name, action,
                                           request, response, error,
                                           duration_ms)
                   VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)""",
                task_id, agent_name, action, request,
                response, error, duration_ms,
            )

    async def get_by_task(self, task_id: str) -> list[dict]:
        """获取某任务的全部日志（按时间升序）。

        按时间升序 → 可以"重放"整个任务的 Agent 操作时间线。
        质检 Agent 用这个来复盘："哪一步花了最长时间？哪一步出了错？"
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM agent_logs
                   WHERE task_id = $1
                   ORDER BY created_at ASC""",
                task_id,
            )
            return [dict(r) for r in rows]

    async def get_recent_errors(
        self, task_id: str, limit: int = 20
    ) -> list[dict]:
        """获取某任务最近的错误日志。

        运维视角：任务失败时，第一时间拉取最近 20 条错误，
        不需要在全部日志里翻找。WHERE error IS NOT NULL 精准过滤。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM agent_logs
                   WHERE task_id = $1 AND error IS NOT NULL
                   ORDER BY created_at DESC
                   LIMIT $2""",
                task_id, limit,
            )
            return [dict(r) for r in rows]
