"""声明式语料：rules.md 每一条边界的可执行版本。

每条 LEGAL 断言**完整**的读法列表（顺序也要对，多读法优先级由此固定下来）；
每条 ILLEGAL 断言这手牌根本打不出去；BEATS 断言压牌关系的不对称性。
"""

from __future__ import annotations

import pytest

from ddz import patterns as P
from helpers import cards_of, classify_keys, reading

# (说明, 牌面, 期望的读法列表——含顺序)
LEGAL: list[tuple[str, str, list[str]]] = [
    # 单张 / 对子 / 三张
    ("单张", "3", ["single:0:1"]),
    ("单张 2", "2", ["single:12:1"]),
    ("单张小王", "小王", ["single:13:1"]),
    ("单张大王", "大王", ["single:14:1"]),
    ("对子", "33", ["pair:0:1"]),
    ("对子 A", "AA", ["pair:11:1"]),
    ("对子 2", "22", ["pair:12:1"]),
    ("三张", "333", ["triple:0:1"]),
    ("三张 2", "222", ["triple:12:1"]),
    # 三带一 / 三带一对
    ("三带一", "3334", ["triple_single:0:1"]),
    ("三带一（带王）", "333 小王", ["triple_single:0:1"]),
    ("三带一对", "33344", ["triple_pair:0:1"]),
    ("三带一对（带 2）", "333 22", ["triple_pair:0:1"]),
    # 炸弹 / 王炸
    ("炸弹", "3333", ["bomb:0:1"]),
    ("炸弹 2", "2222", ["bomb:12:1"]),
    ("王炸", "小王 大王", ["rocket:14:1"]),
    # 顺子：5 张及以上连续单牌，3..A，不能含 2 和王
    ("顺子 5 张", "34567", ["straight:0:5"]),
    ("顺子 10 到 A", "10JQKA", ["straight:7:5"]),
    ("顺子 12 张 3 到 A", "3456789 10 JQKA", ["straight:0:12"]),
    ("顺子 6 张", "456789", ["straight:1:6"]),
    # 连对：3 对及以上连续对子，3..A
    ("连对 3 对", "334455", ["pair_straight:0:3"]),
    ("连对 4 对", "33445566", ["pair_straight:0:4"]),
    ("连对 到 A", "QQKKAA", ["pair_straight:9:3"]),
    # 飞机：2 组及以上连续三张，3..A
    ("飞机 2 组", "333444", ["plane:0:2"]),
    ("飞机 3 组", "333444555", ["plane:0:3"]),
    ("飞机 6 组", "333444555666777888", ["plane:0:6"]),
    # 飞机带单 / 带对
    ("飞机带单（两张散牌）", "33344456", ["plane_single:0:2"]),
    ("飞机带单（一个对子当两张单）", "33344455", ["plane_single:0:2"]),
    ("飞机带单（四组，翅膀含三张同点）", "333444555666 7778", ["plane_single:0:4", "plane_single:1:4"]),
    ("飞机带对", "3334445566", ["plane_pair:0:2"]),
    ("飞机带对 3 组", "333444555 667788", ["plane_pair:0:3"]),
    # 四带二 / 四带两对
    ("四带二（两张单牌）", "333345", ["four_two:0:1"]),
    ("四带二（一对）", "333344", ["four_two:0:1"]),
    ("四带两对", "33334455", ["four_two_pair:0:1"]),
    # 同一手牌多种理解：333444555666 可当飞机，也可当飞机带单（主体取低或取高）
    (
        "多读法 333444555666",
        "333444555666",
        ["plane:0:4", "plane_single:0:3", "plane_single:1:3"],
    ),
    ("多读法 333444555666777888", "333444555666777888", ["plane:0:6"]),
]

# (说明, 牌面)
ILLEGAL: list[tuple[str, str]] = [
    # 顺子 / 连对 / 飞机的主体只能是 3..A
    ("顺子含 2", "JQKA2"),
    ("顺子从 2 起", "23456"),
    ("顺子含王", "10JQKA 小王"),
    ("顺子只有 4 张", "3456"),
    ("连对含 2", "KKAA22"),
    ("连对只有 2 对", "AA22"),
    ("飞机含 2", "KKKAAA222"),
    ("飞机主体到 2 就断", "AAA222"),
    # 对子 / 三张 / 三带的基本形状
    ("不同点的两张", "A2"),
    ("两个对子", "AA22"),
    ("三带一只有两张翅膀", "33345"),
    ("三带一多一张", "333456"),
    # 带牌不能与主体同点：3333 是炸弹，3333+3 不存在（一张 3 最多 4 张）
    ("三张带同点的第四张", "33335"),
    ("四带一不存在", "33334"),
    # 带牌不能包含炸弹
    ("飞机带对时翅膀是炸弹", "3334445555"),
    ("飞机带单时翅膀含炸弹", "333444555666 7777"),
    ("四带两对带第二个炸弹", "3333 4444"),
    # 带牌不能包含王炸
    ("四带二翅膀是王炸", "3333 小王 大王"),
    ("飞机带单翅膀是王炸", "333444 小王 大王"),
    ("三带一对的两张王（点数不同）", "333 小王 大王"),
    # 王炸本身是唯一的两王组合
    ("两张王之外还有牌", "小王 大王 3"),
    ("单独的 2 与王不成型", "A2"),
]

