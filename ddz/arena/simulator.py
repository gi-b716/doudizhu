"""无头对局模拟器：完整规则跑一局，**每一步都断言 agent 的着法合法**。

规则来自 ``src/rules.md``：发牌 → 叫分（叫 3 立刻成地主；三人都不叫重新发牌，连续流局 3 次
后本轮最后一位必须叫分）→ 地主拿底牌先出 → 出牌（同牌型同张数更大，或不出）→ 计分
（底分 × 倍数；每出一个炸弹/王炸 +1 倍；春天 / 反春再 +1 倍）。

它同时是 :mod:`ddz.moves` 的持续集成：任何 agent 想出一手非法牌都会当场炸出来——
所以合法性只有一个来源，模拟器不会偷偷"宽容"。

agent 看到的状态里 ``all_hands`` 永远是 ``None``：只能看自己的手牌（这一点由
:meth:`_state_for` 保证，别改成管理员视角，否则竞技场就废了）。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .. import cards as C
from ..engine.base import Agent
from ..moves import legal_leads
from ..patterns import Pattern, beats, classify, is_bomb
from ..state import (
    PHASE_BIDDING, PHASE_FINISHED, PHASE_PLAYING, SEATS, GameState, PlayerInfo,
    TableEntry,
)

__all__ = ["GameOutcome", "Simulator", "score_deltas", "spring_flags", "MAX_DEALS"]

#: 连续流局的兜底上限：超过就强制第一位叫分，免得模拟器空转。
MAX_DEALS = 6

#: 一局最多走多少步（正常 60–120 步；纯粹是防死循环）。
MAX_STEPS = 400


@dataclass(slots=True)
class GameOutcome:
    """一局的结果。``deltas`` 是三家各自的得分。"""

    index: int = 0
    seats: tuple[str, str, str] = ("", "", "")
    landlord: int = 0
    landlord_won: bool = False
    spring: bool = False
    anti_spring: bool = False
    base: int = 1
    multiplier: int = 1
    bombs: int = 0
    deltas: tuple[int, int, int] = (0, 0, 0)
    steps: int = 0
    deals: int = 1
    redeals: int = 0
    errors: int = 0
    #: 全部决策耗时（ms，按时序）与按座位分开的耗时
    latencies: list[float] = field(default_factory=list)
    latency_by_seat: list[list[float]] = field(default_factory=lambda: [[], [], []])

    def winner_side(self) -> str:
        return "landlord" if self.landlord_won else "farmers"


def score_deltas(base: int, multiplier: int, landlord: int, landlord_won: bool) -> tuple[int, int, int]:
    """rules.md 的计分：地主赢 2 × 底分 × 倍数，每位农民输 底分 × 倍数（农民赢则反之）。

    PLAN §3.2 的示例 ``base=3, multiplier=4 → deltas [12,-6,-6]`` 比这里少一个系数 2；
    两处口径冲突时**以 rules.md 为准**（已确认的决策，不是待办事项）。改这里就是改全站计分，
    所以只此一处实现。
    """
    unit = base * multiplier
    out = []
    for seat in SEATS:
        if seat == landlord:
            out.append(2 * unit if landlord_won else -2 * unit)
        else:
            out.append(-unit if landlord_won else unit)
    return (out[0], out[1], out[2])


def spring_flags(plays_per_seat, landlord: int, landlord_won: bool) -> tuple[bool, bool]:
    """春天（农民一张没出）/ 反春（地主只出过第一手）。"""
    farmers = [s for s in SEATS if s != landlord]
    spring = bool(landlord_won) and all(plays_per_seat[s] == 0 for s in farmers)
    anti_spring = (not landlord_won) and plays_per_seat[landlord] == 1
    return spring, anti_spring


class Simulator:
    """驱动三个 agent 打完整的一局。``strict=True`` 时非法着法直接抛异常。"""

    def __init__(
        self,
        agents: list[Agent],
        *,
        seed: int | None = None,
        rng: random.Random | None = None,
        strict: bool = True,
        oracle: bool = False,
    ) -> None:
        if len(agents) != 3:
            raise ValueError("需要正好三名 agent")
        self.agents = agents
        self.rng = rng if rng is not None else random.Random(seed)
        self.strict = strict
        #: 把三家手牌也发给 agent。**默认关**——真实平台确实会下发 ``hands``（所以服务端的
        #: 残局求解是真能用上的），但竞技场里给所有 agent 开天眼，测出来的强度就没意义了。
        #: 只有评测"残局求解到底值多少"这种问题时才打开，而且要在报告里写明。
        self.oracle = oracle
        #: 本副牌的动作日志（与平台 log 同形状）。**必须维护**：agent 看到的状态要包含
        #: 完整历史——DouZero 的观测就是从这串历史里编码出来的，空日志会让它瞎打。
        self._events: list[dict] = []

    # ----------------------------------------------------------------- 一局

    def play(self, index: int = 0) -> GameOutcome:
        outcome = GameOutcome(index=index, seats=tuple(a.name for a in self.agents))
        hands, bottom = self._deal_and_bid(outcome)
        landlord = outcome.landlord

        hands[landlord] = C.sort_cards(list(hands[landlord]) + list(bottom))
        outcome.base = max(outcome.base, 1)

        # 出牌
        table: list[TableEntry | None] = [None, None, None]
        last: TableEntry | None = None
        played: list[list[int]] = [[], [], []]
        plays_per_seat = [0, 0, 0]
        turn = landlord
        while True:
            if outcome.steps >= MAX_STEPS:  # pragma: no cover - 防死循环
                raise RuntimeError("模拟器步数超限")
            to_beat = None if (last is None or last.seat == turn) else last.pattern
            state = self._state_for(
                seat=turn, hands=hands, table=table, last=last, played=played,
                landlord=landlord, base=outcome.base, multiplier=outcome.multiplier,
                bombs=outcome.bombs, phase=PHASE_PLAYING, turn=turn, bottom=bottom,
            )
            move = self._ask_move(state, hands[turn], to_beat, outcome)
            outcome.steps += 1

            if move is None:
                table[turn] = TableEntry(turn, "pass")
                self._events.append({"kind": "pass", "seat": turn})
            else:
                played[turn].extend(move.cards)
                hands[turn] = tuple(c for c in hands[turn] if c not in set(move.cards))
                if last is None or last.seat == turn:
                    table = [None, None, None]
                table[turn] = TableEntry(turn, "play", move.cards, move.pattern, "sim")
                last = table[turn]
                self._events.append(
                    {"kind": "play", "seat": turn, "cards": list(move.cards),
                     "pattern": move.pattern.key()}
                )
                plays_per_seat[turn] += 1
                if is_bomb(move.pattern):
                    outcome.bombs += 1
                    outcome.multiplier += 1

            if not hands[turn]:
                outcome.landlord_won = turn == landlord
                break
            turn = (turn + 1) % 3

        outcome.spring, outcome.anti_spring = spring_flags(plays_per_seat, landlord, outcome.landlord_won)
        if outcome.spring or outcome.anti_spring:
            outcome.multiplier += 1
        outcome.deltas = score_deltas(outcome.base, outcome.multiplier, landlord, outcome.landlord_won)
        return outcome

    # ----------------------------------------------------------------- 发牌与叫分

    def _deal_and_bid(
        self, outcome: GameOutcome
    ) -> tuple[list[tuple[int, ...]], tuple[int, ...]]:
        deck = C.full_deck()
        for deal_index in range(MAX_DEALS):
            self.rng.shuffle(deck)
            self._events = []
            hands = [tuple(C.sort_cards(deck[17 * i : 17 * i + 17])) for i in SEATS]
            bottom = tuple(C.sort_cards(deck[51:]))
            first_bidder = self.rng.randrange(3)
            order = [(first_bidder + i) % 3 for i in range(3)]
            bids: list[int | None] = [None, None, None]
            high = 0
            landlord: int | None = None
            outcome.deals = deal_index + 1

            for position, seat in enumerate(order):
                # rules.md：连续流局 3 次后，本轮最后一位必须叫分
                forced = position == 2 and (outcome.redeals >= 3 or deal_index >= MAX_DEALS - 1)
                state = self._state_for(
                    seat=seat, hands=hands, table=[None, None, None], last=None,
                    played=[[], [], []], landlord=None, base=0, multiplier=1, bombs=0,
                    phase=PHASE_BIDDING, turn=seat,
                    high_bid=high, must_bid=forced,
                    bids=tuple(bids),
                )
                value = self._ask_bid(state, outcome, high, forced)
                bids[seat] = value
                if value > high:
                    high = value
                if value == 3:
                    landlord = seat
                    break

            if landlord is None and high:
                landlord = bids.index(high)
            if landlord is not None:
                outcome.landlord = landlord
                outcome.base = high or 1
                return hands, bottom
            # 流局
            outcome.redeals += 1
            self._events = []

        raise RuntimeError("连续流局到上限仍未定地主")  # pragma: no cover

    # ----------------------------------------------------------------- 与 agent 交互

    def _ask_move(self, state: GameState, hand, to_beat, outcome: GameOutcome):
        """问 agent 要一手牌，并**断言它合法**。"""
        agent = self.agents[state.turn]
        started = time.perf_counter()
        try:
            candidate = agent.act(state)
        finally:
            self._record_latency(outcome, state.turn, started)
        move = candidate.move

        if move is None:
            if to_beat is None:
                self._illegal(agent, outcome, "领出时不能不出")
                # 宽松模式下也得让牌局往前走：强制打一手最小的合法领出
                return self._fallback_move(hand, None)
            return None

        if not self._is_legal(hand, move.cards, move.pattern, to_beat):
            self._illegal(agent, outcome, f"非法着法 {move.cards}（{move.pattern.key()}）")
            return self._fallback_move(hand, to_beat)
        return move

    @staticmethod
    def _fallback_move(hand, to_beat):
        """宽松模式下的兜底：跟牌就"不出"，领出就打最小的单张（永远合法）。"""
        if to_beat is not None:
            return None
        leads = legal_leads(hand, canonical=True, limit=1)
        return leads[0] if leads else None

    def _is_legal(self, hand, cards, pattern: Pattern, to_beat: Pattern | None) -> bool:
        if not set(cards) <= set(hand):
            return False
        readings = {p.key() for p in classify(cards)}
        if pattern.key() not in readings:
            return False
        if to_beat is None:
            return True
        return beats(pattern, to_beat)

    def _ask_bid(self, state: GameState, outcome: GameOutcome, high: int, forced: bool) -> int:
        agent = self.agents[state.turn]
        started = time.perf_counter()
        try:
            bid = agent.bid(state)
        finally:
            self._record_latency(outcome, state.turn, started)
        value = int(bid.value)
        legal = state.legal_bids
        if value not in legal:
            self._illegal(agent, outcome, f"非法叫分 {value}（合法：{legal}）")
            lower = [v for v in legal if v <= value]
            # 宽松模式下必须退到一个**合法**的分数：legal 非空，最差取其中最小
            value = max(lower) if lower else min(legal)
        return value

    @staticmethod
    def _record_latency(outcome: GameOutcome, seat: int, started: float) -> None:
        elapsed = (time.perf_counter() - started) * 1000.0
        outcome.latencies.append(elapsed)
        outcome.latency_by_seat[seat].append(elapsed)

    def _illegal(self, agent: Agent, outcome: GameOutcome, message: str) -> None:
        outcome.errors += 1
        if self.strict:
            raise AssertionError(f"{agent.name} {message}")
        return None

    # ----------------------------------------------------------------- 状态构造

    def _state_for(
        self,
        *,
        seat: int,
        hands: list[tuple[int, ...]],
        table: list[TableEntry | None],
        last: TableEntry | None,
        played: list[list[int]],
        landlord: int | None,
        base: int,
        multiplier: int,
        bombs: int,
        phase: str,
        turn: int,
        high_bid: int = 0,
        must_bid: bool = False,
        bids: tuple[int | None, int | None, int | None] = (None, None, None),
        bottom: tuple[int, ...] | None = None,
    ) -> GameState:
        """给 ``seat`` 座位看的状态：**只有自己的手牌**，没有 ``all_hands``。"""
        playing = phase == PHASE_PLAYING
        leading = playing and (last is None or last.seat == turn)
        players = [
            PlayerInfo(
                seat=i,
                username=self.agents[i].name,
                count=len(hands[i]),
                bid=bids[i] if phase == PHASE_BIDDING else None,
                role=None if landlord is None else ("landlord" if landlord == i else "farmer"),
            )
            for i in SEATS
        ]
        return GameState(
            seat=seat,
            phase=phase,
            turn=turn,
            leading=leading,
            hand=tuple(hands[seat]),
            all_hands=[tuple(h) for h in hands] if self.oracle else None,
            bottom=bottom if playing else None,
            landlord=landlord,
            high_bid=high_bid or base,
            must_bid=must_bid,
            multiplier=multiplier,
            bombs=bombs,
            players=players,
            table=list(table),
            last=last,
            events=[dict(entry) for entry in self._events],
            played_by_seat=[tuple(sorted(c)) for c in played],
            reserve=None,
        )
