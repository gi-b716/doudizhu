"""本地 FastAPI 服务：浮窗脚本与本机之间的唯一通道。

绑定 ``127.0.0.1:17876``（脚本侧也用字面量 ``127.0.0.1``：Windows 上 ``localhost`` 会先
解析到 ``::1``）。跨源方面，油猴脚本从游戏页面（其它源）打过来属于跨源请求，而且目标
是"私有网络"——Chrome 的 Private Network Access 预检要求响应里显式带上
``Access-Control-Allow-Private-Network: true``（PLAN §3.5）。

Phase 1 只提供 ``/health`` 与 ``/ingest``；``/advise`` ``/config`` 在 Phase 3。
"""

from __future__ import annotations

import importlib.util
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..data.archive import Archive
from ..record import from_finished_state
from ..state import StateParseError
from .schemas import HealthResponse, IngestRequest, IngestResponse

__all__ = ["create_app", "app", "HOST", "PORT"]

HOST = "127.0.0.1"
PORT = 17876


def create_app(archive: Archive | None = None) -> FastAPI:
    app = FastAPI(title="ddz-copilot", version=__version__)
    state: dict[str, Any] = {
        "started_at": time.time(),
        "archive": archive if archive is not None else Archive(),
        "records": None,
    }
    app.state.ddz = state

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
        """PNA 预检：请求头带 ``Access-Control-Request-Private-Network`` 时回一个允许。"""
        response = await call_next(request)
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        records = state["records"]
        if records is None:
            records = state["archive"].count()
        return HealthResponse(
            ok=True,
            version=__version__,
            # torch 只是"装没装"的探测；真实加载状态由 Phase 4 的 douzero.agent 提供
            torch=importlib.util.find_spec("torch") is not None,
            cuda=False,
            douzero_loaded=False,
            models={},
            uptime_s=round(time.time() - state["started_at"], 3),
            records=records,
        )

    @app.post("/ingest", response_model=IngestResponse)
    def ingest(request: IngestRequest) -> IngestResponse:
        try:
            record = from_finished_state(
                request.state,
                game_id=request.game_id,
                captured_at=request.captured_at,
                source=request.source,
                seat_of_interest=request.seat_of_interest,
            )
        except StateParseError as exc:
            # 逻辑问题也返回 200，让脚本只需要看 ok 字段（与 /advise 同一约定）
            return IngestResponse(ok=False, reason="parse_error", message=str(exc))

        archive_obj: Archive = state["archive"]
        try:
            added = archive_obj.add(
                record, raw=request.state, keep_raw=request.keep_raw
            )
        except Exception as exc:  # 归档失败不该把脚本打挂
            return IngestResponse(
                ok=False, reason="archive_error", message=str(exc), game_id=record.game_id
            )

        total = state["records"]
        if total is None:
            total = archive_obj.count()  # 首次问询：直接数文件（已含刚写进去的这局）
        elif added:
            total += 1
        state["records"] = total
        return IngestResponse(
            ok=True,
            added=added,
            duplicate=not added,
            game_id=record.game_id,
            dedupe_key=record.dedupe_key(),
            records=total,
            message="已归档" if added else "这局已经在库里了",
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        return JSONResponse(
            status_code=200,
            content={"ok": False, "reason": "internal_error", "message": str(exc)},
        )

    return app


app = create_app()
