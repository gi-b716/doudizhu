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

__all__ = [
    "SolveResult", "Limits", "solvable", "uncertain_solvable", "solve", "solve_uncertain",
    "is_endgame", "candidate_from",
]


@dataclass(frozen=True, slots=True)
class Limits:
    """什么算"残局"、以及搜多大。

    ``max_cards`` 是**三家里最多的那手**的牌数上限，``max_total`` 是总张数上限。两个都卡：
    只卡单个的话，"三家各 8 张"这种会直接爆掉。
    """

    #: **看不见别家手牌时用这个门槛**：未见牌（对手手里那些我还不知道的牌）最多几张。
    #: 这才是真正该卡的量——**不确定性有多大**，而不是"牌有多少"。
    #: 未见牌 u 张要枚举"从中挑 n 张给对手"的所有可能，u=8 时最坏 70 种、u=10 时 252 种；
    #: 每种都要搜一遍，所以卡在 8（配合时间预算兜底）。
    max_unseen: int = 8

    #: 单人最多几张、总共最多几张、以及搜索节点上限。
    #:
    #: **总张数放到 20 是吃过教训的**：一开始卡在 14（随机残局实测 14 张最慢 114ms、
    #: 16 张 725ms，而服务端 deadline 是 400ms）。但真实对局里出现过这样一手——地主手里
    #: 只剩一对 K（2 张），我 10 张、队友 8 张，共 20 张。**总张数超了，求解器就没介入**，
    #: 结果 DouZero 领出一对 10（把牌权递给只剩两张的地主，他 KK 压上直接走完），
    #: 而求解器 2557 个节点就能证明：这个局面农民**能赢**，赢法是领出任意一张单牌。
    #: 也就是说阈值把它挡在了"本可以算清、而且不算贵"的残局之外。
    #:
    #: 所以现在的口径是：**张数放宽 + 节点预算兜底**。张数只用来排除明显不是残局的局面，
    #: 真正防爆的是 ``max_nodes``——超了照样如实回"没搜完"，由调用方回落，绝不猜。
    max_cards: int = 12
    max_total: int = 20
    #: 在能赢的着法里，优先挑"**队友乱打也照样赢**"的那种：最多试前几个（0 = 不试）。
    solo_scan: int = 3
    #: 上面那一步的**节点预算**：用节点而不是时间。时间预算会让同一个局面两次问出不同首选
    #: （实测踩到了——记忆表复用的测试因此翻过车），而"首选会随机变"是不能接受的。
    solo_nodes: int = 10_000
    #: 保留字段（历史上曾是时间上限）。**现在 solo 那一步只看 ``solo_nodes``**：
    #: 时间预算会让同一局面两次给出不同首选，而首选不能随机变。
    solo_budget_ms: float = 300.0
    #: 结论已经在手、只差"挑哪一手"时额外给的时间（毫秒）。
    #: 排根节点着法通常是一堆记忆表命中，很快；但**不能因为预算小就把已经算出来的结论丢掉**，
    #: 所以这一段单独给一点时间。
    rank_budget_ms: float = 50.0
    #: 搜索节点上限（粗粒度防爆）。
    max_nodes: int = 400_000
    #: **墙钟预算**（毫秒）。真正卡住延迟的是这个：节点的代价差得很远（有的几千节点几毫秒，
    #: 有的几万节点就几秒），光卡节点数卡不住。加到一条建议上的时间绝不会超过它。
    #: 超时/超节点都返回"没搜完"，由调用方回落到引擎（绝不猜）。
    time_budget_ms: float = 250.0


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
    #: 推荐的那一手是不是**不依赖队友**（队友乱打也照样赢）。只在证明为必胜时有意义。
    solo: bool = False
    #: 这个结论覆盖了多少种"对手手牌的可能分配"（1 = 手牌全知道，只有一个世界）。
    worlds: int = 1

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
    deadline: float = float("inf")
    #: "我"坐哪儿——判定"队友乱打也赢"时要把阵营缩成我一个人
    me: int = -1
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
    if ctx.nodes > ctx.max_nodes or time.perf_counter() > ctx.deadline:
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
    memo: dict | None = None,
    time_budget_ms: float | None = None,
) -> SolveResult:
    """解这个残局。``seat`` 默认用 ``state.advise_seat``（我要不要赢）。

    ``memo`` 可以传一张**外部记忆表**进来复用（预取用）：表中存的全是**算完的**结论
    （没算完的绝不会写进去，见 :func:`_value`），所以复用只可能让搜索更快，不可能让它变错。
    残局越打越小，我后面每一手都是前面那棵树的后代——同一棵树接着搜，几乎全命中。
    """
    me = state.advise_seat if seat is None else seat
    if me is None or state.landlord is None:
        return SolveResult()
    hands = tuple(state.hand_for(s) for s in SEATS)
    if any(hand is None for hand in hands):
        return SolveResult()

    side = frozenset({state.landlord}) if me == state.landlord else frozenset(
        s for s in SEATS if s != state.landlord
    )
    return _solve_hands(
        hands, side, me, state.to_beat, state.turn, _passes_behind(state),
        limits=limits, memo=memo, time_budget_ms=time_budget_ms,
    )


