"""结构化日志 — 用标准 logging 实现类 structlog 的键值绑定。

═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — 设计决策】
═══════════════════════════════════════════════════════════════════════════════

为什么不用 structlog？
  — Phase 9 环境 .venv 没有 pip，structlog 安装失败
  — 标准 logging 可完成同样功能（Filter + Formatter）
  — 无外部依赖，省去 1 次 install

使用方式：
  logger = get_logger().bind(task_id="abc", agent="collector")
  logger.info("web_search", extra={"action": "web_search", "results": 15})

输出格式：
  [2026-07-21T19:30:00] [INFO] [task_abc] [collector] [web_search] 搜索完成, results=15
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ═════════════════════════════════════════════════════════════════════════════
# §1 日志上下文（线程不安全，asyncio 下每个 task 独立创建 LoggerAdapter）
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class LogContext:
    """可绑定的日志上下文键值对。

    【L4 工程】为什么用 dataclass 而不是 dict？
    — dataclass 提供默认值 + 类型检查，避免拼写错误
    — 新增字段时所有引用处一目了然（比 dict 的"猜 key"强）
    """
    task_id: str = ""
    agent: str = ""
    action: str = ""


# ═════════════════════════════════════════════════════════════════════════════
# §2 自定义 Formatter — 结构化输出
# ═════════════════════════════════════════════════════════════════════════════

class StructuredFormatter(logging.Formatter):
    """输出格式：[ISO时间戳] [级别] [task_id] [agent] [action] 消息 + 扩展字段"""

    def format(self, record: logging.LogRecord) -> str:
        # ── 提取绑定字段 ──
        task_id = getattr(record, "task_id", "") or ""
        agent = getattr(record, "agent", "") or ""
        action = getattr(record, "action", "") or ""

        # ── 构造时间戳 ──
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        # ── 基础格式 ──
        parts = [f"[{ts}]", f"[{record.levelname}]"]
        if task_id:
            parts.append(f"[{task_id}]")
        if agent:
            parts.append(f"[{agent}]")
        if action:
            parts.append(f"[{action}]")

        # ── 消息体 ──
        msg = record.getMessage()
        parts.append(msg)

        # ── 附加扩展字段（extra 中除了标准字段之外的键） ──
        extras = {}
        for key, val in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno",
                "pathname", "filename", "module", "exc_info",
                "exc_text", "stack_info", "created", "msecs",
                "relativeCreated", "thread", "threadName",
                "process", "processName", "task_id", "agent", "action",
            }:
                extras[key] = val
        if extras:
            extra_str = ", ".join(f"{k}={v}" for k, v in extras.items())
            parts[-1] = f"{parts[-1]}, {extra_str}"

        return " ".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
# §3 日志适配器——支持 .bind() 链式调用
# ═════════════════════════════════════════════════════════════════════════════

class BoundLogger:
    """绑定上下文的日志适配器。

    【L4 工程】API 风格对标 structlog：
      — 创建时绑定：get_logger().bind(task_id="abc")
      — 调用时传 extra：logger.info("msg", extra={"results": 15})
      — bind() 返回新实例（不可变风格，避免并发问题）
    """

    def __init__(self, logger: logging.Logger, context: LogContext | None = None):
        self._logger = logger
        self._ctx = context or LogContext()

    def bind(self, **kwargs: str) -> "BoundLogger":
        """绑定新字段，返回新实例（不修改原实例）。"""
        new_ctx = LogContext(
            task_id=kwargs.get("task_id", self._ctx.task_id),
            agent=kwargs.get("agent", self._ctx.agent),
            action=kwargs.get("action", self._ctx.action),
        )
        return BoundLogger(self._logger, new_ctx)

    def _log(self, level: int, msg: str, extra: dict[str, Any] | None = None) -> None:
        extra = extra or {}
        merged = {
            "task_id": self._ctx.task_id,
            "agent": self._ctx.agent,
            "action": self._ctx.action,
            **extra,
        }
        self._logger.log(level, msg, extra=merged)

    def debug(self, msg: str, **extra: Any) -> None:
        self._log(logging.DEBUG, msg, extra)

    def info(self, msg: str, **extra: Any) -> None:
        self._log(logging.INFO, msg, extra)

    def warning(self, msg: str, **extra: Any) -> None:
        self._log(logging.WARNING, msg, extra)

    def error(self, msg: str, **extra: Any) -> None:
        self._log(logging.ERROR, msg, extra)

    def exception(self, msg: str, **extra: Any) -> None:
        self._log(logging.ERROR, msg, extra)
        # 同步 print traceback（标准 logging 的 exc_info 方式）
        import traceback
        self._logger.error(f"{msg}\n{traceback.format_exc()}")


# ═════════════════════════════════════════════════════════════════════════════
# §4 初始化函数
# ═════════════════════════════════════════════════════════════════════════════

def setup_logging(level: int = logging.INFO) -> BoundLogger:
    """配置并返回根日志器。

    标准 logging 配置：
    — Handler: StreamHandler → stdout
    — Formatter: StructuredFormatter
    — Level: 可配置（默认 INFO）

    Returns:
        BoundLogger: 包装好的日志器，支持 .bind().info() 链式调用。
    """
    root = logging.getLogger("competitive_analysis")
    root.setLevel(level)

    # 避免重复添加 handler（多次调用 setup_logging 时）
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(StructuredFormatter())
        root.addHandler(handler)

    return BoundLogger(root)
