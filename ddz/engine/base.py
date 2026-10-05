"""引擎层共用类型：决策结果、叫分结果、agent 协议。

所有 agent（启发式、随机、将来的 DouZero）都实现同一个 :class:`Agent` 协议，返回同一
种 :class:`Candidate`，所以竞技场、服务、评估脚本可以用一套代码驱动它们。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..moves import Move
from ..state import GameState

__all__ = ["Candidate", "Bid", "Agent"]


@dataclass(slots=True)
class Candidate:
    """一手决策：要出的牌（``None`` 表示不出）以及为什么。"""

    move: Move | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = ""
    #: 引擎自报的胜率估值（0..1）；启发式给不出就不填，DouZero 会填。
    win_prob: float | None = None
    #: 备用选择（按分数降序），服务用来展示"备选 1–3"。
    alternatives: list["Candidate"] = field(default_factory=list)
    #: 引擎内部各阶段的耗时（毫秒），服务原样透传到 ``timing_ms``。
    timing: dict[str, float] = field(default_factory=dict)

    @property
    def is_pass(self) -> bool:
        return self.move is None

    @property
    def cards(self) -> tuple[int, ...]:
        return self.move.cards if self.move is not None else ()

    @property
    def pattern_key(self) -> str | None:
        return self.move.pattern.key() if self.move is not None else None


@dataclass(slots=True)
class Bid:
    """叫分决策。``value=0`` 即不叫。"""

    value: int
    strength: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_pass(self) -> bool:
        return self.value == 0


@runtime_checkable
class Agent(Protocol):
    """统一接口。``name`` 出现在竞技场报表里。"""

    name: str

    def act(self, state: GameState) -> Candidate:
        """轮到 ``state.advise_seat`` 出牌时的决策（含"不出"）。"""

    def bid(self, state: GameState) -> Bid:
        """轮到 ``state.advise_seat`` 叫分时的决策。"""
