"""Agent 模块 — 四个专精 Agent + LLM 客户端工厂。

═══════════════════════════════════════════════════════════════════════════════
                         【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

四个 Agent 各司其职，通过 Supervisor 层编排为流水线：

  Collector ──→ Analyzer ──→ Writer ──→ Quality
  (数据采集)    (推理分析)    (报告撰写)   (质量门禁)
  温度 0.3      温度 0.1      温度 0.3     温度 0.0
  有工具        有工具(RAG)   无工具       无工具

【L5 决策】每个 Agent 的 temperature 为什么不同？
─────────────────────────────────────────────
  Collector (0.3): 生成搜索 query → 需要一定多样性（不同query覆盖不同角度）
  Analyzer  (0.1): 维度分析 → 需要稳定可复现的结论（低温度）
  Writer    (0.3): 报告撰写 → 允许稍微变化的措辞（但结构由 prompt 约束）
  Quality   (0.0): 报告评分 → 评分必须可复现（最大确定性）

  Temperature 不是随便选的——它反映了任务的"确定性需求"。
  需要创意/多样性 → 高温；需要一致性/可复现 → 低温。

【L4 工程】工厂模式的好处
────────────────────────
  create_llm_client() 集中管理 LLM 实例化：
    ① 统一配置入口：api_key/base_url/model 从 settings 来，不用散落各处
    ② 可插拔：如果要切换到其他 LLM（OpenAI/Claude），只改这一处
    ③ 可注入日志/限流/重试：在返回 Client 前包一层 wrapper

用法:
    from src.agents import collector_agent, analyzer_agent, writer_agent, quality_agent
    from src.agents import create_llm_client

    llm = create_llm_client(settings, temperature=0.3)
    result = await collector_agent(task, mcp_server, llm)
"""

from __future__ import annotations

from src.config import Settings
from src.agents.collector import collector_agent
from src.agents.analyzer import analyzer_agent
from src.agents.writer import writer_agent
from src.agents.quality import quality_agent

from langchain_deepseek import ChatDeepSeek


def create_llm_client(
    settings: Settings,
    temperature: float = 0.3,
) -> ChatDeepSeek:
    """便捷工厂：创建配置好的 ChatDeepSeek 客户端。

    【L4 工程】集中工厂模式
    所有 LLM 实例通过这里创建，保证：
      — api_key 来源统一（settings.deepseek_api_key）
      — base_url 来源统一（settings.deepseek_base_url）
      — model 来源统一（settings.deepseek_model）
    不会出现"Agent A 用 key1，Agent B 用 key2"的混乱。

    【L5 决策】默认 temperature=0.3
    这是 Collector 和 Writer 的默认值（需要一定多样性）。
    Analyzer 调用时覆盖为 0.1，Quality 覆盖为 0.0。
    工厂默认值取最常见场景，特殊场景显式覆盖。

    Args:
        settings: 系统配置（api_key, base_url, model）
        temperature: 温度参数（默认 0.3，已按 Agent 需求选择）

    Returns:
        已配置的 ChatDeepSeek 实例
    """
    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=temperature,
    )


# 【L4 工程】__all__ 导出——模块的"路由表"
# 显式声明公开接口，IDE 可以自动补全，import * 不会漏导。
# 新增 Agent: ① 写 Agent 文件 → ② 在此 import → ③ 加入 __all__
__all__ = [
    "collector_agent",
    "analyzer_agent",
    "writer_agent",
    "quality_agent",
    "create_llm_client",
]
