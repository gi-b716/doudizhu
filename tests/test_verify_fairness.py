"""发牌承诺复核：算法对齐客户端，篡改必须被抓出来。"""

from __future__ import annotations

import copy
import json
import random

import pytest

from ddz.data.archive import Archive
from ddz.record import from_finished_state
from ddz.tools.verify_fairness import check_deal, check_record, commitment_of, main
from gamegen import commitment_of as gen_commitment, play_game


def finished(seed: int) -> dict:
    return play_game(random.Random(seed))[-1]


def test_commitment_algorithm_matches_the_client():
    """客户端：sha256(deck.join(',') + '|' + salt)。"""
    state = finished(3)
    deal = state["fairness"]["deals"][0]
    expected = gen_commitment(deal["deck"], deal["salt"])
    assert commitment_of(deal["deck"], deal["salt"]) == expected == deal["commitment"]


def test_generated_deals_verify():
    record = from_finished_state(finished(3))
    checks = check_record(record)
    assert checks and all(c.ok for c in checks)


def test_tampered_salt_is_caught():
    record = from_finished_state(finished(3))
    record.deals[0].salt = "00" * 16
    check = check_deal(record.game_id, record.deals[0])
    assert not check.ok and "承诺不符" in check.reason


def test_swapped_cards_are_caught():
    record = from_finished_state(finished(3))
    deck = list(record.deals[0].deck)
    deck[0], deck[1] = deck[1], deck[0]
    record.deals[0].deck = tuple(deck)
    check = check_deal(record.game_id, record.deals[0])
    assert not check.ok and "承诺不符" in check.reason


def test_deck_that_is_not_a_permutation_is_caught():
    record = from_finished_state(finished(3))
    deck = list(record.deals[0].deck)
    deck[0] = deck[1]
    record.deals[0].deck = tuple(deck)
    check = check_deal(record.game_id, record.deals[0])
    assert not check.ok and "排列" in check.reason


def test_missing_commitment_or_salt_is_caught():
    record = from_finished_state(finished(3))
    record.deals[0].commitment = ""
    assert not check_deal(record.game_id, record.deals[0]).ok
    record = from_finished_state(finished(3))
    record.deals[0].salt = ""
    assert not check_deal(record.game_id, record.deals[0]).ok


def test_cli_reports_success_and_failure(tmp_path, capsys):
    archive = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    archive.add(from_finished_state(finished(3)))
    archive.add(from_finished_state(finished(4)))

    assert main(["--records", str(archive.path)]) == 0
    assert "副通过" in capsys.readouterr().out

    # 篡改第二行里的盐
    lines = archive.path.read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[1])
    payload["deals"][0]["salt"] = "deadbeef"
    lines[1] = json.dumps(payload, ensure_ascii=False)
    archive.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--records", str(archive.path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1
    assert report["deals"] == 2
    assert report["failures"][0]["reason"].startswith("承诺不符")


def test_cli_on_a_missing_file_is_a_no_op(tmp_path, capsys):
    assert main(["--records", str(tmp_path / "nothing.jsonl")]) == 0
    assert "共 0 局" in capsys.readouterr().out
