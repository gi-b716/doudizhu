"""记牌器与威胁提示：口径对齐客户端，威胁随局面出现/消失。"""

from __future__ import annotations

import copy
import random

import pytest

from ddz import cards as C
from ddz import counter
from ddz.state import PHASE_BIDDING, PHASE_PLAYING, from_platform_state
from gamegen import play_game


def playing_states(seed: int, **kwargs):
    return [s for s in play_game(random.Random(seed), **kwargs) if s["phase"] == PHASE_PLAYING]


def solo_state(seed: int, index: int = 3) -> dict:
    """以玩家视角（非管理员）取一份出牌中的 state：此时 ``hand`` 是唯一的手牌来源。"""
    state = copy.deepcopy(playing_states(seed, admin=False)[index])
    assert state["hands"] is None
    return state


def test_unseen_matches_the_client_counter_formula():
    """客户端：没见过 = 一副 54 张 − 我的手牌 − 所有已出的牌。"""
    for state in playing_states(3, admin=True):
        gs = from_platform_state(state)
        left = [4] * 13 + [1, 1]
        for entry in state["log"]:
            if entry["kind"] == "play":
                for card in entry["cards"]:
                    left[C.rank(card)] -= 1
        for card in gs.advise_hand:
            left[C.rank(card)] -= 1
        assert counter.unseen_ranks(gs) == left


def test_possible_bombs_are_ranks_with_four_unseen():
    gs = from_platform_state(playing_states(3, admin=True)[3])
    ranks = counter.unseen_ranks(gs)
    assert counter.possible_bombs(gs) == [r for r in range(13) if ranks[r] == 4]
    assert all(r < C.JOKER_RANK for r in counter.possible_bombs(gs))


def test_rocket_possible_only_when_both_jokers_are_unseen():
    state = solo_state(3)
    gs = from_platform_state(state)
    expected = bool(gs.unseen_ranks[C.JOKER_RANK] and gs.unseen_ranks[C.JOKER_RANK + 1])
    assert counter.rocket_possible(gs) is expected

    # 把两张王塞进自己手里 → 不可能再有王炸
    hand = list(state["hand"])[:15] + [C.JOKER_SMALL, C.JOKER_BIG]
    state["hand"] = hand
    mine = from_platform_state(state)
    assert counter.rocket_possible(mine) is False


def test_no_threats_during_bidding():
    bidding = [s for s in play_game(random.Random(3)) if s["phase"] == PHASE_BIDDING]
    assert bidding
    assert counter.threats(from_platform_state(bidding[-1])) == []


def test_opponent_low_is_urgent_at_one_card():
    state = solo_state(3)
    state["players"][1]["count"] = 1
    gs = from_platform_state(state).for_seat(0)
    low = [t for t in counter.threats(gs) if t.code == "opponent_low"]
    assert low and low[0].level == "danger"
    assert "必须拦" in low[0].text

    state["players"][1]["count"] = 2
    gs = from_platform_state(state).for_seat(0)
    low = [t for t in counter.threats(gs) if t.code == "opponent_low"]
    assert low and low[0].level == "warn"


def test_partner_close_is_an_info_from_the_farmer_side():
    state = solo_state(3)
    landlord = state["landlord"]
    farmer = next(s for s in (0, 1, 2) if s != landlord)
    partner = next(s for s in (0, 1, 2) if s not in (landlord, farmer))
    state["players"][partner]["count"] = 1
    gs = from_platform_state(state).for_seat(farmer)
    close = [t for t in counter.threats(gs) if t.code == "partner_close"]
    assert close and close[0].level == "info"
    # 地主视角没有"队友"
    landlord_view = from_platform_state(state).for_seat(landlord)
    assert not [t for t in counter.threats(landlord_view) if t.code == "partner_close"]


def test_possible_bomb_and_rocket_threats_fire_together():
    state = solo_state(3)
    seat = 0
    state["hand"] = list(C.full_deck()[0:17])  # 干净局面：手上没有王，也没出过牌
    state["players"][seat]["count"] = 17
    gs = from_platform_state(state).for_seat(seat)
    bombs = counter.possible_bombs(gs)
    assert bombs, "造的局面里应当有整点数未出现"
    codes = {t.code for t in counter.threats(gs)}
    assert "possible_bomb" in codes
    if counter.rocket_possible(gs):
        assert "rocket_possible" in codes


def test_top_card_pressed_counts_bigger_unseen_cards():
    state = solo_state(3)
    state["hand"] = [24, 25, 26, 27]  # 全是 9，最大点数 6
    gs = from_platform_state(state).for_seat(0)
    pressed = counter.top_rank_pressed(gs)
    assert pressed == sum(gs.unseen_ranks[7:])
    assert pressed > 0


def test_top_card_pressed_is_zero_when_holding_both_jokers():
    state = solo_state(3)
    state["hand"] = [C.JOKER_SMALL, C.JOKER_BIG, 0, 1]
    gs = from_platform_state(state).for_seat(0)
    assert counter.top_rank_pressed(gs) == 0


def test_threats_are_sorted_by_severity():
    state = solo_state(3)
    state["hand"] = list(C.full_deck()[0:17])
    state["players"][1]["count"] = 1
    gs = from_platform_state(state).for_seat(0)
    levels = [t.level for t in counter.threats(gs)]
    assert levels == sorted(levels, key=lambda x: {"danger": 0, "warn": 1, "info": 2}[x])
    assert gs.threat_strings == [t.text for t in counter.threats(gs)]


def test_counter_snapshot_shape():
    gs = from_platform_state(playing_states(3, admin=True)[3])
    snapshot = counter.counter_snapshot(gs)
    assert set(snapshot) == {"unseen_by_rank", "unseen_total", "possible_bombs", "rocket_possible"}
    assert len(snapshot["unseen_by_rank"]) == C.N_RANKS
    assert snapshot["unseen_total"] == sum(snapshot["unseen_by_rank"])


def test_unseen_ranks_sum_shrinks_as_cards_are_played():
    totals = [sum(counter.unseen_ranks(from_platform_state(s))) for s in playing_states(3, admin=True)]
    assert totals == sorted(totals, reverse=True)
    assert totals[-1] < totals[0]
