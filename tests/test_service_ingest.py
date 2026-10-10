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


def test_health_reports_a_working_service(client):
    """没有 torch 时服务照常跑（douzero_loaded=false），装好了就报 true。"""
    from ddz.douzero.agent import is_ready

    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["records"] == 0
    assert body["douzero_loaded"] is is_ready()
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


def test_health_cuda_is_truthful(client):
    """CPU 版 torch 上 ``cuda`` 必须是 false。

    这条是有血的教训：以前这里报的是"torch 装了没有"，于是纯 CPU 的机器（``2.x+cpu``）
    也报 ``cuda:true``——浮窗页脚和运维都会照着这个撒谎的字段判断。
    """
    body = client.get("/health").json()
    if not body["torch"]:
        assert body["cuda"] is False
        return
    import torch

    assert body["cuda"] is bool(torch.cuda.is_available())


def test_health_says_which_python_it_runs_on(client):
    """服务跑在哪个解释器上必须一眼看得出来。

    ``python -m ddz.service`` 走的是 PATH 里第一个 python：在一个装了 fastapi、没装 torch 的
    conda 环境里，服务照常起来、面板照常出建议，只有 DouZero 永远"未就绪"（用户实际踩过）。
    有了这个字段，`/health` 一看路径就知道是不是起错环境了。
    """
    import sys

    body = client.get("/health").json()
    assert body["python"] == sys.executable
    assert body["python"]


def test_panel_self_report_is_archived(client, tmp_path):
    """浮窗的**自述**要能存下来：引擎、模式、开关、每手建议、有没有被平台托管。

    这一块是被真实对局的复盘逼出来的：存档里没有它，"这局面板到底帮没帮上忙"就只能靠推理
    （判断"是不是工具的锅"花了好几轮）。有了它，一条记录就能回答。
    """
    state = play_game(random.Random(3))[-1]
    panel = {
        "script": "0.5.3", "engine": "douzero", "auto": True, "auto_continue": True,
        "endgame_mode": "prefetch", "auto_actions": 12, "hosted": 1,
        "decisions": [{"v": 7, "cards": [3, 7], "source": "douzero", "engine": "douzero:WP",
                       "solver": 1, "ms": 12}],
    }
    body = client.post("/ingest", json={
        "state": state, "game_id": "p1", "panel": panel,
    }).json()
    assert body["ok"] is True and body["added"] is True

    from ddz.record import GameRecord

    saved = GameRecord.from_line((tmp_path / "games.jsonl").read_text(encoding="utf-8").strip())
    assert saved.panel == panel
    assert saved.panel["hosted"] == 1, "平台托管了几次，一眼可见"


def test_ingest_without_a_panel_block_still_works(client, tmp_path):
    """老脚本不发 panel —— 存档照旧（字段缺省即可）。"""
    state = play_game(random.Random(4))[-1]
    body = client.post("/ingest", json={"state": state, "game_id": "p2"}).json()
    assert body["ok"] is True
    from ddz.record import GameRecord

    saved = GameRecord.from_line((tmp_path / "games.jsonl").read_text(encoding="utf-8").strip())
    assert saved.panel is None
