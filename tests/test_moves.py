"""合法着法生成：完备性（≤10 张手牌与暴力枚举对照）、自洽性、canonical 收敛、硬规则。"""

from __future__ import annotations

import itertools
import random

import pytest

from ddz import cards as C
from ddz import moves as M
from ddz import patterns as P
from helpers import cards_of, move_rank_keys, random_hand, rank_multiset

FOLLOWS = [
    P.Pattern("single", 5),
    P.Pattern("pair", 3),
    P.Pattern("triple", 2),
    P.Pattern("triple_single", 1),
    P.Pattern("triple_pair", 1),
    P.Pattern("straight", 2, 5),
    P.Pattern("pair_straight", 0, 3),
    P.Pattern("plane", 0, 2),
    P.Pattern("plane_single", 0, 2),
    P.Pattern("plane_pair", 0, 2),
    P.Pattern("four_two", 0),
    P.Pattern("four_two_pair", 0),
    P.Pattern("bomb", 5),
    P.Pattern("rocket", 14),
]


def brute_force(cards, to_beat):
    """全部子集 × 全部读法，降到「点数多重集 + 牌型」层面。"""
    out = set()
    for size in range(1, len(cards) + 1):
        for subset in itertools.combinations(cards, size):
            for pattern in P.classify(subset):
                if to_beat is None or P.beats(pattern, to_beat):
                    out.add((rank_multiset(subset), pattern.key()))
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_leads_match_brute_force_on_small_hands(seed):
    rng = random.Random(seed)
    for _ in range(6):
        hand = random_hand(rng, 9)  # 无炸弹，生成口径与合法性口径完全一致
        got = move_rank_keys(M.legal_moves(hand, None, canonical=False))
        assert got == brute_force(hand, None)


@pytest.mark.parametrize("seed", [11, 12])
def test_beats_match_brute_force_on_small_hands(seed):
    rng = random.Random(seed)
    for _ in range(3):
        hand = random_hand(rng, 10)
        for to_beat in FOLLOWS:
            got = move_rank_keys(M.legal_moves(hand, to_beat, canonical=False))
            assert got == brute_force(hand, to_beat), f"{to_beat.key()} @ {hand}"


@pytest.mark.parametrize("seed", [61, 62])
def test_generated_moves_are_legal_even_with_bombs(seed):
    """带炸弹的手牌：生成集必须是合法集的子集（我们只是不拆炸弹当翅膀，绝不生成非法牌）。"""
    rng = random.Random(seed)
    for _ in range(4):
        hand = random_hand(rng, 10, max_count=4)
        for to_beat in [None, *FOLLOWS]:
            got = move_rank_keys(M.legal_moves(hand, to_beat, canonical=False))
            assert got <= brute_force(hand, to_beat)


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_every_move_reclassifies_to_its_own_pattern(seed):
    rng = random.Random(seed)
    for _ in range(5):
        hand = random_hand(rng, 17, max_count=4)
        for to_beat in [None, *FOLLOWS]:
            for move in M.legal_moves(hand, to_beat, canonical=False):
                keys = {p.key() for p in P.classify(move.cards)}
                assert move.pattern.key() in keys
                assert set(move.cards) <= set(hand)
                if to_beat is not None:
                    assert P.beats(move.pattern, to_beat)


@pytest.mark.parametrize("seed", [31, 32])
def test_legal_beats_is_exactly_legal_moves_following(seed):
    """跟牌时 legal_moves 不给「不出」——不出由调用方（advisor）另行补上。"""
    rng = random.Random(seed)
    for _ in range(5):
        hand = random_hand(rng, 14)
        for to_beat in FOLLOWS:
            full = move_rank_keys(M.legal_moves(hand, to_beat, canonical=False))
            beats = move_rank_keys(M.legal_beats(hand, to_beat, canonical=False))
            assert full == beats
            assert all(m.cards for m in M.legal_beats(hand, to_beat, canonical=False))


