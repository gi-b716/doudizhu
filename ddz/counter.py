"""记牌器与威胁提示。

记牌口径与客户端 ``renderCounter()`` 一致：**未见的牌 = 一副 54 张 − 我的手牌 − 已出的牌**。
地主确定后底牌虽然公开，但它属于地主手牌、并且能与他手里的牌凑成炸弹，所以仍算未见
（这样 ``possible_bombs`` 才不会漏报）。

威胁一律站在"被建议的座位"视角（:attr:`ddz.state.GameState.advise_seat`）：下家/上家
按该座位的顺序称呼，文案见 :mod:`ddz.explain`。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import cards as C
from .explain import (
    threat_opponent_low, threat_partner_close, threat_possible_bomb,
    threat_rocket_possible, threat_top_card_pressed, seat_label,
)
from .state import PHASE_FINISHED, PHASE_PLAYING, SEATS, GameState

__all__ = [
    "Threat", "unseen_ranks", "possible_bombs", "rocket_possible",
    "top_rank_pressed", "threats", "counter_snapshot",
]

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_DANGER = "danger"


@dataclass(frozen=True, slots=True)
class Threat:
    code: str
    level: str
    text: str


def unseen_ranks(gs: GameState) -> list[int]:
    """未见牌的点数直方图（长度 15，与客户端记牌器逐格对应）。"""
    return gs.unseen_ranks


def possible_bombs(gs: GameState) -> list[int]:
    """可能有炸弹的点数：某点数 4 张全部未见（且不是王——王只有 1 张）。"""
    counts = gs.unseen_ranks
    return [r for r in range(C.JOKER_RANK) if counts[r] == 4]


def rocket_possible(gs: GameState) -> bool:
    """王炸是否还可能存在：两张王都未见。"""
    counts = gs.unseen_ranks
    return bool(counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1])


def top_rank_pressed(gs: GameState, seat: int | None = None) -> int:
    """我最大的单牌被打过几张未见牌压着（0 表示我的最大单牌已经无敌）。

    只看普通点数与王：数一数"比我手上最大点数还大"的未见牌有几张。
    """
    seat = gs.advise_seat if seat is None else seat
    hand = gs.hand_for(seat) if seat is not None else None
    if not hand:
        return 0
    top = max(C.rank(c) for c in hand)
    counts = gs.unseen_ranks
    return sum(counts[top + 1 :])


def threats(gs: GameState) -> list[Threat]:
    """当前局面的威胁/提醒（按严重程度排序：danger → warn → info）。"""
    if gs.phase != PHASE_PLAYING:
        return []
    me = gs.advise_seat
    if me is None:
        return []

    out: list[Threat] = []
    partner = gs.partner_seat
    counts = gs.counts

    # 对手（不是我也不是队友的座位；地主未定时三名之中除我以外都算）
    opponents = [s for s in SEATS if s != me and s != partner]
    for seat in opponents:
        left = counts[seat]
        if left in (0,):
            continue
        label = seat_label(seat, me)
        if left == 1:
            # 只剩一张：他再出一手就走完了
            out.append(Threat("opponent_low", LEVEL_DANGER, threat_opponent_low(label, left, True)))
        elif left == 2:
            out.append(Threat("opponent_low", LEVEL_WARN, threat_opponent_low(label, left, False)))

    if partner is not None and counts[partner] in (1, 2):
        out.append(Threat("partner_close", LEVEL_INFO, threat_partner_close(counts[partner])))

    bombs = possible_bombs(gs)
    if bombs:
        names = "、".join(C.RANK_NAMES[r] for r in bombs)
        out.append(Threat("possible_bomb", LEVEL_WARN, threat_possible_bomb(names)))
    if rocket_possible(gs):
        out.append(Threat("rocket_possible", LEVEL_INFO, threat_rocket_possible()))

    pressed = top_rank_pressed(gs, me)
    if pressed:
        hand = gs.hand_for(me) or ()
        top = C.RANK_NAMES[max(C.rank(c) for c in hand)]
        out.append(Threat("top_card_pressed", LEVEL_INFO, threat_top_card_pressed(top, pressed)))

    order = {LEVEL_DANGER: 0, LEVEL_WARN: 1, LEVEL_INFO: 2}
    out.sort(key=lambda t: order[t.level])
    return out


def counter_snapshot(gs: GameState) -> dict:
    """给服务响应用的记牌器快照（PLAN §3.5 的 ``card_counter``）。"""
    ranks = gs.unseen_ranks
    return {
        "unseen_by_rank": ranks,
        "unseen_total": sum(ranks),
        "possible_bombs": possible_bombs(gs),
        "rocket_possible": rocket_possible(gs),
    }