def _solve_hands(
    hands, side, me, to_beat, turn: int, passes: int,
    *, limits: Limits, memo: dict | None = None, time_budget_ms: float | None = None,
    deadline: float | None = None, overall_deadline: float | None = None,
) -> SolveResult:
    """已知三家手牌时的搜索内核（``solve`` 与"未知手牌"那条路共用）。"""
    started = time.perf_counter()
    ctx = _Search(
        side=side, memo=memo if memo is not None else {}, max_nodes=limits.max_nodes,
        deadline=deadline if deadline is not None else (
            started + (time_budget_ms if time_budget_ms is not None
                       else limits.time_budget_ms) / 1000.0),
        me=me,
    )

    try:
        winning = _value(ctx, hands, to_beat, turn, passes)
    except _Exhausted:
        return SolveResult(nodes=ctx.nodes, exhausted=True,
                           elapsed_ms=(time.perf_counter() - started) * 1000.0)

    # 排着法另给一点时间（见 Limits.rank_budget_ms）：结论已经确定了，别在这儿丢掉它。
    # 但**不许超过总期限**——多世界枚举时每个世界都排一次，不共享上限就会叠成好几秒。
    ctx.deadline = time.perf_counter() + limits.rank_budget_ms / 1000.0
    if overall_deadline is not None:
        ctx.deadline = min(ctx.deadline, overall_deadline)
    try:
        best = _rank_root_moves(ctx, hands, to_beat, turn, passes, winning)
    except _Exhausted:
        # 结论算出来了、但"哪些着法能保住它"没排完。**不许含糊**：宁可当作没搜完，
        # 也不能返回 winning=True 配一个 move=None——那会被当成"不出才是最优"。
        return SolveResult(nodes=ctx.nodes, exhausted=True,
                           elapsed_ms=(time.perf_counter() - started) * 1000.0)
    solo = False
    if winning and best:
        best, solo = _prefer_solo(ctx, hands, to_beat, turn, best, limits)
    return SolveResult(
        winning=winning,
        # 可能是 None：那表示"不出"才是最优（跟 winning is None 的"没搜完"不是一回事）
        move=best[0] if best and winning else None,
        nodes=ctx.nodes,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        best_moves=best if winning else [],
        solo=solo,
    )


def _prefer_solo(ctx: _Search, hands, to_beat, turn, moves, limits) -> tuple[list, bool]:
    """在能赢的着法里，把"**队友乱打也照样赢**"的挪到最前面，返回 (新顺序, 首选是否 solo)。

    为什么要这一步：证明里的"必胜"是**两个农民配合**的必胜。队友不按剧本走，证明就失效。
    而真人队友（乃至一个启发式队友）恰恰不会按剧本走。所以：把我一个人当主角再搜一遍，
    **队友当对手**——要是这样都赢，那这一手谁来都拦不住，最稳。

    只在我是农民时有意义（地主没有队友，直接跳过）。最多试前 ``solo_scan`` 个，试到第一个就停；
    一个都没有就保持原样（仍然是必胜，只是需要一个配合的队友）。
    """
    if len(ctx.side) == 1 or limits.solo_scan <= 0:   # 我是地主：没有队友这回事
        return moves, False
    # **只看节点，不看时间**：这里用时间做过，同一局面两次会给出不同首选（测试当场抓到），
    # 而"首选会随机变"是不能接受的。10k 节点约合 10~100ms，本来就有界。
    deadline = float("inf")
    for index, move in enumerate(moves[: limits.solo_scan]):
        if move is None:            # "不出"照样要判：它也可能是最稳的那个
            rest = hands[turn]
        else:
            rest = tuple(card for card in hands[turn] if card not in set(move.cards))
        if not rest:
            solo = True             # 一把走完，谁也拦不住
        else:
            solo_ctx = _Search(
                side=frozenset({ctx.me}), memo={}, max_nodes=limits.solo_nodes,
                deadline=deadline, me=ctx.me,
            )
            try:
                solo = _value(
                    solo_ctx,
                    _replace(hands, turn, rest),
                    move.pattern if move is not None else to_beat,
                    (turn + 1) % 3, 0,
                )
            except _Exhausted:
                break
            ctx.nodes += solo_ctx.nodes      # 记账要算上这一段
        if solo:
            return [move, *moves[:index], *moves[index + 1:]], True
    return moves, False


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
        describe_move(move), result.nodes, solo=result.solo, worlds=result.worlds
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


# --------------------------------------------------------------------- 看不见别家手牌时


def uncertain_solvable(state: GameState, limits: Limits = DEFAULT_LIMITS) -> bool:
    """**看不见别家手牌**时能不能算：出牌阶段、知道各家张数、未见牌够少。

    门槛卡的是**未见牌张数**，不是总张数——真正决定代价的是"不确定性有多大"。
    未见牌 u 张时，要枚举"从中挑 n 张给对手"的所有可能：u=8 最坏 70 种、u=10 是 252 种。
    """
    me = state.advise_seat
    if state.phase != PHASE_PLAYING or state.landlord is None or state.turn is None or me is None:
        return False
    return len(state.unseen_for(me)) <= limits.max_unseen


