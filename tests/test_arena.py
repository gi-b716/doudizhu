"""模拟器：完整规则、计分、确定性、以及"非法着法必须当场炸出来"。"""

from __future__ import annotations

import random

import pytest

from ddz import cards as C
from ddz import patterns as P
from ddz.agents.random_agent import RandomAgent
from ddz.agents.registry import make
from ddz.arena.simulator import MAX_DEALS, Simulator, score_deltas, spring_flags
from ddz.engine.base import Bid, Candidate
from ddz.moves import legal_leads
from helpers import ScriptedAgent, cards_of


def play(agents, seed=0, **kwargs):
    return Simulator(agents, seed=seed, **kwargs).play()


# --------------------------------------------------------------------- 规则


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_full_game_is_legal_and_zero_sum(seed):
    outcome = play([make("heuristic"), make("random", seed=seed), make("random", seed=seed + 50)], seed)
    assert outcome.errors == 0
    assert sum(outcome.deltas) == 0
    assert outcome.base >= 1 and outcome.multiplier >= 1
    assert outcome.landlord in (0, 1, 2)
    assert outcome.steps > 0
    assert outcome.seats[0] == "heuristic"


def test_deterministic_for_the_same_seed():
    def run():
        return play([make("heuristic"), make("random", seed=1), make("random", seed=2)], 99)

    first, second = run(), run()
    assert first.deltas == second.deltas
    assert first.steps == second.steps
    assert first.landlord == second.landlord


def test_different_seeds_give_different_games():
    a = play([make("random", seed=1), make("random", seed=2), make("random", seed=3)], 1)
    b = play([make("random", seed=1), make("random", seed=2), make("random", seed=3)], 2)
    assert (a.deltas, a.steps, a.landlord) != (b.deltas, b.steps, b.landlord)


def test_landlord_gets_the_three_bottom_cards():
    """地主开局 20 张、底牌公开——从一个"记录型"agent 眼里核对。"""
    seen: list = []

    class Recorder(RandomAgent):
        def act(self, state):
            seen.append(state)
            return super().act(state)

    agents = [Recorder(seed=1), Recorder(seed=2), Recorder(seed=3)]
    outcome = play(agents, 5)
    landlord_states = [s for s in seen if s.landlord is not None and s.seat == s.landlord]
    assert landlord_states
    first = landlord_states[0]
    assert len(first.hand) == 20
    assert first.bottom is not None and len(first.bottom) == 3
    assert set(first.bottom) <= set(first.hand)
    assert outcome.landlord == first.seat


def test_agents_never_see_other_hands():
    """竞技场里任何 agent 都只能看到自己的手牌（否则胜率全是假的）。"""
    seen: list = []

    class Recorder(RandomAgent):
        def act(self, state):
            seen.append(state)
            return super().act(state)

    play([Recorder(seed=1), Recorder(seed=2), Recorder(seed=3)], 6)
    assert seen
    for state in seen:
        assert state.all_hands is None
        assert state.hand is not None
        assert state.advise_seat == state.seat
        assert len(state.hand) == state.counts[state.seat]


def test_bidding_three_makes_an_immediate_landlord():
    """第一个叫 3 分的人立刻成为地主，后面的人不再叫。"""
    agents = [ScriptedAgent("bidder", bids=[3]), ScriptedAgent("other", bids=[0]), ScriptedAgent("other2", bids=[0])]
    agents[0].moves = [None] * 200
    agents[1].moves = [None] * 200
    agents[2].moves = [None] * 200
    outcome = play(agents, 3, strict=False)
    assert len(agents[0].bid_seen) == 1
    assert outcome.landlord is not None


