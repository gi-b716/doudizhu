"""``python -m ddz.service`` —— 起本地服务（默认 127.0.0.1:17876）。"""

from __future__ import annotations

import argparse
import os

import uvicorn

from .app import HOST, PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.service", description="斗地主 Copilot 本地服务")
    parser.add_argument("--host", default=HOST, help=f"默认 {HOST}（别用 localhost：Windows 会先解析到 ::1）")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    parser.add_argument(
        "--endgame", choices=("ondemand", "off", "prefetch"), default="ondemand",
        help="残局求解模式（默认 ondemand）：ondemand = 轮到我时按需求解；off = 不用求解器。"
             "（prefetch 是历史选项，现在等同于 ondemand。）模式会显示在浮窗页脚。",
    )
    parser.add_argument(
        "--endgame-budget", type=float, default=250.0,
        help="**轮到我时**求解最多花多久（毫秒，默认 250）。这是唯一会加在出建议延迟上的量。",
    )
    parser.add_argument("--keep-alive", type=int, default=65,
                        help="keep-alive 秒数。默认给足：太短的话浏览器会复用到刚被服务端关掉的"
                             "空闲连接，表现为偶发的 Failed to fetch（GET 会被浏览器自动重试，"
                             "POST /advise 不会）")
    args = parser.parse_args(argv)

    # 模式必须经由环境变量传给 app：uvicorn 用 import string 起 app，构造发生在子进程/导入时
    os.environ["DDZ_ENDGAME_MODE"] = args.endgame
    os.environ["DDZ_ENDGAME_BUDGET_MS"] = str(args.endgame_budget)

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
