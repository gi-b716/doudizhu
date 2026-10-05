"""牌与点数。

牌 id 与平台客户端（``src/doudizhu.js``）完全一致，零翻译：
``id = rank * 4 + suit``，rank 0..12 = 3,4,…,K,A,2；52 = 小王、53 = 大王。

一点差异：客户端用 ``rank(id) = id >> 2``，两张王都得到 13；本模块把两张王拆成两个
不同的点数（13 = 小王、14 = 大王），因为牌型比较要求点数构成全序——单张大王必须压过
单张小王。牌 id、排序、花色口径其余部分与客户端一致。

DouZero 牌码（:func:`to_env_card`）是**逐点数**编码：3..14 = 3..A、17 = '2'、
20 = 小王、30 = 大王（15/16 留空，让 '2' 与 A、以及两张王互不连续，上游据此判断顺子）。
上游 54 维观测按 Fortran 序摊平 4×13 矩阵再拼 2 个王位，其下标
``suit + 4 * rank_pos`` 与本模块的牌 id ``rank * 4 + suit`` 恒等——牌 id 就是 DouZero
的 54 维向量下标。Phase 4 的 bridge 启动断言会复核这两条。
"""

from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "N_RANKS",
    "DECK_SIZE",
    "RANK_NAMES",
    "SUITS",
    "JOKER_RANK",
    "JOKER_SMALL",
    "JOKER_BIG",
    "MAX_CHAIN_RANK",
    "rank",
    "suit",
    "is_joker",
    "rank_name",
    "card_name",
    "first_card_of_rank",
    "sort_cards",
    "ranks_of",
    "rank_counts",
    "parse_cards",
    "format_cards",
    "full_deck",
    "to_env_card",
    "from_env_card",
    "env_card_to_rank",
]

#: 点数空间宽度：0..12 普通牌，13 小王，14 大王。
N_RANKS = 15
DECK_SIZE = 54
#: 客户端 ``RANK_NAMES`` 的口径（计数器面板按 14→0 逐行显示）。
RANK_NAMES: tuple[str, ...] = (
    "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2",
    "小王", "大王",
)
SUITS: tuple[str, ...] = ("♠", "♥", "♣", "♦")
#: 小王 rank（``rank(52)``）；大王为 ``JOKER_RANK + 1``。
JOKER_RANK = 13
JOKER_SMALL = 52
JOKER_BIG = 53
#: 顺子 / 连对 / 飞机主体能到的最高点数（A）。2 和王不能进链条。
MAX_CHAIN_RANK = 11

#: DouZero 牌码，按点数索引（0..14）。
_ENV_BY_RANK: tuple[int, ...] = tuple(range(3, 15)) + (17, 20, 30)
_ENV_TO_RANK: dict[int, int] = {code: r for r, code in enumerate(_ENV_BY_RANK)}


def rank(card: int) -> int:
    """牌 id → 点数 0..14（12 = '2'，13 = 小王，14 = 大王）。"""
    if card >= JOKER_SMALL:
        return JOKER_RANK + (card - JOKER_SMALL)
    return card >> 2


def suit(card: int) -> int:
    """牌 id → 花色 0..3（♠♥♣♦）；两张王返回 0/1，无实际意义。"""
    return card & 3


def is_joker(card: int) -> bool:
    return card >= JOKER_SMALL


def rank_name(r: int) -> str:
    return RANK_NAMES[r]


def card_name(card: int) -> str:
    """与客户端 ``cardName()`` 同格式：``♠3`` / ``小王`` / ``大王``。"""
    if card == JOKER_SMALL:
        return "小王"
    if card == JOKER_BIG:
        return "大王"
    return SUITS[suit(card)] + RANK_NAMES[rank(card)]


def first_card_of_rank(r: int) -> int:
    """该点数的首张牌 id（♠，王为 52/53）。"""
    if r >= JOKER_RANK:
        return JOKER_SMALL + (r - JOKER_RANK)
    return r * 4


def sort_cards(cards: Iterable[int]) -> list[int]:
    """升序：先点数（3→2→小王→大王）再花色，与客户端 ``R.sortCards`` 口径一致。

    普通牌下等价于按 id 升序；两张王排在最后。
    """
    return sorted(cards, key=_sort_key)


def _sort_key(card: int) -> tuple[int, int]:
    return (rank(card), suit(card))


def ranks_of(cards: Iterable[int]) -> list[int]:
    return [rank(c) for c in cards]


def rank_counts(cards: Iterable[int]) -> list[int]:
    """长度 15 的直方图，下标即点数。"""
    counts = [0] * N_RANKS
    for c in cards:
        counts[rank(c)] += 1
    return counts


def parse_cards(text: str) -> list[int]:
    """解析平台 ``play`` 参数格式 ``"3,7,11"``（客户端提交时已升序）。

    保持输入顺序（调用方若需要升序请用 :func:`sort_cards`）；重复牌直接报错。
    """
    body = text.replace(" ", "")
    if not body:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for piece in body.split(","):
        card = int(piece)
        if not 0 <= card < DECK_SIZE:
            raise ValueError(f"牌 id 越界：{card}")
        if card in seen:
            raise ValueError(f"同一张牌出现两次：{card}")
        seen.add(card)
        out.append(card)
    return out


def format_cards(cards: Iterable[int]) -> str:
    """升序的 ``"3,7,11"``——直接作为平台 ``play`` 的 ``cards`` 参数。"""
    return ",".join(str(c) for c in sort_cards(cards))


def full_deck() -> list[int]:
    return list(range(DECK_SIZE))


def to_env_card(card: int) -> int:
    """牌 id → DouZero 牌码（逐点数；花色在 DouZero 里不可观测）。"""
    return _ENV_BY_RANK[rank(card)]


def env_card_to_rank(code: int) -> int:
    """DouZero 牌码 → 点数；非法码抛 :class:`ValueError`。"""
    try:
        return _ENV_TO_RANK[code]
    except KeyError:
        raise ValueError(f"不是合法的 DouZero 牌码：{code}") from None


def from_env_card(code: int) -> int:
    """DouZero 牌码 → 该点数的首张牌 id（花色无意义，取 ♠）。"""
    return first_card_of_rank(env_card_to_rank(code))


def _check_unique(cards: Sequence[int]) -> None:
    """调试用：断言一手牌里没有重复 id。"""
    if len(set(cards)) != len(cards):
        raise ValueError("手牌中存在重复的牌 id")
