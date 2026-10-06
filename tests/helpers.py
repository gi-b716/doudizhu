"""测试辅助：把 ``"333444555666"`` 这类牌面简写展开成平台牌 id。

简写支持紧凑与分隔两种写法：``"34567"`` / ``"3 4 5 6 7"`` / ``"小王 大王"`` / ``"10JQKA"``。
同一张点数按 ♠♥♣♦ 顺序分配牌 id，所以简写 → 牌 id 是确定的。
"""

from __future__ import annotations

import random
from typing import Iterable

from ddz import cards as C
from ddz import patterns as P

_SMALL = "小王"
_BIG = "大王"

_ALIAS: dict[str, int] = {
    "3": 0, "4": 1, "5": 2, "6": 3, "7": 4, "8": 5, "9": 6,
    "10": 7, "T": 7, "t": 7,
    "J": 8, "j": 8, "Q": 9, "q": 9, "K": 10, "k": 10, "A": 11, "a": 11, "2": 12,
    "w": 13, "W": 13, "小": 13, _SMALL: 13,
    "d": 14, "D": 14, "大": 14, _BIG: 14,
}


def tokens(spec: str) -> list[str]:
    """逐字符扫描：空白/逗号只是分隔，``10`` 与``小王`` 两字符优先。"""
    text = spec.replace(",", " ").replace("，", " ")
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        two = text[i : i + 2]
        if two in _ALIAS:
            out.append(two)
            i += 2
        elif text[i] in _ALIAS:
            out.append(text[i])
            i += 1
        else:
            raise ValueError(f"无法解析的牌面：{spec!r}（第 {i} 个字符）")
    return out


def ranks_of_spec(spec: str) -> list[int]:
    return [_ALIAS[token] for token in tokens(spec)]


def cards_of(spec: str) -> list[int]:
    """牌面简写 → 升序牌 id 列表。"""
    used = [0] * C.N_RANKS
    out: list[int] = []
    for rank in ranks_of_spec(spec):
        if rank >= C.JOKER_RANK:
            out.append(C.JOKER_SMALL + (rank - C.JOKER_RANK))
        else:
            if used[rank] >= 4:
                raise ValueError(f"牌面里点数 {C.RANK_NAMES[rank]} 超过 4 张")
            out.append(rank * 4 + used[rank])
            used[rank] += 1
    return out


def classify_keys(spec_or_cards: str | Iterable[int]) -> list[str]:
    cards = cards_of(spec_or_cards) if isinstance(spec_or_cards, str) else list(spec_or_cards)
    return [p.key() for p in P.classify(cards)]


def reading(spec: str) -> P.Pattern:
    """简写 → 它优先级最高的读法（断言存在）。"""
    pattern = P.classify_one(cards_of(spec))
    assert pattern is not None, f"{spec} 没有任何合法读法"
    return pattern


def rank_multiset(cards: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(C.ranks_of(cards)))


def move_rank_keys(moves: Iterable) -> set[tuple[tuple[int, ...], str]]:
    """把着法降到「点数多重集 + 牌型」层面——具体花色不影响合法性。"""
    return {(rank_multiset(m.cards), m.pattern.key()) for m in moves}


def random_hand(rng: random.Random, size: int, *, max_count: int = 3) -> list[int]:
    """随机手牌；``max_count`` 限制同点数张数（默认排除炸弹，便于暴力对照）。"""
    deck = C.full_deck()
    for _ in range(200):
        hand = rng.sample(deck, size)
        if max(C.rank_counts(hand)) <= max_count:
            return hand
    raise AssertionError("生成不出满足条件的手牌")


# --------------------------------------------------------------------- 造局面


