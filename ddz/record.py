"""对局库：终局 state → 完美信息记录 → 逐前缀重放。

终局 state 就是一份完美信息样本（PLAN §0.1）：``fairness.deals[]`` 给出每副牌 54 张的
完整牌序与盐，``log`` 给出每一步动作。:func:`replay` 逐行移植客户端的 ``snapshot(k)``，
按 log 前缀重建任意时刻的牌桌；:func:`iter_decisions` 在它之上产出
``(局面, 座位, 实际着法)`` ——评测与将来训练样本的唯一生成器。

存储是 JSONL，一行一局（schema 见 :data:`SCHEMA`）；牌型只存 :meth:`Pattern.key` 字符串。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import cards as C
from .patterns import Pattern, classify, is_bomb
from .state import (
    PHASE_BIDDING, PHASE_FINISHED, PHASE_PLAYING, RULES_TAG, SEATS,
    GameState, PlayerInfo, StateParseError, TableEntry, parse_platform_pattern,
    unwrap_state,
)

__all__ = [
    "SCHEMA", "Deal", "Event", "RecordResult", "GameRecord", "Decision",
    "from_finished_state", "replay", "iter_frames", "iter_decisions",
    "dedupe_key", "dedupe_key_of",
]

SCHEMA = "ddz.record.v1"

#: 记录里出现的动作类型（与平台 log 的 kind 同名）。
ACTION_KINDS = ("play", "pass")


def dedupe_key(deals: Sequence["Deal"], players: Sequence[str]) -> str:
    """去重键 = ``sha256(第一副牌的牌序 | 盐 | 玩家名)``（PLAN §3.2）。

    用第一副牌就够：流局重发会换牌，所以牌序已经唯一标识了一次发牌。
    """
    return _key_of(deals[0].deck, deals[0].salt, players)


def dedupe_key_of(data: Mapping[str, Any]) -> str:
    """同一把键，但直接吃 JSONL 里的一行（归档去重时不必构造 :class:`GameRecord`）。"""
    deals = data.get("deals") or ()
    if not deals:
        raise StateParseError("记录里没有 deals，无法算去重键")
    first = deals[0]
    return _key_of(
        [int(c) for c in (first.get("deck") or ())],
        str(first.get("salt", "")),
        [str(p) for p in (data.get("players") or ())],
    )


def _key_of(deck: Iterable[int], salt: str, players: Sequence[str]) -> str:
    body = ",".join(str(c) for c in deck) + "|" + salt + "|" + ",".join(players)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Deal:
    """一副牌的完整信息（终局才公开）。"""

    index: int
    deck: tuple[int, ...]
    salt: str = ""
    commitment: str = ""
    first_bidder: int = 0

    def hands(self) -> list[tuple[int, ...]]:
        """三家开局手牌（各 17 张，已排序）——与客户端 ``dealHands()`` 同口径。"""
        return [tuple(C.sort_cards(self.deck[17 * i : 17 * i + 17])) for i in SEATS]

    def bottom(self) -> tuple[int, ...]:
        return tuple(C.sort_cards(self.deck[51:]))

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "deck": list(self.deck),
            "salt": self.salt,
            "commitment": self.commitment,
            "first_bidder": self.first_bidder,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Deal":
        deck = tuple(int(c) for c in data["deck"])
        if sorted(deck) != list(range(C.DECK_SIZE)):
            raise StateParseError(f"第 {data.get('index')} 副牌的牌序不是 0..53 的一个排列")
        return cls(
            index=int(data.get("index", 0)),
            deck=deck,
            salt=str(data.get("salt", "")),
            commitment=str(data.get("commitment", "")),
            first_bidder=int(data.get("first_bidder", 0)),
        )


@dataclass(slots=True)
class Event:
    """一条 log 记录（与平台字段同名，``pattern`` 存我们自己的 key 字符串）。"""

    t: int
    kind: str
    seat: int | None = None
    value: int | None = None
    cards: tuple[int, ...] = ()
    pattern: str | None = None
    auto: bool = False
    timeout: bool = False

    def is_action(self) -> bool:
        return self.kind in ACTION_KINDS

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"t": self.t, "kind": self.kind}
        if self.seat is not None:
            out["seat"] = self.seat
        if self.value is not None:
            out["value"] = self.value
        if self.cards:
            out["cards"] = list(self.cards)
        if self.pattern is not None:
            out["pattern"] = self.pattern
        if self.auto:
            out["auto"] = True
        if self.timeout:
            out["timeout"] = True
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        return cls(
            t=int(data.get("t") or 0),
            kind=str(data.get("kind")),
            seat=data.get("seat"),
            value=data.get("value"),
            cards=tuple(int(c) for c in (data.get("cards") or ())),
            pattern=data.get("pattern"),
            auto=bool(data.get("auto")),
            timeout=bool(data.get("timeout")),
        )


@dataclass(slots=True)
class RecordResult:
    landlord_won: bool
    spring: bool
    anti_spring: bool
    base: int
    multiplier: int
    deltas: tuple[int, ...]

    def to_dict(self) -> dict:
        return {
            "landlord_won": self.landlord_won,
            "spring": self.spring,
            "anti_spring": self.anti_spring,
            "base": self.base,
            "multiplier": self.multiplier,
            "deltas": list(self.deltas),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordResult":
        return cls(
            landlord_won=bool(data.get("landlord_won")),
            spring=bool(data.get("spring")),
            anti_spring=bool(data.get("anti_spring")),
            base=int(data.get("base") or 0),
            multiplier=int(data.get("multiplier") or 1),
            deltas=tuple(int(d) for d in (data.get("deltas") or ())),
        )


@dataclass(slots=True)
class GameRecord:
    """一局完美信息对局。"""

    game_id: str
    deals: list[Deal]
    events: list[Event] = field(default_factory=list)
    players: list[str] = field(default_factory=lambda: ["", "", ""])
    seat_of_interest: int | None = None
    captured_at: float = 0.0
    source: str = "platform"
    rules: str = RULES_TAG
    match: dict | None = None
    result: RecordResult | None = None

    # ----------------------------------------------------------------- 基本

    def dedupe_key(self) -> str:
        return dedupe_key(self.deals, self.players)

    def finished(self) -> bool:
        return self.result is not None

    def events_of_deal(self, index: int) -> list[Event]:
        """某一副牌的事件（``redeal`` 之后的事件才算下一副；被流局打断的也算这副的）。"""
        deal = 0
        out: list[Event] = []
        for entry in self.events:
            if entry.kind == "redeal":
                if deal == index:
                    return out
                deal += 1
                out = []
            elif deal == index:
                out.append(entry)
        return out if deal == index else []

    # ----------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "game_id": self.game_id,
            "captured_at": self.captured_at,
            "source": self.source,
            "rules": self.rules,
            "players": list(self.players),
            "seat_of_interest": self.seat_of_interest,
            "match": self.match,
            "result": self.result.to_dict() if self.result else None,
            "deals": [d.to_dict() for d in self.deals],
            "events": [e.to_dict() for e in self.events],
        }
        return payload

    def to_line(self) -> str:
        """一行 JSON（``ensure_ascii=False``：中文玩家名原样存）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GameRecord":
        schema = data.get("schema")
        if schema != SCHEMA:
            raise StateParseError(f"不认识的记录 schema：{schema!r}")
        deals = [Deal.from_dict(d) for d in data.get("deals") or ()]
        if not deals:
            raise StateParseError("记录里没有任何一副牌")
        result = data.get("result")
        seat = data.get("seat_of_interest")
        return cls(
            game_id=str(data.get("game_id") or ""),
            deals=deals,
            events=[Event.from_dict(e) for e in (data.get("events") or ())],
            players=[str(p) for p in (data.get("players") or ("", "", ""))],
            seat_of_interest=None if seat is None else int(seat),
            captured_at=float(data.get("captured_at") or 0.0),
            source=str(data.get("source") or "platform"),
            rules=str(data.get("rules") or RULES_TAG),
            match=data.get("match"),
            result=RecordResult.from_dict(result) if result else None,
        )

    @classmethod
    def from_line(cls, line: str) -> "GameRecord":
        return cls.from_dict(json.loads(line))

    # ----------------------------------------------------------------- 校验

    def validate(self) -> list[str]:
        """结构性检查，返回问题列表（空 = 没问题）。"""

        def err(message: str) -> None:
            problems.append(message)

        problems: list[str] = []
        if not self.deals:
            err("没有 deals")
        for i, deal in enumerate(self.deals):
            if sorted(deal.deck) != list(range(C.DECK_SIZE)):
                err(f"第 {i} 副牌的牌序不是 0..53 的排列")
            if deal.first_bidder not in SEATS:
                err(f"第 {i} 副牌的 first_bidder 非法：{deal.first_bidder}")
        for i, entry in enumerate(self.events):
            if entry.kind not in ("redeal", "bid", "landlord", "play", "pass", "auto_on", "auto_off", "finish"):
                err(f"第 {i} 条事件 kind 未知：{entry.kind!r}")
            if entry.kind in ("bid", "landlord", "play", "pass") and entry.seat not in SEATS:
                err(f"第 {i} 条事件 seat 非法：{entry.seat!r}")
            for card in entry.cards:
                if not 0 <= card < C.DECK_SIZE:
                    err(f"第 {i} 条事件出现越界牌 id：{card}")
            if entry.pattern is not None:
                try:
                    Pattern.parse_key(entry.pattern)
                except (ValueError, AttributeError):
                    err(f"第 {i} 条事件的 pattern 无法解析：{entry.pattern!r}")
        return problems