def solve_uncertain(
    state: GameState, *, limits: Limits = DEFAULT_LIMITS, seat: int | None = None,
) -> SolveResult:
    """看不到别家手牌时的求解：**对一切可能的分配都赢**，才算必胜。

    为什么是"对一切分配"而不是"猜一个最像的"：猜出来的只能叫估计，而我们本来就有一个很强的
    估计器（DouZero）。这里要的是**证明**——所以只要有一种与已知信息一致的分配能让对手赢，
    就不算数。结论因此特别硬：**无论牌怎么分、无论谁怎么打，我都赢**。

    队友也一视同仁当对手：真人队友不会按剧本走，"需要队友配合"的赢法在实战里不算赢
    （和 :func:`_prefer_solo` 同一套理由）。所以这里的"我方"只有我自己。
    """
    started = time.perf_counter()
    me = state.advise_seat if seat is None else seat
    if me is None or state.landlord is None or state.turn is None:
        return SolveResult()
    mine = state.hand_for(me)
    if mine is None:
        return SolveResult()

    others = [s for s in SEATS if s != me]
    unseen = [_rank_of(card) for card in state.unseen_for(me)]
    sizes = {s: state.counts[s] for s in others}
    if len(unseen) != sum(sizes.values()):        # 张数对不上就不猜（比如还有没亮出来的底牌）
        return SolveResult()
    if len(unseen) > limits.max_unseen:
        return SolveResult()

    deadline = started + limits.time_budget_ms / 1000.0
    to_beat, passes = state.to_beat, _passes_behind(state)
    side = frozenset({me})                        # 队友也是对手（见 docstring）
    first, second = others
    total = 0
    alive: list | None = None                     # 所有世界都能赢的着法（交集）
    nodes = 0

    for hand_first in _sub_multisets(unseen, sizes[first]):
        rest = _subtract(unseen, hand_first)
        hands = _hands_for(me, mine, first, hand_first, second, rest)
        result = _solve_hands(
            hands, side, me, to_beat, state.turn, passes,
            limits=limits, deadline=deadline, overall_deadline=deadline,
        )
        total += 1
        nodes += result.nodes
        if result.winning is not True or not result.best_moves:
            return SolveResult(nodes=nodes, worlds=total, exhausted=result.winning is None,
                               elapsed_ms=(time.perf_counter() - started) * 1000.0)
        if alive is None:
            alive = list(result.best_moves)
        else:
            keys = {_move_key(m) for m in result.best_moves}
            alive = [m for m in alive if _move_key(m) in keys]
            if not alive:
                return SolveResult(nodes=nodes, worlds=total,
                                   elapsed_ms=(time.perf_counter() - started) * 1000.0)

    if not total or alive is None:
        return SolveResult()
    return SolveResult(
        winning=True,
        move=alive[0],                            # 可能是 None：那表示"不出"在所有世界里都最优
        nodes=nodes,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        best_moves=alive,
        solo=True,                                # 这个结论本来就比 solo 还硬
        worlds=total,
    )


def _rank_of(card: int) -> int:
    return C.rank(card)


def _hands_for(me: int, mine, first: int, hand_first, second: int, hand_second):
    """按座位摆好三家手牌（点数多重集 → 具体牌号；花色不影响规则）。"""
    by_seat = {me: list(mine), first: _ids_from_ranks(hand_first), second: _ids_from_ranks(hand_second)}
    return tuple(tuple(sorted(by_seat[s])) for s in SEATS)


def _ids_from_ranks(ranks) -> list[int]:
    """点数多重集 → 具体牌号（同点数依次取不同花色；大小王各只有一张）。"""
    used: dict[int, int] = {}
    out: list[int] = []
    for rank in sorted(ranks):
        index = used.get(rank, 0)
        used[rank] = index + 1
        out.append(C.JOKER_SMALL + index if rank >= C.JOKER_RANK else rank * 4 + index)
    return out


def _subtract(pool, taken) -> list[int]:
    """``pool`` 里去掉 ``taken`` 剩下的（多重集差）。"""
    rest = list(pool)
    for rank in taken:
        rest.remove(rank)
    return rest


def _move_key(move) -> tuple | None:
    from ..cards import action_key

    return None if move is None else action_key(move.cards)


def _sub_multisets(pool, size: int):
    """``pool``（点数多重集）里所有大小为 ``size`` 的子多重集。

    只看点数、不看花色：花色不参与任何规则，同一个点数组合的两种花色分法是同一个世界。
    """
    from collections import Counter

    counts = Counter(pool)
    ranks = sorted(counts)
    chosen: dict[int, int] = {}

    def rec(index: int, need: int):
        if need == 0:
            out: list[int] = []
            for rank in ranks:
                out.extend([rank] * chosen.get(rank, 0))
            yield tuple(out)
            return
        if index >= len(ranks):
            return
        rank = ranks[index]
        for take in range(min(counts[rank], need) + 1):
            chosen[rank] = take
            yield from rec(index + 1, need - take)
        chosen.pop(rank, None)

    yield from rec(0, size)
