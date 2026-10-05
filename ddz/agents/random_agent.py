"""随机基线：每一步在合法着法里等概率挑一个。

作为"及格线"存在——启发式的胜率必须显著高于它（PLAN Phase 2 的验收标准）。它按
``canonical=False`` 取全部具体着法（含各种翅膀选择），并且以 ``pass_prob`` 的概率在能压
的时候也选择不出，免得白送"永远出牌"的节奏优势。
"""

from __future__ import annotations

import random

from ..engine.base import Bid, Candidate
from ..explain import reason_random
from ..moves import legal_beats, legal_leads
from ..state import GameState

__all__ = ["RandomAgent"]

#: 一次最多看多少个具体着法（避免大牌局里枚举翅膀枚举太久）。
_LIMIT = 200


class RandomAgent:
    name = "random"

    def __init__(self, seed: int | None = None, *, pass_prob: float = 0.15) -> None:
        self.rng = random.Random(seed)
        self.pass_prob = pass_prob

    def act(self, state: GameState) -> Candidate:
        seat = state.advise_seat
        hand = state.advise_hand
        if seat is None or not hand:
            return Candidate(source=self.name, reasons=[reason_random()])

        to_beat = state.to_beat
        if to_beat is None:
            moves = legal_leads(hand, canonical=False, limit=_LIMIT)
        else:
            moves = legal_beats(hand, to_beat, canonical=False, limit=_LIMIT)
            if not moves or self.rng.random() < self.pass_prob:
                return Candidate(None, 0.0, [reason_random()], ["random:pass"], self.name)

        move = self.rng.choice(moves)
        return Candidate(move, 0.0, [reason_random()], ["random"], self.name)

    def bid(self, state: GameState) -> Bid:
        legal = state.legal_bids
        if not legal:
            return Bid(0, 0.0, [reason_random()])
        return Bid(self.rng.choice(legal), 0.0, [reason_random()])