@dataclass(slots=True)
class Decision:
    """一个出牌决策样本：该座位在**当时**能看到的局面 + 他实际怎么打。"""

    state: GameState
    seat: int
    move: "Move | None"          # 实际着法；pass 为 None
    entry: Event

    @property
    def is_pass(self) -> bool:
        return self.move is None


# --------------------------------------------------------------------- 建模


def from_finished_state(
    state: Mapping[str, Any],
    *,
    game_id: str | None = None,
    captured_at: float | None = None,
    source: str = "platform",
    rules: str = RULES_TAG,
    seat_of_interest: int | None = None,
) -> GameRecord:
    """终局平台 state → :class:`GameRecord`（``/ingest`` 与离线归档的入口）。

    只接受 ``phase == 'finished'`` 且带 ``fairness.deals`` 的 state——那是唯一含完整牌序的
    形态。牌型一律用我们自己的规则引擎从牌面反推（平台 pattern 的形状不可知）。
    """
    state = unwrap_state(state)  # 允许直接喂 {"state": {...}} 外壳
    if not isinstance(state, Mapping):
        raise StateParseError("state 不是对象")
    if state.get("phase") != PHASE_FINISHED:
        raise StateParseError(f"只归档终局 state，收到 phase={state.get('phase')!r}")
    fairness = state.get("fairness") or {}
    raw_deals = fairness.get("deals") or ()
    if not raw_deals:
        raise StateParseError("终局 state 缺少 fairness.deals（没有完整牌序就无法归档）")

    deals = [
        Deal.from_dict(
            {
                "index": i,
                "deck": d.get("deck"),
                "salt": d.get("salt", ""),
                "commitment": d.get("commitment", ""),
                "first_bidder": d.get("first_bidder", 0),
            }
        )
        for i, d in enumerate(raw_deals)
    ]

    players = [_player_name(p) for p in (state.get("players") or ())]
    while len(players) < 3:
        players.append("")
    players = players[:3]

    events: list[Event] = []
    for entry in state.get("log") or ():
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        cards = tuple(C.sort_cards(int(c) for c in (entry.get("cards") or ())))
        pattern_key: str | None = None
        if kind == "play" and cards:
            readings = classify(cards)
            if not readings:
                raise StateParseError(f"log 里有一手牌没有合法读法：{list(cards)}")
            parsed = _platform_pattern_hint(entry.get("pattern"))
            pattern_key = parsed if parsed is not None else readings[0].key()
        events.append(
            Event(
                t=int(entry.get("t") or 0),
                kind=str(kind),
                seat=entry.get("seat"),
                value=entry.get("value"),
                cards=cards,
                pattern=pattern_key,
                auto=bool(entry.get("auto")),
                timeout=bool(entry.get("timeout")),
            )
        )

    result_raw = state.get("result")
    seat = state.get("seat") if seat_of_interest is None else seat_of_interest
    record = GameRecord(
        game_id=game_id or "",
        deals=deals,
        events=events,
        players=players,
        seat_of_interest=None if seat is None else int(seat),
        captured_at=time.time() if captured_at is None else float(captured_at),
        source=source,
        rules=rules,
        match=state.get("match"),
        result=RecordResult.from_dict(result_raw) if result_raw else None,
    )
    if not record.game_id:
        record.game_id = f"local-{record.dedupe_key()[:16]}"
    return record


