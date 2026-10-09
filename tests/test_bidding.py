"""叫分：牌力公式、阈值分档、合法集夹取、必须叫分的理由。"""

from __future__ import annotations

import pytest

import random

from ddz.engine.bidding import THRESHOLDS, BidThresholds, bid_for, strength_of
from ddz.engine.heuristic import HeuristicAgent
from ddz.state import PHASE_BIDDING
from helpers import cards_of, game_state, random_hand

#: 这三手牌的牌力分（实测）：+21.1 / +4.3 / −5.1。STRONG 过 10 分门槛；MEDIUM 在旧阈值下
#: 会叫 2 分（而叫 2 分的地主实测只赢 39%），现在不叫；WEAK 不叫。
STRONG = "小王 大王 2 2 2 A 3 3 3 3 4 5 6 7 8 9"           # 王炸 + 三个 2 + 炸弹
MEDIUM = "小王 大王 3 3 4 5 5 6 7 7 8 9 10 10 J Q A"        # 中等：一对王但没有 2
WEAK = "3 3 4 5 5 7 8 8 9 10 J J Q Q K A 小王"              # 弱：全散牌，手数多


def expected_level(strength: float) -> int:
    one, two, three = THRESHOLDS
    return 3 if strength >= three else 2 if strength >= two else 1 if strength >= one else 0


def test_strength_ordering():
    assert strength_of(cards_of(STRONG)) > strength_of(cards_of(MEDIUM)) > strength_of(cards_of(WEAK))


def test_bid_levels_track_the_thresholds():
    assert bid_for(cards_of(STRONG)).value == 3
    assert bid_for(cards_of(MEDIUM)).value == 0   # 回归：旧阈值会在这里叫 2 分（胜率 39%，负期望）
    assert bid_for(cards_of(WEAK)).value == 0
    # 阈值与档位一一对应（换阈值就该换档）
    for spec in (STRONG, MEDIUM, WEAK):
        hand = cards_of(spec)
        assert bid_for(hand).value == expected_level(strength_of(hand))


def test_default_thresholds_are_equal_so_no_voluntary_one_or_two():
    """1、2 分的期望收益为负（实测地主胜率 34% / 39%），而收益正比于叫分，所以 1、2 分没有保留的理由。"""
    assert len(set(THRESHOLDS)) == 1, "三档阈值应当相同：1、2 分不能比 3 分更容易叫"


def test_default_policy_only_bids_three_or_passes():
    rng = random.Random(20261009)
    seen = set()
    for _ in range(400):
        hand = random_hand(rng, 17)
        bid = bid_for(hand, legal=[0, 1, 2, 3])
        seen.add(bid.value)
        assert bid.value in (0, 3), f"自愿叫分只能是 3 或不叫，却叫了 {bid.value}"
        assert (bid.value == 3) == (bid.strength >= THRESHOLDS[2])
    assert seen == {0, 3}, "随机牌里应当既有叫 3 的，也有不叫的，否则这个测试没在测什么"


def test_thresholds_are_configurable():
    stingy = BidThresholds(one=99, two=100, three=101)
    assert bid_for(cards_of(STRONG), thresholds=stingy).value == 0
    generous = BidThresholds(one=-99, two=-98, three=-97)
    assert bid_for(cards_of(WEAK), thresholds=generous).value == 3
    assert THRESHOLDS == (BidThresholds().one, BidThresholds().two, BidThresholds().three)


def test_legal_set_is_respected():
    strong = cards_of(STRONG)
    # 已经有人叫了 2 分：只能叫 3 分或不叫，不能叫 1/2
    assert bid_for(strong, high_bid=2, legal=[0, 3]).value == 3
    # 有人叫了 1 分，牌力只够 1 分 → 不硬上 2 分，直接不叫
    weak = cards_of(WEAK)
    assert strength_of(weak) < THRESHOLDS[1]
    assert bid_for(weak, high_bid=1, legal=[0, 2, 3]).value == 0
    # 叫满 3 分后只剩"不叫"
    assert bid_for(strong, high_bid=3, legal=[0]).value == 0


def test_mid_strength_hand_passes_and_says_why():
    bid = bid_for(cards_of(MEDIUM))
    assert bid.value == 0 and "不叫" in bid.reasons[0]


def test_must_bid_forces_at_least_one():
    weak = cards_of(WEAK)
    assert bid_for(weak).value == 0  # 平时是不叫的
    forced = bid_for(weak, high_bid=0, must_bid=True, legal=[1, 2, 3])
    assert forced.value == 1
    assert "必须叫分" in forced.reasons[0]
    assert "流局" in forced.reasons[0]


def test_must_bid_never_passes_even_with_a_high_bid():
    forced = bid_for(cards_of(WEAK), high_bid=2, must_bid=True, legal=[3])
    assert forced.value == 3


def test_reasons_mention_the_key_facts():
    bid = bid_for(cards_of(STRONG))
    text = bid.reasons[0]
    assert "王炸" in text and "2" in text and "炸弹" in text
    assert f"叫 {bid.value} 分" in text
    assert f"{bid.strength:.1f}" in text


def test_weak_hand_reason_explains_the_pass():
    bid = bid_for(cards_of(WEAK))
    assert bid.value == 0 and "不叫" in bid.reasons[0]


def test_empty_hand_cannot_bid():
    bid = bid_for([])
    assert bid.value == 0 and bid.reasons


def test_agent_bid_uses_the_state():
    agent = HeuristicAgent()
    state = game_state(cards_of(STRONG), phase=PHASE_BIDDING, turn=0, landlord=None)
    state.high_bid = 0
    state.must_bid = False
    assert agent.bid(state).value == 3

    state.high_bid = 3
    assert agent.bid(state).value == 0  # 合法集只剩"不叫"

    state.high_bid = 0
    state.must_bid = True
    assert agent.bid(state).value >= 1


def test_agent_bid_without_a_hand_is_safe():
    from ddz.state import GameState

    assert HeuristicAgent().bid(GameState(seat=None, hand=None)).value == 0


# --------------------------------------------------------------------- 门槛可配置（竞技场 A/B 用）


def test_parse_thresholds_takes_one_number_or_three():
    from ddz.engine.bidding import parse_thresholds

    same = parse_thresholds("10")
    assert same == BidThresholds(10.0, 10.0, 10.0)
    assert parse_thresholds(" 6, 8 ,10 ") == BidThresholds(6.0, 8.0, 10.0)
    with pytest.raises(ValueError):
        parse_thresholds("6,8")


def test_heuristic_agent_bids_with_its_own_thresholds():
    """同一手弱牌：默认不叫；门槛调到很低就叫 3 分——证明门槛确实从 agent 传到了 bid_for。"""
    state = game_state(cards_of(WEAK), phase=PHASE_BIDDING, turn=0, landlord=None)
    state.high_bid = 0
    state.must_bid = False
    assert HeuristicAgent().bid(state).value == 0
    generous = HeuristicAgent(bid_thresholds=(-99.0, -99.0, -99.0))
    assert generous.bid(state).value == 3
    assert generous.bid_thresholds == BidThresholds(-99.0, -99.0, -99.0)
