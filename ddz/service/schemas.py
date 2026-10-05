"""服务层的请求/响应模型（Phase 3 会在这里加 ``/advise`` 的模型）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["HealthResponse", "IngestRequest", "IngestResponse"]


class HealthResponse(BaseModel):
    ok: bool = True
    version: str
    torch: bool = False
    cuda: bool = False
    douzero_loaded: bool = False
    models: dict[str, str] = Field(default_factory=dict)
    uptime_s: float = 0.0
    records: int = 0


class IngestRequest(BaseModel):
    """归档一份**终局**平台 state。

    ``state`` 就是油猴脚本从 ``/games/doudizhu/api`` 抓到的那个对象（含 ``fairness.deals``
    与全量 ``log``）；``game_id`` 取自页面 ``#ddz`` 的 ``data-game``。
    """

    state: dict[str, Any]
    game_id: str | None = None
    captured_at: float | None = None
    source: str = "platform"
    seat_of_interest: int | None = None
    keep_raw: bool = False


class IngestResponse(BaseModel):
    ok: bool
    added: bool = False
    duplicate: bool = False
    game_id: str = ""
    dedupe_key: str = ""
    records: int = 0
    reason: str = ""
    message: str = ""