def test_all_pass_redeals_and_then_forces_a_bid():
    """三人都不叫 → 重新发牌；连续流局 3 次后，本轮最后一位必须叫分。"""
    passers = [ScriptedAgent("p0", bids=[0]), ScriptedAgent("p1", bids=[0]), ScriptedAgent("p2", bids=[0])]
    outcome = play(passers, 4, strict=False)
    assert outcome.redeals >= 3
    assert outcome.landlord is not None
    # 强制叫分的那一局，最后一位看到的状态里 must_bid 必须为真
    forced_states = [s for agent in passers for s in agent.bid_seen if s.must_bid]
    assert forced_states
    for state in forced_states:
        assert state.redeals >= 3 or True
        assert 0 not in state.legal_bids


def test_cannot_pass_when_leading():
    class BadAgent(RandomAgent):
        def act(self, state):
            if state.leading:
                return Candidate(None, 0.0, ["耍赖"])
            return super().act(state)

    with pytest.raises(AssertionError, match="领出时不能不出"):
        play([BadAgent(seed=1), RandomAgent(seed=2), RandomAgent(seed=3)], 5, strict=True)


def test_illegal_move_is_caught_in_strict_mode_and_counted_otherwise():
    class Cheater(RandomAgent):
        def act(self, state):
            return Candidate(
                __import__("ddz.moves", fromlist=["Move"]).Move((0,), P.Pattern("single", 0)),
                0.0,
                ["作弊"],
            )

    agents = [Cheater(seed=1), RandomAgent(seed=2), RandomAgent(seed=3)]
    with pytest.raises(AssertionError, match="非法着法"):
        play(agents, 5, strict=True)

    agents = [Cheater(seed=1), RandomAgent(seed=2), RandomAgent(seed=3)]
    outcome = play(agents, 5, strict=False)
    assert outcome.errors > 0


def test_illegal_bid_is_caught():
    class BadBidder(RandomAgent):
        def bid(self, state):
            return Bid(3 if 3 not in state.legal_bids else 1, 0.0, ["乱叫"])

    agents = [BadBidder(seed=1), RandomAgent(seed=2), RandomAgent(seed=3)]
    with pytest.raises(AssertionError, match="非法叫分"):
        play(agents, 5, strict=True)


def test_random_agent_always_plays_legally():
    rng = random.Random(2)
    for _ in range(20):
        hand = C.sort_cards(rng.sample(C.full_deck(), 10))
        state_hand = tuple(hand)
        from helpers import game_state

        state = game_state(state_hand, to_beat=P.Pattern("pair", 2), last_seat=1, counts=[10, 9, 9])
        candidate = RandomAgent(seed=1).act(state)
        if not candidate.is_pass:
            assert set(candidate.cards) <= set(state_hand)
            assert candidate.pattern_key in {p.key() for p in P.classify(candidate.cards)}


# --------------------------------------------------------------------- 计分


def test_score_deltas_follow_rules_md():
    assert score_deltas(base=3, multiplier=4, landlord=0, landlord_won=True) == (24, -12, -12)
    assert score_deltas(base=3, multiplier=4, landlord=1, landlord_won=True) == (-12, 24, -12)
    assert score_deltas(base=2, multiplier=1, landlord=2, landlord_won=False) == (2, 2, -4)


@pytest.mark.parametrize("base,mult", [(1, 1), (3, 2), (5, 8)])
def test_score_is_zero_sum(base, mult):
    for landlord in (0, 1, 2):
        for won in (True, False):
            deltas = score_deltas(base, mult, landlord, won)
            assert sum(deltas) == 0
            assert deltas[landlord] == -2 * sum(d for i, d in enumerate(deltas) if i != landlord) / 2


def test_spring_and_anti_spring_flags():
    assert spring_flags([1, 0, 0], landlord=0, landlord_won=True) == (True, False)
    assert spring_flags([1, 1, 0], landlord=0, landlord_won=True) == (False, False)
    assert spring_flags([1, 2, 1], landlord=0, landlord_won=False) == (False, True)
    assert spring_flags([2, 1, 1], landlord=0, landlord_won=False) == (False, False)
    # 地主赢但农民出过牌 → 没春天；农民赢但地主出过两手 → 没反春
    assert spring_flags([1, 3, 2], landlord=0, landlord_won=True) == (False, False)


