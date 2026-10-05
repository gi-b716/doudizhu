"""平台 state 适配：字段、边界（旁观 / unchanged / 流局 / 叫分合法集 / pattern 反推）。"""

from __future__ import annotations

import copy
import random

import pytest

from ddz import cards as C
from ddz import patterns as P
from ddz.state import (
    PHASE_BIDDING, PHASE_FINISHED, PHASE_PLAYING, GameState, StateParseError,
    apply_unchanged, from_platform_state, parse_platform_pattern, split_by_deal,
)
from gamegen import js_pattern, play_game

BID_KINDS = {"bid", "landlord", "play", "pass", "redeal", "auto_on", "auto_off", "finish"}


def game(seed: int, **kwargs) -> list[dict]:
    return play_game(random.Random(seed), **kwargs)


def test_every_state_of_a_random_game_parses():
    states = game(3)
    parsed = [from_platform_state(s) for s in states]
    assert [p.version for p in parsed] == [s["version"] for s in states]
    assert parsed[0].phase == PHASE_BIDDING
    assert parsed[-1].phase == PHASE_FINISHED
    assert any(p.phase == PHASE_PLAYING for p in parsed)


def test_card_conservation_across_a_whole_game():
    """出牌阶段任意时刻：三家手牌 + 已出牌 = 54 张，且互不重叠（底牌已并入地主手牌）。"""
    checked = 0
    for state in game(5, admin=True):
        gs = from_platform_state(state)
        if gs.phase == PHASE_BIDDING or gs.all_hands is None:
            continue
        hands = [c for h in gs.all_hands for c in h]
        played = list(gs.played_cards)
        assert len(set(hands)) == len(hands), "三家手牌里有重复的牌"
        assert not (set(hands) & set(played)), "同一张牌既在手牌又已出"
        assert len(hands) + len(played) == 54
        checked += 1
    assert checked > 10


def test_leading_and_to_beat_match_the_log():
    for state in game(7):
        gs = from_platform_state(state)
        if gs.phase != PHASE_PLAYING:
            assert gs.to_beat is None
            continue
        assert gs.leading == state["leading"]
        if gs.leading:
            assert gs.to_beat is None
        else:
            assert gs.to_beat is not None
            assert gs.last is not None and gs.last.seat != gs.turn


def test_players_roles_and_counts():
    final = from_platform_state(game(3)[-1])
    assert [p.role for p in final.players].count("landlord") == 1
    assert final.landlord in (0, 1, 2)
    assert final.players[final.landlord].role == "landlord"
    assert final.is_landlord is (final.seat == final.landlord)


def test_partner_seat_is_the_other_farmer():
    states = game(3)
    for state in states:
        gs = from_platform_state(state)
        if gs.landlord is None or gs.seat is None or gs.seat == gs.landlord:
            assert gs.partner_seat is None
        else:
            partner = gs.partner_seat
            assert partner is not None and partner != gs.seat and partner != gs.landlord


def test_spectator_has_no_hand_but_can_advise_for_the_turn_seat():
    states = game(11, seat=None, admin=True)
    for state in states:
        gs = from_platform_state(state)
        assert gs.seat is None and gs.hand is None
        assert gs.my_turn is False
        if gs.turn is not None and gs.phase != PHASE_FINISHED:
            assert gs.advise_seat == gs.turn
            assert gs.advise_hand == gs.all_hands[gs.turn]
            assert gs.is_advise_turn is True


