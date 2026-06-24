"""IntentRouter — 代码路由决策器。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

用户 query ──→ LLM 实体提取 ──→ IntentRouter 代码路由
                                     │
               ┌─────────────────────┼─────────────────────┐
               │  classify(parsed)                         │
               │  competitors≥1                            │
               │  dimensions≥1          任何一个不满足 →    │
               │  intent_is_clear                          │
               │        │                                  │
               │   全部满足                                 │
               ▼        ▼                                  ▼
          Pipeline    Supervisor                        Supervisor
         (确定性分析)  (探索模式)                       (探索模式)
              │            │
              │    ┌───────┴───────┐
              │    │ _setup_deps() │  ← 共享依赖创建一次
              │    │ mcp_server    │
              │    │ pool          │
              │    │ HarnessGuard  │  ← Phase 5B：注入 A2ARouter
              │    │ A2ARouter     │
              │    │ llm_supervisor│
              │    └───────────────┘
              ▼            ▼
       run_pipeline    run_supervisor
       _task()         _task()

   85% 的流量走 Pipeline（用户说清楚了竞品+维度）
   15% 的流量走 Supervisor（需要探索或交互澄清）

【L3 核心考点】路由决策的"二段式"设计：
  — 第一阶段：LLM 提取实体 {competitors, dimensions, intent_is_clear}
  — 第二阶段：代码读结构化数据做 if-else → 零 LLM 调用、零 token、100% 可复现
  — 让 LLM 决定"该走哪条路"再让代码判断一次 = 画蛇添足


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 classify() 纯函数路由   — 确定性规则，零 LLM 调用
  §2 _setup_dependencies()   — 依赖创建 + HarnessGuard 注入 A2ARouter
  §3 route() 分流入口         — classify → 创建依赖 → 分派执行 → 异常兜底
  §4 route_history            — 路由审计（内存记录）


═══════════════════════════════════════════════════════════════════════════════
                    【L4 工程 — 数据流向一览】
═══════════════════════════════════════════════════════════════════════════════

  字段/依赖                        来源                      去向
  ──────────────────────────────  ────────────────────────  ──────────────
  llm_parsed (●读)                LLM 实体提取阶段（图外）   classify()
  route_type (●读)                classify() 返回            route() 分支
  route_history (★累加)           route() 每轮追加            Phase 6 Dashboard
  enriched_task (▲写)             route() 构造               run_*_task()
  mcp_server / pool / router (▲写) _setup_dependencies()    run_*_task()
  HarnessGuard (▲写)              _setup_dependencies()     注入 A2ARouter
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from src.config import Settings
from src.agents import create_llm_client
from src.supervisor.a2a import A2ARouter, create_agent_cards
from src.supervisor.supervisor import run_supervisor_task
from src.pipeline.graph import run_pipeline_task
from src.mcp import create_mcp_server
from src.db.connection import create_pool

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from src.mcp.server import MCPServer
    from asyncpg import Pool

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# §1 IntentRouter — 代码路由决策器
# ═════════════════════════════════════════════════════════════════════════════

class IntentRouter:
    """代码路由：根据 LLM 提取的结构化数据分流 Pipeline / Supervisor。

    【L5 决策】单一职责：IntentRouter 只做路由，不执行分析。
    执行逻辑委托给 run_pipeline_task / run_supervisor_task。
    两引擎统一接受 mcp_server + pool 参数，路由层负责创建依赖。

    【L5 决策】为什么不用 LLM 路由？
    LLM 已输出 {competitors, dimensions, intent_is_clear}——
    三个结构化字段已经回答了"该走哪条路"的所有信息。
    代码 if-else 三个条件就够了，再调 LLM = 浪费 200 token + 500ms 延迟。

    【L4 工程】依赖生命周期：
    _setup_dependencies() 在 route() 内部创建，使用完后不关闭——
    pool 和 mcp_server 由运行引擎（Pipeline/Supervisor）管理生命周期。

    ┌──────────────────────────────────────────────────────────┐
    │  方法一览                                                │
    ├───────────────┬──────────────────────┬───────────────────┤
    │ 方法           │ 功能                  │ 考点              │
    ├───────────────┼──────────────────────┼───────────────────┤
    │ classify()    │ 纯函数路由决策         │ L3 确定性规则     │
    │ _setup_deps() │ 创建共享依赖+注入Guard │ L4 依赖管理       │
    │ route()       │ 分流入口+异常兜底      │ L3/L4 全链路      │
    └───────────────┴──────────────────────┴───────────────────┘
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化路由决策器。

        Args:
            settings: 系统配置，None 时使用默认 Settings()
        """
        self.settings = settings or Settings()
        # 【L4 工程】route_history 是内存列表，每 route() 追加一条
        # Phase 6 可改为写入 agent_logs 表（AuditLogger 已有写能力）
        self.route_history: list[dict] = []

    # ── classify() — 纯函数路由决策 ──

    @staticmethod
    def classify(parsed: dict) -> str:
        """根据 LLM 提取的结构化数据判断走哪条路。

        【L3 核心考点】确定性规则，零 LLM 调用：

        ┌───────────────────────┬──────────────┬──────────────────────┐
        │ 条件                    │ 路由          │ 业务含义              │
        ├───────────────────────┼──────────────┼──────────────────────┤
        │ len(competitors)==0   │ supervisor   │ 用户没提竞品→探索发现  │
        │ len(dimensions)==0    │ supervisor   │ 没提分析维度→需澄清    │
        │ intent_is_clear=False │ supervisor   │ 意图模糊→交互式引导    │
        │ 以上全否则              │ pipeline     │ 参数充足→确定性分析    │
        └───────────────────────┴──────────────┴──────────────────────┘

        三个条件用 or 短路：第一个触发就返回，不检查后续。

        【L5 决策】解耦的真正价值：
        路由规则归代码，实体提取归 LLM prompt。
        改路由规则（如"单竞品也走 pipeline"→改代码）不改 prompt。
        改实体提取（如增加 language 字段）→改 prompt，路由不变。

        Args:
            parsed: LLM 提取的结构化数据
                {
                    competitors: [str],       # 如 ["飞书","钉钉","Notion"]
                    dimensions: [str],        # 如 ["功能","定价","市场"]
                    intent_is_clear: bool,    # 用户意图是否明确
                }

        Returns:
            "pipeline" | "supervisor"
        """
        competitors = parsed.get("competitors", []) or []
        dimensions = parsed.get("dimensions", []) or []
        intent_is_clear = parsed.get("intent_is_clear", False)

        if not competitors or len(competitors) == 0:
            logger.info("路由决策: supervisor（未指定竞品，需探索发现）")
            return "supervisor"
        if not dimensions or len(dimensions) == 0:
            logger.info("路由决策: supervisor（未指定分析维度）")
            return "supervisor"
        if not intent_is_clear:
            logger.info("路由决策: supervisor（意图不明确）")
            return "supervisor"

        logger.info("路由决策: pipeline（竞品=%d, 维度=%d）", len(competitors), len(dimensions))
        return "pipeline"

    # ── _setup_dependencies() — 共享依赖创建 ──

    async def _setup_dependencies(self) -> tuple[MCPServer, Pool, A2ARouter, ChatDeepSeek]:
        """创建所有共享依赖（每个 task 创建一次）。

        【L4 工程】执行步骤：

        | 步骤 | 做什么 | 为什么 |
        |:--:|------|------|
        | 1 | `create_mcp_server(settings)` | MCP Server 承载工具能力（web_search 等） |
        | 2 | `await create_pool(settings)` | 异步连接池，复用给 Pipeline/Supervisor |
        | 3 | `HarnessGuard(pool)` 创建安全壳 | Phase 5B：所有 Agent 调用必经五层检查 |
        | 4 | `A2ARouter(mcp_server, harness=guard)` | 注入 HarnessGuard，send_task 自动拦截 |
        | 5 | 注册 4 个 AgentCard + handler + 专属温度 LLM | 温度按角色语义：搜索 0.3、分析 0.1、评分 0.0 |
        | 6 | 创建 `llm_supervisor(t=0.3)` | Supervisor 自身决策需要一定多样性 |

        【L5 决策】HarnessGuard 注入 A2ARouter 而非 IntentRouter：
        IntentRouter 只管分流，不管安全。安全检查在 Agent 调用的实际节点生效——
        Pipeline 的 collect/analyze/write/quality 和 Supervisor 的 ReAct 所有 Agent
        共享同一套 Harness，注入点统一在 A2ARouter.send_task() 第 ②.5 步。

        Returns:
            (mcp_server, pool, router, llm_supervisor)
        """
        mcp_server = create_mcp_server(self.settings)
        pool = await create_pool(self.settings)

        # ─── 创建 A2ARouter + 注入 HarnessGuard ───
        # Phase 5B: 注入 HarnessGuard（白名单+参数校验+频控+PII+审计）
        from src.harness import HarnessGuard
        guard = HarnessGuard(pool)
        router = A2ARouter(mcp_server, harness=guard)
        cards = create_agent_cards()

        # ─── 注册 4 个 Agent（handler + 专属温度 LLM）───
        # 【L5 决策】4 种温度按角色语义选择：
        #   Collector (0.3): 搜索需要多样性，探索更多数据源
        #   Analyzer  (0.1): 分析需要精确，减少幻觉
        #   Writer    (0.3): 撰写可微调措辞风格
        #   Quality   (0.0): 评分必须可复现，完全确定性
        from src.agents import collector_agent, analyzer_agent, writer_agent, quality_agent

        llm_collector = create_llm_client(self.settings, temperature=0.3)
        llm_analyzer = create_llm_client(self.settings, temperature=0.1)
        llm_writer = create_llm_client(self.settings, temperature=0.3)
        llm_quality = create_llm_client(self.settings, temperature=0.0)

        handlers = {
            "collector": (collector_agent, llm_collector),
            "analyzer": (analyzer_agent, llm_analyzer),
            "writer": (writer_agent, llm_writer),
            "quality": (quality_agent, llm_quality),
        }
        for name, card in cards.items():
            handler, llm = handlers[name]
            router.register(card, handler, llm)

        llm_supervisor = create_llm_client(self.settings, temperature=0.3)
        return mcp_server, pool, router, llm_supervisor

    # ── route() — 分流入口 ──

    async def route(self, task: dict, llm_parsed: dict) -> dict:
        """分流入口：classify → 记录 → 创建依赖 → 分派执行 → 异常兜底。

        【L3 核心考点】route 的 6 步流程：

        | 步骤 | 做什么 | 失败时 |
        |:--:|------|------|
        | 1 | `classify(llm_parsed)` → route_type | —（纯函数，不可能失败） |
        | 2 | 记录 `route_history`（task_id + route + reason + timestamp） | — |
        | 3 | 丰富 task dict（注入 competitors + dimensions） | — |
        | 4 | `_setup_dependencies()` 创建共享依赖 | 异常 → except 返回 {error} |
        | 5 | 按 route_type 分派 run_pipeline_task 或 run_supervisor_task | 异常 → except 返回 {error} |
        | 6 | 记录耗时 → 返回 {task_id, route, result, elapsed_ms} | — |

        【L4 工程】异常兜底策略：
        route() 的最外层 try/except 确保路由层不崩溃——
        即使两个引擎都挂了，也返回结构化的 {error} 而非抛异常。
        调用方（未来 FastAPI endpoint）可以据此返回 500 而非超时。

        Args:
            task: 原始任务 {id, title, user_id?, ...}
            llm_parsed: LLM 提取的结构化数据
                {competitors, dimensions, intent_is_clear}

        Returns:
            {
                task_id: str,
                route: "pipeline" | "supervisor",
                result: {...},        # 引擎返回值
                elapsed_ms: float,
            }
            或失败时:
            {
                task_id: str,
                route: "pipeline" | "supervisor",
                error: str,
                elapsed_ms: float,
            }
        """
        route_type = self.classify(llm_parsed)
        start_time = time.monotonic()

        # ─── 记录路由历史 ───
        # 【L4 工程】每条 route 记录追加到内存列表
        # Phase 6 改为写入 agent_logs，Dashboard 展示 80/20 分流比
        history_entry = {
            "task_id": task.get("id", ""),
            "route": route_type,
            "reason": "竞品和维度明确" if route_type == "pipeline" else "开放性探索",
            "timestamp": start_time,
        }
        self.route_history.append(history_entry)

        # ─── 丰富 task — 注入 LLM 提取的实体 ───
        # 【L3 核心考点】原始 task 可能不包含 competitors/dimensions
        # 这些字段是 LLM 从自然语言 query 中提取的，补充到 task dict 后
        # Pipeline 和 Supervisor 都能直接读 task["competitors"] 无需二次提取
        enriched_task = {
            "id": task.get("id", ""),
            "title": task.get("title", "竞品分析"),
            "user_id": task.get("user_id", "default"),
            "competitors": llm_parsed.get("competitors", []),
            "dimensions": llm_parsed.get("dimensions", []),
        }

        logger.info("IntentRouter 分流: route=%s, task_id=%s", route_type, enriched_task["id"])

        try:
            mcp_server, pool, router, llm_supervisor = await self._setup_dependencies()

            # ─── 分派执行 ───
            # 【L5 决策】两个引擎统一接受 mcp_server + pool
            # Supervisor 额外需要 router + llm_supervisor（ReAct 循环的决策 LLM）
            if route_type == "pipeline":
                result = await run_pipeline_task(
                    enriched_task,
                    mcp_server=mcp_server,
                    pool=pool,
                )
            else:
                result = await run_supervisor_task(
                    enriched_task,
                    mcp_server=mcp_server,
                    pool=pool,
                    router=router,
                    llm_supervisor=llm_supervisor,
                )

            elapsed = (time.monotonic() - start_time) * 1000
            logger.info("IntentRouter 完成: route=%s, elapsed=%.0fms", route_type, elapsed)
            return {**result, "route": route_type, "elapsed_ms": elapsed}

        except Exception as e:
            # 【L4 工程】捕获一切异常 → 返回结构化错误
            # 不抛异常（上层 FastAPI 直接拿到 dict 做 500）
            logger.exception("IntentRouter 执行失败: route=%s", route_type)
            return {
                "task_id": task.get("id", ""),
                "route": route_type,
                "error": str(e),
                "elapsed_ms": (time.monotonic() - start_time) * 1000,
            }
