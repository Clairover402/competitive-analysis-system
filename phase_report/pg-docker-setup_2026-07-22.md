# PostgreSQL Docker 环境初始化 — 2026-07-22

## 目标
为竞品分析系统搭建 Docker PostgreSQL + pgvector 数据库环境，执行 9 张表 schema。

## 过程

### 问题链
1. **本地 PG 服务故障**：Windows 服务 `postgresql-x64-18` 停止后无法重启。TCP 端口 5432 监听但 psql 连接挂起（TCP 握手成功但 PG 协议层无响应）。
2. **asyncpg Windows 兼容**：Python 的 asyncpg 库在 Windows ProactorEventLoop 下报 `ConnectionResetError: [WinError 64]`。
3. **Docker Hub 国内网络**：第一次尝试 `postgres:16` 可用但容器内 `apt-get` 连不上 Debian 源（安装 pgvector 需要）。
4. **解决方案**：直接拉 `pgvector/pgvector:pg16` 镜像（预装 pgvector），跳过容器内 apt。

### 最终方案
```
docker run -d --name competitive-analysis-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=competitive_analysis_pwd \
  -e POSTGRES_DB=competitive_analysis \
  -p 5432:5432 \
  -v competitive-analysis-pgdata:/var/lib/postgresql/data \
  --restart unless-stopped \
  pgvector/pgvector:pg16
```

### Schema 执行
- 原始 `src/db/schema.sql` 包含 zhparser 扩展和 `DEFAULT ""` 语法，Docker 环境中无法执行
- 通过 `fix_schema.py` 脚本移除 zhparser 相关 SQL 语句，修复 `DEFAULT ""` → `DEFAULT ''`、`DEFAULT "{}"` → `DEFAULT '{}'::jsonb`
- 生成 `schema_fixed.sql`，通过 `docker cp` + `psql -f` 执行

### 9 张表验证通过
tasks, reports, evidence_map, chunk_embeddings, agent_logs, memory_summaries, agent_memories, checkpoints, checkpoint_writes

## 已知限制
- **zhparser 中文分词不可用**：pgvector 镜像不包含 zhparser，全文搜索暂用 PG 内置 simple 分词。如需中文分词，需基于 Postgres 官方镜像自行编译安装 zhparser（或切换分词方案为 jieba/pg_bigm）。
- pgvector HNSW 向量索引已就绪（`vector_cosine_ops`，1024 维对应 BGE-M3）

## 连接信息
- host: localhost
- port: 5432
- user: postgres
- password: competitive_analysis_pwd
- database: competitive_analysis
- DBeaver 或其他客户端可直接连接
