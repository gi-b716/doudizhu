"""复盘页的 HTTP 面：接口形状、护栏，以及"网页和命令行不许各说各的"。

三条纪律值得单独钉住：

- **页面自带一切**（服务跑在离线机器上，引一个 CDN 就白屏）；
- **护栏杆卡在"是不是出牌/不出"上**，不是卡范围——对着叫分事件求解会返回一份
  看着挺像的**假分析**（引擎照样给建议、牌型字段全空），比报错更糟；
- **服务不许重新装载引擎**：``detail()`` 默认现做现用，每点一手就重装一次 DouZero 权重。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ddz.data.archive import Archive
from ddz.record import from_finished_state, replay
from ddz.service.advisor import Advisor
from ddz.service.app import create_app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gamegen import play_game  # noqa: E402

GAME = "W1"


@pytest.fixture(scope="module")
def record():
    return from_finished_state(play_game(random.Random(3001))[-1], game_id=GAME)


@pytest.fixture()
def client(tmp_path, record):
    archive = Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw")
    archive.add(record)
    advisor = Advisor(archive=archive)
    with TestClient(create_app(advisor=advisor)) as test_client:
        yield test_client


def _decisions(client) -> list[dict]:
    return client.get(f"/review/game/{GAME}").json()["game"]["decisions"]


def _solvable_index(client) -> int:
    return next(d["index"] for d in _decisions(client) if d["solvable"])


def test_the_page_is_self_contained(client):
    """页面必须自带一切：服务跑在离线机器上，引任何外部资源都会白屏。"""
    res = client.get("/review")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    # no-store：不然改完页面刷新没反应，看着像 JS 写错了
    assert res.headers["cache-control"] == "no-store"
    body = res.text
    for bad in ("https://", "http://", "<script src", "<link", "<img"):
        assert bad not in body, f"页面里有外部资源：{bad}"


def test_games_list(client):
    body = client.get("/review/games").json()
    assert body["ok"] is True
    row = body["games"][0]
    assert row["game_id"] == GAME
    assert {"at", "delta", "role", "base", "multiplier", "panel_brief"} <= set(row)
    assert client.get("/review/games?limit=0").status_code == 422, "limit 要有下界（负数和 0 都该被拒）"


def test_skeleton_matches_the_replay(client, record):
    """帧比事件多一条，第 k 帧是 events[k] **执行之前**；手牌要和 replay 出来的一模一样。"""
    game = client.get(f"/review/game/{GAME}").json()["game"]
    assert len(game["frames"]) == len(game["events"]) + 1
    assert [d["index"] for d in game["decisions"]] == [
        i for i, e in enumerate(record.events) if e.is_action() and e.seat in (0, 1, 2)]

    k = game["decisions"][0]["index"]
    state = replay(record, k)
    assert game["frames"][k]["hands"] == [list(state.hand_for(s) or ()) for s in (0, 1, 2)]
    assert game["frames"][0]["phase"] == "bidding"
    assert game["frames"][-1]["result"] is not None, "最后一帧要带上结算"


def test_only_real_decisions_can_be_analyzed(client, record):
    """护栏卡在"是不是出牌/不出"上：叫分、结算、越界一律拒绝，且**不是** 500。"""
    bid = next(i for i, e in enumerate(record.events) if e.kind == "bid")
    assert client.get(f"/review/game/{GAME}/decision/{bid}").json()["reason"] == "not_a_decision"

    last = len(record.events) - 1
    assert record.events[last].kind == "finish", "前提变了：最后一条事件不是 finish"
    assert client.get(f"/review/game/{GAME}/decision/{last}").json()["reason"] == "not_a_decision"
    assert client.get(f"/review/game/{GAME}/decision/9999").json()["reason"] == "not_a_decision"

    missing = client.get("/review/game/没有这一局").json()
    assert missing["ok"] is False and missing["reason"] == "no_such_game"
    # 还没归档的局：不是错误，要说清"等终局归档"
    assert "归档" in missing["message"]


def test_the_service_never_reloads_the_engine(monkeypatch, client):
    """**最容易悄悄退化的地方**：服务必须走 advisor.agent_for（带缓存），不许自己 make()。

    把 ``make`` 换成炸弹——路由只要碰它，这条就红。真退化了的症状是"每点一手
    重装一次 DouZero 权重"（1.6 秒起步，外加一行日志），但功能看起来完全正常。
    """
    import ddz.tools.review as review_tools

    def boom(*args, **kwargs):
        raise AssertionError("服务不许调 make()：那会每点一手就重装一次 DouZero 权重")

    monkeypatch.setattr(review_tools, "make", boom)
    body = client.get(f"/review/game/{GAME}/decision/{_solvable_index(client)}").json()
    assert body["ok"] is True


def test_an_unready_engine_is_not_a_fake_second_opinion(client):
    """DouZero 没就绪时要**明说未就绪**，不能拿启发式的答案冒充它（两行会一模一样）。"""
    engines = client.get(
        f"/review/game/{GAME}/decision/{_solvable_index(client)}"
    ).json()["decision"]["engines"]
    assert engines["heuristic"].get("cards") is not None, "启发式永远可用"
    douzero = engines["douzero:WP"]
    if "error" in douzero:
        assert douzero["error"] and douzero != engines["heuristic"]
    else:
        assert "cards" in douzero


def test_an_empty_archive_is_a_friendly_answer(tmp_path):
    """空档案（刚装好、还没打）不是错误——这也是一键包第一次打开时的样子。"""
    advisor = Advisor(archive=Archive(tmp_path / "games.jsonl", raw_path=tmp_path / "raw"))
    with TestClient(create_app(advisor=advisor)) as client:
        body = client.get("/review/games").json()
        assert body["ok"] is True and body["games"] == []
        assert "归档" in body["message"], "要说清「打一局就会归档」，别只给个空列表"
        assert client.get("/review/game/1").json()["reason"] == "no_such_game"


def test_the_page_javascript_parses(tmp_path):
    """页面里的 JS 要过一遍 node 的语法检查。

    没有构建步骤的代价就是"写错了没人拦"——这里补上。node 本来就是这个项目测试的
    一部分（平台规则引擎那套就是拿 node 跑的），没装就跳过。
    """
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node")
    html = (Path(__file__).resolve().parents[1] / "ddz" / "service" / "review.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "页面里得有脚本"
    path = tmp_path / "page.js"
    path.write_text("\n".join(scripts), encoding="utf-8")
    proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_payload_has_every_field_the_page_reads(client):
    """页面读的字段必须真的在。

    JS 里把 ``row.panel_brief`` 写成 ``row.brief`` 不会报错——只会静默显示空白，
    而且只有人肉打开页面才看得见。这里按页面真正读的那份清单点一遍名。
    """
    row = client.get("/review/games").json()["games"][0]
    assert {"game_id", "at", "delta", "role", "base", "multiplier", "hosted"} <= set(row)

    game = client.get(f"/review/game/{GAME}").json()["game"]
    assert {"game_id", "captured_at", "players", "my_seat", "result",
            "panel_summary", "frames", "events", "decisions"} <= set(game)
    assert {"base", "multiplier", "deltas", "landlord_won"} <= set(game["result"])
    frame = game["frames"][0]
    assert {"index", "turn", "landlord", "counts", "hands", "to_beat",
            "last", "table", "bottom"} <= set(frame)
    # Event.to_dict() 是**稀疏**的：用不上的字段压根不出现（`cards` 只在出牌事件上有）。
    # 页面里到处写的 `(ev.cards || [])` 就是在兜这个——这里只点名必备字段。
    assert all({"kind", "seat"} <= set(e) for e in game["events"])
    assert "cards" in next(e for e in game["events"] if e["kind"] == "play")
    assert {"index", "seat", "solvable"} <= set(game["decisions"][0])

    index = next(d["index"] for d in game["decisions"] if d["solvable"])
    info = client.get(f"/review/game/{GAME}/decision/{index}").json()["decision"]
    assert {"index", "seat", "role", "hands", "to_beat", "played",
            "engines", "solver", "verdict"} <= set(info)
    assert {"cards", "kind", "text"} <= set(info["played"])
    assert {"code", "text"} <= set(info["verdict"])
    if info["solver"] is not None:
        assert {"winning", "proved", "nodes", "best", "best_text", "solo"} <= set(info["solver"])


def test_the_page_and_the_cli_flag_the_same_misses(client, record):
    """``scan()`` 报出来的每一处"打丢必胜"，网页上点开同一手也必须标红。"""
    from ddz.tools.review import scan

    for finding in scan([record]):
        body = client.get(f"/review/game/{GAME}/decision/{finding['index']}").json()
        assert body["decision"]["verdict"]["code"] == "threw_win"
        assert body["decision"]["is_me"] == (finding["seat"] == record.seat_of_interest)
