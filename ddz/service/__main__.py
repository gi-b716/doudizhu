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
    args = parser.parse_args(argv)

    uvicorn.run("ddz.service.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
