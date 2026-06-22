-- ============================================================
-- 竞品分析多Agent协作系统 — 数据库 Schema
-- PostgreSQL 16+ + pgvector + zhparser
-- ============================================================
-- 【L3 架构概览】9 张表，分三层：
--   业务层:    tasks → reports → evidence_map
--   向量层:    chunk_embeddings, agent_memories
--   运维层:    agent_logs, memory_summaries, checkpoints, checkpoint_writes
--
-- 【L4 工程设计原则】
--   1) UUID 主键 > SERIAL（分布式安全，无碰撞风险）
--   2) JSONB 存半结构化数据（竞品列表、分析维度随时变）
--   3) HNSW 索引 > IVFFlat（查询快 10-100 倍，适合读多写少）
--   4) ON DELETE CASCADE 按语义选择（子实体随父实体一同删除）
--   5) pgvector vector(1024) 对应 BGE-M3 嵌入维度
--   6) zhparser 中文分词 + pg_bigm 模糊匹配双引擎
--
-- 【L5 面试准备】
--   面试官可能问："为什么不用 MySQL？"
-- → "三个不可替代的理由：
--    1) pgvector — MySQL 没有原生向量索引（向量数据库赛道 PG 领先）
--    2) zhparser — MySQL 的中文分词插件 ngram 基于字符级切分，
--       zhparser 基于 SCWS 词典分词，准确性高一个数量级
--    3) JSONB — MySQL 的 JSON 类型不支持 GIN 索引，查询慢 5-10 倍"
-- ============================================================

-- ============================================================
-- 扩展注册
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- UUID 生成
CREATE EXTENSION IF NOT EXISTS vector;         -- pgvector 向量类型
CREATE EXTENSION IF NOT EXISTS zhparser;       -- 中文全文检索分词

-- ============================================================
-- zhparser 中文全文检索配置
-- ============================================================
-- 【L3 知识点】为什么只索引 n,v,a,i,e,l,d 七种词性？
-- n(名词) v(动词) a(形容词) i(成语) e(叹词) l(习用语) d(副词)
-- 不索引的：r(代词) u(助词) w(标点) p(介词) c(连词) m(数词) q(量词)
-- 原因：代词和助词对语义搜索没有区分度，
--   "我的产品很好用" → 分词后去掉"我""的"只剩"产品""好用"
--   这是信息检索领域的"停用词"思想通过词性过滤实现
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_ts_config WHERE cfgname = 'zhparse'
    ) THEN
        CREATE TEXT SEARCH CONFIGURATION zhparse (PARSER = zhparser);
        ALTER TEXT SEARCH CONFIGURATION zhparse
            ADD MAPPING FOR n,v,a,i,e,l,d WITH simple;
    END IF;
END
$$;


-- ============================================================
-- 1. tasks — 分析任务（系统主干表）
-- ============================================================
-- 【L3 架构】tasks 是整个数据模型的"根"——所有其他表都通过
-- task_id 外键串联。一个 task 对应一次完整的竞品分析任务。
--
-- 【L4 工程决策】competitors 和 dimensions 为什么用 JSONB 不用关联表？
-- 竞品列表 ["飞书","钉钉","Notion"] 和分析维度 ["功能","定价","市场"]
-- 是典型的"可变长度列表 + 任务级配置"，用 JSONB 比关联表好：
--   1) 不需要 JOIN 三张表——一次 SELECT 拿到全部
--   2) 每个任务的竞品列表不同，关联表模式会产生稀疏数据
--   3) 不需要拆成 TASK_COMPETITOR / TASK_DIMENSION 两张中间表
-- 代价：JSONB 不能做外键约束——但竞品名和维度名本身不是独立实体，
--   不需要参照完整性。这是"半结构化数据存 JSONB"的经典场景。
CREATE TABLE IF NOT EXISTS tasks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       VARCHAR(500) NOT NULL,      -- 任务名称，如"协同办公软件竞品分析"
    competitors JSONB NOT NULL,              -- 竞品列表 ["飞书","钉钉","Notion"]
    dimensions  JSONB NOT NULL,              -- 分析维度 ["功能","定价","市场"]
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
                    -- 状态机: pending → running → completed / failed
    pipeline_mode VARCHAR(20) NOT NULL DEFAULT 'pipeline',
                    -- pipeline = 确定性并行, supervisor = 开放性探索
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 【L4 工程】索引设计——按查询频率建索引
-- status: 看"有哪些任务在运行"（监控面板）
-- created_at: 看"最近创建了哪些任务"（列表按时间排序）
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at);


