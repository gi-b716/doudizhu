"""编排层：会话、缓存、超时回退、各种"给不出建议"的形态。"""

from __future__ import annotations

import copy
import random
import time

import pytest

from ddz.data.archive import Archive
from ddz.engine.base import Bid, Candidate
from ddz.service.advisor import AdviseOptions, Advisor
from gamegen import play_game


class SpyAgent:
    """记录调用次数、可指定延迟的替身。"""

    name = "spy"

    def __init__(self, delay: float = 0.0, name: str = "spy") -> None:
        self.name = name
        self.delay = delay
        self.calls = 0

    def act(self, state):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        move = __import__("ddz.moves", fromlist=["Move"]).Move((0,), __import__(
            "ddz.patterns", fromlist=["Pattern"]).Pattern("single", 0))
        return Candidate(move, 1.0, ["替身"], ["spy"], self.name, timing={"stub": 1.0})

    def bid(self, state):
        self.calls += 1
        return Bid(1, 0.0, ["替身"])


@pytest.fixture()
def advisor(tmp_path) -> Advisor:
    return Advisor(archive=Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw"))


def states(seed: int = 3, **kwargs):
    return play_game(random.Random(seed), **kwargs)


def my_turn_state(seed: int = 3) -> dict:
    return next(s for s in states(seed) if s["phase"] == "playing" and s["turn"] == s["seat"])


def not_my_turn_state(seed: int = 3) -> dict:
    return next(s for s in states(seed) if s["phase"] == "playing" and s["turn"] != s["seat"])


# --------------------------------------------------------------------- 正常路径


def test_advise_returns_a_full_payload(advisor):
    result = advisor.advise("s1", my_turn_state())
    assert result["ok"] is True
    assert result["seat"] == 0
    assert result["phase"] == "playing"
    assert result["is_my_turn"] is True
    assert result["recommended"]["description"]
    assert result["recommended"]["source"] == "heuristic"
    assert result["card_counter"]["unseen_by_rank"]
    assert result["hand_analysis"]["n_plays"] > 0
    assert result["engine"]["name"] == "heuristic"
    assert result["timing_ms"]["total"] > 0 and result["timing_ms"]["parse"] >= 0
    assert "moves" in result["timing_ms"] and "score" in result["timing_ms"]


def test_recommendation_is_legal_for_that_state(advisor):
    from ddz import patterns as P
    from ddz.state import from_platform_state

    state = my_turn_state()
    result = advisor.advise("s1", state)
    gs = from_platform_state(state)
    cards = result["recommended"]["cards"]
    if cards:
        assert set(cards) <= set(gs.hand)
        assert result["recommended"]["pattern_key"] in {p.key() for p in P.classify(cards)}
        if gs.to_beat is not None:
            assert P.beats(P.Pattern.parse_key(result["recommended"]["pattern_key"]), gs.to_beat)


def test_fast_enough_for_the_clock(advisor):
    """解析 + 生成 + 启发式必须远小于 15 秒出牌时限（PLAN §3.5 的延迟预算）。"""
    state = my_turn_state()
    advisor.advise("warm", state)  # 预热
    started = time.perf_counter()
    result = advisor.advise("s1", state)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert elapsed_ms < 100, elapsed_ms
    assert result["timing_ms"]["total"] < 100


def test_top_k_controls_alternatives(advisor):
    state = my_turn_state()
    assert len(advisor.advise("a", state, AdviseOptions(top_k=1))["alternatives"]) == 0
    three = advisor.advise("b", state, AdviseOptions(top_k=3))
    assert len(three["alternatives"]) <= 2


# --------------------------------------------------------------------- 会话与缓存


def test_unchanged_keeps_the_previous_state(advisor):
    """``unchanged`` 只刷新计时：局面不变，还是同一步、同一条建议。"""
    spy = SpyAgent(name="heuristic")
    advisor.register("heuristic", spy)
    state = my_turn_state()
    first = advisor.advise("s1", state)
    refreshed = advisor.advise("s1", {"unchanged": True, "remaining": 9000})
    assert refreshed["version"] == first["version"]
    assert refreshed["to_beat"] == first["to_beat"]
    assert refreshed["recommended"] == first["recommended"]
    assert spy.calls == 1  # 没有重算
    assert advisor.session("s1").state.remaining_ms == 9000


def test_unchanged_without_history_is_a_parse_error(advisor):
    result = advisor.advise("fresh", {"unchanged": True, "remaining": 1})
    assert result["ok"] is False and result["reason"] == "parse_error"


def test_same_version_is_only_computed_once(advisor):
    spy = SpyAgent(name="heuristic")  # 顶掉启发式，看它被调用几次
    advisor.register("heuristic", spy)
    state = my_turn_state()
    first = advisor.advise("s1", state)
    second = advisor.advise("s1", copy.deepcopy(state))
    assert spy.calls == 1
    assert first == second


def test_new_version_recomputes(advisor):
    spy = SpyAgent(name="heuristic")
    advisor.register("heuristic", spy)
    playing = [s for s in states() if s["phase"] == "playing" and s["turn"] == s["seat"]]
    advisor.advise("s1", playing[0])
    advisor.advise("s1", playing[1])
    assert spy.calls == 2


def test_sessions_are_independent(advisor):
    spy = SpyAgent(name="heuristic")
    advisor.register("heuristic", spy)
    state = my_turn_state()
    advisor.advise("s1", state)
    advisor.advise("s2", copy.deepcopy(state))
    assert spy.calls == 2  # 两个标签页各算各的
    assert advisor.status()["sessions"] == 2


def test_forget_session(advisor):
    advisor.advise("s1", my_turn_state())
    assert advisor.forget("s1") is True
    assert advisor.forget("s1") is False


# --------------------------------------------------------------------- 超时与回退


def test_slow_engine_falls_back_to_the_heuristic(advisor):
    slow = SpyAgent(delay=0.5, name="heuristic")
    advisor.register("heuristic", slow)
    result = advisor.advise("s1", my_turn_state(), AdviseOptions(deadline_ms=50))
    assert result["ok"] is True
    assert result["engine"]["fallback"] is True
    assert slow.calls == 1
    assert result["recommended"]["source"] == "heuristic"
    assert result["timing_ms"]["total"] >= 50


def test_broken_engine_falls_back(advisor):
    class Exploding(SpyAgent):
        def act(self, state):
            raise RuntimeError("模型炸了")

    advisor.register("heuristic", Exploding())
    result = advisor.advise("s1", my_turn_state())
    assert result["ok"] is True
    assert result["engine"]["fallback"] is True
    assert "模型炸了" in result["recommended"]["rationale"][0]


# --------------------------------------------------------------------- 给不出建议


def test_not_my_turn_still_reports_the_counter(advisor):
    result = advisor.advise("s1", not_my_turn_state())
    assert result["ok"] is False and result["reason"] == "not_my_turn"
    assert result["recommended"] is None
    assert sum(result["card_counter"]["unseen_by_rank"]) > 0
    assert result["hand_analysis"]["n_plays"] > 0


def test_spectator_without_hands_gets_spectator_reason(advisor):
    state = next(s for s in states(seed=11, seat=None, admin=False) if s["phase"] == "playing")
    result = advisor.advise("spec", state)
    assert result["ok"] is False and result["reason"] == "spectator"
    assert result["card_counter"] is not None


def test_spectator_with_admin_hands_can_advise(advisor):
    state = next(s for s in states(seed=11, seat=None, admin=True) if s["phase"] == "playing")
    result = advisor.advise("spec", state)
    assert result["seat"] == state["turn"]
    if result["ok"]:
        assert result["recommended"] is not None


def test_finished_game_gets_finished_reason(advisor):
    result = advisor.advise("s1", states()[-1])
    assert result["ok"] is False and result["reason"] == "finished"


@pytest.mark.parametrize("bad", [{"phase": "waiting"}, {}, {"phase": "playing", "last": {"seat": 1}}])
def test_parse_error_is_reported_not_raised(advisor, bad):
    result = advisor.advise("s1", bad)
    assert result["ok"] is False and result["reason"] == "parse_error"
    assert result["recommended"] is None
    assert result["message"]


def test_unknown_engine_reports_no_engine(advisor):
    result = advisor.advise("s1", my_turn_state(), AdviseOptions(agent="douzero:WP"))
    assert result["ok"] is False and result["reason"] == "no_engine"
    assert result["engine"]["name"] == "douzero:WP"
    assert result["card_counter"] is not None  # 引擎没有也得给记牌器


def test_for_seat_option_advises_someone_else(advisor):
    """管理员替某一家看牌：给的是那一家该出的牌。"""
    state = next(s for s in states(seed=11, seat=None, admin=True) if s["phase"] == "playing")
    seat = state["turn"]
    result = advisor.advise("spec", state, AdviseOptions(for_seat=seat))
    assert result["seat"] == seat
    if result["ok"]:
        hand = state["hands"][seat]
        assert set(result["recommended"]["cards"]) <= set(hand)


# --------------------------------------------------------------------- 叫分


def test_bidding_advice(advisor):
    state = next(s for s in states() if s["phase"] == "bidding" and s["turn"] == s["seat"])
    result = advisor.advise("s1", state)
    assert result["ok"] is True
    assert result["bidding"]["value"] in (0, 1, 2, 3)
    assert result["bidding"]["label"]
    assert result["bidding"]["rationale"]
    assert result["recommended"] is None
    assert result["card_counter"] is not None


def test_bidding_never_uses_douzero(advisor):
    """叫分永远走启发式——DouZero 没有叫分模型（PLAN §0.2）。"""
    state = next(s for s in states() if s["phase"] == "bidding" and s["turn"] == s["seat"])
    result = advisor.advise("s1", state, AdviseOptions(agent="douzero:WP"))
    assert result["ok"] is True
    assert result["engine"]["name"] == "heuristic"


def test_not_my_bid_turn(advisor):
    state = next(s for s in states() if s["phase"] == "bidding" and s["turn"] != s["seat"])
    result = advisor.advise("s1", state)
    assert result["ok"] is False and result["reason"] == "not_my_turn"


# --------------------------------------------------------------------- 归档与运维


def test_ingest_and_duplicate(advisor):
    final = states()[-1]
    first = advisor.ingest(final, game_id="g1")
    assert first["ok"] is True and first["added"] is True and first["records"] == 1
    second = advisor.ingest(copy.deepcopy(final))
    assert second["duplicate"] is True and second["records"] == 1


def test_ingest_rejects_unfinished(advisor):
    result = advisor.ingest(states()[0])
    assert result["ok"] is False and result["reason"] == "parse_error"


def test_configure_and_status(advisor):
    applied = advisor.configure(default_agent="heuristic", top_k=2, deadline_ms=250)
    assert applied == {"default_agent": "heuristic", "top_k": 2, "deadline_ms": 250}
    status = advisor.status()
    assert status["default_agent"] == "heuristic" and status["top_k"] == 2
    assert status["records"] == 0


def test_configure_can_forget_a_session(advisor):
    advisor.advise("s1", my_turn_state())
    assert advisor.configure(forget_session="s1")["forgot"] is True


def test_auto_engine_resolves_to_heuristic(advisor):
    result = advisor.advise("s1", my_turn_state(), AdviseOptions(agent="auto"))
    assert result["engine"]["name"] == "heuristic"
