"""牌型判定与比较——``src/rules.md`` 的可执行版本。

判定算法是「一次点数直方图 + 分族测试」，不做子集暴力：先算 15 格直方图，再按
王炸 → 炸弹 → 单/对/三 → 三带 → 顺子 → 连对 → 飞机族 → 四带二族 逐族试读。每一族
都是常数级判定，所以 :func:`classify` 可以出现在热路径上。

带牌的三条硬规则（全部来自 rules.md，是**合法性**规则，不是策略偏好）：

1. 带牌不得与主体同点；
2. 带牌不得含炸弹（⇒ ``3333+4444`` 非法，上游 DouZero 的 type-14 允许它——这是
   已知且唯一的口径差异，由 :mod:`ddz.moves` 的合法集在这里拦住）；
3. 带牌不得含王炸（⇒ ``3333+小王大王`` 非法）。

顺子 / 连对 / 飞机的主体只能 3..A（:data:`ddz.cards.MAX_CHAIN_RANK`），2 和王不进链条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from . import cards as C
from .explain import describe_pattern

__all__ = [
    "SINGLE", "PAIR", "TRIPLE", "TRIPLE_SINGLE", "TRIPLE_PAIR",
    "STRAIGHT", "PAIR_STRAIGHT", "PLANE", "PLANE_SINGLE", "PLANE_PAIR",
    "FOUR_TWO", "FOUR_TWO_PAIR", "BOMB", "ROCKET",
    "TYPE_NAMES", "BODY_PER_RANK", "SIZE_PER_LENGTH",
    "Pattern", "classify", "classify_one", "classify_beating",
    "beats", "is_bomb", "explodes_multiplier",
]

SINGLE = "single"
PAIR = "pair"
TRIPLE = "triple"
TRIPLE_SINGLE = "triple_single"
TRIPLE_PAIR = "triple_pair"
STRAIGHT = "straight"
PAIR_STRAIGHT = "pair_straight"
PLANE = "plane"
PLANE_SINGLE = "plane_single"
PLANE_PAIR = "plane_pair"
FOUR_TWO = "four_two"
FOUR_TWO_PAIR = "four_two_pair"
BOMB = "bomb"
ROCKET = "rocket"

TYPE_NAMES: tuple[str, ...] = (
    SINGLE, PAIR, TRIPLE, TRIPLE_SINGLE, TRIPLE_PAIR, STRAIGHT, PAIR_STRAIGHT,
    PLANE, PLANE_SINGLE, PLANE_PAIR, FOUR_TWO, FOUR_TWO_PAIR, BOMB, ROCKET,
)

#: 无翅膀牌型的固定张数。
_FIXED_SIZE = {
    SINGLE: 1, PAIR: 2, TRIPLE: 3, TRIPLE_SINGLE: 4, TRIPLE_PAIR: 5,
    BOMB: 4, ROCKET: 2, FOUR_TWO: 6, FOUR_TWO_PAIR: 8,
}
#: 链条牌型：主体每个点数取几张。
BODY_PER_RANK = {STRAIGHT: 1, PAIR_STRAIGHT: 2, PLANE: 3, PLANE_SINGLE: 3, PLANE_PAIR: 3}
#: 链条牌型：每多一组链条，整手牌多几张（含翅膀）。
SIZE_PER_LENGTH = {STRAIGHT: 1, PAIR_STRAIGHT: 2, PLANE: 3, PLANE_SINGLE: 4, PLANE_PAIR: 5}

#: 多读法的固定优先级：王炸/炸弹最前，其后长链、大张数优先，再按牌型、点数。
_TYPE_ORDER = {
    ROCKET: 0, BOMB: 1, STRAIGHT: 2, PAIR_STRAIGHT: 3, PLANE: 4, PLANE_SINGLE: 5,
    PLANE_PAIR: 6, FOUR_TWO: 7, FOUR_TWO_PAIR: 8, TRIPLE_PAIR: 9, TRIPLE_SINGLE: 10,
    TRIPLE: 11, PAIR: 12, SINGLE: 13,
}


@dataclass(frozen=True, slots=True)
class Pattern:
    """一个牌型读法。

    ``main_rank`` 对链条取最低点数（顺子 ``34567`` → 0），对其它牌型就是主体点数。
    """

    type: str
    main_rank: int
    length: int = 1

    def __post_init__(self) -> None:
        if self.type not in _TYPE_ORDER:
            raise ValueError(f"未知牌型：{self.type!r}")
        if not 0 <= self.main_rank < C.N_RANKS:
            raise ValueError(f"点数越界：{self.main_rank}")
        if self.length < 1:
            raise ValueError(f"链条长度必须 ≥ 1：{self.length}")

    def size(self) -> int:
        """这手牌的张数（链条型按长度折算，含翅膀）。"""
        if self.type in _FIXED_SIZE:
            return _FIXED_SIZE[self.type]
        return self.length * SIZE_PER_LENGTH[self.type]

    def key(self) -> str:
        """稳定、可 JSON 存储、可 grep 的读法标识，如 ``triple:9:1`` / ``straight:0:5``。"""
        return f"{self.type}:{self.main_rank}:{self.length}"

    def label(self) -> str:
        """ASCII 简写，用于日志与断言信息。"""
        tail = "" if self.length == 1 else f"x{self.length}"
        return f"{self.type}:{C.RANK_NAMES[self.main_rank]}{tail}"

    def describe(self) -> str:
        """中文描述（文案见 :mod:`ddz.explain`）。"""
        return describe_pattern(self)

    @classmethod
    def parse_key(cls, key: str) -> "Pattern":
        type_, main_rank, length = key.split(":")
        return cls(type_, int(main_rank), int(length))

    def __str__(self) -> str:  # pragma: no cover - 仅调试
        return self.describe()


# --------------------------------------------------------------------- 读法判定


def classify(cards: Iterable[int]) -> list[Pattern]:
    """一枚牌组的**全部**合法读法，按固定优先级排序。

    ``333444555666`` → ``[plane:0:4, plane_single:0:3, plane_single:1:3]``：可整手当飞机，
    也可按 333444555 或 444555666 当飞机带单（翅膀是三张同点，合法）。
    没有任何读法时返回空列表（该牌组打不出去）。
    """
    cards = tuple(cards)
    n = len(cards)
    if n == 0:
        return []
    counts = C.rank_counts(cards)
    present = [(r, counts[r]) for r in range(C.N_RANKS) if counts[r]]
    k = len(present)
    out: list[Pattern] = []

    # 王炸 / 炸弹（优先级最高，读法唯一）
    if n == 2 and counts[C.JOKER_RANK] == 1 and counts[C.JOKER_RANK + 1] == 1:
        out.append(Pattern(ROCKET, C.JOKER_RANK + 1))
    elif n == 4 and k == 1:
        out.append(Pattern(BOMB, present[0][0]))

    # 单 / 对 / 三 / 三带一 / 三带一对
    elif n == 1:
        out.append(Pattern(SINGLE, present[0][0]))
    elif n == 2 and k == 1:
        out.append(Pattern(PAIR, present[0][0]))
    elif n == 3 and k == 1:
        out.append(Pattern(TRIPLE, present[0][0]))
    elif n == 4 and k == 2:
        for r, c in present:
            if c == 3:
                out.append(Pattern(TRIPLE_SINGLE, r))
    elif n == 5 and k == 2:
        for r, c in present:
            if c == 3:
                out.append(Pattern(TRIPLE_PAIR, r))

    # 顺子：≥5 张全单张、连续、不越过 A
    if n >= 5 and k == n and present[-1][0] <= C.MAX_CHAIN_RANK and _is_run(present):
        out.append(Pattern(STRAIGHT, present[0][0], n))

    # 连对：≥3 对、连续、不越过 A
    if (
        n >= 6
        and n % 2 == 0
        and k == n // 2
        and present[-1][0] <= C.MAX_CHAIN_RANK
        and all(c == 2 for _, c in present)
        and _is_run(present)
    ):
        out.append(Pattern(PAIR_STRAIGHT, present[0][0], k))

    # 飞机族：对每一段连续的三张，枚举各子段与三种形态
    triples = [r for r, c in present if c >= 3 and r <= C.MAX_CHAIN_RANK]
    for a, b in _runs(triples):
        for length in range(2, b - a + 2):
            for start in range(a, b - length + 2):
                body = tuple(range(start, start + length))
                if n == 3 * length:
                    if k == length and all(counts[r] == 3 for r in body):
                        out.append(Pattern(PLANE, start, length))
                elif n == 4 * length:
                    if _single_wings_ok(counts, body):
                        out.append(Pattern(PLANE_SINGLE, start, length))
                elif n == 5 * length:
                    if _pair_wings_ok(counts, body, length):
                        out.append(Pattern(PLANE_PAIR, start, length))

    # 四带二 / 四带两对：一个四张 + 两张（对子或两单）/ 恰两个对子
    if n == 6 or n == 8:
        for r, c in present:
            if c != 4:
                continue
            if n == 6 and _four_two_wings_ok(counts, r):
                out.append(Pattern(FOUR_TWO, r))
            elif n == 8 and _four_two_pair_wings_ok(counts, r):
                out.append(Pattern(FOUR_TWO_PAIR, r))

    out.sort(key=_reading_order)
    return out


def classify_one(cards: Iterable[int]) -> Pattern | None:
    """优先级最高的那一种读法（打不出去则为 ``None``）。"""
    readings = classify(cards)
    return readings[0] if readings else None


def classify_beating(cards: Iterable[int], to_beat: Pattern | None) -> Pattern | None:
    """在全部读法里挑第一个能压过 ``to_beat`` 的（``to_beat`` 为 ``None`` 即领出）。"""
    for pattern in classify(cards):
        if to_beat is None or beats(pattern, to_beat):
            return pattern
    return None


def beats(candidate: Pattern, target: Pattern) -> bool:
    """``candidate`` 能否压过 ``target``。

    王炸最大；炸弹压任何非炸，炸弹之间比点数；其余要求牌型、链条长度都相同且
    主体点数更大（rules.md：「须出同牌型、同张数且更大的牌」）。
    """
    if candidate.type == ROCKET:
        return target.type != ROCKET
    if target.type == ROCKET:
        return False
    if candidate.type == BOMB:
        return target.type != BOMB or candidate.main_rank > target.main_rank
    if target.type == BOMB:
        return False
    return (
        candidate.type == target.type
        and candidate.length == target.length
        and candidate.main_rank > target.main_rank
    )


def is_bomb(pattern: Pattern) -> bool:
    """炸弹或王炸（计分时同样 +1 倍）。"""
    return pattern.type == BOMB or pattern.type == ROCKET


def explodes_multiplier(pattern: Pattern) -> bool:
    """:func:`is_bomb` 的同义函数，站在计分视角命名。"""
    return is_bomb(pattern)


# --------------------------------------------------------------------- 内部工具


def _is_run(present: Sequence[tuple[int, int]]) -> bool:
    """``present`` 已按点数升序：判断是否连续（配合 k == n 或全是对子的前置条件）。"""
    return all(present[i + 1][0] == present[i][0] + 1 for i in range(len(present) - 1))


def _runs(ranks: Sequence[int]) -> list[tuple[int, int]]:
    """升序点数列表 → 若干极大连续段 ``(起, 止)``（闭区间）。"""
    out: list[list[int]] = []
    for r in ranks:
        if out and r == out[-1][1] + 1:
            out[-1][1] = r
        else:
            out.append([r, r])
    return [(a, b) for a, b in out]


def _single_wings_ok(counts: Sequence[int], body: Sequence[int]) -> bool:
    """飞机带单的翅膀：主体之外的点数，每点 1..3 张，不含炸弹、不含王炸，总数恰为 L。"""
    body_set = set(body)
    total = 0
    for r in range(C.N_RANKS):
        c = counts[r]
        if not c:
            continue
        if r in body_set:
            if c != 3:  # 主体同点还有剩余 → 这些牌只能当翅膀，非法
                return False
            continue
        if c > 3:  # 带牌含炸弹
            return False
        total += c
    if total != len(body):
        return False
    return not (counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1])  # 带牌含王炸


def _pair_wings_ok(counts: Sequence[int], body: Sequence[int], n_pairs: int) -> bool:
    """带对的翅膀：主体每个点数恰 3 张，其余点数恰为 ``n_pairs`` 个各 2 张。

    这一条同时挡掉「带第二个四张」（``3333+4444``）与「四张当两个对子」。
    """
    body_set = set(body)
    for r in body_set:
        if counts[r] != 3:
            return False
    pairs = 0
    for r in range(C.N_RANKS):
        c = counts[r]
        if not c or r in body_set:
            continue
        if c != 2:
            return False
        pairs += 1
    return pairs == n_pairs


def _four_two_wings_ok(counts: Sequence[int], body_rank: int) -> bool:
    """四带二的翅膀：其余 2 张（可为对子）；两张王不能同时作翅膀（含王炸）。"""
    if counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1]:
        return False
    return True


def _four_two_pair_wings_ok(counts: Sequence[int], body_rank: int) -> bool:
    """四带两对的翅膀：主体恰 4 张，其余恰两个不同点数的对子。

    这条挡掉 ``3333+4444``（带第二个四张）与「四张当两个对子」。
    """
    if counts[body_rank] != 4:
        return False
    pairs = 0
    for r in range(C.N_RANKS):
        c = counts[r]
        if not c or r == body_rank:
            continue
        if c != 2:
            return False
        pairs += 1
    return pairs == 2


def _reading_order(pattern: Pattern) -> tuple[int, int, int, int, int]:
    bucket = 0 if pattern.type in (ROCKET, BOMB) else 1
    return (bucket, -pattern.size(), -pattern.length, _TYPE_ORDER[pattern.type], pattern.main_rank)
