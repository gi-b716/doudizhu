"""测试用对局生成器：随机打一局，产出**逐步的平台 state**（形状照 PLAN §0.1）。

用途有二：

1. 给 state/record 的测试提供真实形状的输入（叫分、流局、出牌、终局都覆盖）；
2. 让"重放不变量"这类断言可以在**随机对局**上跑，而不是只跑几个手写的例子。

平台端的 pattern 对象形状不可知，所以这里刻意发一种**我们认不出**的形状
（数字 type 码），逼着适配层走"从牌面反推"的那条路——真实抓包里大概率就是这样。
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Iterator, Sequence

from ddz import cards as C
from ddz import moves as M
from ddz import patterns as P

MAX_DEALS = 6  # 防止一直流局打不完

#: 模拟平台 pattern 对象的形状：数字牌型码 + 点数 + 长度（我们的解析器认不出数值 type）。
_JS_TYPE_CODES = {
    P.SINGLE: 1, P.PAIR: 2, P.TRIPLE: 3, P.TRIPLE_SINGLE: 4, P.TRIPLE_PAIR: 5,
    P.STRAIGHT: 6, P.PAIR_STRAIGHT: 7, P.PLANE: 8, P.PLANE_SINGLE: 9, P.PLANE_PAIR: 10,
    P.FOUR_TWO: 11, P.FOUR_TWO_PAIR: 12, P.BOMB: 13, P.ROCKET: 14,
}


def js_pattern(pattern: P.Pattern) -> dict:
    """平台风格的 pattern 对象（故意用我们的解析器认不出的字段值）。"""
    return {"type": _JS_TYPE_CODES[pattern.type], "rank": pattern.main_rank, "len": pattern.length}


def commitment_of(deck: Sequence[int], salt: str) -> str:
    return hashlib.sha256((",".join(str(c) for c in deck) + "|" + salt).encode("utf-8")).hexdigest()


class _Game:
    def __init__(
        self,
        rng: random.Random,
        *,
        seat: int | None,
        admin: bool,
        version_base: int,
        pass_bias: float = 0.0,
    ) -> None:
        self.rng = rng
        self.seat = seat
        self.admin = admin
        self.pass_bias = pass_bias
        self.version = version_base
        self.states: list[dict] = []
        self.log: list[dict] = []
        self.deals: list[dict] = []
        self.t = 0
        self.started = 1_700_000_000_000
        self.redeals = 0
        self.deal_index = 0
        self.bids: list[int | None] = [None, None, None]
        self.auto = [False, False, False]
        self.phase = "bidding"
        self.turn: int | None = None
        self.high_bid = 0
        self.landlord: int | None = None
        self.must_bid = False
        self.bottom: tuple[int, ...] | None = None
        self.secret_bottom: tuple[int, ...] | None = None
        self.hands: list[tuple[int, ...]] = [(), (), ()]
        self.table: list[Any] = [None, None, None]
        self.last: dict | None = None
        self.multiplier = 1
        self.bombs = 0
        self.result: dict | None = None
        self._played: list[list[int]] = [[], [], []]

    # -- log 与 state --------------------------------------------------------

    def push(self, kind: str, **extra: Any) -> dict:
        self.t += self.rng.randrange(300, 4000)
        entry = {"t": self.t, "kind": kind}
        entry.update(extra)
        self.log.append(entry)
        return entry

    def snapshot(self) -> dict:
        self.version += 1
        players = [
            {
                "username": f"p{i}",
                "count": len(self.hands[i]),
                "bid": self.bids[i] if self.phase == "bidding" else None,
                "role": None if self.landlord is None else ("landlord" if self.landlord == i else "farmer"),
                "auto": self.auto[i],
            }
            for i in range(3)
        ]
        state: dict[str, Any] = {
            "version": self.version,
            "seat": self.seat,
            "phase": self.phase,
            "turn": self.turn,
            "remaining": 15000 if self.phase != "finished" else None,
            "reserve_in_remaining": 3000,
            "reserve": [15000, 15000, 15000],
            "bid": self.high_bid,
            "landlord": self.landlord,
            "must_bid": self.must_bid,
            "bottom": list(self.bottom) if self.bottom else None,
            "secret_bottom": list(self.secret_bottom) if self.secret_bottom else None,
            "hand": list(self.hands[self.seat]) if self.seat is not None else None,
            "hands": [list(h) for h in self.hands] if (self.admin or self.phase == "finished") else None,
            "table": [
                "pass" if e == "pass" else (None if e is None else {"cards": e["cards"], "pattern": e["pattern"]})
                for e in self.table
            ],
            "last": None if self.last is None else {"seat": self.last["seat"], "pattern": self.last["pattern"]},
            "log": [dict(e) for e in self.log],
            "multiplier": self.multiplier,
            "bombs": self.bombs,
            "players": players,
            "started": self.started,
            "redeals": self.redeals,
            "leading": self.phase == "playing" and (self.last is None or self.last["seat"] == self.turn),
            "fairness": {
                "commitments": [d["commitment"] for d in self.deals],
                "deals": [dict(d) for d in self.deals] if self.phase == "finished" else None,
            },
            "result": self.result,
            "match": None,
        }
        self.states.append(state)
        return state


def play_game(
    rng: random.Random,
    *,
    seat: int | None = 0,
    admin: bool = False,
    version_base: int = 0,
    pass_bias: float = 0.0,
) -> list[dict]:
    """随机打一局，返回**逐步**的平台 state；最后一份的 ``phase`` 是 ``finished``。

    ``seat=None`` 即旁观；``admin=True`` 时始终带上三家手牌；``pass_bias`` 提高"不叫"
    概率，用来逼出流局（连续流局 3 次后最后一位必须叫分）这条路径。
    """
    game = _Game(rng, seat=seat, admin=admin, version_base=version_base, pass_bias=pass_bias)
    game.snapshot()  # 开局（还没人叫分）

    while game.phase == "bidding" and game.landlord is None:
        deck = C.full_deck()
        rng.shuffle(deck)
        salt = "%032x" % rng.getrandbits(128)
        first_bidder = rng.randrange(3)
        game.deal_index = len(game.deals)
        game.deals.append(
            {
                "index": game.deal_index,
                "deck": list(deck),
                "salt": salt,
                "commitment": commitment_of(deck, salt),
                "first_bidder": first_bidder,
            }
        )
        game.hands = [tuple(C.sort_cards(deck[17 * i : 17 * i + 17])) for i in range(3)]
        game.secret_bottom = tuple(C.sort_cards(deck[51:]))
        game.bids = [None, None, None]
        game.high_bid = 0
        game.last = None
        game.table = [None, None, None]
        game.turn = first_bidder
        game.must_bid = False
        game._bid_order = [(first_bidder + i) % 3 for i in range(3)]

        if game.deal_index >= MAX_DEALS:  # 兜底：不再流局，直接定地主
            game.landlord = first_bidder
            game.high_bid = max(game.high_bid, 1)
            break

        for position, bidder in enumerate(game._bid_order):
            game.turn = bidder
            forced = game.redeals >= 3 and position == 2
            game.must_bid = forced
            options = [v for v in (1, 2, 3) if v > game.high_bid]
            if game.pass_bias and not forced and rng.random() < game.pass_bias:
                value = 0
            else:
                choices = options if forced else [0, *options]
                value = rng.choice(choices) if choices else 0
            game.bids[bidder] = value
            if value:
                game.high_bid = value
            game.push("bid", seat=bidder, value=value, timeout=False)
            game.snapshot()
            if value == 3:
                game.landlord = bidder
                break
        game.must_bid = False

        if game.landlord is None:
            if game.high_bid:
                game.landlord = game.bids.index(game.high_bid)
            else:
                # 流局：重新洗牌发牌（上一副的牌桌与 last 一并作废）
                game.push("redeal", seat=game._bid_order[-1])
                game.redeals += 1
                game.last = None
                game.table = [None, None, None]
                game.secret_bottom = None
                game.snapshot()
                continue

    if game.landlord is None:  # MAX_DEALS 兜底
        game.landlord = 0
        game.high_bid = game.high_bid or 1

    seat_landlord = game.landlord
    game.bottom = game.secret_bottom
    game.hands[seat_landlord] = tuple(C.sort_cards(game.hands[seat_landlord] + tuple(game.bottom)))
    game.push("landlord", seat=seat_landlord, value=game.high_bid, cards=list(game.bottom))
    game.phase = "playing"
    game.turn = seat_landlord
    game.redeals = 0
    game.snapshot()

    # ---- 出牌
    plays_per_seat = [0, 0, 0]
    to_beat: P.Pattern | None = None
    last_seat: int | None = None
    while True:
        seat_now = game.turn
        assert seat_now is not None
        hand = game.hands[seat_now]
        moves = M.legal_moves(hand, to_beat, canonical=False)
        if not moves:
            game.push("pass", seat=seat_now, timeout=False)
            game.table = list(game.table)
            game.table[seat_now] = "pass"
        else:
            move = rng.choice(moves)
            played = set(move.cards)
            game.hands[seat_now] = tuple(c for c in hand if c not in played)
            game._played[seat_now].extend(move.cards)
            plays_per_seat[seat_now] += 1
            if to_beat is None:
                game.table = [None, None, None]
            game.table = list(game.table)
            game.table[seat_now] = {"cards": list(move.cards), "pattern": js_pattern(move.pattern)}
            game.last = {"seat": seat_now, "pattern": js_pattern(move.pattern)}
            if P.is_bomb(move.pattern):
                game.bombs += 1
                game.multiplier += 1
            game.push(
                "play", seat=seat_now, cards=list(move.cards), pattern=js_pattern(move.pattern), timeout=False
            )
            to_beat = move.pattern
            last_seat = seat_now
        finisher = seat_now if not game.hands[seat_now] else None
        game.turn = (seat_now + 1) % 3
        if finisher is not None:
            break
        if game.turn == last_seat:  # 一轮结束，最后出牌的人重新领出
            to_beat = None
        game.snapshot()

    landlord_won = not game.hands[seat_landlord]
    farmers = [s for s in range(3) if s != seat_landlord]
    spring = landlord_won and all(plays_per_seat[s] == 0 for s in farmers)
    anti_spring = (not landlord_won) and plays_per_seat[seat_landlord] == 1
    game.multiplier = 1 + game.bombs + (1 if (spring or anti_spring) else 0)
    base = game.high_bid or 1
    unit = base * game.multiplier
    deltas = [0, 0, 0]
    for s in range(3):
        if s == seat_landlord:
            deltas[s] = 2 * unit if landlord_won else -2 * unit
        else:
            deltas[s] = -unit if landlord_won else unit
    game.push("finish", seat=finisher)
    game.phase = "finished"
    game.turn = None
    game.result = {
        "landlord_won": landlord_won,
        "spring": spring,
        "anti_spring": anti_spring,
        "base": base,
        "multiplier": game.multiplier,
        "deltas": deltas,
        "finished": True,
    }
    game.snapshot()
    return game.states


def finished_state(rng: random.Random, **kwargs: Any) -> dict:
    """只要终局那一份 state。"""
    return play_game(rng, **kwargs)[-1]


def states(rng: random.Random, **kwargs: Any) -> Iterator[dict]:
    yield from play_game(rng, **kwargs)
