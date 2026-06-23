"""记忆系统包入口 — 聚合 5 个记忆子模块，提供统一导入接口。

═══════════════════════════════════════════════════════════════════════════════
                          【L3 核心考点索引】
═══════════════════════════════════════════════════════════════════════════════

  §1 __all__ 白名单: 控制 from src.memory import * 的可见范围
  §2 __init__.py 三重作用: 包标记 / 导入聚合 / 公开 API 定义
  §3 依赖链: MemoryForgetting → AgentMemoryDAO → PostgreSQL → embedding 模型


═══════════════════════════════════════════════════════════════════════════════
                          【L4 工程 — __init__.py 的三重作用】
═══════════════════════════════════════════════════════════════════════════════

① 包标记（历史包袱）
  ────────────────────────────
  Python 3.3+ 不再强制要求目录下有 __init__.py 才算包（隐式命名空间包），
  但显式 __init__.py 仍有三个不可替代的工程价值：
    a) from src.memory import X → Python 必须找到 __init__.py 中的 X
    b) 没有 __init__.py 的包，mypy/pyright 类型检查会报警
    c) 显式包标记让 IDE 和构建工具（如 setuptools.find_packages()）知道这是包

② 导入聚合 — 把深路径缩成短路径
  ────────────────────────────────────
  如果调用方直接 import 子模块:
    from src.memory.long_term import LongTermMemoryEngine
    from src.memory.summarizer import MemorySummarizer
    from src.memory.retrieval import MemoryRetrievalStrategy

  通过 __init__.py 聚合:
    from src.memory import (
        LongTermMemoryEngine,
        MemorySummarizer,
        MemoryRetrievalStrategy,
    )
  效果: 5 行 → 4 行（差距不大），但关键是调用方不需要知道"LongTermMemoryEngine 在哪个文件"。
  就像你去餐厅点菜不需要知道每道菜在哪个厨房做——__init__.py 是菜单。

  这种模式叫 Facade Pattern（门面模式）的直接应用。

③ 公开 API 定义 — __all__ 白名单
  ─────────────────────────────────
  from src.memory import * 只会导入 __all__ 中的名字。
  如果 __all__ 不存在，import * 会导入所有不以 _ 开头的名字——包括内部使用的工具函数。
  白名单保证了"只暴露该暴露的"。

  【L4 工程】为什么不推荐 import *？
    — 命名空间污染: 你无法一眼看出 X 是从哪个模块导入的
    — 工具链问题: IDE 的"查找引用"在 import * 情况下会漏
    — 循环依赖风险: * 导入可能意外触发尚未完全初始化的模块
    但 __all__ 的定义仍然有价值: 它向开发者声明了"这是公开 API"。


═══════════════════════════════════════════════════════════════════════════════
                          【L5 架构 — 记忆模块在系统中的位置】
═══════════════════════════════════════════════════════════════════════════════

记忆模块是竞品分析系统的"知识底座"。它存在于两个层面：

  层面1（当前）: Pipeline 集成
    analyze 节点: LongTermMemoryEngine.retrieve() → 检索历史记忆 → 注入 prompt
    finalize 节点: LongTermMemoryEngine.add_memory() → 提取本次关键决策 → 持久化

  层面2（Phase 5A）: Supervisor 集成
    MemorySummarizer 激活 — ReAct 循环提供对话轮次
    MemoryRetrievalStrategy 扩能 — 增加轮次间隔条件
    MemoryConflictResolver 前移 — 从任务结束后批处理 → 实时检测

当前代码的两层设计:
  — Summarizer 已写但不集成（留给 Supervisor）
  — ConflictResolver 已写但只在 add_memory 触发（不是每轮都检查）
  — Forgetting 已写但不自动调度（留给 Phase 6 cron）

这种"先写逻辑、后接调度"的策略叫 Staged Integration（分阶段集成）——
每层逻辑独立可用，集成只加钩子，不改逻辑。

用法:
    from src.memory import (
        MemorySummarizer,
        LongTermMemoryEngine,
        MemoryRetrievalStrategy,
        MemoryConflictResolver,
        MemoryForgetting,
    )
"""

from __future__ import annotations

# 【L3】子模块导入 — 每种记忆能力封装在独立文件中
from src.memory.summarizer import MemorySummarizer
from src.memory.long_term import LongTermMemoryEngine
from src.memory.retrieval import MemoryRetrievalStrategy
from src.memory.conflict import MemoryConflictResolver
from src.memory.forgetting import MemoryForgetting

# 【L3】__all__ 白名单 — from src.memory import * 只导出这 5 个公开类
# 不导出内部工具函数、私有 helper、DAO 层。调用方只看到 API 面。
__all__ = [
    "MemorySummarizer",
    "LongTermMemoryEngine",
    "MemoryRetrievalStrategy",
    "MemoryConflictResolver",
    "MemoryForgetting",
]
