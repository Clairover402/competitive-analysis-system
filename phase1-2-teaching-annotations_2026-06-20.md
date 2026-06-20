# Phase 1+2 教学注释强化 — 交付记录

**时间**: 2026-06-20 20:44~21:20
**目标**: 为竞品分析系统 Phase 1/2 的全部代码添加 L3/L4/L5 面试导向教学注释

## 修改文件清单（7 个文件，全部代码功能不变）

| # | 文件 | 改动 | 行数（约） |
|---|------|------|-----------|
| 1 | `src/mcp/server.py` | MCP N×M→N+M 理论、ToolDef 设计决策、dict 选型、call_tool 安全切面、Harness Engineering 五层 | 210+ |
| 2 | `src/mcp/tools_rag.py` | Bi/Cross encoder 对比、BGE-M3 1024维选型、懒加载设计、FP16 原理、batch_size 决策、GPU 退 CPU | 210+ |
| 3 | `src/mcp/tools_web.py` | DuckDuckGo 选型 tradeoff、超时设计 15s/20s 差异、正则 vs BS4、降级策略、UA 原理 | 190+ |
| 4 | `src/mcp/__init__.py` | 工厂函数 vs 类设计、lambda 适配器模式、5 工具架构定位 | 140+ |
| 5 | `src/db/dao.py` | asyncpg vs SQLAlchemy、连接池模式、批量写入提升、余弦距离 vs 欧氏距离、三因子加权公式推导、软删除 vs 硬删除、时间衰减原理、冲突检测阈值 | 620+ |
| 6 | `src/db/schema.sql` | UUID vs SERIAL、JSONB vs 关联表、HNSW vs IVFFlat、ON DELETE CASCADE vs SET NULL 语义、zhparser 词性过滤、Checkpoint 三元组主键、长期记忆 vs 短期记忆分表原则 | 330+ |
| 7 | `src/db/__init__.py` | 模块架构说明、三层表结构定位 | 30+ |
| **合计** | — | — | **~1700+ 行教学注释** |

## 注释体系（三级标签）

每条教学注释用 `【L? ...】` 标签标注所属层级，面试候选人和项目开发者可快速定位：

- **【L3 面试必问】** — Agent 设计理论层。MCP N×M→N+M、bi-encoder vs cross-encoder、JSONB 半结构化决策、HNSW vs IVFFlat、记忆分层架构
- **【L4 工程考量】** — 工程落地层。超时设计、懒加载模式、连接池、批量写入、软删除、时间衰减推导、正则 vs 依赖库
- **【L5 面试答题模板】** — 面试串讲层。完整的"面试官会怎么问 + 你怎么答"模版，包含追问和应对策略

## 关键教学知识点统计

- MCP 协议：N×M→N+M 数学推导、Harness 五层安全切面
- RAG 两阶段检索：bi-encoder 粗排 + cross-encoder 精排的全链路推导
- 嵌入维度选型：768 vs 1024 vs 1536 的多维对比（BGE-M3 1024 = 最优）
- 数据库设计原则：UUID > SERIAL、JSONB 适用场景、ON DELETE 语义选择
- 向量索引：HNSW vs IVFFlat（构建成本 vs 查询速度）
- 记忆系统：三因子加权公式推导（语义×重要性×时间衰减）、半衰期类型差异
- 全文检索：zhparser 词性过滤策略、<=>/<#>/<-> 三操作符对比
- 软删除 vs 硬删除：审计需求 + 数据法规 + 训练价值的三重考量
- DAO 工程模式：连接池单例、executemany 提速原理、asyncpg binary protocol 性能优势

## 验证结果

✅ 全部 7 文件 import 通过（8 项检查）
✅ Phase 2 原有 22 项验收重跑全部通过
✅ 代码功能零修改——仅注释层面增强
