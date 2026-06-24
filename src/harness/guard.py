"""HarnessGuard — 五层安全检查中间件。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

所有跨 Agent 调用必经此检查链（A2ARouter.send_task() 第 ②.5 步）：

  请求 ──→ [1.白名单] ──→ [2.参数校验] ──→ [3.频控] ──→ [4.PII检测] ──→ [5.审计] ──→ 放行
              │              │              │           │
           失败返回        失败返回        失败返回     只告警不阻断
         "WHITELIST_     "PARAM_         "RATE_       > 告警写入
          DENIED"         INVALID"        LIMITED"       审计日志

  ┌─────────────────────────────────────────────────────────────────────┐
  │  三层阻断      vs    一层告警       vs    一层记录                    │
  ├─────────────────────────────────────────────────────────────────────┤
  │ Layer 1-3:            Layer 4:             Layer 5:                 │
  │ 操作合法性判断        数据敏感性判断       全量审计记录              │
  │ 不可恢复→必须阻断     分类不确定→只告警   通过和拦截都记录          │
  │ returned degraded     logged + continued   fire-and-forget          │
  └─────────────────────────────────────────────────────────────────────┘

  > Supervisor 看到 degraded=True → 下一轮 think 换策略重试（不崩溃）


═══════════════════════════════════════════════════════════════════════════════
                        【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 TokenBucket           — 双层滑动窗口频控（全局 + 单Agent）
  §2 HarnessGuard          — 五层短路检查链（白名单→参数→频控→PII→审计）
  §3 check_whitelist()     — O(1) 确定性白名单，禁止跨能力调用
  §4 validate_params()     — JSON Schema 结构校验（字段存在+类型匹配）
  §5 scan_for_pii()        — 正则检测三要素（手机号/身份证/邮箱）
  §6 guard() 统一入口       — 短路求值 + degraded 标记 + 审计日志


═══════════════════════════════════════════════════════════════════════════════
                    【L4 工程 — 常量一览】
═══════════════════════════════════════════════════════════════════════════════

  AGENT_WHITELIST         每个 Agent 只允许的能力列表
  PII_PATTERNS            [(类型名, 正则)] — 三要素
  GLOBAL_QPS=100          全局每秒请求数上限
  AGENT_RPS=10            单 Agent 每秒调用上限
  RATE_WINDOW=1.0         滑动窗口宽度（秒）
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncpg import Pool

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# 【L4 工程】常量定义
# ═════════════════════════════════════════════════════════════════════════════

# 白名单：Agent × 能力 矩阵
# 【L5 决策】所有对照关系只此一份，改一处全局生效
AGENT_WHITELIST: dict[str, list[str]] = {
    "collector": ["collect", "web_search", "web_fetch"],
    "analyzer": ["analyze", "embed", "rerank"],
    "writer": ["write", "compose_report"],
    "quality": ["evaluate", "score_report"],
}

# PII 检测：三要素（手机号、身份证、邮箱）
# 【L5 决策】三种正则是最低覆盖率。真实生产需扩展：
#   银行卡号 (\d{16,19})、IP 地址、家庭住址关键词
# 但正则永远有漏网之鱼——最终防线是数据脱敏网关
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("手机号", re.compile(r"1[3-9]\d{9}")),
    ("身份证", re.compile(r"\d{17}[\dXx]")),
    ("邮箱", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
]

# 频控参数
# 【L4 工程】"单体够了，多实例才需要 Redis"
GLOBAL_QPS = 100       # 全局每秒请求上限 — 防系统过载
AGENT_RPS = 10          # 单 Agent 每秒请求上限 — 防单点滥用
RATE_WINDOW = 1.0       # 时间窗口（秒）


# ═════════════════════════════════════════════════════════════════════════════
# §1 TokenBucket — 双层滑动窗口频控器
# ═════════════════════════════════════════════════════════════════════════════

class TokenBucket:
    """内存 TokenBucket — 简单的滑动窗口频控。

    ▎【L3 核心考点】TokenBucket 的两层限流：

    ┌───────────────┬──────────────┬────────────────────────┐
    │ 层级            │ 阈值          │ 触发时                  │
    ├───────────────┼──────────────┼────────────────────────┤
    │ 1. 全局计数     │ 100 req/s    │ 任何 Agent 都拦截      │
    │ 2. 单 Agent 计数 │ 10 req/s     │ 只拦截超限的那个 Agent │
    └───────────────┴──────────────┴────────────────────────┘

    执行顺序：先全局、后单 Agent —— 全局先兜底，单 Agent 再精确。

    ▎【L5 决策】为什么不用 Redis？

    1. 当前是单体部署（asyncio 单线程），不存在多实例竞争
    2. 内存计数器在单线程下是原子操作（self._global_count += 1）
    3. 重启后计数器清零可接受——开发/演示环境，不是 7×24 生产集群
    4. 零外部依赖

    如果需要升级到生产级分布式频控，只需换 TokenBucket 内部实现：
    redis.incr(key) + redis.expire(key, 1)，外部 allow() 接口不变。

    ▎【L4 工程】滑动窗口实现细节：

    每个时间窗口（RATE_WINDOW）结束后计数器归零，新请求从 0 开始计数。
    本质是"最近一秒内不超过 N 次"，而非"每秒整点不超过 N 次"——

    >>> now=0.1s → 窗口 [0.1, 1.1)（以请求到达时间为起点）
    >>> now=1.2s → 窗口 [1.2, 2.2)（上一个窗口过期，自动重置）

    This is NOT a leaky bucket. It IS a simplified sliding window.
    """

    def __init__(self) -> None:
        # 【L4 工程】四个内部字段：
        # _global_count:   当前窗口内已处理的全局请求数
        # _global_reset:   当前全局窗口的起始时间
        # _agent_counts:   {agent_name: 该窗口内计数}
        # _agent_resets:   {agent_name: 该窗口起始时间}
        self._global_count = 0
        self._global_reset = time.monotonic()
        self._agent_counts: dict[str, int] = {}
        self._agent_resets: dict[str, float] = {}

    def allow(self, agent_name: str) -> bool:
        """检查是否允许本次请求（双层检查）。

        【L3 核心考点】allow() 执行步骤：

        | 步骤 | 操作 | 失败时 |
        |:--:|------|------|
        | 1 | 检查全局窗口是否过期 → 过期则重置 | — |
        | 2 | 全局计数 >= 100 → 拒绝 | False |
        | 3 | 全局计数 +1 | — |
        | 4 | 检查 Agent 窗口是否过期 → 过期则重置 | — |
        | 5 | Agent 计数 >= 10 → 拒绝 | False |
        | 6 | Agent 计数 +1 | — |
        | 7 | 返回 True | — |

        Args:
            agent_name: Agent 名称（"collector"/"analyzer"/...）

        Returns:
            True=放行，False=限流
        """
        now = time.monotonic()

        # ── Layer 1: 全局限流（防系统整体过载）──
        # 【L4 工程】window expired? → reset counter + reset time
        if now - self._global_reset >= RATE_WINDOW:
            self._global_count = 0
            self._global_reset = now
        if self._global_count >= GLOBAL_QPS:
            logger.warning("全局频控触发: count=%d, limit=%d", self._global_count, GLOBAL_QPS)
            return False
        self._global_count += 1

        # ── Layer 2: 单 Agent 限流（防单个 Agent 滥用）──
        # 【L4 工程】用 agent_name 做 key，每个 Agent 独立窗口
        if agent_name not in self._agent_resets or now - self._agent_resets[agent_name] >= RATE_WINDOW:
            self._agent_counts[agent_name] = 0
            self._agent_resets[agent_name] = now
        if self._agent_counts.get(agent_name, 0) >= AGENT_RPS:
            logger.warning("Agent 频控触发: agent=%s, count=%d, limit=%d",
                           agent_name, self._agent_counts[agent_name], AGENT_RPS)
            return False
        self._agent_counts[agent_name] = self._agent_counts.get(agent_name, 0) + 1
        return True


# ═════════════════════════════════════════════════════════════════════════════
# §2 HarnessGuard — 五层安全检查中间件
# ═════════════════════════════════════════════════════════════════════════════

class HarnessGuard:
    """五层安全检查中间件。

    ▎使用方式（A2ARouter.send_task() 第 ②.5 步）:

        guard = HarnessGuard(pool)
        result = await guard.guard(
            agent_name="collector",
            action="web_search",
            arguments={"query": "飞书 AI 功能"},
            schema=card.input_schema,
            task_id="abc-123",
        )
        if not result["passed"]:
            task.status = FAILED       # ← 拦截：返回 degraded
            task.error = result["error"]
            return task
        # 通过 → 继续执行 agent handler

    ▎【L5 决策】五层设计的分类逻辑：

    ┌───────┬──────────────┬──────────────┬──────────┬─────────────────┐
    │ 层级   │ 检查什么      │ 失败策略      │ 误杀代价  │ 面试区分词       │
    ├───────┼──────────────┼──────────────┼──────────┼─────────────────┤
    │ 1.白名单│ 操作合法性    │ 阻断          │ 低       │ "最小权限原则"   │
    │ 2.参数 │ 数据类型      │ 阻断          │ 低       │ "契约校验"       │
    │ 3.频控 │ 资源保护      │ 阻断          │ 中       │ "服务保护"       │
    │ 4.PII  │ 数据敏感度    │ 只告警        │ 高       │ "灵敏度 vs 精确度"│
    │ 5.审计 │ 全量记录      │ 不拦截        │ 零       │ "黑匣子"         │
    └───────┴──────────────┴──────────────┴──────────┴─────────────────┘

    前三层阻断：操作的"合法性"问题——不合法的事情执行了也没用。
    第四层告警：数据的"敏感性"问题——正则无法区分公开信息和隐私。
    第五层记录：不管通过还是拦截，全部写入 agent_logs。

    ▎【L5 决策】注入 pool 而非 AuditLogger：
    HarnessGuard 自己负责创建 AuditLogger。
    调用方只需传 pool 一种依赖——降低调用方的认知负载。
    """

    def __init__(self, pool: Pool) -> None:
        """初始化安全检查器。

        Args:
            pool: asyncpg 连接池（用于审计日志写入 agent_logs 表）
        """
        self._bucket = TokenBucket()
        # 【L4 工程】延迟初始化 AuditLogger
        # 避免模块导入时的循环依赖（audit.py → dao.py → connection.py → back to harness）
        self._audit = None
        self._pool = pool

    def _get_audit(self):
        """延迟初始化 AuditLogger。

        【L4 工程】为什么延迟导入？
        解决循环依赖：guard.py → audit.py → dao.py → connection.py → 可能的回路
        延迟导入只在第一次调用 guard() 时执行，对整体性能无影响。
        """
        if self._audit is None:
            from src.harness.audit import AuditLogger
            self._audit = AuditLogger(self._pool)
        return self._audit

    # ═══════════════════════════════════════════════════════════════════════
    # 第一层：白名单
    # ═══════════════════════════════════════════════════════════════════════

    def check_whitelist(self, agent_name: str, action: str) -> bool:
        """检查该 Agent 是否有权执行此 action。

        【L3 核心考点】白名单是确定性检查——O(1) 字典查找，零依赖。

        ▎实现：
            AGENT_WHITELIST.get(agent_name, [])  # 查字典
            action in [...]                       # 成员检查

        ▎白名单矩阵：
             collector:   ["collect", "web_search", "web_fetch"]
             analyzer:    ["analyze", "embed", "rerank"]
             writer:      ["write", "compose_report"]
             quality:     ["evaluate", "score_report"]

        能做什么 不能做什么：
          collector 能 web_search，不能 analyze
          analyzer  能 embed，不能 collect
          writer    能 compose_report，不能 evaluate

        所有对照关系只此一份（AGENT_WHITELIST），改一处全局生效。

        Args:
            agent_name: Agent 名称（"collector"/"analyzer"/"writer"/"quality"）
            action: 动作名称（"web_search"/"analyze"/...）

        Returns:
            True=允许，False=拒绝
        """
        allowed = AGENT_WHITELIST.get(agent_name, [])
        return action in allowed

    # ═══════════════════════════════════════════════════════════════════════
    # 第二层：参数校验
    # ═══════════════════════════════════════════════════════════════════════

    def validate_params(self, action: str, arguments: dict, schema: dict) -> tuple[bool, str]:
        """校验参数类型和必填字段。

        【L4 工程】执行步骤：

        | 步骤 | 做什么 | 代码 |
        |:--:|------|------|
        | 1 | 提取 schema.required 列表 | `required = schema.get("required", [])` |
        | 2 | 遍历 required → 检查是否在 arguments 中 | `for field in required: if field not in arguments: return False` |
        | 3 | 提取 schema.properties | `properties = schema.get("properties", {})` |
        | 4 | 遍历 arguments → 按 properties 做类型映射 | `type_map = {"string":str,"array":list,...}` |
        | 5 | 类型不匹配 → 返回 (False, error_msg) | — |

        【L5 决策】只做结构校验，不做值域校验。

        为什么？值域校验留给 Agent handler：
        - collector 的 "dimensions" 合法值可能是 ["功能","定价"]
        - analyzer 的 "dimensions" 可能是 ["功能","定价","市场","技术栈"]
        Harness 不知道业务语义——硬编码值域就是硬耦合。

        类比：Harness = 门禁（查你有没有卡），Handler = 业务逻辑（你进去后干什么）。

        Args:
            action: 动作名称
            arguments: 实际传入的参数 dict（来自 think 节点决策）
            schema: AgentCard.input_schema（JSON Schema 格式）

        Returns:
            (is_valid, error_message)
            成功 → (True, "")
            失败 → (False, "缺少必填字段: competitors") 或 (False, "参数 ... 类型错误: ...")
        """
        if not schema:
            return True, ""

        required = schema.get("required", [])
        properties = schema.get("properties", {})

        # ── 检查必填字段 ──
        # 【L3 核心考点】schema.required 是 JSON Schema 标准字段
        # 来自 AgentCard 定义——例如 collector 要求必填 "competitors" 和 "dimensions"
        for field in required:
            if field not in arguments:
                return False, f"缺少必填字段: {field}"

        # ── 检查参数类型 ──
        # 【L4 工程】类型映射表 JSON Schema → Python 类型
        # string→str, array→list, object→dict, number→(int,float)
        # 其他类型（boolean、null）当前不校验——必要时扩展 type_map
        for field, value in arguments.items():
            if field in properties:
                expected = properties[field].get("type", "")
                actual = type(value).__name__
                type_map = {"string": str, "array": list, "object": dict, "number": (int, float)}
                expected_type = type_map.get(expected)
                if expected_type and not isinstance(value, expected_type):
                    return False, f"参数 {field} 类型错误: 期望 {expected}, 实际 {actual}"

        return True, ""

    # ═══════════════════════════════════════════════════════════════════════
    # 第三层：频控
    # ═══════════════════════════════════════════════════════════════════════

    def check_rate_limit(self, agent_name: str) -> bool:
        """检查是否超过频控阈值。

        【L3 核心考点】直接委托 TokenBucket.allow()——
        HarnessGuard 只负责"调用"，TokenBucket 负责"怎么判断"。
        单一职责：Guard 不该知道滑动窗口的实现细节。

        Args:
            agent_name: Agent 名称

        Returns:
            True=放行，False=限流
        """
        return self._bucket.allow(agent_name)

    # ═══════════════════════════════════════════════════════════════════════
    # 第四层：PII 检测
    # ═══════════════════════════════════════════════════════════════════════

    def scan_for_pii(self, content: str) -> tuple[bool, list[str]]:
        """检测内容中的敏感信息（三要素正则）。

        【L5 决策】只告警不阻断的原因：

        竞品分析场景的特殊性：
        ┌──────────────────────┬──────────────────────────────────────┐
        │ 场景                   │ 是否含合法联系方式                     │
        ├──────────────────────┼──────────────────────────────────────┤
        │ 分析企业微信功能       │ ✅ 可能含公开客服电话                   │
        │ 对比飞书和钉钉定价     │ ✅ 可能含公司邮箱（sales@feishu.cn）   │
        │ 研究 Notion 技术架构   │ ✅ 可能含技术文档作者邮箱               │
        ├──────────────────────┼──────────────────────────────────────┤
        │ 个人简历分析           │ ❌ 绝不应含私人手机号                   │
        │ 信用卡交易记录         │ ❌ 绝不应含卡号                       │
        └──────────────────────┴──────────────────────────────────────┘

        结论：竞品分析 = 信息分析场景 → 误杀代价 > 漏过代价 → 只告警
              金融/医疗 = 安全敏感场景 → 漏过代价 > 误杀代价 → 应阻断

        Args:
            content: 待检测的文本（arguments 的 str() 序列化）

        Returns:
            (has_pii, [匹配到的 PII 类型列表])
            如 (True, ["手机号", "邮箱"]) 或 (False, [])
        """
        found = []
        for pii_type, pattern in PII_PATTERNS:
            if pattern.search(content):
                found.append(pii_type)
        return len(found) > 0, found

    # ═══════════════════════════════════════════════════════════════════════
    # 第五层：审计
    # ═══════════════════════════════════════════════════════════════════════

    async def _audit_event(self, event: dict) -> None:
        """写入审计日志（异步，不阻塞主流程）。

        【L4 工程】fire-and-forget：不返回值，内部 try/except 兜底。
        审计日志不能阻断 Agent 调用——辅助链路不可成为主链路的单点故障。

        Args:
            event: {
                task_id, agent_name, action,
                request, response, error, duration_ms
            }
        """
        audit = self._get_audit()
        await audit.log(event)

    # ═══════════════════════════════════════════════════════════════════════
    # guard() — 统一入口
    # ═══════════════════════════════════════════════════════════════════════

    async def guard(
        self,
        agent_name: str,
        action: str,
        arguments: dict,
        schema: dict,
        task_id: str = "",
    ) -> dict:
        """五层检查统一入口 —— A2ARouter.send_task() 第 ②.5 步调用。

        【L3 核心考点】guard() 的执行引擎 — 短路求值 + 分段审计：

        正常路径（全部通过）：
        ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌──────────┐    ┌─────────┐
        │ 白名单   │ -> │ 参数校验 │ -> │ 频控    │ -> │ PII 检测  │ -> │ 审计记录 │
        │   ✅     │    │   ✅     │    │   ✅     │    │ (可能告警) │    │ 通过记录  │
        └─────────┘    └─────────┘    └─────────┘    └──────────┘    └─────────┘

        拦截路径（Layer 2 失败）：
        ┌─────────┐    ┌────────────┐                                     
        │ 白名单   │ -> │ 参数校验    │ -> 审计记录(失败) -> return {degraded}
        │   ✅     │    │   ❌       │                                     
        └─────────┘    └────────────┘                                     
                                         → 不执行 Layer 3/4  ← 短路！

        【L5 决策】为什么前三层阻断后还要写审计日志？
        拦截事件 = 安全事件，比正常调用更需要记录。
        以后排查"为什么这次分析失败了"→ 查到 agent_logs.error = "WHITELIST_DENIED"
        → 一眼知道是白名单问题，不用翻代码。

        Args:
            agent_name: Agent 名称（"collector"/"analyzer"/"writer"/"quality"）
            action: 动作名称（"web_search"/"analyze"/"embed"/...）
            arguments: 实际传入的参数 dict
            schema: JSON Schema（来自 AgentCard.input_schema，含 required + properties）
            task_id: 关联任务 ID，用于审计日志追溯

        Returns:
            {
                passed: bool,          # True=通过全部检查，False=被拦截
                checks: {               # 每层检查结果
                    whitelist: bool,
                    param_valid: bool,
                    rate_limit: bool,
                    pii_clean: bool,
                },
                error: str | None,     # 拦截原因（WHITELIST_DENIED / PARAM_INVALID / RATE_LIMITED）
                degraded: bool,        # True=已降级（当前请求不执行，Supervisor 可重试）
            }
        """
        check_start = time.monotonic()
        # 【L4 工程】checks 是累计状态 → 每层通过后设为 True
        # PII 默认 True（没检测到 = clean）
        checks = {
            "whitelist": False,
            "param_valid": False,
            "rate_limit": False,
            "pii_clean": True,
        }
        error = None

        # ── Layer 1: 白名单 ──
        # 【L3 核心考点】短路：白名单失败 → 立即返回
        # 不执行后续检查（参数校验、频控都没意义了——根本不该调）
        if not self.check_whitelist(agent_name, action):
            error = "WHITELIST_DENIED"
            logger.warning("白名单拦截: agent=%s, action=%s", agent_name, action)
            # 【L5 决策】拦截也写审计（安全事件）
            await self._audit_event({
                "task_id": task_id, "agent_name": agent_name, "action": action,
                "request": arguments, "error": error,
                "duration_ms": (time.monotonic() - check_start) * 1000,
            })
            return {"passed": False, "checks": checks, "error": error, "degraded": True}
        checks["whitelist"] = True

        # ── Layer 2: 参数校验 ──
        valid, msg = self.validate_params(action, arguments, schema)
        if not valid:
            error = f"PARAM_INVALID: {msg}"
            logger.warning("参数校验失败: %s", msg)
            await self._audit_event({
                "task_id": task_id, "agent_name": agent_name, "action": action,
                "request": arguments, "error": error,
                "duration_ms": (time.monotonic() - check_start) * 1000,
            })
            return {"passed": False, "checks": checks, "error": error, "degraded": True}
        checks["param_valid"] = True

        # ── Layer 3: 频控 ──
        if not self.check_rate_limit(agent_name):
            error = "RATE_LIMITED"
            logger.warning("频控触发: agent=%s", agent_name)
            await self._audit_event({
                "task_id": task_id, "agent_name": agent_name, "action": action,
                "request": arguments, "error": error,
                "duration_ms": (time.monotonic() - check_start) * 1000,
            })
            return {"passed": False, "checks": checks, "error": error, "degraded": True}
        checks["rate_limit"] = True

        # ── Layer 4: PII 检测（只告警不阻断）──
        # 【L5 决策】has_pii 不设 False → 不放行
        # 只改 checks.pii_clean + 写日志告警
        has_pii, pii_types = self.scan_for_pii(str(arguments))
        if has_pii:
            checks["pii_clean"] = False
            logger.warning("PII 检测告警: agent=%s, types=%s", agent_name, pii_types)

        # ── Layer 5: 审计（通过时记录完整信息）──
        # 【L4 工程】通过时多记录 response（包含 checks 结果）
        # 拦截时 response 为空（没有执行 handler）——保留给 Agent 调用的审计
        duration_ms = (time.monotonic() - check_start) * 1000
        await self._audit_event({
            "task_id": task_id, "agent_name": agent_name, "action": action,
            "request": arguments,
            "response": {"checks": checks},      # ← 完整检查结果
            "duration_ms": duration_ms,
        })

        return {"passed": True, "checks": checks, "error": None, "degraded": False}