def game_state(
    spec_or_cards,
    *,
    to_beat=None,
    seat: int = 0,
    landlord: int | None = 1,
    last_seat: int | None = None,
    counts=None,
    turn: int | None = None,
    phase: str = "playing",
    bottom=None,
):
    """手搓一个出牌阶段的状态，用来逐条测硬规则。

    默认自己坐 0 号、地主是 1 号（于是 2 号是队友）；``to_beat`` 给定时，默认是**上家**
    （2 号）刚打的，也就是"队友出的牌"——要测对手，就传 ``last_seat=1``。
    """
    from ddz.state import GameState, PlayerInfo, TableEntry

    hand = cards_of(spec_or_cards) if isinstance(spec_or_cards, str) else list(spec_or_cards)
    hand = C.sort_cards(hand)
    if counts is None:
        counts = [len(hand), 10, 10]
    last = None
    if to_beat is not None:
        who = last_seat if last_seat is not None else (seat + 2) % 3
        last = TableEntry(who, "play", (), to_beat)
    leading = to_beat is None
    players = [
        PlayerInfo(
            seat=i,
            username=f"p{i}",
            count=counts[i],
            role=None if landlord is None else ("landlord" if landlord == i else "farmer"),
        )
        for i in range(3)
    ]
    return GameState(
        seat=seat,
        phase=phase,
        turn=turn if turn is not None else seat,
        leading=leading,
        hand=tuple(hand),
        landlord=landlord,
        players=players,
        last=last,
        bottom=tuple(bottom) if bottom else None,
    )


class ScriptedAgent:
    """测试用 agent：按剧本出牌/叫分，并记录它看到过的每一个状态。"""

    def __init__(self, name: str = "scripted", *, bids=None, moves=None) -> None:
        self.name = name
        self.bids = list(bids or [])
        self.moves = list(moves or [])
        self.seen: list = []
        self.bid_seen: list = []

    def act(self, state):
        from ddz.engine.base import Candidate

        self.seen.append(state)
        if not self.moves:
            return Candidate(None, 0.0, ["剧本用完了"])
        step = self.moves.pop(0)
        if callable(step):
            step = step(state)
        return step if hasattr(step, "move") else Candidate(step, 0.0, ["剧本"])

    def bid(self, state):
        from ddz.engine.base import Bid

        self.bid_seen.append(state)
        if not self.bids:
            return Bid(0, 0.0, ["剧本用完了"])
        step = self.bids.pop(0)
        return Bid(step if isinstance(step, int) else step(state), 0.0, ["剧本"])


def platform_endgame(hands, *, turn=0, landlord=1, to_beat=None, last_seat=None,
                     play_cards=(), version=7):
    """手搓一个**平台格式**的残局 state（照抄 :func:`from_platform_state` 要吃的字段）。

    和 :func:`game_state` 的区别：这个带三家 ``hands``（真实平台就是这么下发的），
    所以服务层能拿它走"完美信息残局求解"那条路。
    """
    board = [sorted(cards_of(h) if isinstance(h, str) else list(h)) for h in hands]
    log = []
    last = None
    if to_beat is not None:
        who = last_seat if last_seat is not None else (turn + 2) % 3
        if (who + 1) % 3 != turn:
            raise ValueError(f"局面自相矛盾：{who} 号刚出牌，下一个却轮到 {turn} 号")
        played = sorted(play_cards)
        if not played:
            raise ValueError("平台格式的 last 必须带 cards，否则解析不出来")
        board[who] = [c for c in board[who] if c not in set(played)]
        log = [{"kind": "play", "seat": who, "cards": played}]
        last = {"seat": who, "pattern": to_beat.platform_key(), "cards": played}
    return {
        "version": version,
        "seat": turn,
        "phase": "playing",
        "turn": turn,
        "leading": to_beat is None,
        "hand": list(board[turn]),
        "hands": [list(h) for h in board],
        "landlord": landlord,
        "players": [
            {
                "username": f"p{i}",
                "count": len(board[i]),
                "role": "landlord" if i == landlord else "farmer",
            }
            for i in range(3)
        ],
        "log": log,
        "last": last,
    }
