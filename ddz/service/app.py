"""本地 FastAPI 服务：浮窗脚本与本机之间的唯一通道。

绑定 ``127.0.0.1:17876``（脚本侧也用字面量 ``127.0.0.1``：Windows 上 ``localhost`` 会先
解析到 ``::1``）。跨源方面，油猴脚本从游戏页面（其它源）打过来属于跨源请求，而且目标
是"私有网络"——Chrome 的 Private Network Access 预检要求响应里显式带上
``Access-Control-Allow-Private-Network: true``（PLAN §3.5）。

端点：``/health`` ``/advise`` ``/ingest`` ``/config``。**逻辑问题一律 200**：响应里的
``ok`` 说明成败，脚本不用区分状态码。所有编排都在 :mod:`ddz.service.advisor` 里，这里只做
HTTP 与参数校验。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..data.archive import Archive
from .advisor import AdviseOptions, Advisor
from .schemas import (
    AdviseRequest, ConfigRequest, HealthResponse, IngestRequest, IngestResponse,
)

__all__ = ["create_app", "app", "HOST", "PORT"]

HOST = "127.0.0.1"
PORT = 17876


def create_app(archive: Archive | None = None, advisor: Advisor | None = None) -> FastAPI:
    app = FastAPI(title="ddz-copilot", version=__version__)
    advisor = advisor if advisor is not None else Advisor(archive=archive)
    app.state.ddz = {"started_at": time.time(), "advisor": advisor}
    app.state.advisor = advisor

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        # Chrome 的 Private Network Access 预检：不开这个，starlette 会直接回 400
        allow_private_network=True,
    )

    @app.middleware("http")
    async def allow_private_network(request: Request, call_next):
        response = await call_next(request)
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        from ..douzero import agent as douzero_agent

        status = advisor.status()
        douzero = douzero_agent.status()
        ready = bool(douzero.get("ready"))
        return HealthResponse(
            ok=True,
            version=__version__,
            torch=importlib.util.find_spec("torch") is not None,
            cuda=bool(douzero.get("cuda")),
            douzero_loaded=ready,
            python=sys.executable,
            models={
                "default": status["default_agent"],
                "auto_engine": status.get("auto_engine"),
                **({"douzero": "douzero:WP"} if ready else {}),
            },
            uptime_s=status["uptime_s"],
            records=status["records"],
        )

    @app.post("/advise")
    def advise(request: AdviseRequest) -> dict[str, Any]:
        options = AdviseOptions(
            agent=request.options.agent,
            top_k=request.options.top_k,
            deadline_ms=request.options.deadline_ms,
            for_seat=request.options.for_seat,
        )
        return advisor.advise(request.session, request.state, options, game_id=request.game_id)

    @app.post("/ingest", response_model=IngestResponse)
    def ingest(request: IngestRequest) -> dict[str, Any]:
        return advisor.ingest(
            request.state,
            game_id=request.game_id,
            captured_at=request.captured_at,
            source=request.source,
            seat_of_interest=request.seat_of_interest,
            keep_raw=request.keep_raw,
        )

    @app.post("/config")
    def config(request: ConfigRequest) -> dict[str, Any]:
        applied = advisor.configure(**request.model_dump())
        return {"ok": True, "applied": applied, **advisor.status()}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        return JSONResponse(
            status_code=200,
            content={"ok": False, "reason": "internal_error", "message": str(exc)},
        )

    return app


app = create_app()
