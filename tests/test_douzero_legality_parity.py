"""**闸二**：我们的合法着法与上游 ``MovesGener`` 逐手对照（PLAN §3.7）。

做法：把局面塞进上游真正的 :class:`GameEnv`（设好手牌与历史动作），直接调它自己的
``get_legal_card_play_actions()`` ——不复制它的分发逻辑，避免版本漂移。

差异**只有两类**，都是 rules.md 的规定、我们有意照它实现（用户已裁定"以 rules 为准"）：

1. **不拆炸弹当翅膀**：``ddz.moves`` 的翅膀点数池排除"手里四张同点"的点数，上游允许；
2. **翅膀里不能带王炸**：rules.md「带牌不能包含炸弹或王炸」，上游不查这一条
   （例如 ``3333 + 小王 + 大王`` 上游会生成、我们不会）。

顺带纠正 PLAN §0.2 的一条：它说上游「四带两对」允许 ``3333+4444``——这份 commit **不支持**
（``gen_type_14_4_22`` 要求两个翅膀点数不同），所以说好的"唯一差异"其实不存在。

判据：

1. **我们生成的，上游必须也有**（``ours - theirs == ∅``）——我们不会凭空造出上游不认的着法；
2. 上游多出来的必须**全部**能被上面两类解释掉；解释不了的即失败——那说明我们对规则的理解
   与上游不一致，要么改生成、要么把它登记清楚（不能默默吞掉）。
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from ddz import cards as C
from ddz import patterns as P
from ddz.douzero import bridge, vendored
from ddz.moves import legal_moves
from helpers import random_hand

MODULES = vendored.load_env()
pytestmark = pytest.mark.skipif(MODULES is None, reason="没有 third_party/DouZero")

POSITIONS = ("landlord", "landlord_up", "landlord_down")


def upstream_legal_actions(hand_codes, rival_codes):
    """上游在「手牌 = hand_codes、上家刚出 rival_codes」时的合法着法（含"不出"）。"""
    env = MODULES.game.GameEnv({pos: MODULES.env.DummyAgent(pos) for pos in POSITIONS})
    env.acting_player_position = "landlord"
    env.info_sets["landlord"].player_hand_cards = list(hand_codes)
    if rival_codes:
        env.card_play_action_seq = [list(rival_codes)]
    return [tuple(sorted(action)) for action in env.get_legal_card_play_actions()]


def our_legal_actions(hand, rival_pattern, rival_cards):
    moves = legal_moves(hand, rival_pattern, canonical=False)
    actions = {tuple(bridge.encode_cards(move.cards)) for move in moves}
    if rival_pattern is not None:
        actions.add(())  # 跟牌时「不出」也是一个合法动作
    return actions


#: 已知差异的两类原因（见模块 docstring）。
KNOWN_DIFFERENCES = ("拆炸弹当翅膀", "翅膀里带王炸")


def known_difference(action_codes, hand_codes) -> str | None:
    """这手属于哪一类已知差异；解释不了就返回 ``None``。"""
    if not action_codes:
        return None
    move_counts = Counter(action_codes)
    hand_counts = Counter(hand_codes)

    # ① 拆炸弹当翅膀：某个点数手里攥着四张，这一手只用了其中几张
    if any(hand_counts[rank] == 4 and used < 4 for rank, used in move_counts.items()):
        return KNOWN_DIFFERENCES[0]

    # ② 翅膀里带王炸：两张王都在这一手里，而它本身又不是王炸
    kind = MODULES.detector.get_move_type(list(action_codes)).get("type")
    has_both_jokers = move_counts.get(20, 0) > 0 and move_counts.get(30, 0) > 0
    if has_both_jokers and kind != MODULES.utils.TYPE_5_KING_BOMB:
        return KNOWN_DIFFERENCES[1]
    return None


def compare(hand, rival_cards):
    """返回 (只有我们有的, 只有上游有的)。"""
    readings = P.classify(rival_cards)
    rival_pattern = readings[0] if readings else None
    rival_codes = sorted(C.to_env_card(c) for c in rival_cards) if rival_pattern else []
    ours = our_legal_actions(hand, rival_pattern, rival_cards)
    theirs = set(upstream_legal_actions(sorted(C.to_env_card(c) for c in hand), rival_codes))
    return ours - theirs, theirs - ours


#: 覆盖全部 14 种牌型的"上家出的牌"样本——翅膀型（三带/飞机带）必须被跟到，
#: 否则"拆炸弹当翅膀"那条差异永远不会出现，闸二就成了空转。
RIVAL_SPECS = [
    "3", "33", "333", "3334", "33344", "34567", "334455", "333444",
    "33344456", "3334445566", "333344", "33334455", "3333", "小王 大王",
]


def random_rival(rng):
    """造一手合法的"上家的牌"：一半从全牌型样本里取，一半从随机构造里挑。"""
    from helpers import cards_of

    if rng.random() < 0.5:
        cards = cards_of(rng.choice(RIVAL_SPECS))
        return cards, P.classify(cards)[0]
    while True:
        cards = random_hand(rng, rng.randint(1, 8), max_count=4)
        readings = P.classify(cards)
        if readings:
            return cards, readings[0]


SEEN: Counter = Counter()


@pytest.mark.parametrize("seed", range(4))
def test_legal_moves_match_upstream_on_random_hands(seed):
    """2000 手随机局面（每个 seed 500 手）：差异只能有一条。"""
    rng = random.Random(seed)
    checked = unexpected = 0
    seen: Counter = Counter()
    for _ in range(500):
        hand = random_hand(rng, rng.randint(4, 17), max_count=4)
        rival_cards, _ = random_rival(rng)
        checked += 1
        ours_only, theirs_only = compare(hand, rival_cards)
        hand_codes = sorted(C.to_env_card(c) for c in hand)
        if ours_only:
            unexpected += 1
            pytest.fail(f"我们多出上游没有的着法：{sorted(ours_only)[:3]}（手牌 {sorted(hand)}）")
        for action in theirs_only:
            reason = known_difference(action, hand_codes)
            if reason:
                seen[reason] += 1
            else:
                unexpected += 1
                pytest.fail(f"解释不了的差异：{action}（手牌 {sorted(hand)}）")
    SEEN.update(seen)
    assert checked >= 200, f"有效局面太少：{checked}"
    assert unexpected == 0


def test_leading_matches_upstream_modulo_bomb_splitting():
    """领出（没有上家的牌）同样只允许"拆炸弹当翅膀"这一类差异。"""
    rng = random.Random(99)
    checked = 0
    for _ in range(400):
        hand = random_hand(rng, rng.randint(4, 20), max_count=4)
        hand_codes = sorted(C.to_env_card(c) for c in hand)
        ours = our_legal_actions(hand, None, [])
        theirs = set(upstream_legal_actions(hand_codes, []))
        assert not (ours - theirs), f"我们多生成了：{sorted(ours - theirs)[:4]}"
        unexplained = [a for a in theirs - ours if not known_difference(a, hand_codes)]
        assert not unexplained, f"解释不了的差异：{unexplained[:4]}（手牌 {sorted(hand)}）"
        for action in theirs - ours:
            SEEN[known_difference(action, hand_codes)] += 1
        checked += 1
    assert checked >= 300


def test_both_known_difference_classes_actually_show_up():
    """两类已知差异都得在样本里出现过，否则这条闸等于没测到。"""
    for reason in KNOWN_DIFFERENCES:
        assert SEEN[reason] > 0, f"样本里没出现「{reason}」，闸二没覆盖到：{dict(SEEN)}"


def test_four_two_pair_wings_must_be_two_distinct_ranks():
    """PLAN §0.2 说上游「四带两对」允许 ``3333+4444``——**这份 commit 并不支持**。

    上游 ``gen_type_14_4_22`` 的翅膀取 ``select(cards_list, 2)``，两个点数必须不同，
    与 rules.md 的口径一致。所以那条"已知差异"其实不存在；真正的差异只有
    "拆炸弹当翅膀"这一类（见上面那条规则）。
    """
    hand = [0, 1, 2, 3, 4, 5, 6, 7]        # 3333 + 4444
    hand_codes = sorted(C.to_env_card(c) for c in hand)
    theirs = set(upstream_legal_actions(hand_codes, []))
    assert tuple(hand_codes) not in theirs, "上游不该把 3333+4444 当四带两对打出去"
    ours_only, _ = compare(hand, [])
    assert ours_only == set()


def test_wing_from_a_bomb_is_the_only_reason_we_skip_it():
    """把规则钉在具体例子上：手里 4444 5555，我们不出 444 带 5，上游出。"""
    hand = [4, 5, 6, 7, 8, 9, 10, 11]     # 4444 + 5555（4×rank1 + 4×rank2）
    hand_codes = sorted(C.to_env_card(c) for c in hand)
    ours = our_legal_actions(hand, None, [])
    theirs = set(upstream_legal_actions(hand_codes, []))
    trio_with_wing = tuple(sorted([4, 4, 4, 5]))       # 444 + 5
    assert trio_with_wing in theirs
    assert trio_with_wing not in ours
    assert known_difference(trio_with_wing, hand_codes) == KNOWN_DIFFERENCES[0]


def test_rocket_wings_is_the_second_known_difference():
    """第二类：上游允许 ``3333 + 小王 + 大王``（四带二带王炸），rules.md 禁止。"""
    from helpers import cards_of

    hand = cards_of("3333 小王 大王")
    hand_codes = sorted(C.to_env_card(c) for c in hand)
    rocket_wings = tuple(sorted(hand_codes))
    theirs = set(upstream_legal_actions(hand_codes, []))
    ours = our_legal_actions(hand, None, [])
    assert rocket_wings in theirs
    assert rocket_wings not in ours
    assert known_difference(rocket_wings, hand_codes) == KNOWN_DIFFERENCES[1]


def test_pass_is_always_available_when_following():
    """跟牌时「不出」必须在我们与上游两边都算合法动作。"""
    rng = random.Random(5)
    seen = 0
    for _ in range(200):
        hand = random_hand(rng, rng.randint(4, 17), max_count=4)
        rival_cards = random_hand(rng, 1, max_count=4)
        readings = P.classify(rival_cards)
        if not readings:
            continue
        seen += 1
        codes = sorted(C.to_env_card(c) for c in hand)
        assert () in set(upstream_legal_actions(codes, sorted(C.to_env_card(c) for c in rival_cards)))
        assert () in our_legal_actions(hand, readings[0], rival_cards)
    assert seen >= 30
