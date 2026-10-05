"""合法着法生成——本项目的唯一契约。

**合法着法永远由这里生成**：启发式 agent、残局求解器、DouZero bridge 都只在这份
清单上工作（DouZero 只负责给它们打分）。这样规则差异、叫分、流局、版本抖动都进不了
模型。

两条生成口径（是**生成**口径，不是合法性口径——:func:`ddz.patterns.classify` 仍然
认牌组本身的一切合法读法）：

1. **跟牌按牌型定向生成**（如跟顺子只生成同长度的更大顺子 + 炸弹/王炸），绝不枚举
   子集；
2. **翅膀不拆炸弹、不拆出王炸**（``pick_wings`` 的点数池排除手牌里四张同点的点数），
   与上游 DouZero ``MovesGener`` 的口径一致。

``canonical=True`` 时每个 ``(牌型, 主体点数, 链条长度)`` 只留一手最省的着法（领出与
启发式候选用）；``canonical=False`` 给出全部具体着法（DouZero 打分与残局求解用），
用 ``limit`` 防爆。具体牌 id 一律取点数内最小的若干张——花色在斗地主里不可观测，
DouZero 也是逐点数编码，所以这一步不丢信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from . import cards as C
from .patterns import (
    BODY_PER_RANK, BOMB, FOUR_TWO, FOUR_TWO_PAIR, PAIR, PAIR_STRAIGHT, PLANE,
    PLANE_PAIR, PLANE_SINGLE, ROCKET, SINGLE, STRAIGHT, TRIPLE, TRIPLE_PAIR,
    TRIPLE_SINGLE, Pattern, classify_beating,
)

__all__ = ["Move", "legal_moves", "legal_leads", "legal_beats", "has_any_beat", "winning_move"]

#: 翅膀枚举的节点预算：非 canonical 模式下每点数最多取 3 张，正常手牌远达不到这个量级，
#: 超预算时按代价顺序截断（代价低的先枚举）。
_WING_BUDGET = 20000

_PAIR = 2

#: 翅膀代价：键为 ``(该点数手牌张数, 本次取用张数)``。
#: 整取散牌免费；整取对子/三张只是消耗结构；拆对子/三张留下孤张代价更高。
_WING_COST = {
    (1, 1): 0,
    (2, 2): 1,
    (3, 3): 2,
    (2, 1): 4,
    (3, 2): 5,
    (3, 1): 6,
}


@dataclass(frozen=True, slots=True)
class Move:
    """一手具体着法。``cards`` 与 :func:`ddz.cards.format_cards` 同为升序牌 id。"""

    cards: tuple[int, ...]
    pattern: Pattern

    def key(self) -> str:
        """具体着法的唯一标识（牌型 + 牌 id）。"""
        return f"{self.pattern.key()}#{','.join(str(c) for c in self.cards)}"

    def __len__(self) -> int:
        return len(self.cards)

    def __str__(self) -> str:  # pragma: no cover - 仅调试
        names = " ".join(C.card_name(c) for c in self.cards)
        return f"{self.pattern.describe()} [{names}]"


# --------------------------------------------------------------------- 公开接口


def legal_moves(
    hand: Iterable[int],
    to_beat: Pattern | None = None,
    *,
    canonical: bool = False,
    limit: int | None = None,
) -> list[Move]:
    """``hand`` 的全部合法着法（不含「不出」）。``to_beat=None`` 表示领出。"""
    index = _Hand(hand)
    if to_beat is None:
        return _gen_leads(index, canonical, limit)
    return _gen_beats(index, to_beat, canonical, limit)


def legal_leads(
    hand: Iterable[int], *, canonical: bool = True, limit: int | None = None
) -> list[Move]:
    """领出候选（默认 canonical：每个牌型身份一手）。"""
    return _gen_leads(_Hand(hand), canonical, limit)


def legal_beats(
    hand: Iterable[int],
    to_beat: Pattern,
    *,
    canonical: bool = True,
    limit: int | None = None,
) -> list[Move]:
    """能压过 ``to_beat`` 的着法，含炸弹与王炸，不含「不出」（默认 canonical）。"""
    return _gen_beats(_Hand(hand), to_beat, canonical, limit)


def has_any_beat(hand: Iterable[int], to_beat: Pattern) -> bool:
    """是否有牌能压过 ``to_beat``（canonical 生成，只保留第一手即可判定）。"""
    return bool(_gen_beats(_Hand(hand), to_beat, canonical=True, limit=1))


def winning_move(hand: Iterable[int], to_beat: Pattern | None = None) -> Move | None:
    """能一把走完手牌的着法（硬规则「能一把走完就走」用）。"""
    cards = tuple(C.sort_cards(hand))
    pattern = classify_beating(cards, to_beat)
    return Move(cards, pattern) if pattern is not None else None


# --------------------------------------------------------------------- 手牌索引


class _Hand:
    """手牌的点数直方图 + 每个点数下升序的牌 id。"""

    __slots__ = ("cards", "by_rank", "counts")

    def __init__(self, hand: Iterable[int]) -> None:
        self.cards = tuple(C.sort_cards(hand))
        self.by_rank: list[list[int]] = [[] for _ in range(C.N_RANKS)]
        for card in self.cards:
            self.by_rank[C.rank(card)].append(card)
        self.counts = [len(bucket) for bucket in self.by_rank]


class _Emit:
    """着法收集器：去重、按身份收敛（canonical）、最后统一排序与截断。"""

    def __init__(self, hand: _Hand, canonical: bool) -> None:
        self.hand = hand
        self.canonical = canonical
        self.moves: list[Move] = []
        self._seen: set[tuple[str, tuple[int, ...]]] = set()

    def add(
        self,
        pattern: Pattern,
        body: Sequence[tuple[int, int]],
        wings: Sequence[tuple[int, int]] = (),
    ) -> None:
        cards = _pick(self.hand, body, wings)
        token = (pattern.key(), cards)
        if token in self._seen:
            return
        self._seen.add(token)
        self.moves.append(Move(cards, pattern))

    def finish(self, limit: int | None) -> list[Move]:
        moves = self.moves
        if limit is not None and len(moves) > limit:
            moves = moves[:limit]  # 生成顺序 = 牌型优先级 + 翅膀代价升序
        return sorted(moves, key=_move_order)


def _pick(hand: _Hand, body: Sequence[tuple[int, int]], wings: Sequence[tuple[int, int]]) -> tuple[int, ...]:
    cards: list[int] = []
    for r, n in body:
        cards.extend(hand.by_rank[r][:n])
    for r, n in wings:
        cards.extend(hand.by_rank[r][:n])
    cards.sort()
    return tuple(cards)


def _move_order(move: Move) -> tuple[int, int, int, tuple[int, ...]]:
    p = move.pattern
    return (_TYPE_RANK[p.type], p.main_rank, p.length, move.cards)


#: 输出排序用的牌型次序（与读法优先级一致：炸弹/王炸在前）。
_TYPE_RANK = {
    ROCKET: 0, BOMB: 1, STRAIGHT: 2, PAIR_STRAIGHT: 3, PLANE: 4, PLANE_SINGLE: 5,
    PLANE_PAIR: 6, FOUR_TWO: 7, FOUR_TWO_PAIR: 8, TRIPLE_PAIR: 9, TRIPLE_SINGLE: 10,
    TRIPLE: 11, PAIR: 12, SINGLE: 13,
}


# --------------------------------------------------------------------- 领出


def _gen_leads(hand: _Hand, canonical: bool, limit: int | None) -> list[Move]:
    emit = _Emit(hand, canonical)
    counts = hand.counts

    for r in range(C.N_RANKS):
        c = counts[r]
        if c >= 1:
            emit.add(Pattern(SINGLE, r), ((r, 1),))
        if c >= 2:
            emit.add(Pattern(PAIR, r), ((r, 2),))
        if c >= 3:
            emit.add(Pattern(TRIPLE, r), ((r, 3),))
        if c == 4:
            emit.add(Pattern(BOMB, r), ((r, 4),))
    if counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1]:
        emit.add(Pattern(ROCKET, C.JOKER_RANK + 1), _jokers())

    # 三带一 / 三带一对
    for r in range(C.N_RANKS):
        if counts[r] < 3:
            continue
        for wings in _wings(hand, {r}, 1, "single", canonical):
            emit.add(Pattern(TRIPLE_SINGLE, r), ((r, 3),), wings)
        for wings in _wings(hand, {r}, 1, "pair", canonical):
            emit.add(Pattern(TRIPLE_PAIR, r), ((r, 3),), wings)

    # 顺子 / 连对
    for length in range(5, 13):
        for start in range(0, C.MAX_CHAIN_RANK - length + 2):
            if all(counts[r] >= 1 for r in range(start, start + length)):
                body = tuple((r, 1) for r in range(start, start + length))
                emit.add(Pattern(STRAIGHT, start, length), body)
    for length in range(3, 11):
        for start in range(0, C.MAX_CHAIN_RANK - length + 2):
            if all(counts[r] >= 2 for r in range(start, start + length)):
                body = tuple((r, 2) for r in range(start, start + length))
                emit.add(Pattern(PAIR_STRAIGHT, start, length), body)

    # 飞机族
    for length in range(2, 7):
        for start in range(0, C.MAX_CHAIN_RANK - length + 2):
            ranks = range(start, start + length)
            if not all(counts[r] >= 3 for r in ranks):
                continue
            body = tuple((r, 3) for r in ranks)
            emit.add(Pattern(PLANE, start, length), body)
            for wings in _wings(hand, set(ranks), length, "single", canonical):
                emit.add(Pattern(PLANE_SINGLE, start, length), body, wings)
            for wings in _wings(hand, set(ranks), length, "pair", canonical):
                emit.add(Pattern(PLANE_PAIR, start, length), body, wings)

    # 四带二 / 四带两对
    for r in range(C.N_RANKS):
        if counts[r] != 4:
            continue
        for wings in _wings(hand, {r}, 2, "single", canonical):
            emit.add(Pattern(FOUR_TWO, r), ((r, 4),), wings)
        for wings in _wings(hand, {r}, 2, "pair", canonical):
            emit.add(Pattern(FOUR_TWO_PAIR, r), ((r, 4),), wings)

    return emit.finish(limit)


def _jokers() -> tuple[tuple[int, int], ...]:
    return ((C.JOKER_RANK, 1), (C.JOKER_RANK + 1, 1))


# --------------------------------------------------------------------- 跟牌


def _gen_beats(hand: _Hand, to_beat: Pattern, canonical: bool, limit: int | None) -> list[Move]:
    emit = _Emit(hand, canonical)
    counts = hand.counts
    kind = to_beat.type
    main = to_beat.main_rank
    length = to_beat.length

    if kind == ROCKET:
        return []  # 王炸无解

    if kind in (SINGLE, PAIR, TRIPLE):
        need = {SINGLE: 1, PAIR: 2, TRIPLE: 3}[kind]
        for r in range(main + 1, C.N_RANKS):
            if counts[r] >= need:
                emit.add(Pattern(kind, r), ((r, need),))

    elif kind in (TRIPLE_SINGLE, TRIPLE_PAIR):
        wing_kind = "single" if kind == TRIPLE_SINGLE else "pair"
        for r in range(main + 1, C.N_RANKS):
            if counts[r] < 3:
                continue
            for wings in _wings(hand, {r}, 1, wing_kind, canonical):
                emit.add(Pattern(kind, r), ((r, 3),), wings)

    elif kind in (STRAIGHT, PAIR_STRAIGHT, PLANE, PLANE_SINGLE, PLANE_PAIR):
        per_rank = BODY_PER_RANK[kind]
        for start in range(main + 1, C.MAX_CHAIN_RANK - length + 2):
            ranks = range(start, start + length)
            if not all(counts[r] >= per_rank for r in ranks):
                continue
            body = tuple((r, per_rank) for r in ranks)
            if kind in (STRAIGHT, PAIR_STRAIGHT, PLANE):
                emit.add(Pattern(kind, start, length), body)
                continue
            wing_kind = "single" if kind == PLANE_SINGLE else "pair"
            for wings in _wings(hand, set(ranks), length, wing_kind, canonical):
                emit.add(Pattern(kind, start, length), body, wings)

    elif kind in (FOUR_TWO, FOUR_TWO_PAIR):
        wing_kind = "single" if kind == FOUR_TWO else "pair"
        for r in range(main + 1, C.N_RANKS):
            if counts[r] != 4:
                continue
            for wings in _wings(hand, {r}, 2, wing_kind, canonical):
                emit.add(Pattern(kind, r), ((r, 4),), wings)

    elif kind == BOMB:
        for r in range(main + 1, C.N_RANKS):
            if counts[r] == 4:
                emit.add(Pattern(BOMB, r), ((r, 4),))

    # 炸弹压一切非炸；王炸压一切
    if kind != BOMB:
        for r in range(C.N_RANKS):
            if counts[r] == 4:
                emit.add(Pattern(BOMB, r), ((r, 4),))
    if counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1]:
        emit.add(Pattern(ROCKET, C.JOKER_RANK + 1), _jokers())

    return emit.finish(limit)


# --------------------------------------------------------------------- 翅膀枚举


def _wings(
    hand: _Hand,
    body: set[int],
    n_units: int,
    kind: str,
    canonical: bool,
) -> list[tuple[tuple[int, int], ...]]:
    """枚举翅膀（点数多重集层），按代价升序。

    ``kind='single'``：``n_units`` 张单牌型翅膀（每点最多 3 张，整取对子/三张合法）；
    ``kind='pair'``：``n_units`` 个对子（每点恰取 2 张，点数互不相同）。

    硬规则：翅膀不与主体同点；翅膀不含炸弹（点数池排除手牌里的四张同点，且单牌型
    每点最多取 3 张）；翅膀不含王炸（两张王不同时作翅膀）。

    ``canonical=True`` 只返回代价最低的一种。
    """
    if n_units <= 0:
        return [()]

    # pool 项＝(点数, 该点最多提供几个单位, 最便宜的单位均价)；单位：单牌型是「张」，带对是「对」。
    pool: list[tuple[int, int, float]] = []
    for r in range(C.N_RANKS):
        count = hand.counts[r]
        if r in body or count == 0:
            continue
        if kind == "pair":
            if count in (2, 3):  # 四张同点绝不作翅膀
                pool.append((r, 1, float(_WING_COST[(count, 2)])))
        elif count <= 3:  # 四张同点绝不作翅膀
            cap = min(3, count)
            best = min(_WING_COST[(count, use)] / use for use in range(1, cap + 1))
            pool.append((r, cap, best))
    # 便宜的点数先枚举：截断时留下的是最省的那批，canonical 取最小也更快命中。
    pool.sort(key=lambda item: (item[2], item[0]))

    results: list[tuple[int, tuple[tuple[int, int], ...]]] = []
    size = len(pool)
    # 后缀容量剪枝：剩下的点数最多还能提供多少单位。
    suffix = [0] * (size + 1)
    for i in range(size - 1, -1, -1):
        suffix[i] = suffix[i + 1] + pool[i][1]

    def walk(i: int, left: int, chosen: list[tuple[int, int]], cost: int) -> None:
        if left == 0:
            if not (kind == "single" and _has_rocket(chosen)):
                results.append((cost, tuple(sorted(chosen))))
            return
        if i >= size or suffix[i] < left:
            return
        r, cap, _ = pool[i]
        for use in range(min(cap, left), -1, -1):
            if use:
                # use 是「单位数」：带对时 1 个单位就是两张牌
                chosen.append((r, use * (_PAIR if kind == "pair" else 1)))
            walk(i + 1, left - use, chosen, cost + (_wing_cost(hand.counts[r], use, kind) if use else 0))
            if use:
                chosen.pop()

    walk(0, n_units, [], 0)
    if not results:
        return []
    if canonical:
        return [min(results)[1]]

    results.sort()
    if len(results) > _WING_BUDGET:
        results = results[:_WING_BUDGET]
    return [chosen for _, chosen in results]


def _wing_cost(count: int, use: int, kind: str) -> int:
    """``kind='pair'`` 时 use 恒为 1（一对），代价按取 2 张查表。"""
    return _WING_COST[(count, 2 if kind == "pair" else use)]


def _has_rocket(chosen: Sequence[tuple[int, int]]) -> bool:
    ranks = {r for r, _ in chosen}
    return C.JOKER_RANK in ranks and C.JOKER_RANK + 1 in ranks
