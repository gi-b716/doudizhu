"""HTTP 层：/advise /config 的形态与约定（逻辑问题也返回 200）。"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ddz.data.archive import Archive
from ddz.service.advisor import Advisor
from ddz.service.app import create_app
from gamegen import play_game

USERSCRIPT = Path(__file__).resolve().parents[1] / "userscript" / "ddz-copilot.user.js"


@pytest.fixture()
def client(tmp_path) -> TestClient:
    advisor = Advisor(archive=Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw"))
    with TestClient(create_app(advisor=advisor)) as test_client:
        yield test_client


def states(seed: int = 3, **kwargs):
    return play_game(random.Random(seed), **kwargs)


def my_turn(seed: int = 3) -> dict:
    return next(s for s in states(seed) if s["phase"] == "playing" and s["turn"] == s["seat"])


def test_advise_response_matches_the_planned_shape(client):
    response = client.post("/advise", json={"session": "t1", "state": my_turn()})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    for field in ("version", "seat", "phase", "is_my_turn", "leading", "to_beat",
                  "recommended", "alternatives", "card_counter", "threats",
                  "hand_analysis", "bidding", "timing_ms", "engine"):
        assert field in body, field
    rec = body["recommended"]
    assert set(rec) >= {"cards", "pattern_key", "description", "score", "source", "rationale"}
    assert set(body["timing_ms"]) >= {"total", "parse"}
    assert set(body["engine"]) >= {"name", "fallback"}


def test_advise_accepts_the_state_wrapper(client):
    body = client.post("/advise", json={"session": "t1", "state": {"state": my_turn()}}).json()
    assert body["ok"] is True


def test_advise_defaults_are_enough(client):
    """脚本可以只传 session + state，options 全默认。"""
    body = client.post("/advise", json={"session": "t1", "state": my_turn()}).json()
    assert body["ok"] is True


def test_not_my_turn_is_200_with_a_reason(client):
    state = next(s for s in states() if s["phase"] == "playing" and s["turn"] != s["seat"])
    response = client.post("/advise", json={"session": "t1", "state": state})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["reason"] == "not_my_turn"
    assert body["card_counter"]["unseen_total"] > 0


def test_missing_state_is_a_validation_error(client):
    assert client.post("/advise", json={"session": "t1"}).status_code == 422


def test_for_seat_option_over_http(client):
    state = next(s for s in states(seed=11, seat=None, admin=True) if s["phase"] == "playing")
    body = client.post("/advise", json={
        "session": "spec", "state": state, "options": {"for_seat": state["turn"]},
    }).json()
    assert body["seat"] == state["turn"]


def test_agent_option_can_force_the_engine(client):
    body = client.post("/advise", json={
        "session": "t1", "state": my_turn(), "options": {"agent": "douzero:WP"},
    }).json()
    assert body["ok"] is False and body["reason"] == "no_engine"


def test_config_endpoint_switches_the_engine(client):
    applied = client.post("/config", json={"default_agent": "heuristic", "top_k": 2}).json()
    assert applied["ok"] is True
    assert applied["applied"] == {"default_agent": "heuristic", "top_k": 2}
    assert applied["top_k"] == 2
    assert client.get("/health").json()["models"]["default"] == "heuristic"


def test_health_reports_records_after_ingest(client):
    assert client.get("/health").json()["records"] == 0
    client.post("/ingest", json={"state": states()[-1], "game_id": "h1"}).json()
    assert client.get("/health").json()["records"] == 1


def test_advise_after_ingest_still_works(client):
    client.post("/ingest", json={"state": states()[-1]})
    assert client.post("/advise", json={"session": "t1", "state": my_turn()}).json()["ok"] is True


def test_userscript_is_valid_javascript():
    """脚本没法在这里跑浏览器，但至少保证语法是对的（有 node 才检查）。"""
    assert USERSCRIPT.exists()
    source = USERSCRIPT.read_text(encoding="utf-8")
    assert "@grant        none" in source, "必须是 @grant none：要包住页面真实的 fetch"
    assert "@run-at       document-start" in source
    assert "window.fetch" in source and "clone()" in source
    assert "帮我选中" in source

    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node，跳过语法检查")
    result = subprocess.run([node, "--check", str(USERSCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_userscript_never_submits_moves():
    """安全姿态：只读建议，绝不向游戏接口发任何东西，也不改轮询频率（PLAN §6.5）。"""
    source = USERSCRIPT.read_text(encoding="utf-8")
    # 平台的写操作全走 URLSearchParams{action: play|pass|bid|auto}，脚本里不该有这些
    assert "URLSearchParams" not in source
    assert "action=" not in source
    # 轮询频率是页面自己的事，脚本不许插手
    assert "poll" not in source.replace("polling", "")
    # 所有出站请求都打本地服务（health 探测走原生 fetch，其它走 API 前缀）
    assert "fetch(API + path" in source and "nativeFetch(API + '/health')" in source
