"""服务层的请求/响应模型（Phase 3 会在这里加 ``/advise`` 的模型）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "HealthResponse", "IngestRequest", "IngestResponse",
    "AdviseOptionsModel", "AdviseRequest", "ConfigRequest",
]


class HealthResponse(BaseModel):
    ok: bool = True
    version: str
    torch: bool = False
    cuda: bool = False
    douzero_loaded: bool = False
    #: 服务实际跑在哪个解释器上。**这个字段是踩过坑才加的**：`python -m ddz.service` 会走
    #: PATH 里第一个 python（比如 conda），那里可能装了 fastapi、没装 torch——服务照常起来、
    #: 面板照常出建议，只是 DouZero 永远"未就绪"。一眼看出路径不对，比逐层排查省事得多。
    python: str = ""
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


class AdviseOptionsModel(BaseModel):
    """``/advise`` 的 ``options``（都有默认值，脚本可以只传 state）。"""

    agent: str = "auto"          # auto | heuristic | douzero:WP
    top_k: int = 3
    deadline_ms: int = 400
    for_seat: int | None = None  # 指定替哪一家建议（默认：自己；旁观时是当前出牌者）


class AdviseRequest(BaseModel):
    """浮窗每步发过来的东西：原始平台 state + 会话标识。

    ``state`` 就是 ``POST /games/doudizhu/api`` 响应里的 ``state`` 字段（也可能是
    ``{"state": {...}}`` 外壳，两种都吃）。
    """

    session: str = "default"
    state: dict[str, Any]
    #: 页面 ``#ddz`` 的 ``data-game``。换局时 version 会从 0 重来，靠它区分两局。
    game_id: str | None = None
    options: AdviseOptionsModel = Field(default_factory=AdviseOptionsModel)


class ConfigRequest(BaseModel):
    """``/config``：运行时切换默认引擎等。字段全可选。"""

    default_agent: str | None = None
    top_k: int | None = None
    deadline_ms: int | None = None
    forget_session: str | None = None
