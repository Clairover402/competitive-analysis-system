"""LongTermMemoryEngine — 长期记忆的写入端（检索端已删除）。

═══════════════════════════════════════════════════════════════════════════════
                       【2026-10-28 删除检索端】
═══════════════════════════════════════════════════════════════════════════════

这里曾有 200 行「五步混合检索流水线」（Query 重写 → 向量/关键词双路 →
RRF 融合 → 元数据过滤 → Cross-encoder 精排）和 60 行 `_rrf_fusion`。
连同 `src/memory/retrieval.py` 的 MemoryRetrievalStrategy 一起删了。

理由不是"用不上所以砍掉"，是**方向错了**：

  · 生产路径从未接过线 —— `build_supervisor_graph` 一直只传 4 个参数，
    retrieval_strategy 恒为 None，`retrieve_if_needed` 在任何一次线上请求里
    都没执行过。它一直是死代码，只是没人注意。

  · 即便接上也会答错 —— 用户问「飞书定价现在多少」，要的是
    subject = '飞书·定价' 这一个键；语义召回给的是"看起来像"的 60 条，
    同竞品同维度没有任何保证，取回来还得按 subject 再过滤一遍。
    那不如一开始就按 subject 查。`WHERE content ILIKE '%定价%'` 也不行 ——
    对比句「与飞书相比，钉钉…」会被串到别的竞品头上。

  · **读取靠键，写入靠向量** —— 表里有 vector(1024) 是真的在用，但它服务的是
    写入时的取代判定（同 subject 余弦 ≥0.90 → 旧观测退出 active），不是读取。
    把这个区别写进代码，比留一套没人调用的检索实现诚实。

删掉的知识点没丢（RRF 为什么用排名不用分数、粗排/精排为什么分两层），
但它们属于"做过的方案"，不属于"现在跑的代码"。代码里留一份不存在的实现，
只会让后来人以为读取真的走向量。

═══════════════════════════════════════════════════════════════════════════════
                       【L3 核心概念 — 三因子加权排序】
═══════════════════════════════════════════════════════════════════════════════

score = 0.6 × 语义相似度 + 0.3 × 重要性 + 0.1 × 时间衰减

为什么从乘法改为加法？
  — 乘法: 任一因子接近 0 就整条淘汰。sim=0.95 × imp=0.9 × decay=0.25 = 0.21，
    一条半年前的 decision 极其相关却被淘汰。
  — 加法: 三因子独立贡献，不互相钳制。同一条记忆 = 0.87，排序正常。

为什么相似度权重最大（0.6）？"找到最相关的内容"是检索的本义；
重要性 0.3 用来抬 memory_type 的影响；时间 0.1 只做轻微调节 ——
旧但重要的记忆不该被过度打压。

⚠️ 这段排序现在跑在 SQL 里（`AgentMemoryDAO.similarity_search` 的 ORDER BY），
   不在这个类里。本类只剩**写入**。

═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — add_memory 的冲突检测流水线】
═══════════════════════════════════════════════════════════════════════════════

写入一条新记忆时:
  1. 映射 importance + half_life（从 _IMPORTANCE_MAP 查类型默认值）
  2. embed(content) → 1024 维向量（失败不阻塞写入，存 NULL）
  3. 有 conflict_resolver → detect_conflict → resolve → soft_delete
  4. 插入 agent_memories（含 embedding + importance + half_life_days）

冲突检测可选：传 resolver 走标准流程；不传直接插入（更快，可能有重复），
好处是 Pipeline 与单测能复用同一个 engine，单测不必 mock 额外依赖。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from src.db.dao import AgentMemoryDAO
from src.mcp.tools_rag import embed_query

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.memory.conflict import MemoryConflictResolver

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 记忆类型 → (默认 importance, 默认 half_life_days)
# ══════════════════════════════════════════════════════════════════════════════

# 【L5 决策】为什么每种类型有不同默认值？
# ──────────────────────────────────
# decision: 高重要性(0.9) + 长半衰期(90天) → 用户的关键决策必须长期保留
# preference: 中高重要性(0.7) + 中等半衰期(60天) → 偏好会变化但变化不快
# fact: 中等重要性(0.5) + 中等半衰期(30天) → 产品定价/功能等事实信息
# chat: 低重要性(0.1) + 短半衰期(7天) → 闲聊记忆一周内衰减到几乎为零

_IMPORTANCE_MAP = {
    "decision":   (0.9, 90),    # 关键决策 — 长保留 + 高权重
    "preference": (0.7, 60),    # 用户偏好 — 中保留 + 中高权重
    "fact":       (0.5, 30),    # 事实信息 — 中保留 + 中权重
    "chat":       (0.1, 7),     # 闲聊 — 短保留 + 低权重
}


def importance_for(
    memory_type: str, default: tuple[float, int] = (0.5, 30)
) -> tuple[float, int]:
    """取某类记忆的 (重要性, 半衰期) 默认值 —— 表外唯一读口。

    【为什么要有这个函数】semantic 写入（`memory/semantic.py`）不能走
    `add_memory()`：它需要在"算完向量"与"插入"之间插一步"找出被取代的旧观测"，
    而 add_memory 是把这两步焊死的一次性封装。直接读 `_IMPORTANCE_MAP` 又等于
    让两个模块共享私有符号——加这个函数是为了让"改默认值只改一处"这条成立。
    """
    return _IMPORTANCE_MAP.get(memory_type, default)


# ══════════════════════════════════════════════════════════════════════════════
# LongTermMemoryEngine — 五步检索引擎
# ══════════════════════════════════════════════════════════════════════════════

class LongTermMemoryEngine:
    """长期记忆写入引擎——事实提炼结果落库、取代判定、冲突消解。

    【职责边界】只写不读。
      — 写：semantic.write_task_facts() 把 evidence_map 里的事实升格成
        agent_memories 一行（带 embedding、importance、half_life_days）。
      — 读：不在这里。按 subject 精确键取 content（读取口见 query_handlers），
        lookup 走 source_task_id。两者都不碰 embedding。

    【取代判定为什么用向量】同一个 subject（如「飞书·定价」）可能有新旧两条观测，
    判断"新的是不是在取代旧的"需要看内容像不像 —— 这是全文里唯一需要余弦的地方。
    余弦 ≥0.90 → 旧观测 status 置 replaced，保证一个 subject 只有一个 active。

    用法:
        engine = LongTermMemoryEngine(llm, dao, conflict_resolver)
        memory_id = await engine.add_memory(user_id, "飞书企业版 ¥200/人/月", "fact")
    """

    def __init__(
        self,
        llm: ChatDeepSeek,
        dao: AgentMemoryDAO,
        conflict_resolver: MemoryConflictResolver | None = None,
        model_version: str | None = None,
    ) -> None:
        """初始化检索引擎。

        Args:
            llm: ChatDeepSeek 实例（温度 0.1，检索需要精准）
            dao: agent_memories 表的 DAO 层
            conflict_resolver: 冲突解决器（可选——不传则跳过冲突检测）

        【L5 决策】为什么 conflict_resolver 是可选的？
        ─────────────────────────────────────────
          冲突检测是写入链路的事，检索链路不需要。
          单测时可以只创建 engine 不传 resolver → 减少 mock 负担。
          这是"分层依赖"策略: 基础功能只需要 llm + dao，
          冲突检测是增强功能，加在 resolver 参数上。
        """
        self._llm = llm
        self._dao = dao
        self._conflict = conflict_resolver
        # 生成 embedding 的模型版本，检索时按此值过滤（见 AgentMemoryDAO.similarity_search）。
        # None = 不做版本过滤，仅用于离线分析/单测；生产由 lifespan 传 settings.embed_model_version。
        self.model_version = model_version

    @property
    def dao(self) -> AgentMemoryDAO:
        """暴露 DAO —— 只给"取代链"这种需要两步写入的路径用。

        正常写入走 `add_memory()`；但 semantic 事实必须先查旧观测、再插新观测、
        最后回写 `superseded_by`，这个序列 add_memory 封不出来。
        与其在 semantic.py 里 `engine._dao`（跨模块用私有属性），不如显式开这个口子，
        让"我知道这是内部依赖"这件事写在代码里而不是藏在调用点。
        """
        return self._dao

    async def add_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str = "fact",
        importance: float | None = None,
        half_life_days: int | None = None,
        source_task_id: str | None = None,
        *,
        observed_at: datetime | None = None,
        ttl_days: int | None = None,
        evidence_url: str | None = None,
        subject: str | None = None,
    ) -> str:
        """写入长期记忆——含冲突检测。

        全流程:
          1. 查 _IMPORTANCE_MAP 获取类型默认值
          2. 调用方传了自定义 importance/half_life → 覆盖默认值
          3. 调用 embed_query(content) 生成 1024 维向量（失败不阻塞写入）
          4. 如果有 conflict_resolver → detect → resolve → soft_delete
          5. DAO.insert() 写入 agent_memories 表

        Args:
            user_id: 用户 ID（多用户隔离）
            content: 记忆内容文本
            memory_type: 记忆类型（decision/preference/fact/chat）
            importance: 重要性评分（0~1，None 则从 _IMPORTANCE_MAP 取）
            half_life_days: 半衰期天数（None 则从 _IMPORTANCE_MAP 取）
            source_task_id: 来源任务 ID（追溯记忆由哪个分析任务产生）

        Returns:
            新记忆的 UUID。

        【L5 决策】importance 和 half_life 的参数设计
        ──────────────────────────────────────────
        _IMPORTANCE_MAP 定义了每种类型的"默认值"。
        但调用方可以通过 importance=0.95 覆盖（例如: 用户明确标记为"非常重要"）。
        两层设计:
          — 类型层: decision=0.9（大部分 decision 都是用这个值）
          — 调用层: 可以覆盖（特定几笔 decision 可能重要性为 1.0）

        【L4 工程】source_task_id 的追溯价值
        ─────────────────────────────────
        从分析报告中提取的记忆携带 source_task_id——知道"这句话来自哪次分析"。
        出问题时可以溯源: 这次分析的结论提取出了什么记忆？
        这是一个可观测性字段——不必需但你不想没有。

        【⚠️ 写 fact 要走 `semantic.write_task_facts`，不要依赖这里的 conflict_resolver】
        本方法的冲突检测是 0.85 全库比对；fact 的取代需要先同 subject 再比 0.90
        （否则"支持多维表格"与"支持自动化流程"会被判成同一条而误删）。
        两套判据同时生效，会把刚写入的 fact 又软删一遍。
        """
        # ── 1. 取默认值 + 调用方覆盖 ──
        imp, half = _IMPORTANCE_MAP.get(
            memory_type,
            (importance or 0.5, half_life_days or 30)
        )
        if importance is not None:
            imp = importance
        if half_life_days is not None:
            half = half_life_days

        # ── 2. 生成 embedding ──
        embedding = await embed_query(content)

        # ── 3. 冲突检测（如果启用了 resolver） ──
        if embedding and self._conflict:
            conflicts = await self._conflict.detect_conflict(user_id, content)
            if conflicts:
                # 检测到冲突 → LLM 仲裁 → 软删除应删除的旧记忆
                await self._conflict.resolve(content, conflicts)

        # ── 4. 写入 DB ──
        memory_id = await self._dao.insert(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            importance=imp,
            embedding=embedding if embedding else None,
            source_task_id=source_task_id,
            half_life_days=half,
            observed_at=observed_at,
            ttl_days=ttl_days,
            evidence_url=evidence_url,
            subject=subject,
            # ⚠️ 没配 model_version 时写 DDL 默认值（'bge-m3'），不要写 NULL：
            #    NULL 会让该行在 similarity_search 的 `model_version = $4` 下永远命中不了，
            #    等于"写入即失联"。宁可标签写成一个"大概是它"的值。
            model_version=self.model_version or "bge-m3",
        )

        logger.info("长期记忆写入成功: id=%s, 类型=%s, 重要性=%.1f, 有向量=%s",
                     memory_id[:8], memory_type, imp, bool(embedding))
        return memory_id