-- ============================================================
-- 2. reports — 分析报告（支持多版本）
-- ============================================================
-- 【L3 知识点】为什么 reports 支持多版本？
-- Writer Agent 先写初版，Quality Agent 质检后发现分数低（< 80），
-- Supervisor 决定重写 → Writer 创建 version=2。
-- 保留历史版本可以对比"初版有什么问题、重写改了什么"——
-- 这是 Agent 系统的"带版本控制的迭代优化"模式。
--
-- 【L4 工程】ON DELETE CASCADE：task 删除 → 报告全部删除
-- 合理——没有任务就没有独立存在的报告。
CREATE TABLE IF NOT EXISTS reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    content         TEXT,                    -- Markdown 格式报告内容（可超长）
    quality_score   FLOAT,                  -- 质检分数 0-100，NULL = 未质检
    quality_details JSONB,                  -- 五维评分详情（完整性/准确性/可读性/...）
    version         INT NOT NULL DEFAULT 1, -- 版本号，每次重写 +1
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reports_task_id ON reports (task_id);
CREATE INDEX IF NOT EXISTS idx_reports_quality_score ON reports (quality_score);


-- ============================================================
-- 3. evidence_map — 证据追溯（可信度基石）
-- ============================================================
-- 【L5 面试核心】evidence_map 是竞品分析系统的"可信度引擎"——
-- 每个分析结论都必须能追溯到来源 URL + 原文片段。
-- 没有证据支撑的结论在商业场景中无法使用（"你说飞书比钉钉便宜，
-- 证据呢？"）。
--
-- 【L4 工程】ON DELETE CASCADE：报告删除 → 证据也删
-- 证据依附于报告，报告删了证据无意义。
CREATE TABLE IF NOT EXISTS evidence_map (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id   UUID NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    claim       TEXT NOT NULL,              -- 分析结论（如"飞书定价低于钉钉 20%"）
    source_url  TEXT NOT NULL,              -- 来源 URL（可点击验证）
    source_text TEXT,                        -- 引用的原文片段
    dimension   VARCHAR(50),                -- 所属维度（功能/定价/市场）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_report_id ON evidence_map (report_id);
CREATE INDEX IF NOT EXISTS idx_evidence_dimension ON evidence_map (dimension);


-- ============================================================
-- 4. chunk_embeddings — 文档分块向量存储（RAG 检索核心）
-- ============================================================
-- 【L3 面试必问】为什么用 HNSW 索引而不是 IVFFlat？
-- IVFFlat（倒排文件）：
--   - 构建快（扫描所有向量建聚类中心）
--   - 查询慢（需要指定 probes 参数，probes 大多扫太多，少漏召回）
--   - 适合：数据频繁变动的场景
-- HNSW（分层可导航小世界图）：
--   - 构建慢（需要建多层图结构，内存占用大）
--   - 查询极快（O(log N) 图遍历，比 IVFFlat 快 10-100 倍）
--   - 适合：读多写少场景 ← 竞品分析的 chunk 写入一次，查询频繁
-- 结论：分析场景查询远多于写入，HNSW 是最优选择。
--
-- 【L4 工程】vector(1024) 为什么是 1024？
-- BGE-M3 嵌入模型输出 1024 维。这个维度是写死的——
-- 如果换模型（如 OpenAI 1536 维），需要 ALTER TABLE 改列类型。
-- 实际上不同维度的向量不可比，所以一个表通常只存一种模型的嵌入。
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    chunk_text  TEXT NOT NULL,              -- 文档分块原文（用于检索后展示）
    chunk_index INT NOT NULL,               -- 分块序号（保持原文顺序）
    source_url  TEXT NOT NULL,              -- 来源 URL（可追溯）
    embedding   vector(1024),               -- BGE-M3 1024 维嵌入向量
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunk_task_id ON chunk_embeddings (task_id);
-- vector_cosine_ops 指定余弦距离运算——配合 <=> 操作符使用
CREATE INDEX IF NOT EXISTS idx_chunk_embedding_hnsw
    ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);


