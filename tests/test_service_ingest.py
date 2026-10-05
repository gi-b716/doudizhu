"""本地服务的 /health 与 /ingest（Phase 1 范围；/advise 在 Phase 3）。"""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from ddz.data.archive import Archive
from ddz.record import from_finished_state
from ddz.service.app import create_app
from gamegen import play_game


@pytest.fixture()
def client(tmp_path) -> TestClient:
    archive = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    with TestClient(create_app(archive=archive)) as test_client:
        yield test_client


def test_health_reports_a_working_service_without_torch(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["records"] == 0
    assert body["douzero_loaded"] is False
    assert isinstance(body["torch"], bool)
    assert body["uptime_s"] >= 0


def test_ingest_archives_a_finished_game(client, tmp_path):
    state = play_game(random.Random(3))[-1]
    response = client.post("/ingest", json={"state": state, "game_id": "777"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["added"] is True and body["duplicate"] is False
    assert body["game_id"] == "777"
    assert body["dedupe_key"]
    assert body["records"] == 1
    assert (tmp_path / "games.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert client.get("/health").json()["records"] == 1


def test_ingest_is_idempotent(client):
    state = play_game(random.Random(3))[-1]
    first = client.post("/ingest", json={"state": state}).json()
    second = client.post("/ingest", json={"state": state}).json()
    assert first["added"] is True
    assert second["ok"] is True and second["duplicate"] is True and second["added"] is False
    assert second["dedupe_key"] == first["dedupe_key"]
    assert client.get("/health").json()["records"] == 1


def test_records_count_includes_games_already_in_the_archive(tmp_path):
    """库里已有的对局要算进计数（不能从 0 重新数起）。"""
    archive = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    archive.add(from_finished_state(play_game(random.Random(3))[-1]))
    archive.add(from_finished_state(play_game(random.Random(4))[-1]))
    with TestClient(create_app(archive=archive)) as client:
        assert client.get("/health").json()["records"] == 2
        body = client.post("/ingest", json={"state": play_game(random.Random(5))[-1]}).json()
        assert body["records"] == 3
        assert client.post("/ingest", json={"state": play_game(random.Random(6))[-1]}).json()["records"] == 4
        assert client.get("/health").json()["records"] == 4


def test_ingest_keeps_raw_when_asked(client, tmp_path):
    state = play_game(random.Random(4))[-1]
    body = client.post("/ingest", json={"state": state, "game_id": "888", "keep_raw": True}).json()
    assert body["ok"] is True
    assert (tmp_path / "raw" / "888.json").exists()


def test_ingest_rejects_non_finished_states(client):
    state = play_game(random.Random(3))[0]
    body = client.post("/ingest", json={"state": state}).json()
    assert body["ok"] is False
    assert body["reason"] == "parse_error"
    assert "只归档终局" in body["message"]
    assert client.get("/health").json()["records"] == 0


def test_ingest_rejects_garbage_without_crashing(client):
    assert client.post("/ingest", json={"state": {"phase": "finished"}}).json()["ok"] is False
    assert client.post("/ingest", json={"nope": 1}).status_code == 422  # pydantic 拦下


def test_ingest_accepts_the_compact_state_wrapper(client):
    state = play_game(random.Random(5))[-1]
    body = client.post("/ingest", json={"state": {"state": state}}).json()
    assert body["ok"] is True


def test_private_network_preflight_is_allowed(client):
    """Chrome 的 PNA 预检要求显式回 Access-Control-Allow-Private-Network。"""
    response = client.options(
        "/ingest",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-private-network") == "true"
    assert response.headers.get("access-control-allow-origin") == "*"


def test_plain_preflight_has_no_pna_header(client):
    response = client.options(
        "/ingest",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-private-network" not in response.headers
