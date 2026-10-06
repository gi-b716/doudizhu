"""DouZero 接入的工程面：没装好时**必须**安静回退，装好了才启用。

这台开发机没有 torch、也没有权重，所以这些用例覆盖的正是"降级路径"——它比 happy path 更
重要：一旦它坏了，服务会整个挂掉而不是退回启发式（PLAN §3.4 的核心承诺）。
"""

from __future__ import annotations

import pytest

from ddz.agents.registry import make
from ddz.douzero import agent as agent_module
from ddz.douzero import bridge, vendored, weights
from ddz.engine.heuristic import HeuristicAgent
from helpers import game_state, reading

NEEDS_DOUZERO = pytest.mark.skipif(
    not agent_module.is_ready(), reason="没装 torch / 权重，跳过需要真模型的部分"
)


# --------------------------------------------------------------------- 降级路径


def test_is_ready_reports_missing_pieces():
    status = agent_module.status()
    assert set(status) == {"ready", "torch", "cuda", "repo", "weights"}
    assert isinstance(status["ready"], bool)
    assert isinstance(status["cuda"], bool)
    if not status["torch"]:
        assert status["cuda"] is False   # 没 torch 就不可能用得上 GPU
    if not status["ready"]:
        assert status["repo"] or not vendored.is_installed()


def test_load_fails_softly_and_says_why():
    agent = agent_module.DouZeroAgent()
    loaded = agent.load()
    if loaded:
        assert agent.loaded and agent.models
        return
    assert agent.loaded is False
    assert agent.last_error, "装载失败必须留下原因"


def test_acting_without_a_model_falls_back_to_the_heuristic():
    agent = agent_module.DouZeroAgent()
    agent.load()
    if agent.loaded:
        pytest.skip("这台机器上 DouZero 是好的，降级路径由下面的用例单独测")
    candidate = agent.act(game_state("34567 88 9 10 J"))
    assert not candidate.is_pass
    assert candidate.source == agent.name
    assert any("回落启发式" in reason for reason in candidate.reasons)
    assert "fallback:heuristic" in candidate.tags


def test_bid_always_uses_the_heuristic():
    """DouZero 没有叫分模型，也不认识流局——叫分固定走启发式（PLAN §0.2）。"""
    agent = agent_module.DouZeroAgent()
    agent.load()
    state = game_state("小王 大王 2 2 2 A 3 3 3 3 4 5 6 7 8 9", phase="bidding", landlord=None)
    bid = agent.bid(state)
    assert bid.value in (0, 1, 2, 3)
    assert bid.value == HeuristicAgent().bid(state).value


def test_status_shape():
    status = agent_module.DouZeroAgent().status()
    assert set(status) >= {"name", "loaded", "device", "last_error", "positions"}
    assert status["name"] == "douzero:WP"
    assert status["device"] in ("cpu", "cuda")


def test_registry_returns_a_usable_agent_even_without_weights():
    agent = make("douzero:WP")
    assert agent.name == "douzero:WP"
    candidate = agent.act(game_state("34567 88"))
    assert candidate.move is not None or candidate.is_pass


def test_registry_accepts_an_explicit_weights_path(tmp_path):
    agent = make(f"douzero:{tmp_path}")
    assert agent.weights_path == tmp_path
    assert agent.loaded is False


# --------------------------------------------------------------------- 权重与仓库


def test_weights_dir_default_points_into_the_vendored_repo():
    directory = weights.weights_dir()
    assert directory.name == "douzero_WP"
    assert "DouZero" in str(directory)


def test_missing_weights_are_listed():
    missing = weights.missing()
    assert isinstance(missing, list)
    for name in missing:
        assert name in weights.POSITIONS


def test_weights_status_shape():
    status = weights.status()
    assert set(status) == {"directory", "found", "missing", "complete"}
    assert status["complete"] is (len(status["found"]) == 3)


def test_vendored_repo_is_pinned():
    if not vendored.is_installed():
        pytest.skip("没有 clone third_party/DouZero")
    assert vendored.commit(), "COMMIT.txt 里应当记着 clone 时的 commit"
    assert (vendored.root_dir() / "douzero" / "env" / "game.py").exists()


def test_env_modules_load_without_torch():
    """闸一闸二能在没有 torch 的机器上跑，全靠 env/* 不 import torch。"""
    modules = vendored.load_env()
    if modules is None:
        pytest.skip("没有 clone third_party/DouZero")
    assert modules.env and modules.game and modules.generator
    assert modules.get_obs is not None
    assert modules.game.RealCard2EnvCard["2"] == 17   # PLAN §0.2 的牌码表


# --------------------------------------------------------------------- 观测与动作


@NEEDS_DOUZERO
def test_bridge_produces_observations_for_every_position():
    modules = vendored.load_env()
    state = game_state("2 2 3 4 5 6 7 8 9 10 J", counts=[11, 9, 8], landlord=1)
    info = bridge.build_infoset(state, 0)
    obs = modules.get_obs(info)
    assert obs["position"] in bridge.POSITIONS
    assert obs["x_batch"].shape[0] == len(info.legal_actions)
    assert obs["z_batch"].shape[1:] == (5, 162)


def test_bench_reports_missing_pieces_instead_of_faking_numbers(capsys):
    from ddz.tools import bench_douzero

    if agent_module.is_ready():
        pytest.skip("这台机器上 DouZero 是好的")
    code = bench_douzero.main(["--games", "1"])
    assert code == 2
    out = capsys.readouterr().out
    assert "还没就绪" in out and "权重" in out