def test_spectator_without_admin_hands_cannot_advise():
    """非管理员的旁观者拿不到三家手牌（终局除外——那时 hands 对所有人公开）。"""
    playing = [s for s in game(11, seat=None, admin=False) if s["phase"] == PHASE_PLAYING]
    assert playing
    gs = from_platform_state(playing[len(playing) // 2])
    assert gs.all_hands is None
    assert gs.hand is None
    assert gs.advise_seat is None
    assert gs.advise_hand is None
    # 终局确实会对旁观者公开全部手牌
    assert from_platform_state(game(11, seat=None, admin=False)[-1]).all_hands is not None


def test_finished_state_exposes_all_hands_and_deals():
    final = from_platform_state(game(3)[-1])
    assert final.all_hands is not None and len(final.all_hands) == 3
    assert final.fairness and final.fairness["deals"]
    assert final.result and final.result["finished"] is True


def test_unchanged_only_refreshes_the_clock():
    first = from_platform_state(game(3)[0])
    refreshed = from_platform_state({"unchanged": True, "remaining": 4321, "reserve_in_remaining": 1000}, prev=first)
    assert refreshed.remaining_ms == 4321
    assert refreshed.reserve_in_remaining == 1000
    assert refreshed.version == first.version
    assert refreshed.players == first.players
    # 没写出来的字段沿用旧值
    kept = apply_unchanged(first, {"unchanged": True})
    assert kept.remaining_ms == first.remaining_ms


def test_unchanged_without_previous_state_is_an_error():
    with pytest.raises(StateParseError):
        from_platform_state({"unchanged": True, "remaining": 100})


def test_payload_wrapper_is_unwrapped():
    state = game(3)[5]
    assert from_platform_state({"state": state}).version == state["version"]


@pytest.mark.parametrize("bad", [{"version": 1}, {"phase": "waiting", "version": 1}, [], "nope"])
def test_unusable_states_raise(bad):
    with pytest.raises(StateParseError):
        from_platform_state(bad)


# --------------------------------------------------------------------- 叫分


def test_legal_bids_follow_the_current_high_bid():
    state = game(3)[2]
    state = copy.deepcopy(state)
    state["phase"] = PHASE_BIDDING
    for high, expected in ((0, [0, 1, 2, 3]), (1, [0, 2, 3]), (2, [0, 3]), (3, [0])):
        state["bid"] = high
        state["must_bid"] = False
        assert from_platform_state(state).legal_bids == expected
        state["must_bid"] = True
        assert from_platform_state(state).legal_bids == [v for v in expected if v]


def test_must_bid_after_three_redeals():
    states = game(2, pass_bias=0.95)
    forced = [s for s in states if s["must_bid"]]
    assert forced, "这局没有逼出 must_bid"
    for state in forced:
        gs = from_platform_state(state)
        assert gs.redeals >= 3
        assert 0 not in gs.legal_bids


def test_redeal_truncates_events_and_bumps_deal_index():
    states = game(2, pass_bias=0.95)
    redeals_seen = 0
    for state in states:
        gs = from_platform_state(state)
        events, deal_index = split_by_deal(state["log"])
        assert gs.deal_index == deal_index == sum(1 for e in state["log"] if e["kind"] == "redeal")
        assert gs.events == events
        assert not any(e["kind"] == "redeal" for e in gs.events)
        redeals_seen = max(redeals_seen, gs.deal_index)
    assert redeals_seen >= 3  # 这局是为了逼出流局才生成的


# --------------------------------------------------------------------- pattern 反推


def test_generated_platform_patterns_are_unrecognized_and_derived_from_cards():
    """生成器发的数字型 pattern 我们认不出 → 一律从牌面反推（真实抓包大概率如此）。"""
    state = next(s for s in game(3) if s["phase"] == PHASE_PLAYING and s["last"])
    gs = from_platform_state(state)
    assert gs.last is not None
    assert gs.last.pattern_source == "derived"
    assert gs.last.cards
    assert gs.last.pattern.key() in {p.key() for p in P.classify(gs.last.cards)}


def test_recognized_platform_pattern_is_used():
    state = copy.deepcopy(next(s for s in game(3) if s["phase"] == PHASE_PLAYING and s["last"]))
    cards = state["last"].get("cards")
    if cards is None:  # last 只有 {seat, pattern}，从 log 里取牌面
        seat = state["last"]["seat"]
        entry = next(e for e in reversed(state["log"]) if e["kind"] == "play" and e["seat"] == seat)
        cards = entry["cards"]
    pattern = P.classify(cards)[0]
    state["last"]["pattern"] = pattern.key()  # 平台若恰好用这种字符串，我们就直接用
    gs = from_platform_state(state)
    assert gs.last.pattern_source == "platform"
    assert gs.last.pattern.key() == pattern.key()


def test_recognized_dict_pattern_with_string_type():
    state = copy.deepcopy(next(s for s in game(3) if s["phase"] == PHASE_PLAYING and s["last"]))
    seat = state["last"]["seat"]
    entry = next(e for e in reversed(state["log"]) if e["kind"] == "play" and e["seat"] == seat)
    pattern = P.classify(entry["cards"])[0]
    state["last"]["pattern"] = {"type": pattern.type, "rank": pattern.main_rank, "len": pattern.length}
    gs = from_platform_state(state)
    assert gs.last.pattern_source == "platform"
    assert gs.last.pattern == pattern


def test_contradictory_platform_pattern_falls_back_to_the_cards():
    state = copy.deepcopy(next(s for s in game(3) if s["phase"] == PHASE_PLAYING and s["last"]))
    state["last"]["pattern"] = "rocket:14:1"  # 与牌面不符 → 丢掉平台说法
    gs = from_platform_state(state)
    assert gs.last.pattern_source == "derived"


def test_unclassifiable_last_cards_raise():
    state = copy.deepcopy(next(s for s in game(3) if s["phase"] == PHASE_PLAYING and s["last"]))
    seat = state["last"]["seat"]
    # 换一手打不出去的牌：3 3 4 4
    entry = next(e for e in reversed(state["log"]) if e["kind"] == "play" and e["seat"] == seat)
    entry["cards"] = [0, 1, 4, 5]
    with pytest.raises(StateParseError):
        from_platform_state(state)


def test_last_without_a_matching_log_entry_raises():
    state = copy.deepcopy(next(s for s in game(3) if s["phase"] == PHASE_PLAYING and s["last"]))
    state["log"] = [e for e in state["log"] if e["kind"] != "play"]
    with pytest.raises(StateParseError):
        from_platform_state(state)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("triple:9:1", "triple:9:1"),
        ({"type": "straight", "rank": 2, "len": 5}, "straight:2:5"),
        ({"kind": "plane_single", "main": 3, "length": 2}, "plane_single:3:2"),
        ({"type": "rocket"}, "rocket:14:1"),
        ("nonsense", None),
        ({"type": 4, "rank": 9}, None),          # 数值牌型码：认不出
        ({"type": "triple"}, None),              # 缺点数
        ({"type": "unknown_type", "rank": 1}, None),
        (None, None),
    ],
)
def test_parse_platform_pattern(raw, expected):
    parsed = parse_platform_pattern(raw)
    assert (parsed.key() if parsed else None) == expected


