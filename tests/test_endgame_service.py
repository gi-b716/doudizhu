"""残局求解在服务里的行为：什么时候解、什么时候不解、账怎么记。

两条主要的路：**看得见三家手牌**（管理员/复盘/竞技场）按张数卡；**看不见**（真实对局）
按"未见牌张数"卡，走"对一切可能的分配都赢"那条。off 模式一处都不许用求解器。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ddz.data.archive import Archive
from ddz.engine.endgame import solve
from ddz.service.advisor import Advisor
from ddz.service.app import create_app
from helpers import platform_endgame


@pytest.fixture()
def advisor(tmp_path) -> Advisor:
    return Advisor(archive=Archive(tmp_path / "g.jsonl", raw_path=tmp_path / "raw"))


@pytest.fixture()
def client(advisor) -> TestClient:
    with TestClient(create_app(advisor=advisor)) as test_client:
        yield test_client


def endgame_payload():
    from ddz.cards import parse_cards
    from ddz.patterns import Pattern

    return platform_endgame(
        ["小王 大王", "3", "8 7"], turn=0, landlord=2,
        to_beat=Pattern("single", 5), play_cards=parse_cards("8"),
    )


def wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_default_mode_is_ondemand(advisor):
    """默认按需求解（预取那套删了：实测它在实盘买不到东西）。"""
    assert advisor.endgame_mode == "ondemand"
    assert advisor.endgame_status()["mode"] == "ondemand"


def test_health_reports_the_mode_and_the_gates(client):
    """浮窗要显示当前模式，所以 /health 必须报出来。"""
    body = client.get("/health").json()["endgame"]
    assert body["mode"] == "ondemand"
    assert body["gate_cards"] == 20
    assert body["max_unseen"] == 8


def test_midgame_and_big_endgames_never_touch_the_solver(advisor, client):
    """中局、以及"看不见手牌且未见牌太多"的局面，一条建议都不许让求解器碰。"""
    import random

    from gamegen import play_game

    mid = next(s for s in play_game(random.Random(11), admin=True)
               if s["phase"] == "playing" and s["turn"] == s["seat"])
    result = client.post("/advise", json={"session": "m1", "state": mid}).json()
    assert result["solver"] is None
    assert result["recommended"]["source"] != "solver"

    big = platform_endgame(["K K 3 3 3", "7 7 8 8 J 2 小王", "4 6 7 9 9 10 10 Q A 2"],
                           turn=2, landlord=0)
    result = client.post("/advise", json={"session": "m2", "state": big}).json()
    assert result["solver"] is None
    assert advisor.endgame_stats["solves"] == 0


def test_off_mode_never_touches_the_solver(tmp_path):
    """``--endgame off`` 要真的关掉：建议里没有 solver 块。"""
    advisor = Advisor(
        archive=Archive(tmp_path / "g.jsonl", raw_path=tmp_path / "raw"), endgame_mode="off",
    )
    with TestClient(create_app(advisor=advisor)) as client:
        payload = endgame_payload()
        assert client.get("/health").json()["endgame"]["mode"] == "off"

        result = client.post("/advise", json={
            "session": "o1", "state": payload, "options": {"agent": "heuristic"},
        }).json()
        assert result["ok"] is True
        assert result["solver"] is None
        assert result["recommended"]["source"] != "solver"


def test_the_on_demand_gates(advisor):
    """两道门各管一段：**看得见手牌**按张数卡（20 张），**看不见**按未见牌卡（8 张）。

    为什么要分两套：真实对局看不到别家手牌，那时候"牌有多少"不是关键，
    **不确定性有多大**才是——未见 8 张要枚举的分配数还在可控范围。
    """
    ondemand, blind = advisor.endgame_limits(), advisor.uncertain_limits()
    assert ondemand.max_total == 20, "看得见手牌时按张数卡"
    assert blind.max_unseen == 8, "看不见手牌时按未见牌张数卡"
    assert ondemand.time_budget_ms <= 300 and blind.time_budget_ms <= 300


def test_midgame_and_big_endgames_never_touch_the_solver(advisor, client):
    """中局和"还没到门槛"的局面，一条建议都不许让求解器碰。"""
    import random

    from gamegen import play_game

    mid = next(s for s in play_game(random.Random(11))
               if s["phase"] == "playing" and s["turn"] == s["seat"])
    result = client.post("/advise", json={"session": "m1", "state": mid}).json()
    assert result["solver"] is None
    assert result["recommended"]["source"] != "solver"

    big = platform_endgame(["K K 3 3 3", "7 7 8 8 J 2 小王", "4 6 7 9 9 10 10 Q A 2"],
                           turn=2, landlord=0)
    result = client.post("/advise", json={"session": "m2", "state": big}).json()
    assert result["solver"] is None
    assert advisor.endgame_stats["skipped_gate"] + advisor.endgame_stats["skipped_no_hands"] >= 2
    assert advisor.endgame_stats["solves"] == 0


def test_skips_are_split_by_reason(advisor, client):
    """**"看不到手牌"和"牌太多"必须分开记**。

    这条是被真实对局的复盘逼出来的：用户库里有 8 处"求解器能证明能赢、实际却打丢"，
    而且**全部都在门槛之内（12~20 张）**——说明求解器压根没跑。要么是服务没重启，
    要么是平台不给普通对局下发别家手牌（`src/doudizhu.js` 里写着"超级用户才收得到所有手牌"）。
    分开记账，一眼就能分清是哪一种。
    """
    from gamegen import play_game
    import random

    mid = next(s for s in play_game(random.Random(11), admin=True)
               if s["phase"] == "playing" and s["turn"] == s["seat"])
    client.post("/advise", json={"session": "r1", "state": mid})       # 有 hands，但牌多
    from helpers import complete_payload

    # 看不见手牌 **而且** 未见牌太多（>8 张）：这才该记在"看不到手牌"那一栏
    # 未见牌 = 两家的手牌 = 5 + 5 = 10 张 > 上限 8 —— 这才该被"看不到手牌"拦下
    big_blind = complete_payload(["3 4 5", "6 7 8 9 10", "J Q K A 2"], turn=0, landlord=2)
    big_blind.pop("hands")
    client.post("/advise", json={"session": "r2", "state": big_blind})

    stats = client.get("/health").json()["endgame"]["stats"]
    assert stats["skipped_gate"] >= 1, "中局该记在「牌太多」那一栏"
    assert stats["skipped_no_hands"] >= 1, "看不到手牌要单独记一笔（这是平台行为，不是我们的取舍）"


def test_a_blind_endgame_gets_solved(advisor, client):
    """**真实对局的样子**：看不到别家手牌，但残局够小 —— 这条路现在能算了。

    这是这一版的重点：以前 `all_hands is None` 就直接放弃（于是实盘一次都没生效），
    现在改用"对一切可能的分配都赢"，门槛按**未见牌张数**卡。
    """
    from helpers import complete_payload

    # 注意用 complete_payload 而不是 platform_endgame：真实对局里 54 张牌全有归属，
    # 求解器依赖"未见牌 = 两家手牌"这个等式（手搓的残缺 state 会被正确地拒绝）
    state = complete_payload(["小王 大王", "3", "4 5"], turn=0, landlord=1)
    state.pop("hands")          # 看不见别家的牌 = 真实对局的样子
    result = client.post("/advise", json={"session": "b1", "state": state}).json()
    assert result["ok"] is True
    assert result["hands_visible"] is False
    assert result["solver"] is not None and result["solver"]["used"] is True
    assert "无论对手的牌怎么分" in result["recommended"]["rationale"][0]
    assert advisor.endgame_stats["solves_uncertain"] == 1


def test_health_accounts_for_every_solver_call(advisor, client):
    """``/health`` 要能回答"它到底在什么时候干活"——跳过几次、解了几次、平均花多少。"""
    mid = None
    import random

    from gamegen import play_game

    mid = next(s for s in play_game(random.Random(11), admin=True)
               if s["phase"] == "playing" and s["turn"] == s["seat"])
    client.post("/advise", json={"session": "a1", "state": mid})          # 跳过
    client.post("/advise", json={"session": "a2", "state": endgame_payload()})  # 解一次

    stats = client.get("/health").json()["endgame"]["stats"]
    assert stats["skipped_gate"] + stats["skipped_no_hands"] >= 1 and stats["solves"] >= 1
    assert stats["proved"] + stats["exhausted"] == stats["solves"]
    assert stats["solve_ms"] > 0 and stats["avg_ms"] <= 200
    assert client.get("/health").json()["endgame"]["gate_cards"] == 20


def test_a_state_without_hands_is_reported_separately(advisor, client):
    """**普通对局可能压根没有别家手牌**——这时求解器一次都跑不起来，必须能一眼看出来。

    这条是被真实对局逼出来的：用户库里有 8 处"求解器能证明能赢、实际却打丢"，而且全在
    门槛之内（12~20 张）。要么服务没重启，要么平台不给普通用户发 hands ——
    ``src/doudizhu.js`` 里那句注释是后者："Super users receive every hand"。
    两种原因的**处置完全不同**（重启 vs 换方案），所以账要分开记。
    """
    import random

    from gamegen import play_game

    # 默认（非 admin）的 state 的 hands 是 null——和真实平台给普通玩家的形状一致
    hidden = next(s for s in play_game(random.Random(11))
                  if s["phase"] == "playing" and s["turn"] == s["seat"])
    assert not hidden.get("hands"), "前提变了：非 admin 的 state 现在带 hands 了"
    result = client.post("/advise", json={"session": "h1", "state": hidden}).json()
    assert result["ok"] is True
    assert result["hands_visible"] is False
    # 中局：未见牌太多，枚举不动 —— 求解器仍然不该出手
    assert result["solver"] is None
    stats = client.get("/health").json()["endgame"]["stats"]
    assert stats["skipped_no_hands"] >= 1 and stats["solves"] == 0
