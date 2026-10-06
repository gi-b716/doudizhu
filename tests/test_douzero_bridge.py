"""**闸一**：用上游自己的 ``GameEnv`` 重放同一局，逐决策点比对 ``get_obs``（PLAN §3.7）。

做法：把同一副牌、同一串动作同时喂给
  * 上游的真 ``GameEnv``（它自己算 infoset），
  * 我们的 :func:`ddz.douzero.bridge.build_infoset`（从对局库重放出来的局面），
然后在每个决策点断言两边的 ``z_batch`` / ``x_batch`` **逐元素相等**（含形状）。

为了把"桥接字段对不对"与"合法集口径差异"分开测，比对时把 ``legal_actions`` 换成上游那一份
——合法集本身由 ``test_douzero_legality_parity.py`` 单独守。这样任何一个字段（手牌、未见牌、
历史动作、剩余张数、已出牌、炸弹数、底牌）抄错都会当场炸出来。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from ddz import cards as C
from ddz.douzero import bridge, vendored
from ddz.record import from_finished_state, replay
from gamegen import play_game

MODULES = vendored.load_env()
pytestmark = pytest.mark.skipif(MODULES is None, reason="没有 third_party/DouZero")

POSITIONS = ("landlord", "landlord_up", "landlord_down")


def build_upstream_env(record):
    """按对局库里的发牌给上游 GameEnv 起一手同样的牌。"""
    deal = record.deals[0]
    landlord = next(event.seat for event in record.events if event.kind == "landlord")
    hands, bottom = deal.hands(), deal.bottom()
    data = {
        # 上游口径：地主手牌已经把底牌含进去了（20 张）
        "landlord": sorted(C.to_env_card(c) for c in list(hands[landlord]) + list(bottom)),
        "three_landlord_cards": sorted(C.to_env_card(c) for c in bottom),
    }
    for seat in range(3):
        if seat != landlord:
            data[bridge.position_of(landlord, seat)] = sorted(C.to_env_card(c) for c in hands[seat])
    players = {pos: MODULES.env.DummyAgent(pos) for pos in POSITIONS}
    env = MODULES.game.GameEnv(players)
    env.card_play_init(data)
    return env, players, landlord


def compare_observations(ours, theirs) -> str | None:
    """逐元素比对；不同就返回一句能看懂的原因。"""
    if ours["position"] != theirs["position"]:
        return f"position 不同：{ours['position']} vs {theirs['position']}"
    for key in ("z_batch", "x_batch"):
        a, b = np.asarray(ours[key]), np.asarray(theirs[key])
        if a.shape != b.shape:
            return f"{key} 形状不同：{a.shape} vs {b.shape}"
        if not np.array_equal(a, b):
            index = np.argwhere(a != b)[0]
            return f"{key} 内容不同（首个差异下标 {tuple(index.tolist())}）"
    if ours["legal_actions"] != theirs["legal_actions"]:
        return f"legal_actions 不同：{len(ours['legal_actions'])} vs {len(theirs['legal_actions'])}"
    return None


def replay_game(seed: int, *, force_legal_actions: bool = True):
    """跑一局并把每个决策点都比一遍；返回 (比对次数, 首个不一致, 形状集合)。"""
    record = from_finished_state(play_game(random.Random(seed))[-1])
    if len(record.deals) != 1:
        return None
    env, players, landlord = build_upstream_env(record)
    checked, shapes = 0, set()
    for index, event in enumerate(record.events):
        if event.kind not in ("play", "pass"):
            continue
        state = replay(record, index)          # 打这一步**之前**的局面
        if state.landlord is None:
            continue
        ours = bridge.build_infoset(state, event.seat)
        theirs = env.game_infoset
        if force_legal_actions:
            ours.legal_actions = [list(action) for action in theirs.legal_actions]
        a, b = MODULES.get_obs(ours), MODULES.get_obs(theirs)
        shapes.add((np.asarray(a["z_batch"]).shape, np.asarray(a["x_batch"]).shape))
        problem = compare_observations(a, b)
        if problem:
            return checked, (seed, index, event.kind, event.seat, problem), shapes
        checked += 1
        action = [] if event.kind == "pass" else sorted(C.to_env_card(c) for c in event.cards)
        players[env.acting_player_position].set_action(action)
        env.step()
    return checked, None, shapes


@pytest.mark.parametrize("seed", range(6))
def test_observations_match_upstream_at_every_decision(seed):
    result = replay_game(seed)
    if result is None:
        pytest.skip("这局有流局，跳过（闸一按副比对）")
    checked, problem, _ = result
    assert problem is None, f"观测不一致：{problem}"
    assert checked >= 5, f"这局只有 {checked} 个决策点可比"


def test_observation_shapes_and_positions():
    """形状与位置：地主 (N,5,162)/(N,373)，农民 (N,484)。"""
    shapes = {}
    for seed in range(4):
        result = replay_game(seed)
        if result is None:
            continue
        _, problem, seen = result
        assert problem is None
        shapes[seed] = seen
    flat = {item for seen in shapes.values() for item in seen}
    assert any(x[0] == (1, 5, 162) or x[0][1:] == (5, 162) for x in flat)
    dims = {x[1][-1] for x in flat}
    assert dims <= {373, 484}, dims
    assert 373 in dims, "地主视角的 373 维没出现"


def test_positions_are_consistent_both_ways():
    for landlord in range(3):
        for seat in range(3):
            position = bridge.position_of(landlord, seat)
            assert bridge.seat_of(landlord, position) == seat
        assert bridge.position_of(landlord, landlord) == "landlord"
        assert bridge.position_of(landlord, (landlord + 1) % 3) == "landlord_down"
        assert bridge.position_of(landlord, (landlord + 2) % 3) == "landlord_up"


def test_card_code_bijection_against_upstream_constants():
    bridge.assert_code_bijection()   # 内部会与 upstream 的 EnvCard2RealCard 对照


def test_encode_decode_round_trip():
    for spec in ("3", "33", "333", "34567", "333444", "小王 大王", "3333"):
        from helpers import cards_of

        cards = cards_of(spec)
        codes = bridge.encode_cards(cards)
        assert codes == sorted(codes)
        assert bridge.decode_action(codes) == tuple(C.sort_cards(cards))


def test_legal_actions_carry_pass_when_following():
    from helpers import cards_of
    from ddz.patterns import classify_one

    hand = cards_of("2 3 4 5 6 7")
    assert [] not in bridge.legal_actions(hand, None)              # 领出没有"不出"
    assert [] in bridge.legal_actions(hand, classify_one(cards_of("3")))  # 跟牌有


def test_action_to_move_round_trip():
    from helpers import cards_of
    from ddz.patterns import classify_one

    hand = cards_of("2 3 4 5 6 7")
    to_beat = classify_one(cards_of("3"))
    for action in bridge.legal_actions(hand, to_beat):
        move = bridge.action_to_move(action, hand, to_beat)
        if move is None:
            continue
        assert set(move.cards) <= set(hand)
        assert bridge.encode_cards(move.cards) == sorted(action)


def test_build_infoset_refuses_without_a_landlord():
    from helpers import game_state

    state = game_state("3 4 5", landlord=None)
    with pytest.raises(ValueError):
        bridge.build_infoset(state, 0)


def test_infoset_history_fields_match_a_hand_played_game():
    """手工核对一局的 infoset 历史字段（不依赖上游）：序列、last_move、已出牌、炸弹数。"""
    record = from_finished_state(play_game(random.Random(2))[-1])
    if len(record.deals) != 1:
        pytest.skip("这局有流局")
    landlord = next(event.seat for event in record.events if event.kind == "landlord")
    last = None
    for index, event in enumerate(record.events):
        if event.kind not in ("play", "pass"):
            continue
        state = replay(record, index)
        if state.landlord is None:
            continue
        info = bridge.build_infoset(state, event.seat)
        assert info.player_position == bridge.position_of(landlord, event.seat)
        assert len(info.card_play_action_seq) == len(
            [e for e in state.events if e.get("kind") in ("play", "pass")]
        )
        assert len(info.player_hand_cards) == state.counts[event.seat]
        assert sum(len(v) for v in info.played_cards.values()) == len(state.played_cards)
        assert info.bomb_num == state.bombs
        assert info.num_cards_left_dict["landlord"] == state.counts[landlord]
        last = info
    assert last is not None
