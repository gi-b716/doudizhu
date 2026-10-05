"""对局库：建模、去重键、JSONL 往返，以及**重放不变量**（PLAN Phase 1 的验收标准）。"""

from __future__ import annotations

import copy
import json
import random

import pytest

from ddz import cards as C
from ddz import patterns as P
from ddz.record import (
    SCHEMA, GameRecord, dedupe_key_of, from_finished_state, iter_decisions,
    iter_frames, replay,
)
from ddz.state import PHASE_BIDDING, PHASE_FINISHED, PHASE_PLAYING, StateParseError
from gamegen import play_game


def finished(seed: int, **kwargs) -> dict:
    states = play_game(random.Random(seed), **kwargs)
    return states[-1]


def record_of(seed: int, **kwargs) -> GameRecord:
    return from_finished_state(finished(seed, **kwargs))


# --------------------------------------------------------------------- 建模


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_from_finished_state_builds_a_consistent_record(seed):
    rec = record_of(seed)
    assert rec.validate() == []
    assert rec.game_id
    assert len(rec.deals) >= 1
    assert rec.result is not None and rec.result.landlord_won in (True, False)
    assert [p or "?" for p in rec.players]
    assert any(e.kind == "play" for e in rec.events)
    assert all(e.pattern for e in rec.events if e.kind == "play")


def test_only_finished_states_can_be_archived():
    states = play_game(random.Random(3))
    with pytest.raises(StateParseError):
        from_finished_state(states[0])
    with pytest.raises(StateParseError):
        from_finished_state({"phase": "playing"})


def test_state_without_deals_is_rejected():
    state = copy.deepcopy(finished(3))
    state["fairness"]["deals"] = None
    with pytest.raises(StateParseError):
        from_finished_state(state)


def test_broken_deck_is_rejected():
    state = copy.deepcopy(finished(3))
    state["fairness"]["deals"][0]["deck"][0] = 99
    with pytest.raises(StateParseError):
        from_finished_state(state)


def test_unclassifiable_play_in_the_log_is_rejected():
    state = copy.deepcopy(finished(3))
    for entry in state["log"]:
        if entry["kind"] == "play":
            entry["cards"] = [0, 1, 4, 5]  # 3 3 4 4 打不出去
            break
    with pytest.raises(StateParseError):
        from_finished_state(state)


def test_game_id_falls_back_to_the_dedupe_key():
    rec = from_finished_state(finished(3))
    assert rec.game_id.startswith("local-")


def test_dedupe_key_is_stable_and_deal_specific():
    state = finished(3)
    first = from_finished_state(state)
    second = from_finished_state(copy.deepcopy(state))
    assert first.dedupe_key() == second.dedupe_key()
    assert dedupe_key_of(json.loads(first.to_line())) == first.dedupe_key()

    other = from_finished_state(finished(4))
    assert other.dedupe_key() != first.dedupe_key()


def test_same_deal_but_different_players_gives_a_different_key():
    state = finished(3)
    a = from_finished_state(state)
    renamed = copy.deepcopy(state)
    renamed["players"][0]["username"] = "someone-else"
    b = from_finished_state(renamed)
    assert a.dedupe_key() != b.dedupe_key()


# --------------------------------------------------------------------- JSONL


@pytest.mark.parametrize("seed", [3, 6])
def test_jsonl_round_trip(seed):
    rec = record_of(seed)
    line = rec.to_line()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["schema"] == SCHEMA
    again = GameRecord.from_line(line)
    assert again.to_dict() == rec.to_dict()
    assert again.dedupe_key() == rec.dedupe_key()


def test_line_stores_pattern_as_our_key_strings():
    line = json.loads(record_of(3).to_line())
    plays = [e for e in line["events"] if e["kind"] == "play"]
    assert plays
    for entry in plays:
        assert P.Pattern.parse_key(entry["pattern"])


def test_unknown_schema_is_rejected():
    line = json.loads(record_of(3).to_line())
    line["schema"] = "ddz.record.v2"
    with pytest.raises(StateParseError):
        GameRecord.from_dict(line)


def test_validate_reports_broken_events():
    rec = record_of(3)
    rec.events[0].kind = "teleport"
    rec.events[1].seat = 7
    rec.events[2].pattern = "not:a:key"
    problems = rec.validate()
    assert len(problems) == 3


# --------------------------------------------------------------------- 重放不变量


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 12])
def test_replay_invariants(seed):
    """计数恒 54、手牌与已出牌不重叠、倍数单调、leading 自洽、终局与 result 一致。"""
    rec = record_of(seed)
    frames = list(iter_frames(rec))
    assert len(frames) == len(rec.events) + 1

    previous_multiplier = 0
    for frame in frames:
        assert frame.version >= 0
        if frame.phase == PHASE_BIDDING:
            assert frame.landlord is None
            assert frame.leading is False
            assert frame.to_beat is None
        if frame.phase in (PHASE_PLAYING, PHASE_FINISHED):
            hands = [c for h in frame.all_hands for c in h]
            played = list(frame.played_cards)
            assert len(set(hands)) == len(hands)
            assert not (set(hands) & set(played))
            assert len(hands) + len(played) == 54, f"计数不守恒：{len(hands)} + {len(played)}"
            assert frame.landlord in (0, 1, 2)
            assert sum(p.count for p in frame.players) == len(hands)
        if frame.phase == PHASE_PLAYING:
            assert frame.leading == (frame.last is None or frame.last.seat == frame.turn)
            assert frame.to_beat is None if frame.leading else frame.to_beat is not None
            assert frame.turn in (0, 1, 2)
        assert frame.multiplier >= previous_multiplier, "倍数只能单调不降"
        previous_multiplier = frame.multiplier

    final = frames[-1]
    assert final.phase == PHASE_FINISHED
    assert final.result is not None
    assert final.result["multiplier"] == rec.result.multiplier
    assert sum(final.result["deltas"]) == 0


