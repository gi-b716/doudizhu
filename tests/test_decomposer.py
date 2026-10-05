"""拆解器：拆出来的每一手都合法、牌不重不漏；精确搜索与暴力枚举一致；牌力公式对齐 PLAN。"""

from __future__ import annotations

import random
from functools import lru_cache

import pytest

from ddz import cards as C
from ddz import patterns as P
from ddz.engine import decomposer as D
from ddz.moves import legal_leads
from helpers import cards_of, random_hand


def check_partition(hand, decomp):
    flat = [c for play in decomp.plays for c in play]
    assert sorted(flat) == C.sort_cards(hand)
    assert len(set(flat)) == len(flat)
    for cards, pattern in zip(decomp.plays, decomp.patterns):
        assert pattern.key() in {p.key() for p in P.classify(cards)}
    assert decomp.n_plays == len(decomp.plays)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_greedy_output_is_a_valid_partition(seed):
    rng = random.Random(seed)
    for _ in range(10):
        hand = random_hand(rng, 17, max_count=4)
        check_partition(hand, D.greedy_decompose(hand))
        check_partition(hand, D.best_decomposition(hand))


def test_never_splits_a_bomb_or_rocket():
    hand = cards_of("3333 小王 大王 5 6")
    decomp = D.greedy_decompose(hand)
    assert P.Pattern("bomb", 0) in decomp.patterns
    assert P.Pattern("rocket", 14) in decomp.patterns
    assert decomp.bombs == (0,) and decomp.rockets == 1
    # 炸弹不会被拆成单张/三张
    assert not any(p.type in (P.SINGLE, P.TRIPLE) and p.main_rank == 0 for p in decomp.patterns)


def test_chains_are_kept_together():
    hand = cards_of("34567 88 9")
    decomp = D.greedy_decompose(hand)
    assert len(decomp.plays) <= 4
    assert any(p.type == P.STRAIGHT for p in decomp.patterns)


def test_empty_hand():
    decomp = D.greedy_decompose([])
    assert decomp.n_plays == 0 and decomp.plays == ()


# --------------------------------------------------------------------- 精确搜索


@lru_cache(maxsize=None)
def brute_min_plays(counts: tuple[int, ...]) -> int:
    """暴力下界：在合法着法上做记忆化 DFS（点数计数向量为状态）。"""
    if not any(counts):
        return 0
    cards = [c for r, n in enumerate(counts) for c in _cards_of_rank(r, n)]
    best = sum(counts)
    for move in legal_leads(cards, canonical=False, limit=64):
        rest = list(counts)
        for card in move.cards:
            rest[C.rank(card)] -= 1
        best = min(best, 1 + brute_min_plays(tuple(rest)))
    return best


def _cards_of_rank(rank: int, count: int) -> list[int]:
    if rank >= C.JOKER_RANK:
        return [C.JOKER_SMALL + (rank - C.JOKER_RANK)] * count
    return [rank * 4 + i for i in range(count)]


@pytest.mark.parametrize("seed", range(12))
def test_exact_matches_brute_force_on_small_hands(seed):
    rng = random.Random(1000 + seed)
    hand = random_hand(rng, rng.randint(4, 8), max_count=4)
    exact = D.exact_min_plays(hand)
    assert exact is not None
    assert exact.n_plays == brute_min_plays(tuple(C.rank_counts(C.sort_cards(hand))))


def test_exact_never_worse_than_greedy():
    rng = random.Random(5)
    for _ in range(30):
        hand = random_hand(rng, rng.randint(6, 12), max_count=4)
        exact = D.exact_min_plays(hand)
        if exact is None:
            continue
        assert exact.n_plays <= D.greedy_decompose(hand).n_plays


def test_exact_gives_up_on_big_hands():
    hand = random_hand(random.Random(1), 17, max_count=4)
    assert D.exact_min_plays(hand) is None          # 超过 max_cards
    assert D.exact_min_plays(hand, max_cards=17, max_nodes=1) is None  # 节点预算耗尽


def test_exact_finds_the_one_shot_win():
    hand = cards_of("34567")
    exact = D.exact_min_plays(hand)
    assert exact.n_plays == 1 and exact.patterns[0].type == P.STRAIGHT


# --------------------------------------------------------------------- 牌力与统计


def test_hand_strength_follows_the_plan_formula():
    hand = cards_of("小王 大王 2 2 2 2 AAA 3333 4 5 6 7 8")  # 20 张太长，去掉几张
    hand = cards_of("小王 大王 2 2 2 2 A A A 3 3 3 3 4 5 6 7")
    decomp = D.best_decomposition(hand)
    counts = C.rank_counts(hand)
    jokers = counts[13] + counts[14]
    straights = sum(1 for p in decomp.patterns if p.type == P.STRAIGHT)
    expected = (
        2.2 * jokers + 1.4 * counts[12] + 0.8 * counts[11]
        + 3.0 * len(decomp.bombs) + 4.0 * decomp.rockets
        + (17 - 3.2 * decomp.n_plays) + 0.5 * straights
    )
    assert D.hand_strength(hand, decomp) == pytest.approx(expected)


def test_hand_strength_puts_bombs_and_jokers_on_top():
    weak = D.hand_strength(cards_of("3 4 5 6 7 9 10 J Q K 4 6 8 9 J Q K"))
    strong = D.hand_strength(cards_of("小王 大王 2 2 2 2 A A 3 3 3 3 4 5 6 7"))
    assert strong > weak


def test_control_ranks_cover_jokers_twos_and_bombs():
    hand = cards_of("小王 2 3333 5 6")
    assert D.control_ranks(hand, bombs=(0,)) == (0, 12, 13)
    both = cards_of("小王 大王 5")
    assert D.control_ranks(both, rockets=1) == (14,)  # 王炸算一张控制牌，记在大王上


def test_n_plays_is_cached_and_agrees_with_the_decomposition():
    hand = cards_of("34567 88 999")
    assert D.n_plays(hand) == D.greedy_decompose(hand).n_plays
    assert D.n_plays(hand) == D.n_plays(list(hand))  # 缓存按点数多重集
