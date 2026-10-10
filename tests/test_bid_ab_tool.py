"""叫分阈值 A/B 工具：结构与可复现性（不跑大样本，只保证它能跑通、口径正确）。"""

from __future__ import annotations

from collections import Counter

import pytest

from ddz.engine.bidding import BidThresholds, thresholds_of_tier
from ddz.tools import bid_ab

LOOSE = thresholds_of_tier("loose")
STRICT = thresholds_of_tier("strict")


def test_run_ab_returns_one_sample_per_game_for_the_candidate_seat():
    samples = bid_ab.run_ab("heuristic", STRICT, LOOSE, 9, 123)
    assert len(samples) == 9
    for sample in samples:
        assert isinstance(sample.delta, int)
        assert sample.base in (1, 2, 3)
        assert sample.multiplier >= 1
        # 零和：候选 + 两个基线座位之和必须是 0（rules.md 的记分口径）
        assert sample.delta + sum(sample.baseline_deltas) == 0
        assert sample.won == (sample.delta > 0)
        assert sample.seat in (0, 1, 2)


def test_run_ab_is_reproducible_for_the_same_seed():
    args = ("heuristic", STRICT, LOOSE, 6, 77)
    assert bid_ab.run_ab(*args) == bid_ab.run_ab(*args)


def test_summarize_splits_by_role_and_base():
    samples = bid_ab.run_ab("heuristic", STRICT, LOOSE, 30, 4242)
    stats = bid_ab.summarize(samples)
    assert stats["games"] == 30
    assert stats["groups"]["地主"]["games"] + stats["groups"]["农民"]["games"] == 30
    by_base = sum(stats["groups"][f"底分{b}"]["games"] for b in (1, 2, 3))
    assert by_base == 30, "每一局都有一个最终底分"
    # 轮换座位：三个座位各坐 10 局（不轮换的话候选会一直坐同一个位，ADP 就没法比）
    seats = Counter(sample.seat for sample in samples)
    assert seats == Counter({0: 10, 1: 10, 2: 10})
    assert pytest.approx(stats["adp"], abs=1e-9) == sum(s.delta for s in samples) / len(samples)
    assert pytest.approx(stats["baseline_adp"], abs=1e-9) == -stats["adp"] / 2


def test_cli_prints_the_summary_line(capsys):
    code = bid_ab.main(["--candidate", "strict", "--games", "3", "--seed", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "ADP" in out and "候选 strict" in out and "基线 loose" in out
    assert "切分" in out and "农民" in out


def test_cli_takes_plain_numbers_too(capsys):
    code = bid_ab.main(["--candidate", "10", "--baseline", "4,8,12", "--games", "3", "--seed", "1"])
    out = capsys.readouterr().out
    assert code == 0 and "候选 10" in out and "基线 4,8,12" in out


def test_cli_quiet_prints_only_the_headline(capsys):
    bid_ab.main(["--candidate", "strict", "--games", "3", "--seed", "1", "--quiet"])
    out = capsys.readouterr().out
    assert "ADP" in out and "切分" not in out


def test_cli_rejects_a_bad_threshold(capsys):
    with pytest.raises(SystemExit):
        bid_ab.main(["--candidate", "6,8"])
    with pytest.raises(SystemExit):
        bid_ab.main(["--candidate", "很严"])


def test_thresholds_reach_the_agent_as_objects():
    """三元组、字典、现成对象都要能变成 BidThresholds（竞技场 CLI 会这么传）。"""
    from ddz.engine.bidding import as_thresholds

    assert as_thresholds(None) is None
    assert as_thresholds((4, 8, 12)) == BidThresholds(4.0, 8.0, 12.0)
    assert as_thresholds({"one": 4, "two": 8, "three": 12}) == BidThresholds(4.0, 8.0, 12.0)
    assert as_thresholds(STRICT) is STRICT
