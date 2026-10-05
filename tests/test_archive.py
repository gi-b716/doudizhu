"""对局库归档：JSONL 往返、按发牌去重、--keep-raw、坏行容错。"""

from __future__ import annotations

import json
import random

import pytest

from ddz.data.archive import Archive, ArchiveError, data_dir, games_path, raw_dir
from ddz.record import SCHEMA, GameRecord, from_finished_state
from gamegen import play_game


def make_record(seed: int, **kwargs) -> GameRecord:
    state = play_game(random.Random(seed), **kwargs)[-1]
    return from_finished_state(state)


@pytest.fixture()
def archive(tmp_path) -> Archive:
    return Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")


def test_add_writes_one_line_per_game(archive):
    assert archive.add(make_record(3)) is True
    assert archive.add(make_record(4)) is True
    lines = archive.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["schema"] == SCHEMA for line in lines)
    assert archive.count() == 2


def test_duplicate_games_are_rejected(archive):
    record = make_record(3)
    assert archive.add(record) is True
    assert archive.add(make_record(3)) is False  # 同一副发牌
    assert archive.count() == 1
    assert record.dedupe_key() in archive.keys()


def test_archive_is_append_only_across_instances(tmp_path):
    first = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    first.add(make_record(3))
    second = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    assert second.count() == 1
    assert second.add(make_record(3)) is False
    assert second.add(make_record(4)) is True
    assert second.count() == 2


def test_keep_raw_saves_the_original_state(tmp_path):
    state = play_game(random.Random(3))[-1]
    archive = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    record = from_finished_state(state, game_id="4242")
    assert archive.add(record, raw=state, keep_raw=True) is True
    saved = tmp_path / "raw" / "4242.json"
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8"))["fairness"]["deals"][0]["deck"] == state["fairness"]["deals"][0]["deck"]


def test_keep_raw_is_off_by_default(tmp_path):
    state = play_game(random.Random(3))[-1]
    archive = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    archive.add(from_finished_state(state, game_id="4242"), raw=state)
    assert not (tmp_path / "raw" / "4242.json").exists()


def test_inconsistent_records_are_refused(archive):
    record = make_record(3)
    record.events[0].kind = "teleport"
    with pytest.raises(ArchiveError, match="不自洽"):
        archive.add(record)
    assert not archive.path.exists() or archive.count() == 0


def test_broken_lines_are_reported_or_skipped(archive):
    archive.path.parent.mkdir(parents=True, exist_ok=True)
    archive.add(make_record(3))
    with archive.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    with pytest.raises(ArchiveError, match="解析失败"):
        list(archive.records())
    assert len(list(archive.records(strict=False))) == 1


def test_unknown_schema_line_is_skipped_non_strictly(archive):
    archive.path.parent.mkdir(parents=True, exist_ok=True)
    archive.path.write_text(json.dumps({"schema": "ddz.record.v9", "deals": []}) + "\n", encoding="utf-8")
    with pytest.raises(ArchiveError):
        list(archive.records())
    assert list(archive.records(strict=False)) == []


def test_missing_file_reads_as_empty(tmp_path):
    archive = Archive(tmp_path / "nope.jsonl", raw_path=tmp_path / "raw")
    assert archive.count() == 0
    assert list(archive.records()) == []
    assert archive.keys() == set()


def test_default_paths_come_from_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DDZ_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path
    assert games_path() == tmp_path / "games.jsonl"
    assert raw_dir() == tmp_path / "raw"
    archive = Archive()
    assert archive.path == tmp_path / "games.jsonl"


def test_records_round_trip_through_the_archive(archive):
    original = make_record(3)
    archive.add(original)
    restored = next(iter(archive.records()))
    assert restored.dedupe_key() == original.dedupe_key()
    assert restored.to_dict() == original.to_dict()
