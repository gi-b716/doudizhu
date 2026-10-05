"""``python -m ddz.service`` —— 起本地服务（默认 127.0.0.1:17876）。"""

from __future__ import annotations

import argparse

import uvicorn

from .app import HOST, PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.service", description="斗地主 Copilot 本地服务")
    parser.add_argument("--host", default=HOST, help=f"默认 {HOST}（别用 localhost：Windows 会先解析到 ::1）")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    parser.add_argument("--keep-alive", type=int, default=65,
                        help="keep-alive 秒数。默认给足：太短的话浏览器会复用到刚被服务端关掉的"
                             "空闲连接，表现为偶发的 Failed to fetch（GET 会被浏览器自动重试，"
                             "POST /advise 不会）")
    args = parser.parse_args(argv)

    uvicorn.run(
        "ddz.service.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        timeout_keep_alive=args.keep_alive,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
