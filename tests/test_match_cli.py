"""竞技场 CLI：轮换座位、报表结构、非法 agent 名。"""

from __future__ import annotations

import json

import pytest

from ddz.agents.registry import available, make
from ddz.arena.match import default_out_dir, main, run_match


def test_rotation_covers_every_seat():
    metrics, _ = run_match(["heuristic", "random", "random"], games=9, seed=1, rotate=True)
    # 每个 agent 都坐遍了 0/1/2；每个座位都打满 9 局
    assert metrics.by_agent["heuristic"].seats == {0, 1, 2}
    assert [metrics.by_seat[seat].games for seat in range(3)] == [9, 9, 9]
    assert all(len(metrics.by_seat[seat].seats) == 1 for seat in range(3))


def test_without_rotation_seats_are_fixed():
    metrics, _ = run_match(["heuristic", "random", "random"], games=3, seed=1, rotate=False)
    assert metrics.by_agent["heuristic"].seats == {0}
    assert metrics.by_seat[0].name == "heuristic"
    assert metrics.by_seat[1].name == "random"


def test_summary_shape():
    metrics, elapsed = run_match(["heuristic", "random", "random"], games=6, seed=2, rotate=True)
    summary = metrics.summary(meta={"seed": 2})
    assert elapsed >= 0
    assert summary["meta"]["games"] == 6
    heuristic = summary["by_agent"]["heuristic"]
    assert heuristic["games"] == 6
    assert 0.0 <= heuristic["win_rate"] <= 1.0
    assert heuristic["wins"] + heuristic["losses"] + heuristic["draws"] == 6
    assert set(heuristic["latency_ms"]) == {"p50", "p95", "p99", "max", "mean"}
    assert heuristic["errors"] == 0
    assert len(summary["by_seat"]) == 3
    assert "games" not in summary


def test_summary_can_dump_every_game():
    metrics, _ = run_match(["random", "random", "random"], games=4, seed=3)
    summary = metrics.summary(include_games=True)
    assert len(summary["games"]) == 4
    assert all(sum(row["deltas"]) == 0 for row in summary["games"])


def test_cli_writes_summary_json(tmp_path, capsys):
    out = tmp_path / "arena"
    code = main(["--players", "heuristic,random,random", "--games", "6", "--seed", "4",
                 "--rotate", "--out", str(out), "--quiet"])
    assert code == 0
    data = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert data["meta"]["games"] == 6
    assert data["meta"]["rotate"] is True
    assert data["meta"]["rules"].startswith("rules.md@")
    assert data["by_agent"]["heuristic"]["games"] == 6


def test_cli_prints_a_table(capsys, tmp_path):
    main(["--players", "heuristic,random,random", "--games", "3", "--seed", "5",
          "--out", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert "heuristic" in out and "random" in out and "共 3 局" in out


def test_cli_rejects_wrong_player_count(capsys):
    with pytest.raises(SystemExit):
        main(["--players", "heuristic,random", "--games", "1"])


def test_default_out_dir_encodes_players_and_seed():
    path = default_out_dir(["heuristic", "random", "random"], 7)
    assert path.parts[0] == "runs"
    assert "heuristic" in path.name and "7" in path.name


def test_unknown_agent_names_are_rejected():
    with pytest.raises(ValueError, match="不认识的 agent"):
        make("teleporter")
    with pytest.raises(NotImplementedError, match="Phase 4"):
        make("douzero:WP")
    assert available() == ["heuristic", "random"]


def test_heuristic_beats_random_comfortably():
    """Phase 2 的验收标准：启发式对随机胜率远超 50%（这里跑小样本，快）。"""
    metrics, _ = run_match(["heuristic", "random", "random"], games=45, seed=17, rotate=True)
    stats = metrics.by_agent["heuristic"].summary()
    assert stats["win_rate"] > 0.65, stats
    assert stats["adp"] > 0