-- ============================================================
-- 5. agent_logs — Agent 审计日志
-- ============================================================
-- 【L5 面试必问】Harness Engineering 的审计模块落地方案
-- agent_logs 是整个系统的"黑匣子"——
-- 每条日志记录谁(agent_name) + 做了什么(action) +
-- 入参(request) + 出参(response) + 是否出错(error) +
-- 耗时多少(duration_ms)。
--
-- 面试官追问："日志量大了怎么办？"
-- → "按 created_at 做时间分区（如果 PG 12+ 用原生分区表），
--   保留最近 90 天热数据，超期归档到对象存储或 agent_logs_archive。
--   也可在 call_tool 层做采样（非错误按 10% 记录）。"
CREATE TABLE IF NOT EXISTS agent_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_name   VARCHAR(50) NOT NULL,      -- collector/analyzer/writer/quality/supervisor
    action       VARCHAR(100) NOT NULL,     -- 如 web_search / embed_texts / grade_report
    request      JSONB,                     -- 请求内容（方便重放调试）
    response     JSONB,                     -- 响应内容
    error        TEXT,                      -- 错误信息（NULL = 无错误）
    duration_ms  FLOAT,                     -- 耗时（毫秒），NULL = 未记录
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_logs_task_id ON agent_logs (task_id);
CREATE INDEX IF NOT EXISTS idx_agent_logs_agent_name ON agent_logs (agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_logs_created_at ON agent_logs (created_at);


-- ============================================================
-- 6. memory_summaries — 摘要记忆（短期/工作记忆）
-- ============================================================
-- 【L3 核心考点】memory_summaries 和 agent_memories 的区别
-- 这是 Agent 记忆系统的"两层架构"：
--   memory_summaries = 短期/工作记忆
--     - 粒度：按 round_range（如 "1-10"）组织
--     - 生命周期：随 task，task 完成后可丢弃
--     - 作用：压缩前 10 轮对话，避免上下文窗口溢出
--     - 策略：incremental 每轮 + full_merge 每 10 轮校准
--   agent_memories = 长期/永久记忆（见下一张表）
--
-- 类比给面试官："memory_summaries 是你的会议笔记（会后就扔），
-- agent_memories 是你的行业知识库（长期积累）。"
CREATE TABLE IF NOT EXISTS memory_summaries (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    round_range  VARCHAR(50) NOT NULL,      -- 如 "1-10", "11-20"
    summary_text TEXT NOT NULL,              -- LLM 生成的摘要内容
    summary_type VARCHAR(20) NOT NULL DEFAULT 'incremental',
                     -- incremental = 递增摘要, full_merge = 全量合并
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_summaries_task_id ON memory_summaries (task_id);


-- ============================================================
-- 7. agent_memories — 长期记忆（跨会话知识库）
-- ============================================================
-- 【L5 面试核心】三因子加权检索 + 时间衰减遗忘策略
-- 这是本系统设计上最出彩的点之一，面试必问。
--
-- 检索排序公式（在 DAO 层执行）：
--   score = (1.0 - cosine_distance) × importance × 0.5^(age/half_life)
--
-- 三个因子：
--   1) 语义相似度：query 和 memory 的内容匹配度
--   2) 重要性：decision(0.9) > preference(0.7) > fact(0.5) > chat(0.1)
--   3) 时间衰减：半衰期按类型不同——decision 90天, chat 7天
--
-- 【L4 工程】ON DELETE SET NULL vs CASCADE
-- 为什么 agent_memories 用 SET NULL 而其他表用 CASCADE？
-- task 删除 → 任务数据消失是合理（CASCADE），
-- 但 task 过程中产生的长期知识不应丢失——
-- "飞书定价分析时发现 API 限制"这条知识即使 task 删了也值得保留。
-- source_task_id 设为 NULL 表示"来源已不可追溯，但知识保留"。
CREATE TABLE IF NOT EXISTS agent_memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(100) NOT NULL,  -- 用户标识（多用户隔离）
    memory_type     VARCHAR(30) NOT NULL,   -- decision/preference/fact/chat
    content         TEXT NOT NULL,          -- 记忆正文
    importance      FLOAT NOT NULL DEFAULT 0.5,
                        -- 重要性 0.0-1.0：decision=0.9 preference=0.7 fact=0.5 chat=0.1
    embedding       vector(1024),          -- BGE-M3 嵌入，NULL = 无向量（仅支持关键词检索）
    source_task_id  UUID REFERENCES tasks(id) ON DELETE SET NULL,
                        -- 来源任务，task 删除后置 NULL 但不删除记忆
    access_count    INT NOT NULL DEFAULT 0, -- 被检索次数（用于热度排序）
    half_life_days  INT NOT NULL DEFAULT 30,
                        -- 半衰期（天）：decision=90 preference=60 fact=30 chat=7
    is_active       BOOLEAN NOT NULL DEFAULT true,
                        -- 软删除标记：false = 已删除/已归档
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed   TIMESTAMPTZ              -- 最后被检索时间
);

