"""启动入口：python run.py [--host 127.0.0.1] [--port 8000] [--reload]"""
from __future__ import annotations

import argparse
import webbrowser
from threading import Timer

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="ls agent 本地服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"ls agent -> {url}")
    if args.open:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,  # 访问日志由 app 自己的中间件记（带请求 id、耗时、级别），uvicorn 那份只重复一遍
    )


if __name__ == "__main__":
    main()
