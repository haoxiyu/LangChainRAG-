"""开发启动脚本。

在 uvicorn 创建事件循环之前切好策略,避免 Windows 上 psycopg 异步报错。

用法(在 backend 目录下):
    python run.py                 # 默认 8000 端口,开启热重载
    python run.py --port 9000
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.compat import setup_event_loop_policy  # noqa: E402

setup_event_loop_policy()

import uvicorn  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="启动知识库问答系统后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-reload", action="store_true", help="关闭热重载")
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        # 必须显式指定循环工厂:uvicorn 在 Windows 非子进程模式下会硬编码用
        # ProactorEventLoop,而 psycopg 异步不支持它。事件循环策略对 uvicorn 无效。
        loop="app.compat:selector_loop_factory",
        log_level="info",
    )


if __name__ == "__main__":
    main()
