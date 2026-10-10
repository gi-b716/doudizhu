"""战报工具：它的全部价值在**切分维度**上——按「角色 × 底分」切，才看得出亏损在哪一格。

这些断言盯的都是"数字不许骗人"：地主座位与计分定义一致、各格相加等于总账、
遵循率的分母只能是"建议条数"（拿"我实际出手"当分母会混进浮窗没给建议的回合）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from ddz.record import from_finished_state
from ddz.tools.report import _bucket_lines, landlord_seat, report, role_of, summarize

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gamegen import play_game  # noqa: E402


def _record(seed: int, panel: dict | None = None):
    record = from_finished_state(play_game(random.Random(seed))[-1])
    record.panel = panel
    return record


def test_landlord_is_the_one_scored_double():
    """地主座位要和 rules.md 的计分定义自洽：地主的分永远是每个农民的两倍。"""
    for seed in range(6):
        record = _record(1000 + seed)
        seat = landlord_seat(record)
        deltas = record.result.deltas
        others = [abs(d) for s, d in enumerate(deltas) if s != seat]
        assert all(abs(deltas[seat]) == 2 * abs(d) for d in others), deltas
        assert role_of(record) in ("地主", "农民")


def test_buckets_add_up_to_the_total():
    """按角色×底分切完，各格之和必须等于总账——切分不许丢局、不许重复。"""
    records = [_record(2000 + i) for i in range(9)]
    s = summarize(records)
    assert sum(len(xs) for xs in s["buckets"].values()) == s["games"] == 9
    assert sum(sum(xs) for xs in s["buckets"].values()) == s["net"]
    assert s["wins"] + sum(1 for r in records if r.result.deltas[r.seat_of_interest] <= 0) == 9


def test_the_worst_bucket_is_flagged():
    """最亏的那一格要标出来——扫一眼就该知道往哪儿看。"""
    lines = _bucket_lines({("农民", 3): [-3, -3, -3], ("农民", 1): [1, 1], ("地主", 1): [2]})
    assert "亏损集中在这一格" in lines[1], lines      # 农民·底分1 在前，农民·底分3 第二行
    assert "亏损集中在这一格" not in lines[2]


def test_compliance_counts_advice_not_my_actions():
    """遵循率 = 面板出手 / 建议条数。分母用"我实际出手"是错的（里面混着没给建议的回合）。"""
    panel = {
        "script": "0.7.1", "engine": "douzero", "auto": True, "auto_continue": True,
        "auto_actions": 2, "auto_passes": 3, "hosted": 0,
        "decisions": [{"v": 1, "cards": [3], "solver": 0}, {"v": 2, "cards": [4], "solver": 1},
                      {"v": 3, "cards": [], "solver": 0}, {"v": 4, "cards": [], "solver": 0},
                      {"v": 5, "cards": [], "solver": 0}],
    }
    s = summarize([_record(3000, panel)])["panel"]
    assert (s["dec_plays"], s["dec_passes"]) == (2, 3)
    assert (s["plays"], s["passes"]) == (2, 3)
    assert s["solver"] == 1
    text = report([_record(3000, panel)])
    assert "该出牌 2 次，面板出手 2 次（100%）" in text


def test_old_scripts_are_labelled_not_silently_wrong():
    """0.7.0 及以前不记「不出」——那就明说，别让"出手 0 次"看着像没跟上。"""
    old = {"script": "0.7.0", "auto_actions": 1, "hosted": 0,
           "decisions": [{"v": 1, "cards": [3]}, {"v": 2, "cards": []}]}
    text = report([_record(4000, old)])
    assert "只有 0.7.1 以上的脚本才记「不出」" in text


def test_report_survives_a_gbk_console():
    """中文 Windows 控制台默认 GBK：报告里出现 GBK 编不出的字符（⚠ 就是）会直接崩。

    这不是假想——`⚠ 平台托管` 真的让工具在用户机器上崩过一次。
    """
    panel = {"script": "0.7.0", "auto_actions": 1, "auto_passes": 0, "hosted": 7,
             "decisions": [{"v": 1, "cards": [3]}]}
    report([_record(5000, panel)]).encode("gbk")