def _pattern_of(entry: Event) -> Pattern:
    """事件里的牌型：优先用记录下来的 key，缺失时从牌面反推。"""
    if entry.pattern:
        return Pattern.parse_key(entry.pattern)
    readings = classify(entry.cards)
    if not readings:
        raise StateParseError(f"这手牌没有合法读法：{list(entry.cards)}")
    return readings[0]


def _player_name(entry: Any) -> str:
    if isinstance(entry, Mapping):
        return str(entry.get("username", ""))
    return str(entry or "")


def _platform_pattern_hint(raw: Any) -> str | None:
    """平台的 pattern 只当提示用；认得出且是合法 key 才采纳（否则返回 None 走牌面反推）。"""
    parsed = parse_platform_pattern(raw)
    return parsed.key() if parsed is not None else None


# --------------------------------------------------------------------- 重放


class _Replayer:
    """客户端 ``snapshot()`` 的逐行移植：状态随事件推进，可随时取一份 '当前牌桌'。"""

    def __init__(self, record: GameRecord) -> None:
        self.record = record
        self.deal_index = 0
        self.hands = list(record.deals[0].hands())
        self.bids: list[int | None] = [None, None, None]
        self.auto = [False, False, False]
        self.phase = PHASE_BIDDING
        self.turn: int | None = record.deals[0].first_bidder
        self.high_bid = 0
        self.landlord: int | None = None
        self.bottom: tuple[int, ...] | None = None
        self.table: list[TableEntry | None] = [None, None, None]
        self.last: TableEntry | None = None
        self.multiplier = 1
        self.bombs = 0
        self.redeals = 0
        self.result: RecordResult | None = None
        self.step = 0
        self._played: list[list[int]] = [[], [], []]

    # -- 推进 ---------------------------------------------------------------

    def advance(self, entry: Event) -> None:
        self.step += 1
        kind = entry.kind
        if kind == "bid":
            if entry.seat in SEATS:
                self.bids[entry.seat] = entry.value
                if (entry.value or 0) > self.high_bid:
                    self.high_bid = int(entry.value or 0)
                self.turn = (entry.seat + 1) % 3
        elif kind == "redeal":
            self.deal_index = min(self.deal_index + 1, len(self.record.deals) - 1)
            self.hands = list(self.record.deals[self.deal_index].hands())
            self.bids = [None, None, None]
            self.high_bid = 0
            self.redeals += 1
            self.turn = self.record.deals[self.deal_index].first_bidder
            # 上一副牌打出的牌重新洗回牌堆——「已出牌」必须按副重置
            self._played = [[], [], []]
            self.table = [None, None, None]
            self.last = None
        elif kind == "landlord":
            seat = entry.seat
            if seat in SEATS:
                self.landlord = seat
                self.high_bid = int(entry.value or self.high_bid)
                self.bottom = tuple(C.sort_cards(entry.cards))
                self.hands[seat] = tuple(C.sort_cards(self.hands[seat] + self.bottom))
                self.phase = PHASE_PLAYING
                self.turn = seat
                self.redeals = 0
        elif kind == "play":
            seat = entry.seat
            if seat not in SEATS:
                return
            played = set(entry.cards)
            self.hands[seat] = tuple(c for c in self.hands[seat] if c not in played)
            self._played[seat].extend(entry.cards)
            if self.last is None or self.last.seat == seat:
                self.table = [None, None, None]     # 新一轮，旧牌收走
            pattern = _pattern_of(entry)
            self.table = list(self.table)
            self.table[seat] = TableEntry(seat, "play", entry.cards, pattern, "record")
            self.last = self.table[seat]
            if is_bomb(pattern):
                self.bombs += 1
                self.multiplier += 1
            self.turn = (seat + 1) % 3
        elif kind == "pass":
            seat = entry.seat
            if seat not in SEATS:
                return
            self.table = list(self.table)
            self.table[seat] = TableEntry(seat, "pass")
            self.turn = (seat + 1) % 3
        elif kind in ("auto_on", "auto_off"):
            if entry.seat in SEATS:
                self.auto[entry.seat] = kind == "auto_on"
        elif kind == "finish":
            self.phase = PHASE_FINISHED
            self.turn = None
            self.result = self.record.result
            if self.result is not None:
                self.multiplier = self.result.multiplier

    def state(self, events: Sequence[Event]) -> GameState:
        seat = self.record.seat_of_interest
        playing = self.phase == PHASE_PLAYING
        leading = playing and (self.last is None or self.last.seat == self.turn)
        # log 不记录 must_bid；按 rules.md「连续流局 3 次后，本轮最后一位必须叫分」重建
        first_bidder = self.record.deals[self.deal_index].first_bidder
        must_bid = (
            self.phase == PHASE_BIDDING
            and self.redeals >= 3
            and self.turn == (first_bidder + 2) % 3
        )
        players = [
            PlayerInfo(
                seat=i,
                username=self.record.players[i] if i < len(self.record.players) else "",
                count=len(self.hands[i]),
                bid=self.bids[i] if self.phase == PHASE_BIDDING else None,
                role=(
                    None if self.landlord is None
                    else "landlord" if self.landlord == i else "farmer"
                ),
                auto=self.auto[i],
            )
            for i in SEATS
        ]
        all_hands = [tuple(h) for h in self.hands]
        frame = GameState(
            version=self.step,
            seat=seat,
            phase=self.phase,
            turn=self.turn,
            leading=leading,
            hand=all_hands[seat] if seat is not None else None,
            all_hands=all_hands,
            bottom=self.bottom,
            landlord=self.landlord,
            high_bid=self.high_bid,
            must_bid=must_bid,
            multiplier=self.multiplier,
            bombs=self.bombs,
            players=players,
            table=list(self.table),
            last=self.last,
            # GameState.events 的类型统一成 list[dict]（和 from_platform_state 一致）：
            # 重放出来的帧以前塞的是 Event 对象，下游按 dict 取值会炸
            events=[entry.to_dict() for entry in events],
            deal_index=self.deal_index,
            played_by_seat=[tuple(sorted(c)) for c in self._played],
            redeals=self.redeals,
            result=self.result.to_dict() if self.result else None,
            match=self.record.match,
            raw={},
        )
        return frame


