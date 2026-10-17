# -*- coding: utf-8 -*-
"""竞品分析系统 — uvicorn 启动入口。

用法:
    python main.py                          # 默认 localhost:8000
    python main.py --host 0.0.0.0 --port 8080
    python main.py --reload                 # 开发模式热重载

启动后访问:
    — 登录页: http://localhost:8000/static/login.html
    — API 文档: http://localhost:8000/docs
    — 健康检查: http://localhost:8000/health
"""

from __future__ import annotations

import argparse
import logging
import sys

# 【2026-10-02 LangSmith 接入修复】必须在任何 langchain/langgraph import 之前
# 显式加载 .env，否则 LANGCHAIN_TRACING_V2 等变量在 langchain 首次 import 时
# 还不在 os.environ 里，导致 tracing 被静默禁用、trace 不上报。
#
# 根因：pydantic-settings 只在 Settings() 实例化时才加载 .env，而 langchain 的
# tracing 开关在模块 import 时就检查 os.environ，二者存在竞态。这里用
# python-dotenv 的 load_dotenv 在最前面强制加载，保证顺序正确。
from dotenv import load_dotenv

load_dotenv()

import uvicorn


def main() -> None:
    import os as _os
    _os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass  # stdout redirected (pipe)

    # 【L4 工程】全局日志配置——INFO 级别才能看到 Pipeline 节点日志
    # 默认 WARNING 过滤掉所有 INFO，导致中间节点静默执行
    _log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=_log_fmt,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 【2026-10-16 修复】此前 observability/logging.py 的 setup_logging() 带文件落盘
    # 但全仓库无人调用，导致 logs/ 恒为空、跑完任务无法事后 grep 排查。
    # 这里直接在 root 上追加 RotatingFileHandler：控制台 + 文件双通道。
    try:
        from logging.handlers import RotatingFileHandler
        _log_dir = _os.environ.get("LOG_DIR", "logs")
        _os.makedirs(_log_dir, exist_ok=True)
        _fh = RotatingFileHandler(
            _os.path.join(_log_dir, "app.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        _fh.setLevel(logging.INFO)
        _fh.setFormatter(logging.Formatter(_log_fmt, datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(_fh)
    except Exception as _e:  # 日志落盘失败不应阻断服务启动
        print(f"[warn] 日志文件落盘初始化失败: {_e}")
    parser = argparse.ArgumentParser(
        description="竞品分析多Agent协作系统",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="监听地址（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="监听端口（默认 8000）",
    )
    parser.add_argument(
        "--reload", action="store_true",
        help="开发模式热重载",
    )
    args = parser.parse_args()

    # 环境变量优先于命令行参数（兼容 Docker / systemd）
    import os
    host = os.environ.get("HOST", args.host)
    port = int(os.environ.get("PORT", args.port))

    uvicorn.run(
        "src.api.routes:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
