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
    assert set(rec) >= {"cards", "pattern_key", "platform_key", "description", "score", "source", "rationale"}
    assert set(body["timing_ms"]) >= {"total", "parse"}
    assert set(body["engine"]) >= {"name", "fallback"}


def test_advise_accepts_the_state_wrapper(client):
    body = client.post("/advise", json={"session": "t1", "state": {"state": my_turn()}}).json()
    assert body["ok"] is True


def test_recommendation_carries_the_platform_key(client):
    """代打要用平台自己的 key 提交 choice，这里保证响应里带着它、格式对得上。"""
    from ddz import patterns as P

    cards = None
    for seed in (0, 3, 7):
        state = next(s for s in states(seed) if s["phase"] == "playing" and s["turn"] == s["seat"])
        body = client.post("/advise", json={"session": f"k{seed}", "state": state}).json()
        if body["ok"] and body["recommended"]["cards"]:
            cards = body["recommended"]
            break
    assert cards, "没找到有出牌建议的局面"
    pattern = P.Pattern.parse_key(cards["pattern_key"])
    assert cards["platform_key"] == pattern.platform_key()
    assert cards["platform_key"].split(":")[0] in set(P.PLATFORM_TYPES.values())


def test_pass_recommendation_has_no_platform_key(client):
    """「不出」没有牌型，platform_key 必须是 null（前端据此走点「不出」那条路）。"""
    for seed in range(8):
        state = next(s for s in states(seed) if s["phase"] == "playing" and s["turn"] == s["seat"])
        body = client.post("/advise", json={"session": f"p{seed}", "state": state}).json()
        if body["ok"] and body["recommended"] and not body["recommended"]["cards"]:
            assert body["recommended"]["platform_key"] is None
            return
    pytest.skip("这批种子里没有出现「不出」的建议")


def test_advise_accepts_game_id(client):
    """换局靠 game_id 区分：同一个 session 连着问两局，不能串。"""
    def index(seed):
        return {
            s["version"]: s
            for s in states(seed)
            if s["turn"] == s["seat"] and s["phase"] in ("bidding", "playing")
        }

    left, right = index(0), index(2)  # 这两局有大量重合的 version
    version = min(set(left) & set(right))  # 同一个 version 号，两局完全不同
    a = client.post("/advise", json={"session": "tab", "state": left[version], "game_id": "g1"}).json()
    b = client.post("/advise", json={"session": "tab", "state": right[version], "game_id": "g2"}).json()
    assert a["version"] == b["version"]
    assert a["hand"] != b["hand"], "换局后不能还拿上一局的手牌"


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
    """显式点名 DouZero：装好了就用它，没装好就明确报 no_engine（不静默换引擎）。"""
    from ddz.douzero.agent import is_ready

    body = client.post("/advise", json={
        "session": "t1", "state": my_turn(), "options": {"agent": "douzero:WP"},
    }).json()
    if is_ready():
        assert body["ok"] is True
        assert body["engine"]["name"] == "douzero:WP"
        assert body["recommended"]["win_prob"] is not None
    else:
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


def test_userscript_offers_two_engines():
    """浮窗要能选「启发式 / DouZero」两个引擎。"""
    source = USERSCRIPT.read_text(encoding="utf-8")
    assert 'data-engine="heuristic"' in source and 'data-engine="douzero"' in source
    assert "douzero:WP" in source
    assert "options: {agent: engineSpec()}" in source
    assert "engineSpec" in source


def test_userscript_only_acts_through_the_page_buttons():
    """代打只按**页面自己的按钮**：不自己构造请求打给游戏接口，也就不改协议、不加频率。

    （"不代打"曾经是设计前提，用户明确要求改成自动打牌；这条守住的是剩下的部分：
    我们只驱动页面已有的按钮，绝不绕过它自己发 play/pass/bid。）
    """
    source = USERSCRIPT.read_text(encoding="utf-8")
    # 平台的写操作全走 URLSearchParams{action: play|pass|bid|auto}——脚本里不该有
    assert "URLSearchParams" not in source
    assert "action=" not in source
    # 轮询频率是页面自己的事，脚本不许插手
    assert "poll" not in source.replace("polling", "")
    # 游戏接口只出现在 fetch 拦截里（只读旁路），有且仅有一次
    assert source.count("/games/doudizhu/api") == 1
    # 出站请求只打本地服务
    assert "fetch(API + path" in source and "nativeFetch(API + '/health'" in source