# (说明, 先出的牌, 后出的牌, 后者能否压过前者)
BEATS: list[tuple[str, str, str, bool]] = [
    ("同点数单张互不压制", "3", "3", False),
    ("大单张压小单张", "3", "5", True),
    ("小王压 2", "2", "小王", True),
    ("大王压小王", "小王", "大王", True),
    ("对子比点数", "33", "44", True),
    ("不同牌型不能压", "33", "444", False),
    ("顺子同长比最低点", "34567", "45678", True),
    ("顺子长度不同不能压", "34567", "456789", False),
    ("连对同长比最低点", "334455", "445566", True),
    ("炸弹压四带两对", "33334455", "5555", True),
    ("炸弹比点数", "3333", "5555", True),
    ("小炸弹压不过大炸弹", "5555", "3333", False),
    ("炸弹压顺子", "34567", "3333", True),
    ("王炸压炸弹", "3333", "小王 大王", True),
    ("炸弹压不过王炸", "小王 大王", "3333", False),
    ("王炸不压王炸", "小王 大王", "小王 大王", False),
    ("飞机带单不压飞机", "333444", "33344456", False),
    ("飞机带对同长比主体", "3334445566", "4445556677", True),
]

# 「对子不压单张」的写法有歧义，单独断言一次牌型必须相同
BEATS_TYPED: list[tuple[str, str, str, bool]] = [
    ("对子对单张：牌型不同 → 不能压", "3", "44", False),
]


@pytest.mark.parametrize("name, spec, expected", LEGAL, ids=[c[0] for c in LEGAL])
def test_legal_readings(name, spec, expected):
    assert classify_keys(spec) == expected


@pytest.mark.parametrize("name, spec", ILLEGAL, ids=[c[0] for c in ILLEGAL])
def test_illegal_plays_have_no_reading(name, spec):
    assert classify_keys(spec) == []


@pytest.mark.parametrize("name, low, high, expected", BEATS, ids=[c[0] for c in BEATS])
def test_beats(name, low, high, expected):
    # 参数顺序：先出 low，后出 high 看能否压过
    assert P.beats(reading(high), reading(low)) is expected


@pytest.mark.parametrize("name, low, high, expected", BEATS_TYPED, ids=[c[0] for c in BEATS_TYPED])
def test_beats_requires_same_type(name, low, high, expected):
    assert P.beats(reading(high), reading(low)) is expected


def test_readings_are_sorted_by_documented_priority():
    """炸弹/王炸最前，其后长链、大张数优先。"""
    keys = classify_keys("333444555666")
    assert keys[0] == "plane:0:4"  # 12 张最长链条
    assert keys[1:] == ["plane_single:0:3", "plane_single:1:3"]  # 同长时主体点数小的在前


@pytest.mark.parametrize(
    "spec, n",
    [("3", 1), ("33", 2), ("333", 3), ("3334", 4), ("33344", 5), ("333444", 6),
     ("3333", 4), ("小王 大王", 2), ("34567", 5), ("33334455", 8), ("3334445566", 10),
     ("333444555666", 12)],
)
def test_pattern_size_matches_card_count(spec, n):
    cards = cards_of(spec)
    for pattern in P.classify(cards):
        assert pattern.size() == n


def test_pattern_key_round_trip():
    for spec in ("333444555666", "33334455", "34567", "小王 大王", "333444555 667788"):
        for pattern in P.classify(cards_of(spec)):
            assert P.Pattern.parse_key(pattern.key()) == pattern


def test_classify_rejects_empty_hand():
    assert P.classify([]) == []
    assert P.classify_one([]) is None


def test_bomb_predicates():
    assert P.is_bomb(reading("3333")) and P.explodes_multiplier(reading("3333"))
    assert P.is_bomb(reading("小王 大王")) and P.explodes_multiplier(reading("小王 大王"))
    assert not P.is_bomb(reading("3334445566"))
    assert not P.explodes_multiplier(reading("34567"))
