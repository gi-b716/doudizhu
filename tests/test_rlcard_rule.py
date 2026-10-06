"""RLCard 规则基线（**别人的启发式**）的适配层。

这个参照物只有在评测里才有意义，所以这里要钉住的不是"它下得好不好"，而是**它还是不是它**：

1. 它交出来的每一手都合法（合法性由我们给，它自己也会 ``assert``）；
2. 它**不回落**到我们的启发式——一旦回落，比的就不是它了，那个胜率也就没有意义；
3. 叫分走启发式（上游基线没有叫分逻辑，这是有意为之的偏离）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from ddz import patterns as P
from ddz.agents.registry import make
from ddz.agents.rlcard_rule import RLCardRuleAgent, is_available
from ddz.record import from_finished_state, replay
from helpers import game_state

pytestmark = pytest.mark.skipif(not is_available(), reason="没装 rlcard 或没有 third_party/DouZero")


def real_states(count: int = 6) -> list:
    """从随机对局里取真局面（出牌阶段、轮到我）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gamegen import play_game

    out = []
    for index in range(count):
        record = from_finished_state(play_game(random.Random(100 + index))[-1])
        if len(record.deals) != 1:
            continue
        for k, event in enumerate(record.events):
            if event.kind != "play" or event.seat is None:
                continue
            state = replay(record, k)
            if state.landlord is not None and state.hand_for(event.seat) is not None:
                out.append(state.for_seat(event.seat))
    return out


def test_it_plays_legally_and_stays_itself():
    """每手都合法，而且**一次都不回落**——回落了它就变成我们的启发式了。"""
    agent = RLCardRuleAgent()
    states = real_states()
    assert len(states) > 50, f"局面太少（{len(states)}）"
    for state in states:
        candidate = agent.act(state)
        assert candidate.source == "rlcard-rule"
        assert "fallback:heuristic" not in candidate.tags, f"回落了：{candidate.reasons}"
        if candidate.move is None:
            assert state.to_beat is not None, "领出时它不该不出"
            continue
        assert set(candidate.move.cards) <= set(state.hand_for(state.seat))
        assert candidate.move.pattern.key() in {p.key() for p in P.classify(candidate.move.cards)}
        if state.to_beat is not None:
            assert P.beats(candidate.move.pattern, state.to_beat)
    assert agent.last_error is None


def test_leading_never_passes():
    agent = RLCardRuleAgent()
    for state in real_states():
        if state.to_beat is not None:
            continue
        assert agent.act(state).move is not None


def test_bidding_goes_through_the_heuristic():
    """上游基线没有叫分逻辑，这是有意委托给启发式的。"""
    from ddz.engine.heuristic import HeuristicAgent

    state = game_state("34567 88 9 10 J", phase="bidding")
    ours = RLCardRuleAgent().bid(state)
    theirs = HeuristicAgent().bid(state)
    assert ours.value == theirs.value


def test_it_is_not_in_the_product_engine_list():
    """它只在竞技场里当参照，不进 ``available()``（那是产品的引擎列表）。"""
    from ddz.agents.registry import available

    assert "rlcard-rule" not in available()


def test_it_can_be_made_from_the_registry():
    assert make("rlcard-rule").name == "rlcard-rule"
