"""MemoryRetrievalStrategy — 混合触发策略（关键词预检 + LLM 兜底）。

═══════════════════════════════════════════════════════════════════════════════
                          【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 混合触发模式: 关键词快速通道（零成本） + LLM 歧义兜底（有成本）
  §2 80/20 覆盖: 关键词覆盖 ~80% 场景，LLM 仅处理模糊边界 ~20%
  §3 保守原则: 宁少勿多——不该检索时不检索，false negative 优于 false positive


═══════════════════════════════════════════════════════════════════════════════
                     【L4 工程 — 为什么用关键词预检】
═══════════════════════════════════════════════════════════════════════════════

每次对话都调用 LLM 判断"要不要检索记忆"的成本:
  — LLM 调用延迟 ~500ms → 每次对话多等半秒
  — token 消耗: prompt ~150 tokens + response ~50 tokens = 200 tokens/次
  — 每天 1000 次对话 → 200k tokens/天 → ~20 万字符

关键词预检的零成本优势:
  — `if kw in message.lower()` 是 O(k*n) 字符串匹配，零延迟、零 token
  — RECALL_KEYWORDS 覆盖"我要查历史"类表达（remember/last time/recall 等）
  — SKIP_KEYWORDS 覆盖"不用查历史"类表达（hello/thanks/goodbye 等）

两类关键词的设计原则:
  RECALL（应检索）: 用户明确提到历史 → 直接返回 True，不走 LLM
  SKIP（不应检索）: 用户只是打招呼/道谢 → 直接返回 False，不走 LLM
  两头确定 + 中间模糊走 LLM = 最快路径覆盖最多场景

RECALL_KEYWORDS 为什么用英文而不全用中文？
  — 记忆检索的触发词主要是英文（模型训练预料中这些词更敏感）
  — 中文对应用 "上次" "之前" "历史" 不在列表中——这会被 LLM 兜底处理
  — 中英文都加的话列表会膨胀到 20+ 词，维护成本上升


═══════════════════════════════════════════════════════════════════════════════
               【L5 架构 — Pipeline vs Supervisor 中的检索触发】
═══════════════════════════════════════════════════════════════════════════════

Pipeline 模式（当前）:
  analyze 节点直接调用 engine.retrieve()，不走 MemoryRetrievalStrategy 的判断。
  原因: Pipeline 中的 analyze 必然需要检索——每次分析任务一定需要历史记忆。
  所以策略层在 Pipeline 中不生效——这是特例不是 Bug。

Supervisor 模式（Phase 5A）:
  用户每轮对话不一定需要查记忆。Supervisor 需要 before 调用前判断:
    should, query = await strategy.should_retrieve(user_message)
    if should:
        memories = await engine.retrieve(user_id, query)
        # 将 memories 注入 Supervisor 的 think prompt

  此时策略层的价值最大化:
    — 问候消息不触发检索（SKIP_KEYWORDS 命中）
    — 知识类问题不触发检索（LLM 判断 false）
    — 涉及历史的才触发（RECALL_KEYWORDS 命中或 LLM 判断 true）

两种模式的差异体现了"有选择地使用记忆"的设计哲学:
  — 不是所有场景都需要记忆——无关检索 = 噪音干扰 + 延迟浪费
  — Pipeline 是特例（必然需要），Supervisor 是常态（按需检索）
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.memory.long_term import LongTermMemoryEngine

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 关键词列表
# ══════════════════════════════════════════════════════════════════════════════

# 【L4 工程】检索触发关键词 — 命中的消息应查历史
RECALL_KEYWORDS = [
    "remember", "last time", "previous", "recall",
    "history", "you mentioned", "do you remember", "we discussed",
    "上次", "之前", "历史", "记不记得", "说过", "聊过",    # 中文触发词
]

# 【L4 工程】跳过关键词 — 命中的消息无需查历史
SKIP_KEYWORDS = ["hello", "thanks", "goodbye", "weather"]


class MemoryRetrievalStrategy:
    """混合触发策略: 判断是否应该查询长期记忆。

    【L3 核心概念】三步决策流水线
    ─────────────────────────
      Step 1: SKIP 预检 — 如果是问候/道谢/闲聊 → False（零成本）
      Step 2: RECALL 预检 — 如果提到"上次/之前/remember" → True（零成本）
      Step 3: LLM 兜底 — 模糊消息 → LLM 判断 need=true/false
      Step 4: 默认保守 — LLM 失败或判断不明确 → False（宁可少检索）

    【L4 工程】保守原则的工程价值
    ──────────────────────────
    为什么要"宁可少检索"（conservative）？
      — false negative（该查没查）: 回答缺少历史上下文，但不影响正确性
      — false positive（不该查却查了）: 注入无关记忆 → LLM 被误导 → 答非所问
      false negative 的危害远小于 false positive。记忆污染比记忆缺失更危险。

    用法:
        strategy = MemoryRetrievalStrategy(llm)
        should, query = await strategy.should_retrieve(user_message)
        if should:
            results = await engine.retrieve(user_id, query)
    """

    def __init__(self, llm: ChatDeepSeek | None = None) -> None:
        """初始化检索策略。

        Args:
            llm: LLM 客户端（可选）。不传则只用关键词预检，模糊消息默认不检索。

        【L4 工程】为什么 LLM 是可选的？
        ───────────────────────────
          关键词预检能覆盖 ~80% 场景。对低延迟要求的场景，
          可以只走关键词通道——快速且不影响主要路径。
          这就是 degradable design（可降级设计）:
            完整模式: 关键词 + LLM 兜底（全覆盖）
            降级模式: 仅关键词（覆盖 80%，零延迟）
        """
        self._llm = llm

    async def should_retrieve(
        self, user_message: str
    ) -> tuple[bool, str]:
        """判断是否应该查询长期记忆。

        Returns:
            (是否检索, 检索关键词)
            — (False, "") → 不检索
            — (True, "pricing strategy") → 用此 query 检索

        【L4 工程】返回的 query 不是原始 user_message
        ─────────────────────────────────────────
        原始: "飞书上次降价到多少了"
        返回: "飞书定价变化"（LLM 重写后更精准的检索 query）
        这就是策略层的另一个价值——不仅判断要不要查，还决定用什么查。
        """
        if not user_message:
            return (False, "")

        # ── Step 1: SKIP 关键词预检（跳过检索） ──
        for kw in SKIP_KEYWORDS:
            if kw in user_message.lower():
                return (False, "")

        # ── Step 2: RECALL 关键词预检（触发检索） ──
        for kw in RECALL_KEYWORDS:
            if kw in user_message.lower():
                logger.debug("关键词触发记忆检索: %s", kw)
                return (True, user_message)

        # ── Step 3: LLM 兜底（模糊边界） ──
        if self._llm:
            try:
                prompt = (
                    "判断这条用户消息是否需要查询历史记忆。"
                    "如果用户引用了之前讨论过的内容，返回 true；否则返回 false。"
                    "返回 JSON: {\"need\": true/false, \"query\": \"检索关键词\"}"
                    "\n\n用户消息: " + user_message
                )
                resp = await self._llm.ainvoke(prompt)
                text = resp.content.strip()

                # 【L4 工程】防御性清理: 去掉 LLM 包裹的 ```json 代码块
                if text.startswith("```"):
                    text = text.split("```", 2)[1].strip()

                result = json.loads(text)
                if result.get("need"):
                    # LLM 返回 need=true → 用 LLM 生成的 query 检索
                    return (True, result.get("query", user_message))
            except Exception:
                # 【L4 工程】LLM 失败 → 默认不检索
                # 不抛异常——因为不是核心路径，不影响主流程
                logger.debug("LLM 检索判断失败，默认跳过")

        # ── Step 4: 默认不检索（保守原则） ──
        return (False, "")

    async def retrieve_if_needed(
        self,
        user_id: str,
        message: str,
        engine: LongTermMemoryEngine,
    ) -> list[dict]:
        """统一入口: 判断 → 检索 → 返回结果。

        【L3 核心概念】这是 Strategy 层的完整工作流:
          1. should_retrieve() → 判断要不要查
          2. engine.retrieve() → 执行五步检索
          3. 返回结果列表 → 调用方注入 prompt

        【L5 架构】这是 Supervisor 的入口模式
        ───────────────────────────────────
        Pipeline 中 analyze 节点直接调 engine.retrieve()，
        Supervisor 中调用方走 retrieve_if_needed()。
        两者共用同一个 engine，但触发逻辑不同。
        """
        should, query = await self.should_retrieve(message)
        if not should:
            return []
        return await engine.retrieve(user_id, query)
