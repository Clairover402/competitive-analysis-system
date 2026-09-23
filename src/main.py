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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
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
