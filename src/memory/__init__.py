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

  通过 __init__.py 聚合:
    from src.memory import (
        LongTermMemoryEngine,
        MemorySummarizer,
    )
  效果: 行数差不多，但关键是调用方不需要知道"LongTermMemoryEngine 在哪个文件"。
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

记忆模块是竞品分析系统的"知识底座"。它现在只有两条真实通路：

  写: observe 节点之后 → write_task_facts() → LongTermMemoryEngine.add_memory()
      事实来自 evidence_map（有 URL），不来自报告正文 —— 正文里的句子没有来源，
      把"模型说过的话"升格成"观测到的事实"，之后就再也查不回来了。

  读: 按 subject 精确键取 content（原 verify 的读取口已随该意图下线）
      **不走向量**。表里有 vector(1024)，不等于这次查询该用向量 —— 判据永远是
      "这次要找的是同一个键，还是相似的东西"。向量只服务写入时的取代判定
      （同 subject 余弦 ≥0.90）。

【2026-10-28 删除的内容】
  原 `retrieval.py` 的 MemoryRetrievalStrategy（混合触发：关键词预检 + LLM 兜底）
  与 `LongTermMemoryEngine.retrieve()`（五步检索流水线）一并删除。
  它们在生产路径上从未接过线（build_supervisor_graph 一直只传 4 个参数，
  retrieval_strategy 恒为 None），而且方向本身是错的：读取要靠键，不靠召回。
  删掉比留着"以后可能用"干净 —— 留一个没人调用的向量检索实现，
  只会让后来人以为读取真的走向量。

当前代码的两层设计:
  — Summarizer 已写但不集成（留给 Supervisor）
  — ConflictResolver 已写但只在 add_memory 触发（不是每轮都检查）
  — Forgetting 已写但不自动调度（留给 Phase 9 cron）

这种"先写逻辑、后接调度"的策略叫 Staged Integration（分阶段集成）——
每层逻辑独立可用，集成只加钩子，不改逻辑。

用法:
    from src.memory import (
        MemorySummarizer,
        LongTermMemoryEngine,
        MemoryConflictResolver,
        MemoryForgetting,
    )
"""

from __future__ import annotations

# 【L3】子模块导入 — 每种记忆能力封装在独立文件中
from src.memory.summarizer import MemorySummarizer
from src.memory.long_term import LongTermMemoryEngine
from src.memory.conflict import MemoryConflictResolver
from src.memory.forgetting import MemoryForgetting

# 【L3】__all__ 白名单 — from src.memory import * 只导出这 4 个公开类
# 不导出内部工具函数、私有 helper、DAO 层。调用方只看到 API 面。
__all__ = [
    "MemorySummarizer",
    "LongTermMemoryEngine",
    "MemoryConflictResolver",
    "MemoryForgetting",
]
