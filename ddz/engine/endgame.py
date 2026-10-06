"""残局求解器：牌少、三家手牌都看得见时，直接在完美信息下搜到底。

**"完美信息"不是我假设的**：平台下发的 state 里带 ``hands``（三家手牌），我们归档的第一局
真实对局就有这个字段——所以真打到残局时，这一手是**算得出来的**，不是估的。看不到手牌
（旁观、或平台哪天不发了）就老实不用它，回落到启发式。

搜的是什么：把"我这一手打出去之后，双方都最优应对"整棵树搜完，回答一个问题——
**我方（地主或农民队）到底赢不赢**。于是界面上能说出"这一手必胜"这种话，而不是"估计 63%"。

几个关键决定：

- **按点数建键，不按牌 id**。花色在这游戏里从不参与规则，所以 (3♠,3♥) 和 (3♠,3♣) 是同一个
  局面。这样记忆化的命中率高得多，搜索也快得多；取回的着法再落回手上具体那几张牌。
- **必须枚举全部具体着法（``canonical=False``）**。PLAN §3.1 就是这么规定的，理由是翅膀：
  三带一里"带 3"还是"带 9"是不同的着法（后续手牌不同），canonical 只留"最省的一手"会漏解。
- **有预算**：节点数超了就如实说"没搜完"（``exhausted=True``），绝不拿没搜完的结论当结论。
- **农民是一个队**：两个农民都按"农民队赢"来下，这也是真实对局里的合作方式。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .. import cards as C
from ..moves import Move, legal_moves
from ..patterns import Pattern
from ..state import PHASE_PLAYING, SEATS, GameState

__all__ = ["SolveResult", "Limits", "solvable", "solve", "is_endgame", "candidate_from"]


@dataclass(frozen=True, slots=True)
class Limits:
    """什么算"残局"、以及搜多大。

    ``max_cards`` 是**三家里最多的那手**的牌数上限，``max_total`` 是总张数上限。两个都卡：
    只卡单个的话，"三家各 8 张"这种会直接爆掉。
    """

    #: 单人最多几张、总共最多几张。**实测定的**（随机残局 30 例）：
    #: 总 12 张最慢 37ms、14 张最慢 114ms、16 张最慢 725ms——服务端 deadline 默认 400ms，
    #: 所以卡在 14。多放两张就会经常性超时回落，白烧 CPU 还拖住线程池。
    max_cards: int = 10
    max_total: int = 14
    #: 搜索节点上限。超了就返回"没搜完"，由调用方回落到启发式（绝不猜）。
    max_nodes: int = 120_000


DEFAULT_LIMITS = Limits()


@dataclass(slots=True)
class SolveResult:
    """一次求解的结论。

    ``winning`` 为 ``None`` 表示**没搜完**（撞预算了），此时 ``move`` 无意义，调用方必须回落。
    """

    winning: bool | None = None
    #: 推荐着法。``winning`` 为真而它是 ``None``，意思是**"不出"才是最优**（过给队友），
    #: 不是"没答案"——"没答案"由 ``winning is None`` 表示。
    move: Move | None = None
    nodes: int = 0
    elapsed_ms: float = 0.0
    exhausted: bool = False
    #: 所有能保住结论的动作（按"代价小"排序，出牌排在"不出"前面）；``None`` 元素即"不出"
    best_moves: list[Move | None] | None = None

    @property
    def proved(self) -> bool:
        return self.winning is not None


# --------------------------------------------------------------------- 局面判定


def is_endgame(state: GameState, limits: Limits = DEFAULT_LIMITS) -> bool:
    """牌够少了吗（这一条只看牌数，不看信息够不够）。"""
    if state.phase != PHASE_PLAYING or state.landlord is None or state.turn is None:
        return False
    hands = [state.hand_for(seat) for seat in SEATS]
    if any(hand is None for hand in hands):
        return False
    if max(len(hand) for hand in hands) > limits.max_cards:
        return False
    return sum(len(hand) for hand in hands) <= limits.max_total


def solvable(state: GameState, limits: Limits = DEFAULT_LIMITS) -> bool:
    """能不能求解 = 是残局 **且** 三家手牌都在手里。"""
    return is_endgame(state, limits) and state.all_hands is not None


# --------------------------------------------------------------------- 搜索


class _Exhausted(Exception):
    """撞到节点预算。用它从递归里干净地退出来（不能把没搜完的值写进记忆表）。"""


@dataclass(slots=True)
class _Search:
    #: 我方座位集合：地主就一个，农民是两个
    side: frozenset[int]
    memo: dict
    nodes: int = 0
    max_nodes: int = 200_000
    #: 下面两张表纯属提速：合法着法只取决于（手牌, 要压的牌型），点数只取决于手牌，
    #: 都是纯函数，而这两样在全树里会被反复问到——实测这是搜索的主要开销。
    moves: dict = field(default_factory=dict)
    ranks: dict = field(default_factory=dict)


def _ranks(ctx: _Search, hand) -> tuple[int, ...]:
    """按点数排序的手牌——花色不影响任何规则，所以这就是局面的"手牌"部分。"""
    got = ctx.ranks.get(hand)
    if got is None:
        got = tuple(sorted(C.rank(card) for card in hand))
        ctx.ranks[hand] = got
    return got


def _moves(ctx: _Search, hand, to_beat: Pattern | None) -> list[Move]:
    key = (hand, to_beat.key() if to_beat is not None else None)
    got = ctx.moves.get(key)
    if got is None:
        got = legal_moves(hand, to_beat, canonical=False)
        ctx.moves[key] = got
    return got


def _on_side(seat: int, side: frozenset[int]) -> bool:
    return seat in side


def _replace(hands, seat: int, hand) -> tuple:
    """把 ``seat`` 的手牌换成 ``hand``（其余不动）。"""
    return tuple(hand if s == seat else hands[s] for s in SEATS)


def _value(ctx: _Search, hands, to_beat: Pattern | None, turn: int, passes: int) -> bool:
    """``turn`` 该动时，我方在双方都最优的对局下能不能赢。

    ``passes`` 是从上一次有人出牌起"连续不出"的次数：攒到 2 就清台，牌权回到出牌那家
    （三家轮转下，从第二个不出的人往前走一位正好是他）。

    轮到**我方**时是 OR 节点（存在一手能赢就赢）；轮到**对方**时是 AND 节点（他每一手都
    改变不了结果，我们才算赢）。农民是一个队，所以"我方"是两个座位。
    """
    key = (
        _ranks(ctx, hands[0]), _ranks(ctx, hands[1]), _ranks(ctx, hands[2]),
        to_beat.key() if to_beat is not None else None, turn, passes,
    )
    hit = ctx.memo.get(key)
    if hit is not None:
        return hit

    ctx.nodes += 1
    if ctx.nodes > ctx.max_nodes:
        raise _Exhausted

    mine = _on_side(turn, ctx.side)
    value: bool | None = None

    def take(child: bool) -> bool | None:
        """吃进一个分支的结果；返回"已定"的值（None 表示还得看别的分支）。"""
        nonlocal value
        value = child if value is None else (value or child if mine else value and child)
        return value if value == mine else None

    if to_beat is not None:
        # 跟牌时可以不出
        if passes + 1 >= 2:
            passed = _value(ctx, hands, None, (turn + 1) % 3, 0)
        else:
            passed = _value(ctx, hands, to_beat, (turn + 1) % 3, passes + 1)
        settled = take(passed)
        if settled is not None:
            ctx.memo[key] = settled
            return settled

    for move in _moves(ctx, hands[turn], to_beat):
        rest = tuple(card for card in hands[turn] if card not in set(move.cards))
        child = mine if not rest else _value(
            ctx, _replace(hands, turn, rest), move.pattern, (turn + 1) % 3, 0
        )
        settled = take(child)
        if settled is not None:
            ctx.memo[key] = settled
            return settled

    # 一个分支都没提前定下来：对手没得选 / 我方怎么走都改变不了结果
    result = not mine if value is None else value
    ctx.memo[key] = result
    return result


def solve(
    state: GameState,
    *,
    limits: Limits = DEFAULT_LIMITS,
    seat: int | None = None,
) -> SolveResult:
    """解这个残局。``seat`` 默认用 ``state.advise_seat``（我要不要赢）。"""
    started = time.perf_counter()
    me = state.advise_seat if seat is None else seat
    if me is None or state.landlord is None:
        return SolveResult()
    hands = tuple(state.hand_for(s) for s in SEATS)
    if any(hand is None for hand in hands):
        return SolveResult()

    side = frozenset({state.landlord}) if me == state.landlord else frozenset(
        s for s in SEATS if s != state.landlord
    )
    ctx = _Search(side=side, memo={}, max_nodes=limits.max_nodes)
    to_beat = state.to_beat
    passes = _passes_behind(state)

    try:
        winning = _value(ctx, hands, to_beat, state.turn, passes)
    except _Exhausted:
        return SolveResult(nodes=ctx.nodes, exhausted=True,
                           elapsed_ms=(time.perf_counter() - started) * 1000.0)

    best = _rank_root_moves(ctx, hands, to_beat, state.turn, passes, winning)
    return SolveResult(
        winning=winning,
        # 可能是 None：那表示"不出"才是最优（跟 winning is None 的"没搜完"不是一回事）
        move=best[0] if best and winning else None,
        nodes=ctx.nodes,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        best_moves=best if winning else [],
    )


def _passes_behind(state: GameState) -> int:
    """眼下已经"连续不出"了几次（0 或 1）。

    桌上没牌可压（``to_beat is None``）就是 0；否则看一眼最后一条事件是出牌还是不出就行。
    """
    if state.to_beat is None:
        return 0
    events = state.events
    return 1 if events and events[-1].get("kind") == "pass" else 0


def _rank_root_moves(ctx, hands, to_beat, turn, passes, winning) -> list[Move | None]:
    """根节点上哪些动作能保住结论——按"代价小"排序（先甩小牌、先动小牌型）。

    **"不出"也是一个动作**，而且它可能正是唯一保得住结论的那个（上家是队友、或者让队友
    先走完）。漏掉它就会出现"算出必胜、却拿不出任何着法"这种自相矛盾的结果——这个坑踩过。
    出牌和不出都能赢时，出牌排在前面：能走牌就走牌。
    """
    if winning is None:
        return []
    mine = _on_side(turn, ctx.side)
    passing: list[None] = []
    if to_beat is not None:
        if passes + 1 >= 2:
            passed = _value(ctx, hands, None, (turn + 1) % 3, 0)
        else:
            passed = _value(ctx, hands, to_beat, (turn + 1) % 3, passes + 1)
        if passed == winning:
            passing.append(None)

    good: list[Move] = []
    for move in _moves(ctx, hands[turn], to_beat):
        rest = tuple(card for card in hands[turn] if card not in set(move.cards))
        child = mine if not rest else _value(
            ctx, _replace(hands, turn, rest), move.pattern, (turn + 1) % 3, 0
        )
        if child == winning:
            good.append(move)
    # 赢法里挑"代价小"的：先甩小牌、先动小牌型（留着大牌和大牌型当后手）
    good.sort(key=lambda m: (m.pattern.main_rank, len(m.cards)))
    return [*good, *passing]


def candidate_from(result: SolveResult):
    """把求解器的结论包成一手决策（agent 与服务层共用这一个出口）。

    ``result.move is None`` 且 ``winning`` 为真，表示**"不出"才是最优**。
    """
    from ..engine.base import Candidate
    from ..explain import describe_move, reason_solver_pass, reason_solver_win

    move = result.move
    reason = reason_solver_pass() if move is None else reason_solver_win(
        describe_move(move), result.nodes
    )
    alternatives = [
        Candidate(other, 1.0, ["残局求解：这样走同样必胜"], ["solver"], "solver", win_prob=1.0)
        for other in (result.best_moves or [])[1:4]
        if other is not None
    ]
    return Candidate(
        move, 1.0, [reason], ["solver", "proved:win"], "solver",
        win_prob=1.0, alternatives=alternatives,
    )
