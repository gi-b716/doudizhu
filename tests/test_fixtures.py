"""对 :file:`tests/fixtures/*.json` 里的每一份终局 state 跑一遍完整流水线。

**把真实抓到的终局 state 丢进 ``tests/fixtures/`` 即可**（文件名随意）：这个测试会自动
带上它——适配、归档、重放不变量、发牌承诺复核全都过一遍。不是终局完美信息 state 的文件
会被跳过（只提示，不算失败）。

现在库里那份是随机对局生成的（``gamegen``），用作不依赖生成器改动的回归样本。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddz import cards as C
from ddz.record import from_finished_state, iter_decisions, iter_frames
from ddz.state import PHASE_BIDDING, PHASE_FINISHED, PHASE_PLAYING
from ddz.tools.verify_fairness import check_record

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def test_there_is_at_least_one_fixture():
    assert FIXTURES, f"{FIXTURE_DIR} 里没有样本"


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_fixture_pipeline(path: Path):
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("phase") != PHASE_FINISHED or not (state.get("fairness") or {}).get("deals"):
        pytest.skip(f"{path.name} 不是终局完美信息 state")

    record = from_finished_state(state, game_id=path.stem)
    assert record.validate() == []
    assert all(check.ok for check in check_record(record)), "发牌承诺复核失败"

    frames = list(iter_frames(record))
    assert len(frames) == len(record.events) + 1
    assert frames[0].phase == PHASE_BIDDING
    assert frames[-1].phase == PHASE_FINISHED

    previous = 0
    for frame in frames:
        if frame.phase in (PHASE_PLAYING, PHASE_FINISHED):
            hands = [c for h in frame.all_hands for c in h]
            played = list(frame.played_cards)
            assert len(hands) + len(played) == C.DECK_SIZE
            assert not (set(hands) & set(played))
            assert frame.bottom is not None and len(frame.bottom) == 3
        assert frame.multiplier >= previous
        previous = frame.multiplier

    decisions = list(iter_decisions(record, include_passes=False))
    assert decisions
    for decision in decisions:
        assert set(decision.move.cards) <= set(decision.state.hand)