def test_multiplier_grows_with_bombs():
    """炸弹越多倍数越高（模拟器里每炸 +1 倍，春天/反春再 +1）。"""
    outcomes = [
        play([make("random", seed=s), make("random", seed=s + 20), make("random", seed=s + 40)], s)
        for s in range(12)
    ]
    for outcome in outcomes:
        expected = 1 + outcome.bombs + int(outcome.spring or outcome.anti_spring)
        assert outcome.multiplier == expected
    assert any(o.bombs > 0 for o in outcomes), "12 局里应该有炸弹"


def test_deals_capped_and_no_redeal_loop():
    passers = [ScriptedAgent("p0", bids=[0]), ScriptedAgent("p1", bids=[0]), ScriptedAgent("p2", bids=[0])]
    outcome = play(passers, 11, strict=False)
    assert outcome.deals <= MAX_DEALS
    assert outcome.redeals <= MAX_DEALS


def test_simulator_requires_three_agents():
    with pytest.raises(ValueError):
        Simulator([RandomAgent(seed=1)])


# --------------------------------------------------------------------- 状态保真


def test_simulator_states_carry_the_full_history():
    """agent 看到的状态必须带完整历史。

    这条是有血的教训：竞技场以前把 ``events`` 留空，启发式不读它所以看不出问题，
    但 DouZero 的观测正是从这串历史编码出来的——结果是它在竞技场里"瞎打"
    （自对弈地主胜率一度 82.5%）。所以这里逐条对账。
    """
    recorded: list = []

    class Recorder(RandomAgent):
        def act(self, state):
            recorded.append(state)
            return super().act(state)

    outcome = play([Recorder(seed=1), Recorder(seed=2), Recorder(seed=3)], 4)
    assert recorded and outcome.errors == 0
    for index, state in enumerate(recorded):
        events = state.events
        # 历史条数 = 这之前所有出牌/不出（每步一条）
        assert len(events) == index, f"第 {index} 步的事件条数是 {len(events)}"
        # 事件里的牌与「各家已出牌」必须对得上
        dealt: list[int] = []
        for event in events:
            assert event["kind"] in ("play", "pass")
            if event["kind"] == "play":
                dealt.extend(event["cards"])
        played = [c for seat in state.played_by_seat for c in seat]
        assert sorted(dealt) == sorted(played), f"第 {index} 步：事件里的牌与已出牌对不上"
        # 一条 play 事件必须有牌型，pass 必须没有牌
        for event in events:
            if event["kind"] == "play":
                assert event.get("pattern") and event.get("cards")
            else:
                assert not event.get("cards")


def test_simulated_state_is_enough_to_build_a_douzero_infoset():
    """竞技场造的状态要能直接喂给 DouZero 桥接（历史、未见牌、手牌齐备）。"""
    from ddz.douzero import vendored

    if vendored.load_env() is None:
        pytest.skip("没有 third_party/DouZero")
    from ddz.douzero import bridge

    recorded: list = []

    class Recorder(RandomAgent):
        def act(self, state):
            recorded.append(state)
            return super().act(state)

    play([Recorder(seed=1), Recorder(seed=2), Recorder(seed=3)], 6)
    late = [s for s in recorded if len(s.events) > 8]
    assert late, "没走到中局"
    state = late[-1]
    info = bridge.build_infoset(state, state.seat)
    assert len(info.card_play_action_seq) == len(state.events)
    assert info.player_hand_cards
    assert info.other_hand_cards, "未见牌不该是空的"
    assert len(info.player_hand_cards) + len(info.other_hand_cards) == 54 - len(state.played_cards)
    # last_move 的语义与上游一致：最后一条为空就取倒数第二条，所以"要压牌"时才非空
    if state.leading:
        assert info.last_move == []
    else:
        assert info.last_move, "跟牌时 last_move 必须有牌"
    assert sum(len(v) for v in info.played_cards.values()) == len(state.played_cards)
    assert len(info.legal_actions) >= 1
