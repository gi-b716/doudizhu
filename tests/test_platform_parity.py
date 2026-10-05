"""拿平台自己的规则引擎（``src/doudizhu-rules.js``）当金标准做对照。

平台那份是 ``web/app/models/UOJDoudizhuRules.php`` 的镜像、服务器说了算，所以它是**唯一**
权威。这里在 node 里跑它，与我们的引擎比对三件事：

1. 同一手牌的全部读法（换算成同一套点数约定后必须一模一样，**包括顺序**）；
2. 压牌关系（随机牌型对）；
3. 牌型 key（自动出牌要提交 ``choice=<平台 key>``，错一个字符服务器就拒）。

没有 node 时整组跳过——它只是校验，不参与运行。
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from ddz import cards as C
from ddz import patterns as P
from helpers import cards_of, random_hand

RULES_JS = Path(__file__).resolve().parents[1] / "src" / "doudizhu-rules.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="没装 node，跳过与平台引擎的对照")

#: 平台 TYPE_ORDER 的顺序（它们的 classify 就是按这个排的）。
_PLATFORM_ORDER = [
    "rocket", "bomb", "single", "pair", "trio", "straight", "pairs", "plane",
    "trio1", "trio2", "four2", "four22", "plane1", "plane2",
]

BRIDGE_JS = Path(__file__).resolve().parent / "platform_bridge.js"


def _ask_platform(hands, beats=(), keys=()):
    """把牌组/牌型对丢给 node 里跑的平台引擎，取回它的判断。"""
    result = subprocess.run(
        [NODE, str(BRIDGE_JS), str(RULES_JS)],
        input=json.dumps({"hands": hands, "beats": beats, "keys": keys}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def to_ours(entry) -> tuple[str, int, int]:
    """平台的 ``{type, rank(=最高点), len}`` → ``(我们的类型, 最低点, 长度)``。"""
    pattern = P.Pattern.from_platform(entry)
    assert pattern is not None, entry
    return (pattern.type, pattern.main_rank, pattern.length)


def readings(cards):
    return [(p.type, p.main_rank, p.length) for p in P.classify(cards)]


def test_rank_convention_matches():
    """验证前置条件：平台 ``rank`` 把两张王算成 13/14，和我们完全一致。"""
    assert _ask_platform(hands=[])["ranks"] == [0, 12, C.JOKER_RANK, C.JOKER_RANK + 1]


def sample_plays(rng, count: int = 40, *, size: int = 17) -> list[list[int]]:
    """随机手牌里的**合法着法**（随机牌组基本都打不出去，必须从着法里取）。"""
    from ddz.moves import legal_leads

    out: list[list[int]] = []
    while len(out) < count:
        hand = random_hand(rng, size, max_count=4)
        plays = legal_leads(hand, canonical=False, limit=12)
        out.extend(list(m.cards) for m in plays if len(m.cards) <= 12)
    return out[:count]


@pytest.mark.parametrize("seed", range(6))
def test_classify_parity_on_random_hands(seed):
    rng = random.Random(seed)
    hands = [random_hand(rng, rng.randint(1, 20), max_count=4) for _ in range(120)]
    theirs = _ask_platform(hands=hands)["classify"]
    for cards, platform_readings in zip(hands, theirs):
        mine = sorted(readings(cards), key=_order_key)
        assert sorted(map(to_ours, platform_readings), key=_order_key) == mine, cards


def _order_key(reading):
    """按平台 TYPE_ORDER 排，同类型按最高点降序——两边都能排成同一个序列。"""
    type_name, main_rank, length = reading
    platform_type = P.PLATFORM_TYPES[type_name]
    top = main_rank + length - 1 if length > 1 else main_rank
    return (_PLATFORM_ORDER.index(platform_type), -top)


def test_classify_parity_on_corpus_hands():
    """语料里的每种边界牌型都过一遍（这些才是规则差异最容易藏身的地方）。"""
    specs = [
        "3", "33", "333", "3334", "33344", "3333", "小王 大王",
        "34567", "10JQKA", "334455", "333444", "333444555",
        "33344456", "33344455", "3334445566", "33334455", "333345", "333344",
        "333444555666", "333444555666 7777", "3333 4444", "3333 小王 大王",
        "333444555666 7778", "333444 小王 大王", "33335", "33334", "AA22", "KKAA22",
    ]
    hands = [cards_of(spec) for spec in specs]
    theirs = _ask_platform(hands=hands)["classify"]
    for spec, cards, platform_readings in zip(specs, hands, theirs):
        assert sorted(map(to_ours, platform_readings), key=_order_key) == sorted(
            readings(cards), key=_order_key
        ), f"{spec} 读法不一致"


@pytest.mark.parametrize("seed", range(4))
def test_beats_parity(seed):
    """压牌关系：把每对牌型的两边第一个读法都拿去比对。"""
    rng = random.Random(100 + seed)
    plays = sample_plays(rng, 60)
    pairs, expected = [], []
    for i, a in enumerate(plays):
        for b in plays[i + 1 : i + 4]:
            first, second = P.classify(a)[0], P.classify(b)[0]
            pairs.append([first.to_platform(), second.to_platform()])
            expected.append(P.beats(first, second))
    theirs = _ask_platform(hands=[], beats=pairs)["beats"]
    assert theirs == expected
    assert len(pairs) >= 100


#: 每种牌型各来一手（14 种全覆盖），逼着 beats 的每条分支都被比到。
COVERAGE = [
    "3", "33", "333", "3334", "33344", "34567", "334455", "333444",
    "33344456", "3334445566", "333344", "33334455", "3333", "小王 大王",
]


def test_beats_parity_across_every_type():
    """14 种牌型两两互压，逐对比对；顺便要求覆盖面真的到位。"""
    readings = [(cards, P.classify(cards)[0]) for cards in map(cards_of, COVERAGE)]
    rng = random.Random(3)
    readings += [(cards, P.classify(cards)[0]) for cards in sample_plays(rng, 40)]

    pairs, expected = [], []
    for _, mine in readings:
        for _, other in readings:
            pairs.append([other.to_platform(), mine.to_platform()])
            expected.append(P.beats(other, mine))
    theirs = _ask_platform(hands=[], beats=pairs)["beats"]
    assert theirs == expected
    assert len({p.type for _, p in readings}) == len(P.TYPE_NAMES)


def test_pattern_key_parity():
    """我们的 ``platform_key()`` 必须与平台的 ``patternKey`` 逐字相同。"""
    rng = random.Random(7)
    keys, expected = [], []
    for cards in sample_plays(rng, 150):
        for pattern in P.classify(cards):
            keys.append(pattern.to_platform())
            expected.append(pattern.platform_key())
    theirs = _ask_platform(hands=[], keys=keys)["keys"]
    assert theirs == expected
    assert len(keys) > 120


def test_platform_key_examples():
    """人工核对几个容易错位的：链条记的是最高点，类型名是 trio1/plane1/four22。"""
    assert P.Pattern.parse_key("straight:0:5").platform_key() == "straight:4:5"    # 34567
    assert P.Pattern.parse_key("pair_straight:0:3").platform_key() == "pairs:2:3"  # 334455
    assert P.Pattern.parse_key("plane_single:0:3").platform_key() == "plane1:2:3"
    assert P.Pattern.parse_key("triple_single:5:1").platform_key() == "trio1:5:1"
    assert P.Pattern.parse_key("four_two_pair:3:1").platform_key() == "four22:3:1"
    assert P.Pattern.parse_key("rocket:14:1").platform_key() == "rocket:14:1"


def test_from_platform_round_trip():
    for spec in ("333444555666", "33344456", "3334445566", "33334455", "10JQKA", "334455"):
        for pattern in P.classify(cards_of(spec)):
            assert P.Pattern.from_platform(pattern.to_platform()) == pattern


def test_platform_pattern_parsing_is_now_exact():
    """平台日志里的 pattern 对象（``{type, rank, len}``）现在能精确解析。"""
    from ddz.state import parse_platform_pattern

    parsed = parse_platform_pattern({"type": "plane1", "rank": 3, "len": 3})
    assert parsed is not None and parsed.key() == "plane_single:1:3"
    assert parse_platform_pattern({"type": "trio", "rank": 9, "len": 1}).key() == "triple:9:1"
    assert parse_platform_pattern({"type": "nonsense", "rank": 1, "len": 1}) is None
