"""手牌拆解：把这手牌拆成尽量少的「手」，以及由它导出的牌力估值。

拆解结果喂给三处：启发式打分的「手数减少」项、叫分的牌力公式、界面上的
``hand_analysis``。两个层次：

- :func:`greedy_decompose`——几种抽取顺序各拆一遍取最少手数。几十微秒，热路径用。
- :func:`exact_min_plays`——在点数计数向量上做记忆化 DFS，带节点/时间预算；预算内给出
  最少手数，超预算返回 ``None`` 让调用方回退贪心。小局面（≤12 张）才划算。

两条硬约束与 :mod:`ddz.moves` 的生成口径一致：**绝不拆炸弹、绝不拆王炸**。所以拆解算出
的"手数"是"用我们真会打的方式走完需要几手"，而不是理论下界。

性能约定：启发式对每个候选着法都要再拆一次剩余手牌，所以那条路径上只用
:func:`greedy_decompose`；:func:`best_decomposition`（可能跑精确搜索）只用于整手牌的
分析与叫分。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

from .. import cards as C
from ..moves import wing_options
from ..patterns import (
    BOMB, FOUR_TWO, FOUR_TWO_PAIR, PAIR, PAIR_STRAIGHT, PLANE, PLANE_PAIR,
    PLANE_SINGLE, ROCKET, SINGLE, STRAIGHT, TRIPLE, TRIPLE_PAIR, TRIPLE_SINGLE,
    Pattern, is_bomb,
)

__all__ = [
    "Decomposition", "greedy_decompose", "exact_min_plays", "best_decomposition",
    "n_plays", "hand_strength", "control_ranks", "MAX_CHAIN_RANK",
]

MAX_CHAIN_RANK = C.MAX_CHAIN_RANK

#: 搜索里的"无穷大"：每手至少走掉一张，所以手数不会超过总张数（最多 20）。
_INF_PLAYS = 21

#: 贪心抽取顺序：每种顺序都从前往后试，先能抽的牌型先抽。
#: 「单张」永远在最后——它保证循环一定能推进。
#: 校准（400 手随机 17 张牌）：3 种顺序与 4 种结果完全一致，1 种则会在 11% 的手牌上更差，
#: 所以取 3 种。
_ORDERS: tuple[tuple[str, ...], ...] = (
    (STRAIGHT, PAIR_STRAIGHT, PLANE_SINGLE, PLANE, TRIPLE_SINGLE, TRIPLE_PAIR, TRIPLE, PAIR, SINGLE),
    (PLANE_SINGLE, PLANE, TRIPLE_SINGLE, TRIPLE_PAIR, TRIPLE, STRAIGHT, PAIR_STRAIGHT, PAIR, SINGLE),
    (PAIR, PAIR_STRAIGHT, TRIPLE_PAIR, TRIPLE, TRIPLE_SINGLE, PLANE, PLANE_SINGLE, STRAIGHT, SINGLE),
)


@dataclass(frozen=True, slots=True)
class Decomposition:
    """一种拆法。``plays`` 与 ``patterns`` 一一对应。"""

    plays: tuple[tuple[int, ...], ...] = ()
    patterns: tuple[Pattern, ...] = ()
    control: tuple[int, ...] = ()
    bombs: tuple[int, ...] = ()
    rockets: int = 0
    exact: bool = False

    @property
    def n_plays(self) -> int:
        """需要几手才能走完。"""
        return len(self.plays)

# --------------------------------------------------------------------- 点数空间工具


def _take(counts: Sequence[int], taken: Iterable[tuple[int, int]]) -> list[int]:
    out = list(counts)
    for rank, number in taken:
        out[rank] -= number
    return out


def _runs(counts: Sequence[int], minimum: int, *, max_rank: int = MAX_CHAIN_RANK) -> list[tuple[int, int]]:
    """``counts[r] >= minimum`` 的极大连续段（闭区间），只看到 ``max_rank``（A）。"""
    out: list[list[int]] = []
    for rank in range(min(max_rank, C.N_RANKS - 1) + 1):
        if counts[rank] >= minimum:
            if out and rank == out[-1][1] + 1:
                out[-1][1] = rank
            else:
                out.append([rank, rank])
    return [(a, b) for a, b in out]


def _lowest(counts: Sequence[int], minimum: int, *, exclude: Iterable[int] = ()) -> int | None:
    skip = set(exclude)
    for rank in range(C.N_RANKS):
        if rank not in skip and counts[rank] >= minimum:
            return rank
    return None


def _junk_singles(counts: Sequence[int], need: int, *, exclude: Iterable[int] = ()) -> tuple[tuple[int, int], ...]:
    """拿 ``need`` 张散牌当翅膀：只取单张（count == 1），从低到高，绝不碰四张同点/对子。"""
    skip = set(exclude)
    picked: list[tuple[int, int]] = []
    for rank in range(C.N_RANKS):
        if len(picked) >= need:
            break
        if rank in skip or counts[rank] != 1:
            continue
        picked.append((rank, 1))
    return tuple(picked) if len(picked) == need else ()


# --------------------------------------------------------------------- 贪心


def _greedy_with_order(
    counts: Sequence[int],
    order: Sequence[str],
    *,
    stop_at: int | None = None,
) -> list[tuple[Pattern, tuple[tuple[int, int], ...]]]:
    """一种抽取顺序：每一步抽当前顺序里第一个能抽的牌型（链条尽量长、点数尽量低）。

    ``stop_at`` 给一个已知的手数上界：一旦这轮的中间结果已经不优于它就直接放弃——
    多顺序贪心的绝大部分时间省在这里。
    """
    counts = list(counts)
    plays: list[tuple[Pattern, tuple[tuple[int, int], ...]]] = []

    # 王炸与炸弹先整手拿走——绝不拆
    if counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1]:
        plays.append((Pattern(ROCKET, C.JOKER_RANK + 1), ((C.JOKER_RANK, 1), (C.JOKER_RANK + 1, 1))))
        counts = _take(counts, ((C.JOKER_RANK, 1), (C.JOKER_RANK + 1, 1)))
    for rank in range(C.JOKER_RANK):
        if counts[rank] == 4:
            plays.append((Pattern(BOMB, rank), ((rank, 4),)))
            counts = _take(counts, ((rank, 4),))

    while any(counts):
        if stop_at is not None and len(plays) >= stop_at:
            return []  # 已经不可能更优，放弃这条顺序
        shape = _Shape(counts)
        for kind in order:
            if not shape.maybe(kind):
                continue
            body = _pick_greedy_body(shape, kind)
            if body is None:
                continue
            pattern, taken, wings = body
            plays.append((pattern, (*taken, *wings)))
            counts = _take(counts, (*taken, *wings))  # 主体和翅膀都要从手牌里拿走
            break
        else:  # pragma: no cover - 「单张」兜底，实际到不了
            rank = _lowest(counts, 1)
            assert rank is not None
            plays.append((Pattern(SINGLE, rank), ((rank, 1),)))
            counts = _take(counts, ((rank, 1),))
    return plays


#: 每种牌型至少要有几个同点（用来便宜地跳过不可能的牌型）。
_MIN_COUNT = {
    SINGLE: 1, PAIR: 2, TRIPLE: 3, TRIPLE_SINGLE: 3, TRIPLE_PAIR: 3,
    STRAIGHT: 1, PAIR_STRAIGHT: 2, PLANE: 3, PLANE_SINGLE: 3, PLANE_PAIR: 3,
}
#: 链条牌型至少要几个不同点数。
_MIN_DISTINCT = {STRAIGHT: 5, PAIR_STRAIGHT: 3, PLANE: 2, PLANE_SINGLE: 2, PLANE_PAIR: 2}


class _Shape:
    """一步之内的点数形状：连续段按需算一次，避免每种牌型都重扫一遍直方图。"""

    __slots__ = ("counts", "top", "distinct", "_runs")

    def __init__(self, counts: Sequence[int]) -> None:
        self.counts = counts
        self.top = max(counts)
        self.distinct = sum(1 for c in counts if c)
        self._runs: dict[int, list[tuple[int, int]]] = {}

    def maybe(self, kind: str) -> bool:
        """这种牌型还有可能抽得出来吗（便宜的先行判断）。"""
        if self.top < _MIN_COUNT[kind]:
            return False
        need = _MIN_DISTINCT.get(kind)
        return need is None or self.distinct >= need

    def runs(self, minimum: int) -> list[tuple[int, int]]:
        found = self._runs.get(minimum)
        if found is None:
            found = _runs(self.counts, minimum)
            self._runs[minimum] = found
        return found


def _pick_greedy_body(
    shape: _Shape, kind: str
) -> tuple[Pattern, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]] | None:
    """按牌型挑一手。返回 (牌型, 主体, 翅膀)。"""
    counts = shape.counts
    if kind == STRAIGHT:
        runs = [(a, b) for a, b in shape.runs(1) if b - a + 1 >= 5]
        if not runs:
            return None
        start, end = max(runs, key=lambda ab: ab[1] - ab[0])  # 尽量长，同长取低点
        length = end - start + 1
        taken = tuple((r, 1) for r in range(start, start + length))
        return Pattern(STRAIGHT, start, length), taken, ()

    if kind == PAIR_STRAIGHT:
        runs = [(a, b) for a, b in shape.runs(2) if b - a + 1 >= 3]
        if not runs:
            return None
        start, end = max(runs, key=lambda ab: ab[1] - ab[0])
        length = end - start + 1
        taken = tuple((r, 2) for r in range(start, start + length))
        return Pattern(PAIR_STRAIGHT, start, length), taken, ()

    if kind == PLANE:
        for start, end in shape.runs(3):
            length = end - start + 1
            if length >= 2:
                taken = tuple((r, 3) for r in range(start, start + length))
                return Pattern(PLANE, start, length), taken, ()
        return None

    if kind in (PLANE_SINGLE, PLANE_PAIR):
        pair_wings = kind == PLANE_PAIR
        for start, end in shape.runs(3):
            length = end - start + 1
            if length < 2:
                continue
            body_ranks = tuple(range(start, start + length))
            taken = tuple((r, 3) for r in body_ranks)
            if pair_wings:
                wings = _cheap_pairs(_take(counts, taken), length, exclude=body_ranks)
            else:
                wings = _junk_singles(_take(counts, taken), length, exclude=body_ranks)
            if wings:
                return Pattern(kind, start, length), taken, wings
        return None

    if kind in (TRIPLE, TRIPLE_SINGLE, TRIPLE_PAIR):
        rank = _lowest(counts, 3)
        if rank is None:
            return None
        taken = ((rank, 3),)
        if kind == TRIPLE:
            return Pattern(TRIPLE, rank), taken, ()
        rest = _take(counts, taken)
        if kind == TRIPLE_PAIR:
            wings = _cheap_pairs(rest, 1, exclude=(rank,))
            if wings:
                return Pattern(TRIPLE_PAIR, rank), taken, wings
            return None
        wings = _junk_singles(rest, 1, exclude=(rank,))
        if wings:
            return Pattern(TRIPLE_SINGLE, rank), taken, wings
        return Pattern(TRIPLE, rank), taken, ()

    if kind == PAIR:
        rank = _lowest(counts, 2)
        if rank is None:
            return None
        return Pattern(PAIR, rank), ((rank, 2),), ()

    if kind == SINGLE:
        rank = _lowest(counts, 1)
        if rank is None:
            return None
        return Pattern(SINGLE, rank), ((rank, 1),), ()

    raise ValueError(f"贪心抽取不支持这种牌型：{kind}")


def _cheap_pairs(counts: Sequence[int], need: int, *, exclude: Iterable[int] = ()) -> tuple[tuple[int, int], ...]:
    """拿 ``need`` 个对子当翅膀：优先整取对子（count == 2），其次从三张里拿两张。"""
    skip = set(exclude)
    picked: list[tuple[int, int]] = []
    for wanted in (2, 3):
        for rank in range(C.N_RANKS):
            if len(picked) >= need:
                break
            if rank in skip or counts[rank] != wanted:
                continue
            picked.append((rank, 2))
        if len(picked) >= need:
            break
    return tuple(picked) if len(picked) == need else ()


def _greedy_plays(counts: Sequence[int]) -> list[tuple[Pattern, tuple[tuple[int, int], ...]]]:
    """多种抽取顺序各拆一遍，取手数最少的那种（点数空间，返回点数多重集）。"""
    best: list[tuple[Pattern, tuple[tuple[int, int], ...]]] = []
    for order in _ORDERS:
        plays = _greedy_with_order(counts, order, stop_at=len(best) or None)
        if plays and (not best or len(plays) < len(best)):
            best = plays
            if len(best) == 1:
                break
    return best


def greedy_decompose(hand: Iterable[int]) -> Decomposition:
    """多种抽取顺序各拆一遍，取手数最少的那种。"""
    cards = C.sort_cards(hand)
    if not cards:
        return Decomposition()
    return _materialize(cards, _greedy_plays(C.rank_counts(cards)))


@lru_cache(maxsize=16384)
def _greedy_n_plays(counts: tuple[int, ...]) -> int:
    return len(_greedy_plays(counts))


def n_plays(hand: Iterable[int]) -> int:
    """只要手数（启发式热路径用）：带缓存，同一个点数多重集只算一次。

    同一手牌打出的不同具体牌（比如不同花色的单张）常常留下同一个点数多重集，缓存命中
    率很高——这是让"每个候选都重拆一遍"跑得动的原因。
    """
    cards = C.sort_cards(hand)
    if not cards:
        return 0
    return _greedy_n_plays(tuple(C.rank_counts(cards)))


# --------------------------------------------------------------------- 精确搜索


class _Budget(Exception):
    pass


def exact_min_plays(
    hand: Iterable[int],
    *,
    max_nodes: int = 2000,
    time_budget_ms: float = 40.0,
    max_cards: int = 12,
    wing_limit: int = 4,
) -> Decomposition | None:
    """记忆化 DFS 求最少手数；超出节点/时间预算返回 ``None``。

    候选集与 :mod:`ddz.moves` 同口径（翅膀不拆炸弹/王炸）；每个主体的翅膀只取最省的
    ``wing_limit`` 种，以免分支爆炸——所以结果是"在这套候选下的最少手数"，实践中与真
    下界一致（测试里用暴力枚举对照过 ≤8 张的全部情形）。
    """
    cards = C.sort_cards(hand)
    if not cards:
        return Decomposition(exact=True)
    if len(cards) > max_cards:
        return None

    started = time.perf_counter()
    deadline = started + time_budget_ms / 1000.0
    nodes = 0
    memo: dict[tuple[int, ...], int] = {}
    choice: dict[tuple[int, ...], tuple[Pattern, tuple[tuple[int, int], ...]]] = {}

    def search(counts: tuple[int, ...]) -> int:
        nonlocal nodes
        if not any(counts):
            return 0
        cached = memo.get(counts)
        if cached is not None:
            return cached
        nodes += 1
        if nodes > max_nodes or time.perf_counter() > deadline:
            raise _Budget
        best = _INF_PLAYS
        best_move: tuple[Pattern, tuple[tuple[int, int], ...]] | None = None
        for pattern, taken in _enumerate_plays(counts, wing_limit):
            value = 1 + search(tuple(_take(counts, taken)))
            if value < best:
                best = value
                best_move = (pattern, taken)
                if best == 1:
                    break
        memo[counts] = best
        if best_move is not None:
            choice[counts] = best_move
        return best

    try:
        search(tuple(C.rank_counts(cards)))
    except (_Budget, RecursionError):
        return None

    plays: list[tuple[Pattern, tuple[tuple[int, int], ...]]] = []
    counts = tuple(C.rank_counts(cards))
    while any(counts):
        step = choice.get(counts)
        if step is None:  # pragma: no cover - 理论上不会发生
            return None
        plays.append(step)
        counts = tuple(_take(counts, step[1]))
    return _materialize(cards, plays, exact=True)


def _enumerate_plays(
    counts: Sequence[int], wing_limit: int
) -> list[tuple[Pattern, tuple[tuple[int, int], ...]]]:
    """点数空间里枚举全部候选出法（含翅膀），与合法着法同口径。"""
    out: list[tuple[Pattern, tuple[tuple[int, int], ...]]] = []
    ranks = [r for r in range(C.N_RANKS) if counts[r]]

    if counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1]:
        out.append((Pattern(ROCKET, C.JOKER_RANK + 1), ((C.JOKER_RANK, 1), (C.JOKER_RANK + 1, 1))))

    for rank in ranks:
        count = counts[rank]
        if count >= 1:
            out.append((Pattern(SINGLE, rank), ((rank, 1),)))
        if count >= 2:
            out.append((Pattern(PAIR, rank), ((rank, 2),)))
        if count >= 3:
            out.append((Pattern(TRIPLE, rank), ((rank, 3),)))
        if count == 4:
            out.append((Pattern(BOMB, rank), ((rank, 4),)))
            for wings in wing_options(_cards_from_counts(counts), {rank}, 2, "single", limit=wing_limit):
                out.append((Pattern(FOUR_TWO, rank), ((rank, 4), *wings)))
            for wings in wing_options(_cards_from_counts(counts), {rank}, 2, "pair", limit=wing_limit):
                out.append((Pattern(FOUR_TWO_PAIR, rank), ((rank, 4), *wings)))

    # 三带
    for rank in ranks:
        if counts[rank] < 3:
            continue
        cards = _cards_from_counts(counts)
        for wings in wing_options(cards, {rank}, 1, "single", limit=wing_limit):
            out.append((Pattern(TRIPLE_SINGLE, rank), ((rank, 3), *wings)))
        for wings in wing_options(cards, {rank}, 1, "pair", limit=wing_limit):
            out.append((Pattern(TRIPLE_PAIR, rank), ((rank, 3), *wings)))

    # 顺子 / 连对
    for start, end in _runs(counts, 1):
        for length in range(5, end - start + 2):
            for s in range(start, end - length + 2):
                out.append((Pattern(STRAIGHT, s, length), tuple((r, 1) for r in range(s, s + length))))
    for start, end in _runs(counts, 2):
        for length in range(3, end - start + 2):
            for s in range(start, end - length + 2):
                out.append((Pattern(PAIR_STRAIGHT, s, length), tuple((r, 2) for r in range(s, s + length))))

    # 飞机族
    cards = _cards_from_counts(counts)
    for start, end in _runs(counts, 3):
        for length in range(2, end - start + 2):
            for s in range(start, end - length + 2):
                body_ranks = set(range(s, s + length))
                taken = tuple((r, 3) for r in body_ranks)
                out.append((Pattern(PLANE, s, length), taken))
                for wings in wing_options(cards, body_ranks, length, "single", limit=wing_limit):
                    out.append((Pattern(PLANE_SINGLE, s, length), (*taken, *wings)))
                for wings in wing_options(cards, body_ranks, length, "pair", limit=wing_limit):
                    out.append((Pattern(PLANE_PAIR, s, length), (*taken, *wings)))
    return out


def best_decomposition(
    hand: Iterable[int],
    *,
    exact_max_cards: int = 12,
    max_nodes: int = 2000,
    time_budget_ms: float = 40.0,
) -> Decomposition:
    """小局面先试精确搜索，其余用贪心。整手牌分析 / 叫分用这个。"""
    cards = C.sort_cards(hand)
    if len(cards) <= exact_max_cards:
        exact = exact_min_plays(cards, max_nodes=max_nodes, time_budget_ms=time_budget_ms)
        if exact is not None:
            return exact
    return greedy_decompose(cards)


# --------------------------------------------------------------------- 物化与统计


def _cards_from_counts(counts: Sequence[int]) -> list[int]:
    """把点数计数变成一副"最省"的具体牌 id（每点数取最小的若干张）。"""
    out: list[int] = []
    for rank, count in enumerate(counts):
        for i in range(count):
            out.append(rank * 4 + i if rank < C.JOKER_RANK else C.JOKER_SMALL + (rank - C.JOKER_RANK))
    return out


def _materialize(
    cards: Sequence[int],
    plays: Sequence[tuple[Pattern, tuple[tuple[int, int], ...]]],
    *,
    exact: bool = False,
) -> Decomposition:
    """点数多重集的拆解 → 具体牌 id（每点数取最小的若干张，与 moves.py 同约定）。"""
    buckets: list[list[int]] = [[] for _ in range(C.N_RANKS)]
    for card in cards:
        buckets[C.rank(card)].append(card)

    used = [0] * C.N_RANKS
    out_cards: list[tuple[int, ...]] = []
    patterns: list[Pattern] = []
    for pattern, taken in plays:
        hand_cards: list[int] = []
        for rank, number in taken:
            start = used[rank]
            hand_cards.extend(buckets[rank][start : start + number])
            used[rank] += number
        out_cards.append(tuple(sorted(hand_cards)))
        patterns.append(pattern)

    bombs = tuple(sorted(p.main_rank for p in patterns if p.type == BOMB))
    rockets = sum(1 for p in patterns if p.type == ROCKET)
    return Decomposition(
        plays=tuple(out_cards),
        patterns=tuple(patterns),
        control=control_ranks(cards, bombs=bombs, rockets=rockets),
        bombs=bombs,
        rockets=rockets,
        exact=exact,
    )


def control_ranks(
    hand: Iterable[int],
    *,
    bombs: Sequence[int] = (),
    rockets: int = 0,
) -> tuple[int, ...]:
    """控制牌：两张王、2、以及留着当炸弹的四张同点。

    这些是"能重新拿回出牌权"的牌，剩下的手数才是真正的负担。
    """
    counts = C.rank_counts(hand)
    control = {r for r in (C.JOKER_RANK, C.JOKER_RANK + 1, 12) if counts[r]}
    control.update(bombs)
    if rockets:  # 王炸整体算一张控制牌，点数记在大王上
        control.discard(C.JOKER_RANK)
        control.add(C.JOKER_RANK + 1)
    return tuple(sorted(control))


def hand_strength(hand: Iterable[int], decomposition: Decomposition | None = None) -> float:
    """叫分用的牌力分（PLAN §3.3 的公式）。

    典型区间约 ``-12 .. +28``：0 分以下是散牌，20 分以上是能叫 3 分的好牌。
    """
    cards = C.sort_cards(hand)
    counts = C.rank_counts(cards)
    d = decomposition if decomposition is not None else best_decomposition(cards)
    jokers = counts[C.JOKER_RANK] + counts[C.JOKER_RANK + 1]
    straights = sum(1 for p in d.patterns if p.type == STRAIGHT)
    return (
        2.2 * jokers
        + 1.4 * counts[12]
        + 0.8 * counts[11]
        + 3.0 * len(d.bombs)
        + 4.0 * d.rockets
        + (17 - 3.2 * d.n_plays)
        + 0.5 * straights
    )
