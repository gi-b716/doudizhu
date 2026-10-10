"""复盘工具：判据必须硬——"求解器证明了能赢、实际没保住"才算失误。"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from ddz.engine.endgame import solvable, solve
from ddz.record import from_finished_state, iter_decisions
from ddz.tools.review import REVIEW_LIMITS, detail, scan

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gamegen import play_game  # noqa: E402


@pytest.fixture(scope="module")
def records():
    return [from_finished_state(play_game(random.Random(9000 + i))[-1]) for i in range(6)]


def test_scan_only_reports_real_misses(records):
    """扫出来的每一条都要经得起复核：那一手确实不在"能赢的着法"里。"""
    from ddz.tools.eval_records import action_key

    from ddz.record import replay

    findings = scan(records)
    for f in findings:
        # 复核：按报出来的事件序号重建局面、重判一次——必须真的是打丢
        record = next(r for r in records if r.game_id == f["game_id"])
        entry = record.events[f["index"]]
        view = replay(record, f["index"]).for_seat(entry.seat)
        solved = solve(view, limits=REVIEW_LIMITS)
        best = {None if m is None else action_key(m.cards) for m in (solved.best_moves or [])}
        played = action_key(entry.cards if entry.kind == "play" else None)
        assert played not in best
        assert solved.proved and solved.winning


def test_verdict_separates_lost_from_thrown():
    """判定必须三种分开：**本来就必败**绝不能念成"打丢了必胜"。

    求解器证明必败时 ``best_moves`` 是空的——只用一个布尔值判断的话，
    "实际这一手不在能赢的着法里"对必败局面也成立，于是每一手都会变成"打丢了"。
    """
    from types import SimpleNamespace

    from ddz.cards import parse_cards
    from ddz.tools.review import verdict_of

    def solved(winning=True, proved=True, best=(), solo=False):
        return SimpleNamespace(
            winning=winning, proved=proved, solo=solo, nodes=7,
            best_moves=[None if c is None else SimpleNamespace(cards=c) for c in best],
        )

    pair = parse_cards("4,5")      # 两张 4（牌 id 是 rank*4+suit）
    other = parse_cards("0")       # 一张 3
    assert verdict_of(None, other)["code"] == "not_solvable"
    assert verdict_of(solved(proved=False), other)["code"] == "unknown"
    assert verdict_of(solved(winning=False), other)["code"] == "lose"
    kept = verdict_of(solved(best=[pair]), pair)
    assert kept["code"] == "held" and "保住" in kept["text"]
    threw = verdict_of(solved(best=[pair]), other)
    assert threw["code"] == "threw_win" and "打丢" in threw["text"]
    # 能赢的着法里包含"不出"时，跟牌时该 Pass 也保得住
    assert verdict_of(solved(best=[None]), None)["code"] == "held"


def test_detail_reports_all_three_sides(records):
    """某一手的详细对比：三家手牌、实际、各引擎、求解器结论，一个都不能少。"""
    from ddz.record import replay

    record = records[0]
    index = next(
        i for i, entry in enumerate(record.events)
        if entry.is_action() and solvable(replay(record, i).for_seat(entry.seat))
    )
    info = detail(record, index)
    assert info["game_id"] == record.game_id and info["index"] == index
    assert set(info["hands"]) == {0, 1, 2}
    assert info["landlord"] in (0, 1, 2)
    assert "played" in info and "engines" in info
    assert info["solver"] is not None