-- 复合查询路径：按用户 + 类型 + 活跃状态过滤
CREATE INDEX IF NOT EXISTS idx_agent_memories_user_id ON agent_memories (user_id);
CREATE INDEX IF NOT EXISTS idx_agent_memories_memory_type ON agent_memories (memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_memories_is_active ON agent_memories (is_active);
-- HNSW 向量索引：加速 similarity_search
CREATE INDEX IF NOT EXISTS idx_agent_memories_embedding_hnsw
    ON agent_memories USING hnsw (embedding vector_cosine_ops);


-- ============================================================
-- 8-9. LangGraph Checkpoint 表（超标交付）
-- ============================================================
-- 【L3 知识点】LangGraph 的 Checkpoint 存储 ——
-- 这两张表是 LangGraph 官方 AsyncPostgresSaver 的标准表结构。
-- 有了它们，Supervisor Agent 的 ReAct 循环状态可以在每次
-- 节点执行后自动持久化到 PostgreSQL，实现：
--   1) 中断恢复——服务重启后从最近的 checkpoint 继续
--   2) 时间旅行——回退到任意历史状态重新决策
--   3) 审计——每个状态节点都有快照可追溯
--
-- 【L4 工程】为什么 LangGraph 选这个复合主键？
-- (thread_id, checkpoint_ns, checkpoint_id) 三元组保证：
--   - 同一对话线程的多个 checkpoint 共存（按 checkpoint_id 区分）
--   - 不同命名空间的 checkpoint 隔离（checkpoint_ns）
--   - 多线程并行不冲突（thread_id 分区）
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id           TEXT NOT NULL,
    checkpoint_ns       TEXT NOT NULL DEFAULT "",
    checkpoint_id       TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type                TEXT,
    checkpoint          JSONB NOT NULL,
    metadata            JSONB NOT NULL DEFAULT "{}",
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- checkpoint_writes: 存储 checkpoint 级别的待发送数据
-- 典型场景：Supervisor 暂停 → 保存待发送消息 → 恢复后继续发送
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id       TEXT NOT NULL,
    checkpoint_ns   TEXT NOT NULL DEFAULT "",
    checkpoint_id   TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    idx             INT NOT NULL,
    channel         TEXT NOT NULL,
    type            TEXT,
    value           JSONB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