def test_userscript_looks_up_the_play_button_after_selecting():
    """回归：页面的「出牌」按钮在**没选牌时是 disabled 的**（``!sel.options.length``），
    所以必须先选牌、核对，再去查按钮；在选牌前查会永远查不到 → 自动出牌静默失效。"""
    source = USERSCRIPT.read_text(encoding="utf-8")
    body = source.split("function executeRecommended")[1].split("function autoContinue")[0]
    select_at = body.index("pointerdown")
    verify_at = body.index("ddz-selected")
    play_at = body.index("text === '出牌'")
    assert select_at < play_at, "先选牌再查「出牌」按钮"
    assert verify_at < play_at, "先核对选中再查按钮"


def test_userscript_separates_service_down_from_a_failed_request():
    """偶发一次请求失败 ≠ 服务没起来：只有 health 探不通才显示「服务未启动」。

    （浏览器复用已被服务端关掉的 keep-alive 连接时，POST 会偶发 Failed to fetch，
    GET 会被浏览器自动重试而 POST 不会——所以这两种情况必须分开报。）
    """
    source = USERSCRIPT.read_text(encoding="utf-8")
    assert "function probeHealth()" in source
    assert "serviceOk = ok" in source                      # 只有探活能改这个状态
    assert "requestFailures <= 3" in source                # 失败退避重试
    assert "setTimeout(requestAdvise, 300 * requestFailures)" in source
    # 请求失败不再直接判服务死刑：catch 块里不该出现 markServiceOk(false)
    catch_block = source.split(".catch((err) => {")[-1].split("});")[0]
    assert "markServiceOk(false)" not in catch_block
    assert "probeHealth()" in catch_block      # 连续失败才去探活确认


def test_server_keeps_connections_alive_long_enough():
    """服务端 keep-alive 不能太短，否则浏览器会复用到刚被关掉的连接。"""
    from ddz.service.__main__ import main  # noqa: F401
    import inspect

    source = inspect.getsource(__import__("ddz.service.__main__", fromlist=["main"]))
    assert "timeout_keep_alive" in source
    assert "--keep-alive" in source


def test_userscript_retries_until_the_dom_is_ready():
    """动作没做成（按钮还没渲染出来之类）要重试，不能静默什么都不做。"""
    source = USERSCRIPT.read_text(encoding="utf-8")
    assert "attempt < 6" in source
    assert "if (!ok && attempt" in source


def test_userscript_has_one_master_switch():
    """一个总开关管叫分/出牌/续局，不再分成三个。"""
    source = USERSCRIPT.read_text(encoding="utf-8")
    assert "auto-master" in source
    assert "自动打牌：" in source
    for gone in ("auto-bid", "auto-play", "auto-continue", "autoBid", "autoPlay"):
        assert gone not in source, f"应该已经合并成总开关了：{gone}"
    assert "if (!config.auto) return;" in source  # 所有自动动作的总闸


def test_userscript_automation_has_guards():
    """代打的四条护栏必须在：按 version 过期、可取消、提交前核对选中、只按一次。"""
    source = USERSCRIPT.read_text(encoding="utf-8")
    assert "lastActedVersion" in source          # 同一局面只动一次手
    assert "cancelPending" in source             # 局面一变就作废待执行动作
    assert "ddz-selected" in source              # 选完牌核对 DOM
    assert "放弃这一手" in source                 # 核对不过就放弃，不瞎点
    assert "continuedOnce" in source             # 续局只点一次
    assert "platform_key" in source              # 多种读法时按平台 key 拨下拉框