@pytest.mark.parametrize("seed", [41, 42])
def test_canonical_keeps_one_move_per_identity(seed):
    rng = random.Random(seed)
    for _ in range(5):
        hand = random_hand(rng, 17)
        for to_beat in [None, *FOLLOWS]:
            full = M.legal_moves(hand, to_beat, canonical=False)
            lean = M.legal_moves(hand, to_beat, canonical=True)
            assert len({m.pattern.key() for m in lean}) == len(lean)
            assert {m.pattern.key() for m in lean} == {m.pattern.key() for m in full}
            lean_rank_keys = move_rank_keys(lean)
            assert lean_rank_keys <= move_rank_keys(full)
            for move in lean:
                assert set(move.cards) <= set(hand)


def test_canonical_prefers_cheap_wings():
    # 333444 的翅膀：{6,7} 是两张散牌（免费），{5,5} 要消耗一个对子——canonical 取散牌
    hand = cards_of("333444 5 5 6 7")
    lean = M.legal_leads(hand, canonical=True)
    plane_single = next(m for m in lean if m.pattern.key() == "plane_single:0:2")
    assert rank_multiset(plane_single.cards) == (0, 0, 0, 1, 1, 1, 3, 4)


def test_canonical_wings_never_split_a_triple_when_a_single_exists():
    hand = cards_of("333 444 5 6 6 6")
    lean = M.legal_leads(hand, canonical=True)
    # 三带一：宁可带散牌 5，也不拆 666
    triple_single = next(m for m in lean if m.pattern.key() == "triple_single:0:1")
    assert rank_multiset(triple_single.cards) == (0, 0, 0, 2)


def test_following_is_generated_by_type_not_by_subsets():
    hand = cards_of("3456789 10 33 44 小王")
    to_beat = P.Pattern("straight", 0, 5)
    moves = M.legal_moves(hand, to_beat, canonical=False)
    kinds = {m.pattern.key() for m in moves}
    # 只生成同长度的更大顺子：45678 / 56789 / 6789 10（手牌里没有炸弹与王炸）
    assert kinds == {"straight:1:5", "straight:2:5", "straight:3:5"}
    assert all(len(m.cards) == 5 for m in moves)


def test_nothing_beats_rocket():
    hand = cards_of("3333 2222 小王 大王")
    rocket = P.Pattern("rocket", 14)
    assert M.legal_beats(hand, rocket, canonical=False) == []
    assert M.has_any_beat(hand, rocket) is False


def test_bombs_and_rocket_beat_any_non_bomb():
    hand = cards_of("3333 小王 大王 5")
    to_beat = P.Pattern("straight", 0, 5)
    kinds = {m.pattern.key() for m in M.legal_beats(hand, to_beat, canonical=False)}
    assert kinds == {"bomb:0:1", "rocket:14:1"}


def test_bombs_beat_bombs_only_by_rank():
    hand = cards_of("3333 7777")
    # 对手出 6666（点数 3），手里只有 7777（点数 4）压得过
    kinds = {m.pattern.key() for m in M.legal_beats(hand, P.Pattern("bomb", 3), canonical=False)}
    assert kinds == {"bomb:4:1"}
    assert M.legal_beats(hand, P.Pattern("bomb", 5), canonical=False) == []


def test_wings_never_split_a_bomb():
    """555 带 6 可以，555 带 3（拆炸弹）不生成——翅膀点数池排除四张同点。"""
    hand = cards_of("3333 555 6")
    to_beat = P.Pattern("triple_single", 0)
    moves = M.legal_beats(hand, to_beat, canonical=False)
    kinds = {m.pattern.key() for m in moves}
    assert kinds == {"triple_single:2:1", "bomb:0:1"}
    for move in moves:
        if move.pattern.type == P.TRIPLE_SINGLE:
            assert rank_multiset(move.cards) == (2, 2, 2, 3)  # 555 + 6
    # 「555 带 3」本身是合法读法，只是我们不生成——生成口径与合法性口径的差别
    assert P.classify_one(cards_of("3555")).key() == "triple_single:2:1"


def test_three_with_one_cannot_beat_a_plain_triple():
    """rules.md：须出同牌型、同张数——三带一压不了三张，反之亦然。"""
    hand = cards_of("444 5 6")
    kinds = {m.pattern.key() for m in M.legal_beats(hand, P.Pattern("triple", 0), canonical=False)}
    assert kinds == {"triple:1:1"}
    kinds = {m.pattern.key() for m in M.legal_beats(hand, P.Pattern("triple_single", 0), canonical=False)}
    assert kinds == {"triple_single:1:1"}


