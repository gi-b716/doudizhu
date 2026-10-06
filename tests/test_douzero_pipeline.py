"""整条 DouZero 链路：装载 → 前向 → 选择 → 落回具体牌（用**替身权重**跑的）。

为什么用替身：真权重不在 git 里，要从官方 Drive/百度网盘下。用随机初始化的模型跑一遍能证明
**管道**是通的（形状对不对、动作能不能落地、延迟多少），但**不能**证明强度——棋力要靠真权重，
见 README 的下载步骤。

没有 torch 时整组跳过。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from ddz import cards as C
from ddz.douzero import agent as agent_module
from ddz.douzero import bridge, vendored, weights
from ddz.record import from_finished_state, replay
from helpers import cards_of, game_state

pytestmark = pytest.mark.skipif(
    not vendored.torch_available(), reason="没装 torch，跳过模型链路"
)

STUB_NOTE = "替身权重（随机初始化）"


@pytest.fixture(scope="module")
def stub_dir(tmp_path_factory) -> Path:
    """造三个形状正确的随机权重。"""
    models, _ = vendored.load_model_modules()
    if models is None:
        pytest.skip("读不到上游模型定义")
    import torch

    directory = tmp_path_factory.mktemp("douzero_stub")
    for position, cls in models.model_dict.items():
        torch.save(cls().state_dict(), directory / f"{position}.ckpt")
    return directory


@pytest.fixture()
def stub_agent(stub_dir) -> agent_module.DouZeroAgent:
    agent = agent_module.DouZeroAgent(stub_dir)
    if not agent.load():
        pytest.skip(f"替身权重装载失败：{agent.last_error}")
    return agent


def real_states(count: int = 3, seed: int = 0) -> list:
    """从随机对局里取真局面（出牌阶段的 GameState）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gamegen import play_game

    out = []
    for index in range(count):
        record = from_finished_state(play_game(random.Random(seed + index))[-1])
        if len(record.deals) != 1:
            continue
        for k, event in enumerate(record.events):
            if event.kind != "play" or event.seat is None:
                continue
            state = replay(record, k)
            if state.landlord is not None and state.hand_for(event.seat) is not None:
                out.append(state.for_seat(event.seat))
                break
    return out


def test_stub_weights_are_detected_as_a_complete_set(stub_dir):
    found = weights.find_weights(stub_dir)
    assert sorted(found) == sorted(weights.POSITIONS)
    assert weights.resolve(stub_dir) is not None
    assert weights.status(stub_dir)["complete"] is True


def test_agent_loads_and_reports_status(stub_agent):
    status = stub_agent.status()
    assert status["loaded"] is True
    assert status["positions"] == sorted(weights.POSITIONS)
    assert status["device"] in ("cpu", "cuda")
    assert stub_agent.load_seconds is not None


def test_choose_action_returns_a_legal_move(stub_agent):
    """前向 → argmax → 落到手牌里的具体牌，全程合法。"""
    from ddz.patterns import beats

    states = real_states()
    assert states, "取不到局面"
    for state in states:
        candidate = stub_agent.act(state)
        assert candidate.source == "douzero:WP"
        assert candidate.win_prob is not None and 0.0 <= candidate.win_prob <= 1.0
        if candidate.move is None:
            assert state.to_beat is not None      # 只有跟牌时才允许不出
            continue
        assert set(candidate.move.cards) <= set(state.hand_for(state.seat))
        if state.to_beat is not None:
            assert beats(candidate.move.pattern, state.to_beat)


def test_observation_encoder_is_used_not_rewritten(stub_agent):
    """确认观测确实来自上游 get_obs（形状就说明问题：地主 373、农民 484）。"""
    from ddz.state import from_platform_state

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gamegen import play_game

    modules = vendored.load_env()
    raw = next(
        s for s in play_game(random.Random(5), admin=True)
        if s["phase"] == "playing" and s["landlord"] is not None
    )
    state = from_platform_state(raw)
    landlord = state.landlord
    for seat in range(3):
        info = bridge.build_infoset(state, seat)
        obs = modules.get_obs(info)
        width = 373 if seat == landlord else 484
        assert obs["x_batch"].shape == (len(info.legal_actions), width), (seat, info.player_position)
        assert obs["z_batch"].shape == (len(info.legal_actions), 5, 162)


def test_bench_runs_and_meets_the_latency_budget(stub_dir, monkeypatch):
    """PLAN §3.7 的验收线：bench 的 p95 < 50ms（替身权重下先证明管道够快）。"""
    from ddz.tools import bench_douzero

    monkeypatch.setenv("DDZ_DOUZERO_WEIGHTS", str(stub_dir))
    result = bench_douzero.bench(games=2, repeat=1)
    assert result["ready"] is True, result
    assert result["samples"] > 20
    assert result["latency_ms"]["p95"] < 50, result["latency_ms"]


def test_single_action_short_circuits(stub_agent):
    """只剩一个合法动作时直接返回（上游也这么短路），估值给中性的 0.5。"""
    modules = vendored.load_env()
    state = game_state("5", counts=[1, 9, 8], landlord=1)
    info = bridge.build_infoset(state, 0)
    assert len(info.legal_actions) == 1
    action, value = bridge.choose_action(stub_agent.models[info.player_position], info, modules=modules)
    assert action == info.legal_actions[0]
    assert value == 0.5


def test_shape_validation_catches_a_broken_checkpoint(stub_dir, tmp_path):
    """形状对不上的权重必须被判为不可用（宁可回落启发式，也别拿错模型算）。"""
    import torch

    models, _ = vendored.load_model_modules()
    model = models.model_dict["landlord"]()
    state = model.state_dict()
    key = next(iter(state))
    state[key] = torch.zeros(3)          # 故意改坏一个张量
    broken = tmp_path / "landlord.ckpt"
    torch.save(state, broken)

    info = weights.verify(broken, model=model)
    assert info["usable"] is False
    assert info["shape_mismatch"], info


def test_checksum_mismatch_is_refused(stub_dir):
    with pytest.raises(weights.WeightsError, match="sha256"):
        weights.load_state_dict(stub_dir / "landlord.ckpt", checksum="0" * 64)


def test_corrupt_checkpoint_raises_a_clear_error(tmp_path):
    bad = tmp_path / "landlord.ckpt"
    bad.write_bytes(b"not a checkpoint at all")
    with pytest.raises(weights.WeightsError):
        weights.load_state_dict(bad)
