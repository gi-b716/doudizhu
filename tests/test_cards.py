"""牌编码：与平台客户端（src/doudizhu.js）一致，并核对 DouZero 牌码表。"""

from __future__ import annotations

import pytest

from ddz import cards as C
from ddz.cards import (
    DECK_SIZE, JOKER_BIG, JOKER_RANK, JOKER_SMALL, MAX_CHAIN_RANK, N_RANKS,
    RANK_NAMES, card_name, env_card_to_rank, format_cards, from_env_card,
    full_deck, is_joker, parse_cards, rank, rank_counts, rank_name, ranks_of,
    sort_cards, suit, to_env_card,
)


def test_deck_is_54_distinct_cards():
    deck = full_deck()
    assert len(deck) == DECK_SIZE == 54
    assert len(set(deck)) == 54
    assert deck[0] == 0 and deck[-1] == 53


def test_rank_and_suit_match_platform_encoding():
    # id = rank * 4 + suit，rank 0..12 = 3,4,…,K,A,2 —— 与客户端完全一致
    assert rank(0) == 0 and suit(0) == 0
    assert rank(51) == 12 and suit(51) == 3  # ♦2
    assert [rank(c) for c in range(0, 52, 4)] == list(range(13))
    assert [suit(c) for c in range(4)] == [0, 1, 2, 3]


def test_jokers_get_distinct_ranks():
    assert rank(JOKER_SMALL) == JOKER_RANK == 13
    assert rank(JOKER_BIG) == JOKER_RANK + 1 == 14
    assert is_joker(JOKER_SMALL) and is_joker(JOKER_BIG)
    assert not is_joker(51)


def test_card_names_match_client_format():
    assert card_name(0) == "♠3"
    assert card_name(51) == "♦2"
    assert card_name(JOKER_SMALL) == "小王"
    assert card_name(JOKER_BIG) == "大王"
    assert RANK_NAMES == ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2", "小王", "大王")
    assert rank_name(MAX_CHAIN_RANK) == "A"


def test_rank_distribution_of_a_full_deck():
    counts = rank_counts(full_deck())
    assert len(counts) == N_RANKS == 15
    assert counts[:13] == [4] * 13
    assert counts[13:] == [1, 1]


def test_sort_cards_orders_by_rank_then_suit_jokers_last():
    hand = [JOKER_BIG, 1, JOKER_SMALL, 0, 2, 51, 48]
    assert sort_cards(hand) == [0, 1, 2, 48, 51, JOKER_SMALL, JOKER_BIG]
    assert ranks_of(sort_cards(hand)) == [0, 0, 0, 12, 12, 13, 14]


def test_parse_and_format_cards_round_trip():
    assert parse_cards("3,7,11") == [3, 7, 11]
    assert parse_cards("") == []
    assert format_cards([11, 3, 7]) == "3,7,11"  # 客户端提交前会 sortCards
    assert parse_cards(format_cards([51, 0, 53])) == [0, 51, 53]


@pytest.mark.parametrize("bad", ["0,0", "54", "-1", "1,1"])
def test_parse_cards_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_cards(bad)


def test_env_card_codes_cover_every_rank_once():
    # 3..14 = 3..A、17 = '2'、20 = 小王、30 = 大王（15/16 留空隔断连续性）
    codes = [to_env_card(C.first_card_of_rank(r)) for r in range(N_RANKS)]
    assert codes == list(range(3, 15)) + [17, 20, 30]
    assert len(set(codes)) == N_RANKS
    for r, code in enumerate(codes):
        assert env_card_to_rank(code) == r
        assert rank(from_env_card(code)) == r


def test_env_codes_break_chains_at_two_and_jokers():
    # 上游靠「点数差 1」判断顺子，所以 '2' 与王必须与相邻点数不连续
    assert to_env_card(C.first_card_of_rank(MAX_CHAIN_RANK)) + 1 < to_env_card(C.first_card_of_rank(12))
    assert to_env_card(C.first_card_of_rank(12)) + 1 < to_env_card(C.first_card_of_rank(13))
    assert to_env_card(C.first_card_of_rank(13)) + 1 < to_env_card(C.first_card_of_rank(14))


def test_env_codes_reject_unknown_values():
    for bad in (0, 1, 2, 15, 16, 18, 19, 21, 29, 31, 99):
        with pytest.raises(ValueError):
            env_card_to_rank(bad)


def test_platform_id_is_douzero_vector_index():
    """上游观测把牌点摊平进 4×13 矩阵（Fortran 序）+ 2 个王位，下标恰为平台牌 id。"""
    for card in range(52):
        assert suit(card) + 4 * rank(card) == card
    # 两张王是矩阵之后的两个王位，正好是 52 / 53
    assert (JOKER_SMALL, JOKER_BIG) == (52, 53)