def test_jokers_never_both_used_as_wings():
    """两张王只能组成王炸，不能一起当翅膀（带牌不含王炸）。"""
    hand = cards_of("333444 小王 大王 7")
    jokers = {C.JOKER_SMALL, C.JOKER_BIG}
    moves = M.legal_leads(hand, canonical=False)
    assert any(m.pattern.type == P.ROCKET for m in moves)
    for move in moves:
        if move.pattern.type != P.ROCKET:
            assert not jokers <= set(move.cards)


def test_four_two_wings_may_be_a_pair():
    hand = cards_of("3333 55")
    kinds = {m.pattern.key() for m in M.legal_leads(hand, canonical=False)}
    assert "four_two:0:1" in kinds
    move = next(m for m in M.legal_leads(hand, canonical=False) if m.pattern.key() == "four_two:0:1")
    assert rank_multiset(move.cards) == (0, 0, 0, 0, 2, 2)


def test_four_two_pair_requires_two_distinct_pairs():
    hand = cards_of("3333 55 66")
    kinds = {m.pattern.key() for m in M.legal_leads(hand, canonical=False)}
    assert "four_two_pair:0:1" in kinds
    assert "four_two:0:1" in kinds  # 同一手牌也可以是四带二（带一个对子）


def test_winning_move_when_hand_is_one_play():
    hand = cards_of("34567")
    move = M.winning_move(hand, None)
    assert move is not None and move.pattern.key() == "straight:0:5"
    assert M.winning_move(hand, P.Pattern("straight", 0, 5)) is None  # 压不过自己
    # 顺子压不了单张——牌型不同
    assert M.winning_move(hand, P.Pattern("single", 0)) is None
    assert M.winning_move(cards_of("大王"), P.Pattern("single", 0)).pattern.key() == "single:14:1"


def test_winning_move_prefers_a_reading_that_beats():
    hand = cards_of("333444555666")
    # 对手出的是 飞机带单 3 组：整手牌可读成 444555666 带 333（主体更大）
    move = M.winning_move(hand, P.Pattern("plane_single", 0, 3))
    assert move is not None and move.pattern.key() == "plane_single:1:3"
    # 对手出 飞机 2 组（6 张）时这手 12 张压不上——张数必须一致
    assert M.winning_move(hand, P.Pattern("plane", 0, 2)) is None


def test_winning_move_none_when_hand_cannot_be_played_at_once():
    assert M.winning_move(cards_of("33345"), None) is None
    assert M.winning_move(cards_of("333444555666 7777"), None) is None


def test_has_any_beat_matches_generation():
    rng = random.Random(7)
    for _ in range(20):
        hand = random_hand(rng, 13)
        for to_beat in FOLLOWS:
            expected = bool(M.legal_beats(hand, to_beat, canonical=False))
            assert M.has_any_beat(hand, to_beat) is expected


def test_limit_truncates_but_keeps_cheapest_prefix():
    hand = cards_of("3456789 10 JQKA 2345")  # 一堆散牌
    full = M.legal_leads(hand, canonical=False)
    limited = M.legal_leads(hand, canonical=False, limit=5)
    assert len(limited) == 5
    assert set(limited) <= set(full)


def test_pass_is_not_a_move():
    hand = cards_of("34567")
    assert all(m.cards for m in M.legal_moves(hand, None, canonical=False))
    assert all(m.cards for m in M.legal_moves(hand, P.Pattern("single", 9), canonical=False))


def test_empty_hand_has_no_moves():
    assert M.legal_moves([], None, canonical=False) == []
    assert M.legal_moves([], P.Pattern("single", 0), canonical=False) == []
    assert M.has_any_beat([], P.Pattern("single", 0)) is False


def test_move_key_is_stable_and_unique():
    moves = M.legal_leads(cards_of("3334 55"), canonical=False)
    keys = [m.key() for m in moves]
    assert len(keys) == len(set(keys))
    for move in moves:
        assert str(move.cards[0]) in move.key()


def test_move_keys_match_format_cards_order():
    hand = cards_of("3 5 7 9 小王")
    for move in M.legal_leads(hand, canonical=False):
        assert list(move.cards) == C.sort_cards(move.cards)
        assert move.key().endswith(C.format_cards(move.cards))
