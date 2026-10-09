"""叫分门槛 A/B 工具：结构与可复现性（不跑大样本，只保证它能跑通、口径正确）。"""

from __future__ import annotations

from ddz.engine.bidding import BidThresholds
from ddz.tools import bid_ab


def test_run_ab_returns_one_score_per_game_for_the_candidate_seat():
    scores = bid_ab.run_ab("heuristic", BidThresholds(10.0, 10.0, 10.0), BidThresholds(-2.0, 2.0, 6.0), 9, 123)
    assert len(scores) == 9
    assert all(isinstance(s, int) for s in scores)


def test_run_ab_is_reproducible_for_the_same_seed():
    args = ("heuristic", BidThresholds(10.0, 10.0, 10.0), BidThresholds(-2.0, 2.0, 6.0), 6, 77)
    assert bid_ab.run_ab(*args) == bid_ab.run_ab(*args)


def test_cli_prints_the_summary_line(capsys):
    code = bid_ab.main(["--candidate", "10", "--games", "3", "--seed", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "ADP" in out and "候选 10" in out and "基线 -2,2,6" in out


def test_cli_rejects_a_bad_threshold(capsys):
    import pytest

    with pytest.raises(SystemExit):
        bid_ab.main(["--candidate", "6,8"])