def test_pattern_object_from_our_own_engine_round_trips():
    pattern = P.Pattern("plane_pair", 4, 3)
    assert parse_platform_pattern(pattern.key()) == pattern


# --------------------------------------------------------------------- 视角


def test_for_seat_switches_hand_and_seat():
    gs = from_platform_state(game(3, admin=True)[-1])
    other = gs.for_seat(1)
    assert other.seat == 1 and other.hand == gs.all_hands[1]
    assert gs.for_seat(gs.seat) is gs


def test_hand_for_uses_all_hands_when_available():
    gs = from_platform_state(game(3, admin=True)[-1])
    for seat in (0, 1, 2):
        assert gs.hand_for(seat) == gs.all_hands[seat]


def test_unseen_is_the_deck_minus_hand_minus_played():
    for state in game(9):
        gs = from_platform_state(state)
        if gs.phase == PHASE_BIDDING or gs.advise_hand is None:
            continue
        expected = sorted(set(C.full_deck()) - set(gs.advise_hand) - set(gs.played_cards))
        assert list(gs.unseen) == expected
        assert sum(gs.unseen_ranks) == len(expected)


def test_clock_fields_pass_through():
    state = game(3)[1]
    gs = from_platform_state(state)
    assert gs.remaining_ms == state["remaining"]
    assert gs.reserve_in_remaining == state["reserve_in_remaining"]
    assert gs.reserve == tuple(state["reserve"])
    assert gs.started == state["started"]
    assert gs.raw["version"] == state["version"]