@pytest.mark.parametrize("seed", [3, 8])
def test_replay_k_matches_iter_frames(seed):
    rec = record_of(seed)
    frames = list(iter_frames(rec))
    for k in range(len(frames)):
        assert replay(rec, k) == frames[k]  # GameState 是 dataclass，逐字段比较


def test_replay_out_of_range():
    rec = record_of(3)
    with pytest.raises(ValueError):
        replay(rec, len(rec.events) + 1)
    with pytest.raises(ValueError):
        replay(rec, -1)


def test_replay_of_the_first_deal_has_17_card_hands():
    rec = record_of(3)
    first = replay(rec, 0)
    assert first.phase == PHASE_BIDDING
    assert [len(h) for h in first.all_hands] == [17, 17, 17]
    assert first.turn == rec.deals[0].first_bidder
    assert first.bottom is None and first.landlord is None


def test_replay_after_landlord_gives_the_landlord_20_cards():
    rec = record_of(3)
    index = next(i for i, e in enumerate(rec.events) if e.kind == "landlord")
    frame = replay(rec, index + 1)
    assert frame.phase == PHASE_PLAYING
    assert len(frame.all_hands[frame.landlord]) == 20
    assert frame.bottom == rec.deals[0].bottom()
    assert frame.turn == frame.landlord


def test_replay_bombs_raise_the_multiplier():
    """找一个有炸弹的局，验证倍数恰好多出炸弹数。"""
    for seed in range(40):
        rec = record_of(seed)
        bombs = sum(1 for e in rec.events if e.kind == "play" and e.pattern and P.is_bomb(P.Pattern.parse_key(e.pattern)))
        if bombs:
            frames = list(iter_frames(rec))
            playing = [f for f in frames if f.phase == PHASE_PLAYING]
            assert max(f.bombs for f in playing) == bombs
            assert max(f.multiplier for f in playing) == 1 + bombs
            return
    pytest.skip("前 20 个种子里没有炸弹")


def test_redeal_invalidates_the_previous_deal():
    states = play_game(random.Random(2), pass_bias=0.95)
    rec = from_finished_state(states[-1])
    assert len(rec.deals) >= 4
    # 副数变化处：手牌回到 17 张、已出牌清空
    for k, entry in enumerate(rec.events):
        if entry.kind == "redeal":
            frame = replay(rec, k + 1)
            assert frame.phase == PHASE_BIDDING
            assert [len(h) for h in frame.all_hands] == [17, 17, 17]
            assert frame.played_by_seat == [(), (), ()]
            assert frame.events == []


# --------------------------------------------------------------------- 决策样本


@pytest.mark.parametrize("seed", [3, 9])
def test_iter_decisions_covers_every_action(seed):
    rec = record_of(seed)
    actions = [e for e in rec.events if e.kind in ("play", "pass")]
    decisions = list(iter_decisions(rec))
    assert len(decisions) == len(actions)
    assert [d.entry for d in decisions] == actions


def test_decisions_are_legal_in_their_own_state():
    """每个决策：着法确实在自己手里、是该局面下的合法读法、并且能压过上家。

    （「不出」这里不断言"没有牌能管"——rules.md 允许主动不出，只有启发式的硬规则才禁止。）
    """
    rec = record_of(3)
    passed = played = 0
    for decision in iter_decisions(rec):
        state = decision.state
        assert state.seat == decision.seat
        assert state.hand == state.all_hands[decision.seat]
        if decision.move is None:
            passed += 1
            continue
        played += 1
        assert set(decision.move.cards) <= set(state.hand)
        assert decision.move.pattern.key() in {p.key() for p in P.classify(decision.move.cards)}
        if state.to_beat is not None:
            assert P.beats(decision.move.pattern, state.to_beat)
    assert passed and played


def test_iter_decisions_can_skip_passes():
    rec = record_of(3)
    only_plays = list(iter_decisions(rec, include_passes=False))
    assert all(not d.is_pass for d in only_plays)
    assert len(only_plays) == sum(1 for e in rec.events if e.kind == "play")


def test_decisions_expose_the_actor_perspective():
    rec = record_of(3)
    decision = next(iter_decisions(rec))
    assert decision.state.seat == decision.seat
    assert decision.state.hand is not None
    assert len(decision.state.hand) == decision.state.counts[decision.seat]


def test_events_of_deal_slices_by_redeal():
    states = play_game(random.Random(2), pass_bias=0.95)
    rec = from_finished_state(states[-1])
    total = sum(len(rec.events_of_deal(i)) for i in range(len(rec.deals)))
    assert total == sum(1 for e in rec.events if e.kind != "redeal")


def test_record_carries_match_and_capture_metadata():
    rec = from_finished_state(finished(3), game_id="12345", captured_at=1_700_000_000.0, seat_of_interest=2)
    assert rec.game_id == "12345"
    assert rec.captured_at == 1_700_000_000.0
    assert rec.seat_of_interest == 2
    assert rec.rules.startswith("rules.md@")
    assert from_finished_state(finished(3), seat_of_interest=1).seat_of_interest == 1
