"""平台 state → 内部 :class:`GameState`。

平台 state 的字段与语义以 ``src/doudizhu.js`` 为准（PLAN §0.1），本模块只做适配，
不改写。几条边界处理（都在 PLAN §3.2 里点名过）：

- **旁观**：``seat = null``、``hand = null``；管理员还能拿到 ``hands``，此时可以替
  ``turn`` 座位给建议（:attr:`GameState.advise_seat`）。
- **叫分合法集**：``{0} ∪ {v > bid}``，``must_bid`` 时禁 0（见 :attr:`GameState.legal_bids`）。
- **unchanged**：只刷新计时，不重建状态（:func:`apply_unchanged`）。
- **流局**：``redeal`` 事件把 ``events`` 截断并让 ``deal_index`` 递增——
  与客户端 ``snapshot(k)`` 逐个前缀重建的口径一致。
- **version 跳跃**：不做任何增量假设，每份 state 都整体重建；``prev`` 只用于
  ``unchanged``。

**平台 pattern 的形状**（``src/doudizhu-rules.js`` 到手后已确认）：``{type, rank, len}``，
类型名是 ``trio1``/``plane1``/``four2`` 那套，链条的 ``rank`` 记的是**最高**点数——
:meth:`ddz.patterns.Pattern.from_platform` 能精确解析它。所以现在优先采信平台的 pattern，
认不出或与牌面矛盾时才退到 :func:`ddz.patterns.classify` 的优先读法（:attr:`TableEntry.
pattern_source` 记录来源）。真正无法解释的 state（例如出了我们的规则认不出的牌）抛
:class:`StateParseError`，由服务转成降级响应，绝不崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from . import cards as C
from .patterns import TYPE_NAMES, Pattern, classify

__all__ = [
    "RULES_TAG", "PHASE_BIDDING", "PHASE_PLAYING", "PHASE_FINISHED",
    "StateParseError", "PlayerInfo", "TableEntry", "GameState",
    "from_platform_state", "apply_unchanged", "parse_platform_pattern",
    "split_by_deal", "played_by_seat", "unwrap_state",
]

#: 归档时记下的规则口径版本（rules.md 的用语在 2026-10 核对过）。
RULES_TAG = "rules.md@2026-10"

PHASE_BIDDING = "bidding"
PHASE_PLAYING = "playing"
PHASE_FINISHED = "finished"

#: 一局里 log 的 kind 取值（PLAN §0.1）。
LOG_KINDS = ("redeal", "bid", "landlord", "play", "pass", "auto_on", "auto_off", "finish")

SEATS = (0, 1, 2)


class StateParseError(Exception):
    """平台 state 无法解释。服务捕获后返回降级响应，绝不崩溃。"""


# --------------------------------------------------------------------- 数据结构


@dataclass(slots=True)
class PlayerInfo:
    seat: int = 0
    username: str = ""
    count: int = 0
    bid: int | None = None
    role: str | None = None       # 'landlord' | 'farmer' | None
    auto: bool = False


@dataclass(slots=True)
class TableEntry:
    """牌桌上某一家最近一次动作。"""

    seat: int
    kind: str                                # 'play' | 'pass'
    cards: tuple[int, ...] = ()
    pattern: Pattern | None = None
    pattern_source: str = "derived"          # 'platform' | 'derived'

    @property
    def is_pass(self) -> bool:
        return self.kind == "pass"


@dataclass(slots=True)
class GameState:
    """一份平台状态的可计算视图。字段名与 PLAN §3.2 对齐。"""

    version: int = 0
    seat: int | None = None
    phase: str = PHASE_BIDDING
    turn: int | None = None
    leading: bool = False
    hand: tuple[int, ...] | None = None
    all_hands: list[tuple[int, ...]] | None = None
    bottom: tuple[int, ...] | None = None
    secret_bottom: tuple[int, ...] | None = None
    landlord: int | None = None
    high_bid: int = 0
    must_bid: bool = False
    multiplier: int = 1
    bombs: int = 0
    players: list[PlayerInfo] = field(default_factory=list)
    table: list[TableEntry | None] = field(default_factory=lambda: [None, None, None])
    last: TableEntry | None = None
    events: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    deal_index: int = 0
    played_by_seat: list[tuple[int, ...]] = field(default_factory=lambda: [(), (), ()])
    remaining_ms: int | None = None
    reserve_in_remaining: int = 0
    reserve: tuple[int, ...] | None = None
    redeals: int = 0
    started: int | None = None
    result: dict | None = None
    match: dict | None = None
    fairness: dict | None = None
    raw: dict = field(default_factory=dict)

    # ----------------------------------------------------------------- 基本属性

    @property
    def my_turn(self) -> bool:
        """客户端 ``myTurn()`` 的口径：轮到"我"本人且未终局。"""
        return self.seat is not None and self.phase != PHASE_FINISHED and self.turn == self.seat

    @property
    def is_landlord(self) -> bool:
        return self.seat is not None and self.landlord == self.seat

    @property
    def partner_seat(self) -> int | None:
        """队友座位（自己是农民时是另一个农民；地主或未知时为 ``None``）。"""
        if self.seat is None or self.landlord is None or self.seat == self.landlord:
            return None
        return next(s for s in SEATS if s != self.seat and s != self.landlord)

    @property
    def to_beat(self) -> Pattern | None:
        """需要压过的牌型；领出（或非出牌阶段）为 ``None``。"""
        if self.phase != PHASE_PLAYING or self.leading or self.last is None:
            return None
        return self.last.pattern

    @property
    def counts(self) -> list[int]:
        return [p.count for p in self.players]

    @property
    def legal_bids(self) -> list[int]:
        """叫分合法集：``{0} ∪ {v > bid}``；``must_bid`` 时不能"不叫"。"""
        options = [v for v in (1, 2, 3) if v > self.high_bid]
        return options if self.must_bid else [0, *options]

    @property
    def bidding_done(self) -> bool:
        return self.landlord is not None

    # ----------------------------------------------------------------- 建议座位

    @property
    def advise_seat(self) -> int | None:
        """能替哪个座位给建议。

        自己是玩家 → 自己；旁观（``seat = null``）且管理员手上有 ``hands`` →
        当前出牌者（PLAN §0.1：旁观也能为 ``turn`` 座位建议）；否则 ``None``。
        """
        if self.seat is not None:
            return self.seat
        if self.all_hands is not None and self.turn is not None:
            return self.turn
        return None

    @property
    def is_advise_turn(self) -> bool:
        return (
            self.advise_seat is not None
            and self.phase != PHASE_FINISHED
            and self.turn == self.advise_seat
        )

    def hand_for(self, seat: int) -> tuple[int, ...] | None:
        """某座位的手牌（只有自己或管理员的全量手牌能给出来）。"""
        if self.all_hands is not None and 0 <= seat < len(self.all_hands):
            return self.all_hands[seat]
        if seat == self.seat:
            return self.hand
        return None

    @property
    def advise_hand(self) -> tuple[int, ...] | None:
        seat = self.advise_seat
        return self.hand_for(seat) if seat is not None else None

    def for_seat(self, seat: int) -> "GameState":
        """换成以 ``seat`` 为视角的副本（``hand`` = 该座位手牌，``seat`` = 该座位）。

        ``iter_decisions`` 用它产出"当时该座位能看到什么"的样本。
        """
        if seat == self.seat:
            return self
        return replace(self, seat=seat, hand=self.hand_for(seat))

    # ----------------------------------------------------------------- 未见牌

    @property
    def played_cards(self) -> list[int]:
        """本副牌已经打出的所有牌（三家合并）。"""
        return sorted(c for cards in self.played_by_seat for c in cards)

    @property
    def seen_cards(self) -> set[int]:
        """被建议的那个座位确定位置的牌（手牌 + 已出的牌）。"""
        return self.seen_cards_for(self.advise_seat)

    def seen_cards_for(self, seat: int | None) -> set[int]:
        """某个座位确定位置的牌：他的手牌 + 已出的牌。

        地主确定后底牌是公开的，但它属于地主的手牌、且能与他手里的牌凑成炸弹，所以
        仍算"未见"（与客户端记牌器同口径）。
        """
        hand = self.hand_for(seat) if seat is not None else None
        return set(hand or ()) | set(self.played_cards)

    @property
    def unseen(self) -> tuple[int, ...]:
        """被建议座位的未见牌。需要别的座位的视角时用 :meth:`unseen_for`。"""
        return self.unseen_for(self.advise_seat)

    def unseen_for(self, seat: int | None) -> tuple[int, ...]:
        """``seat`` 视角下的未见牌（每点数取 id 最小的若干张，约定见模块文档）。

        只有**点数多重集**有意义——花色在斗地主里不可观测。需要计数请用
        :attr:`unseen_ranks`。

        注意别用错视角：给 DouZero 桥接时是**出牌的那个座位**，不是会话主人。
        """
        seen = self.seen_cards_for(seat)
        return tuple(card for card in C.full_deck() if card not in seen)

    @property
    def unseen_ranks(self) -> list[int]:
        """未见牌的点数直方图（长度 15，与客户端记牌器同口径）。"""
        return C.rank_counts(self.unseen)

    @property
    def threat_strings(self) -> list[str]:
        """威胁/提醒的纯文本（结构化版本见 :func:`ddz.counter.threats`）。"""
        from .counter import threats

        return [t.text for t in threats(self)]


# --------------------------------------------------------------------- 平台 state 适配


def from_platform_state(payload: Mapping[str, Any], prev: GameState | None = None) -> GameState:
    """平台 state（或 ``{"state": ...}`` 外壳）→ :class:`GameState`。

    ``unchanged`` 响应走 :func:`apply_unchanged`：只刷新计时，不重建。
    """
    state = unwrap_state(payload)
    if not isinstance(state, Mapping):
        raise StateParseError(f"state 不是对象：{type(state).__name__}")
    if state.get("unchanged"):
        return apply_unchanged(prev, state)
    if "phase" not in state:
        raise StateParseError("state 缺少 phase 字段")

    phase = state["phase"]
    if phase not in (PHASE_BIDDING, PHASE_PLAYING, PHASE_FINISHED):
        raise StateParseError(f"未知 phase：{phase!r}")

    log = [e for e in (state.get("log") or []) if isinstance(e, Mapping)]
    events, deal_index = split_by_deal(log)

    players_raw = state.get("players") or ()
    players: list[PlayerInfo] = []
    for i in SEATS:
        p = players_raw[i] if i < len(players_raw) and isinstance(players_raw[i], Mapping) else {}
        players.append(
            PlayerInfo(
                seat=i,
                username=str(p.get("username", "")),
                count=int(p.get("count") or 0),
                bid=_as_int(p.get("bid")),
                role=p.get("role"),
                auto=bool(p.get("auto")),
            )
        )

    hands_raw = state.get("hands")
    all_hands = (
        [tuple(C.sort_cards(h)) for h in hands_raw] if isinstance(hands_raw, (list, tuple)) and hands_raw else None
    )

    played = played_by_seat(events)
    table = _parse_table(state.get("table"), events)
    last = _parse_last(state.get("last"), events)

    leading = state.get("leading")
    if leading is None:
        leading = phase == PHASE_PLAYING and (last is None or last.seat == state.get("turn"))
    remaining = _as_int(state.get("remaining"))
    reserve = state.get("reserve")
    hand = state.get("hand")

    return GameState(
        version=int(state.get("version") or 0),
        seat=_as_int(state.get("seat")),
        phase=phase,
        turn=_as_int(state.get("turn")),
        leading=bool(leading) and phase == PHASE_PLAYING,
        hand=tuple(C.sort_cards(hand)) if hand else None,
        all_hands=all_hands,
        bottom=tuple(C.sort_cards(state["bottom"])) if state.get("bottom") else None,
        secret_bottom=(
            tuple(C.sort_cards(state["secret_bottom"])) if state.get("secret_bottom") else None
        ),
        landlord=_as_int(state.get("landlord")),
        high_bid=int(state.get("bid") or 0),
        must_bid=bool(state.get("must_bid")),
        multiplier=int(state.get("multiplier") or 1),
        bombs=int(state.get("bombs") or 0),
        players=players,
        table=table,
        last=last,
        events=list(events),
        log=list(log),
        deal_index=deal_index,
        played_by_seat=played,
        remaining_ms=remaining,
        reserve_in_remaining=int(state.get("reserve_in_remaining") or 0),
        reserve=tuple(int(v) for v in reserve) if isinstance(reserve, (list, tuple)) else None,
        redeals=int(state.get("redeals") or 0),
        started=_as_int(state.get("started")),
        result=state.get("result"),
        match=state.get("match"),
        fairness=state.get("fairness"),
        raw=dict(state),
    )


def apply_unchanged(prev: GameState | None, payload: Mapping[str, Any]) -> GameState:
    """``{unchanged: true, remaining}``：只刷新计时字段（客户端 ``accept()`` 同口径）。"""
    if prev is None:
        raise StateParseError("收到 unchanged，但没有可沿用的上一份状态")
    remaining = _as_int(payload.get("remaining"))
    reserve_hint = payload.get("reserve_in_remaining")
    return replace(
        prev,
        remaining_ms=prev.remaining_ms if remaining is None else remaining,
        reserve_in_remaining=(
            prev.reserve_in_remaining if reserve_hint is None else int(reserve_hint)
        ),
        raw=dict(payload),
    )


# --------------------------------------------------------------------- log 处理


def split_by_deal(log: Sequence[Mapping[str, Any]]) -> tuple[list[dict], int]:
    """把全量 log 切成「当前这副牌的事件」与「第几副牌」。

    客户端 ``snapshot(k)`` 的口径：``redeal`` 表示重新洗牌发牌，之前的事件作废、副数 +1。
    """
    deal_index = 0
    events: list[dict] = []
    for entry in log:
        if entry.get("kind") == "redeal":
            deal_index += 1
            events = []
        else:
            events.append(dict(entry))
    return events, deal_index


def played_by_seat(events: Sequence[Mapping[str, Any]]) -> list[tuple[int, ...]]:
    """本副牌里每家已经打出的牌。"""
    played: list[list[int]] = [[], [], []]
    for entry in events:
        if entry.get("kind") == "play":
            seat = _as_int(entry.get("seat"))
            if seat in SEATS:
                played[seat].extend(int(c) for c in (entry.get("cards") or ()))
    return [tuple(sorted(cards)) for cards in played]


def _parse_table(raw: Any, events: Sequence[Mapping[str, Any]]) -> list[TableEntry | None]:
    table: list[TableEntry | None] = [None, None, None]
    if not isinstance(raw, (list, tuple)):
        return table
    for seat in SEATS:
        entry = raw[seat] if seat < len(raw) else None
        if entry == "pass":
            table[seat] = TableEntry(seat=seat, kind="pass")
        elif isinstance(entry, Mapping):
            cards = _entry_cards(entry, events, seat)
            pattern, source = resolve_pattern(entry.get("pattern"), cards)
            table[seat] = TableEntry(seat, "play", cards, pattern, source)
    return table


def _parse_last(raw: Any, events: Sequence[Mapping[str, Any]]) -> TableEntry | None:
    if not isinstance(raw, Mapping):
        return None
    seat = _as_int(raw.get("seat"))
    if seat not in SEATS:
        return None
    cards = _entry_cards(raw, events, seat)
    if not cards:
        raise StateParseError("last 指向的出牌在手牌日志里找不到（log 不完整）")
    pattern, source = resolve_pattern(raw.get("pattern"), cards)
    return TableEntry(seat, "play", cards, pattern, source)


def _entry_cards(entry: Mapping[str, Any], events: Sequence[Mapping[str, Any]], seat: int) -> tuple[int, ...]:
    """拿到这手牌的牌面：优先用 entry 自带的 ``cards``，否则回 log 里找这家最近一次出牌。

    ``last`` 只有 ``{seat, pattern}``，没有 ``cards``（PLAN §0.1），所以必须回查。
    """
    cards = entry.get("cards")
    if cards:
        return tuple(C.sort_cards(int(c) for c in cards))
    for past in reversed(events):
        if past.get("kind") == "play" and _as_int(past.get("seat")) == seat:
            return tuple(C.sort_cards(int(c) for c in (past.get("cards") or ())))
    return ()


def resolve_pattern(raw_pattern: Any, cards: Iterable[int]) -> tuple[Pattern, str]:
    """定出这手牌的读法，并说明来源（``'platform'`` / ``'derived'``）。

    牌型一律以我们自己的规则引擎为准：平台的 pattern 只有"认得出且与牌面自洽"时才采用。
    平台的 pattern 形状不可知（``DoudizhuRules`` 没有副本），所以认不出不算错误，
    只是退回 :func:`classify` 的优先读法。
    """
    readings = classify(cards)
    if not readings:
        raise StateParseError(f"这手牌没有任何合法读法：{list(cards)}")
    parsed = parse_platform_pattern(raw_pattern)
    if parsed is not None:
        for reading in readings:
            if reading.key() == parsed.key():
                return reading, "platform"
    return readings[0], "derived"


#: 平台 pattern 里可能出现的字段名——纯属"尽力而为"，认不出就走牌面反推。
_TYPE_KEYS = ("type", "kind", "name", "pattern", "t")
_RANK_KEYS = ("main_rank", "main", "rank", "value", "lowest", "min_rank", "r")
_LEN_KEYS = ("length", "len", "count", "n", "l", "size")


def parse_platform_pattern(raw: Any) -> Pattern | None:
    """尽力把平台的 pattern 对象读成 :class:`Pattern`；读不出返回 ``None``。

    首选**精确**解析：``src/doudizhu-rules.js`` 里的形状是 ``{type, rank, len}``，类型名是
    ``trio1``/``plane1``/``four2`` 这一套，链条的 ``rank`` 记的是最高点数
    （见 :meth:`ddz.patterns.Pattern.from_platform`）。另外兼容形如 ``'triple:9:1'`` 的
    字符串与常见字段名，纯属兜底。**不做猜测性修正**——读错了会在 :func:`resolve_pattern`
    里与牌面对照时被丢掉。
    """
    if raw is None:
        return None
    exact = Pattern.from_platform(raw)
    if exact is not None:
        return exact
    if isinstance(raw, str):
        try:
            return Pattern.parse_key(raw.strip())
        except (ValueError, TypeError, AttributeError):
            return None
    if isinstance(raw, Mapping):
        kind = _first(raw, _TYPE_KEYS)
        if not isinstance(kind, str):
            return None
        kind = kind.strip().lower().replace("-", "_").replace(" ", "_")
        if kind not in TYPE_NAMES:
            return None
        rank = _first_int(raw, _RANK_KEYS)
        if rank is None:
            # 王炸没有点数可言，缺省就是大王
            rank = C.JOKER_RANK + 1 if kind == "rocket" else None
        if rank is None:
            return None
        length = _first_int(raw, _LEN_KEYS)
        if length is not None and length <= 0:
            return None
        try:
            return Pattern(kind, rank, length or 1)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------- 小工具


def unwrap_state(payload: Any) -> Any:
    """``{"state": {...}}`` → ``{...}``；已经是裸 state 就原样返回。

    ``/games/doudizhu/api`` 的响应外壳与 ``/advise`` 的请求体都长这样，两种都能吃。
    """
    if isinstance(payload, Mapping) and isinstance(payload.get("state"), Mapping):
        inner = payload["state"]
        if "phase" in inner or inner.get("unchanged"):
            return inner
    return payload


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(raw: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _first_int(raw: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = _as_int(raw.get(key))
        if value is not None:
            return value
    return None
