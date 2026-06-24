"""Harness Engineering — 五层安全检查壳（Phase 5B）。

═══════════════════════════════════════════════════════════════════════════════
                    【L5 架构全景图】
═══════════════════════════════════════════════════════════════════════════════

  Harness 不是独立运行的模块——它是注入到 A2ARouter.send_task() 的拦截层。

  IntentRouter._setup_dependencies()
       │
       ├── guard = HarnessGuard(pool)                    ← 创建安全壳（持有 TokenBucket + AuditLogger）
       ├── router = A2ARouter(mcp_server, harness=guard) ← 注入（send_task 第 ②.5 步生效）
       │
       └── 4 Agent 注册后 — Pipeline / Supervisor 所有的 Agent 调用
                  │
                  └── router.send_task(task)
                        │
                        ├── Step ①: 查 AgentCard / handler / LLM
                        ├── Step ②.5: HarnessGuard.guard()        ← 五层检查在这里
                        │       ├── Layer 1: 白名单（AGENT_WHITELIST 查字典）
                        │       ├── Layer 2: 参数校验（JSON Schema 类型+必填）
                        │       ├── Layer 3: 频控（TokenBucket 双层限流）
                        │       ├── Layer 4: PII 检测（三正则匹配，只告警）
                        │       └── Layer 5: AuditLogger.log() → agent_logs
                        │         (通过 → request+response+checks)
                        │         (拦截 → request+error)
                        ├── Step ③: 标记 RUNNING + 执行 handler
                        └── ...

  【L4 工程】工作流程（一句话）：
    HarnessGuard 收到 guard(agent, action, params) →
    短路检查五层 →
    返回 {passed, checks, error, degraded} →
    A2ARouter 读 passed:True→继续 / False→task.status=FAILED


═══════════════════════════════════════════════════════════════════════════════
                    【对外导出】
═══════════════════════════════════════════════════════════════════════════════

  from src.harness import HarnessGuard, AuditLogger

  Guard 注入 A2ARouter:
    guard = HarnessGuard(pool)
    router = A2ARouter(mcp_server, harness=guard)

  AuditLogger 也可独立使用（Phase 6 服务化时写其他审计事件）:
    audit = AuditLogger(pool)
    await audit.log({"task_id": "...", "agent_name": "...", ...})
    trail = await audit.get_task_trail(task_id)
"""

from __future__ import annotations

from src.harness.guard import HarnessGuard
from src.harness.audit import AuditLogger

__all__ = [
    "HarnessGuard",
    "AuditLogger",
]