def iter_frames(record: GameRecord) -> Iterator[GameState]:
    """依次产出 ``k = 0..len(events)`` 的局面（``k`` 步之后）。"""
    replayer = _Replayer(record)
    prefix: list[Event] = []
    yield replayer.state(prefix)
    for entry in record.events:
        if entry.kind == "redeal":
            prefix = []
        replayer.advance(entry)
        if entry.kind != "redeal":
            prefix = [*prefix, entry]
        yield replayer.state(prefix)


def replay(record: GameRecord, k: int) -> GameState:
    """log 前 ``k`` 步之后的局面（移植客户端 ``snapshot(k)``）。

    重建出的局面没有计时信息（log 不记录局时，客户端重放同样不做），``version`` 即步号。
    """
    if k < 0 or k > len(record.events):
        raise ValueError(f"步号越界：{k}（共 {len(record.events)} 步）")
    frame: GameState | None = None
    for i, state in enumerate(iter_frames(record)):
        if i == k:
            frame = state
            break
    assert frame is not None
    return frame


def iter_decisions(
    record: GameRecord,
    *,
    include_passes: bool = True,
) -> Iterator[Decision]:
    """产出每个出牌决策的样本：``(当时的局面, 座位, 实际着法)``。

    ``state`` 已换成该座位的视角（``hand`` 是他的手牌），所以可以直接拿去问 agent
    "你会怎么打"——评测脚本与将来做行为克隆都用这一个生成器。
    """
    from .moves import Move

    replayer = _Replayer(record)
    prefix: list[Event] = []
    for entry in record.events:
        if entry.kind == "redeal":
            prefix = []
        if entry.is_action() and entry.seat in SEATS:
            if include_passes or entry.kind == "play":
                state = replayer.state(prefix).for_seat(entry.seat)
                move = (
                    Move(entry.cards, _pattern_of(entry)) if entry.kind == "play" else None
                )
                yield Decision(state=state, seat=entry.seat, move=move, entry=entry)
        replayer.advance(entry)
        if entry.kind != "redeal":
            prefix = [*prefix, entry]
