"""残局求解器：**必须与暴力枚举一致**（PLAN §4 Phase 5 的验收线）。

参照实现（``brute``）是故意写成另一个样子的：它不记忆化、不剪枝、不设预算，而且用
"牌权归谁"来表达清台，而求解器用的是"连续不出几次"。两种表达独立收敛到同一个答案，
才说明求解器里的剪枝、记忆化和清台规则没写错——照抄一遍就没意义了。
"""

from __future__ import annotations

import random
import time
from dataclasses import replace

import pytest

from ddz import cards as C
from ddz.engine.endgame import (
    Limits, SolveResult, _passes_behind, is_endgame, solvable, solve, solve_uncertain,
    uncertain_solvable,
)
from ddz.moves import legal_beats, legal_leads
from ddz.patterns import Pattern, classify, classify_beating
from ddz.state import GameState, PlayerInfo, TableEntry
from helpers import cards_of

SEATS = (0, 1, 2)


# --------------------------------------------------------------------- 手搓局面


def endgame_state(hands, *, turn=0, landlord=1, to_beat=None, last_seat=None, play_cards=()):
    """三家手牌全摆出来的残局。``to_beat`` 给定时默认是上家（turn+2）刚打的。"""
    board = [
        tuple(C.sort_cards(cards_of(h) if isinstance(h, str) else list(h))) for h in hands
    ]
    if to_beat is not None:
        who = last_seat if last_seat is not None else (turn + 2) % 3
        # 出牌的人必须正好是"我上家"：座位是按 +1 轮转的，谁刚出完牌、下一个该谁，
        # 这两件事不能各说各话（这个坑踩过：手写的局面自相矛盾，求解器却被当成错的）
        if (who + 1) % 3 != turn:
            raise ValueError(f"局面自相矛盾：{who} 号刚出牌，下一个却轮到 {turn} 号")
        played = tuple(play_cards)
        if played:
            board[who] = tuple(c for c in board[who] if c not in set(played))
    else:
        who, played = None, ()
    last = TableEntry(who, "play", played, to_beat) if to_beat is not None else None
    return GameState(
        seat=turn,
        phase="playing",
        turn=turn,
        leading=to_beat is None,
        hand=board[turn],
        all_hands=list(board),
        landlord=landlord,
        players=[
            PlayerInfo(
                seat=i, username=f"p{i}", count=len(board[i]),
                role="landlord" if landlord == i else "farmer",
            )
            for i in SEATS
        ],
        last=last,
    )


# --------------------------------------------------------------------- 暴力参照


def brute(hands, to_beat: Pattern | None, owner, turn: int, side: frozenset[int]) -> bool:
    """把整棵树走完：不记忆化、不剪枝、不设预算。

    ``owner`` 是"牌权在谁手上"（``to_beat is None`` 时无意义）。跟牌的人一圈转回到
    ``owner`` 就说明另外两家都没压，牌权回到他手里——这就是清台。
    """
    if to_beat is None:
        options = [(m, False) for m in legal_leads(hands[turn], canonical=False)]
    else:
        options = [(m, False) for m in legal_beats(hands[turn], to_beat, canonical=False)]
        options.append((None, True))

    values = []
    for move, is_pass in options:
        if is_pass:
            nxt = (turn + 1) % 3
            if nxt == owner:
                values.append(brute(hands, None, None, nxt, side))
            else:
                values.append(brute(hands, to_beat, owner, nxt, side))
            continue
        rest = tuple(c for c in hands[turn] if c not in set(move.cards))
        if not rest:
            values.append(turn in side)
            continue
        nxt_hands = tuple(rest if s == turn else hands[s] for s in SEATS)
        values.append(brute(nxt_hands, move.pattern, turn, (turn + 1) % 3, side))

    return any(values) if turn in side else all(values)


def _side_of(state: GameState) -> frozenset[int]:
    me, landlord = state.advise_seat, state.landlord
    return frozenset({landlord}) if me == landlord else frozenset(s for s in SEATS if s != landlord)


def _owner_of(state: GameState):
    """牌权在"最后出牌那家"手上；不给他，跟牌的两家会永远互相不出（这个坑真踩过）。"""
    return state.last.seat if state.last is not None and state.to_beat is not None else None


def brute_solve(state: GameState) -> bool:
    """当前局面下，我方能不能赢（暴力枚举）。"""
    return brute(
        tuple(state.hand_for(s) for s in SEATS), state.to_beat, _owner_of(state),
        state.turn, _side_of(state),
    )


def brute_after(state: GameState, action) -> bool:
    """走这个动作（``None`` = 不出）**之后**我方能不能赢——仍然用暴力枚举算。

    故意不去"造一个子局面"再判：那要重挑 advise_seat，很容易把"我是谁"换掉，
    于是两边算的根本不是同一个问题（当时表现为"求解器自己和自己不一致"）。
    """
    hands = tuple(state.hand_for(s) for s in SEATS)
    turn, side = state.turn, _side_of(state)
    nxt = (turn + 1) % 3
    if action is None:
        passes = 1 if state.events and state.events[-1].get("kind") == "pass" else 0
        if state.to_beat is None:
            raise ValueError("领出时不能不出")
        if passes + 1 >= 2:
            return brute(hands, None, None, nxt, side)
        return brute(hands, state.to_beat, _owner_of(state), nxt, side)
    rest = tuple(c for c in hands[turn] if c not in set(action.cards))
    if not rest:
        return turn in side
    return brute(
        tuple(rest if s == turn else hands[s] for s in SEATS),
        action.pattern, turn, nxt, side,
    )


# --------------------------------------------------------------------- 随机局面


def random_endgame(rng: random.Random, total: int = 10):
    """随机发一副小牌局，一半的局面上摆着一手要压的牌。"""
    deck = C.full_deck()
    rng.shuffle(deck)
    sizes = [1, 1, 1]
    for _ in range(total - 3):
        sizes[rng.randrange(3)] += 1
    hands, at = [], 0
    for size in sizes:
        hands.append(tuple(C.sort_cards(deck[at : at + size])))
        at += size
    landlord = rng.randrange(3)
    turn = rng.randrange(3)

    to_beat = None
    if rng.random() < 0.5:
        prev = (turn + 2) % 3
        leads = legal_leads(hands[prev], canonical=False)
        if leads:
            choice = leads[rng.randrange(len(leads))]
            to_beat = choice.pattern
            return endgame_state(
                hands, turn=turn, landlord=landlord,
                to_beat=to_beat, last_seat=prev, play_cards=choice.cards,
            ), choice
    return endgame_state(hands, turn=turn, landlord=landlord), None


# --------------------------------------------------------------------- 与暴力一致


def test_matches_brute_force_on_small_endgames():
    """PLAN 的验收线：≤10 张的残局，求解器与暴力枚举**结论必须一致**。"""
    rng = random.Random(20261006)
    cases = 0
    for _ in range(60):
        state, _ = random_endgame(rng)
        result = solve(state)
        assert result.proved, f"不该搜不完（{result.nodes} 节点）"
        assert result.winning is brute_solve(state), f"结论不一致：{state.all_hands}"
        cases += 1
    assert cases == 60


def test_returned_move_really_keeps_the_result():
    """求解器说"这样走能赢"，那一手就**必须**真的赢（拿暴力枚举复核）。"""
    rng = random.Random(7)
    winning_cases = 0
    for _ in range(40):
        state, _ = random_endgame(rng)
        result = solve(state)
        assert result.proved
        if not result.winning:
            continue
        winning_cases += 1
        assert result.best_moves, "能赢就要给出所有能赢的动作"
        assert result.move is result.best_moves[0]
        for action in result.best_moves:
            label = action.pattern.key() if action is not None else "不出"
            assert brute_after(state, action), f"求解器推荐的 {label} 其实赢不了"
    assert winning_cases >= 5, "样本里能赢的局面太少，这条没测到什么"


def test_lost_positions_really_have_no_winning_move():
    """反过来也要成立：求解器说"怎么都赢不了"，就一手能赢的都不该有。"""
    rng = random.Random(99)
    lost = 0
    for _ in range(40):
        state, _ = random_endgame(rng)
        result = solve(state)
        if result.winning is not False:
            continue
        lost += 1
        assert result.best_moves == []
        plays = (
            legal_leads(state.hand, canonical=False) if state.to_beat is None
            else legal_beats(state.hand, state.to_beat, canonical=False)
        )
        actions = list(plays) + ([None] if state.to_beat is not None else [])
        for action in actions:
            label = action.pattern.key() if action is not None else "不出"
            assert not brute_after(state, action), f"{label} 居然能赢"
    assert lost >= 5, "样本里必败的局面太少"


# --------------------------------------------------------------------- 手写小例子


def test_one_card_left_is_a_win_when_leading():
    state = endgame_state(["3", "4", "5"], turn=0, landlord=0)
    result = solve(state)
    assert result.winning is True
    assert result.move.cards == tuple(cards_of("3"))


def test_landlord_one_card_away_and_nobody_can_beat():
    """地主只剩一张、我和队友都压不住他刚打的牌 → 必败。

    地主坐 2 号（我的上家）、桌上是他打的一张 7、手里留着 6；我只有 4、5，队友只有 3——
    两家都压不住，两次不出之后牌权回到他手上，他把最后一张走掉，我方输。
    """
    state = endgame_state(
        ["4 5", "3", "6"], turn=0, landlord=2, to_beat=Pattern("single", 4),
    )
    result = solve(state)
    assert result.winning is False
    assert result.move is None


def test_rocket_wins_the_race():
    """我有王炸、地主只剩一张，压住就赢。"""
    state = endgame_state(
        ["小王 大王", "3", "8 7"], turn=0, landlord=2,
        to_beat=Pattern("single", 5), play_cards=cards_of("8"),
    )
    result = solve(state)
    assert result.winning is True
    # 单出小王也能赢（两家都压不住，我下一手走大王），所以"能赢的走法"里两个都该在
    kinds = {frozenset(m.cards) for m in result.best_moves if m is not None}
    assert frozenset(cards_of("小王")) in kinds
    assert frozenset(cards_of("小王 大王")) in kinds


# --------------------------------------------------------------------- 边界与预算


def test_solvable_needs_the_hands_and_a_small_board():
    hidden = endgame_state(["3 4", "5 6", "7 8"], turn=0, landlord=1)
    assert solvable(hidden) is True
    assert solvable(replace(hidden, all_hands=None)) is False   # 看不到别家的牌就不算残局解

    big = endgame_state(["3 4 5 6", "3 4", "5 6"], turn=0, landlord=1)   # 4+2+2 = 8 张
    assert is_endgame(big, Limits(max_cards=3)) is False        # 一家 4 张，超了
    assert is_endgame(big, Limits(max_total=7)) is False        # 总共 8 张，超了
    assert is_endgame(big, Limits(max_cards=4, max_total=8)) is True


def test_default_thresholds_stay_where_they_were_measured():
    """默认阈值是**实测 + 真实对局教训**定的，不是拍的。

    一开始总张数卡在 14（随机残局实测 14 张最慢 114ms、16 张 725ms，deadline 是 400ms）。
    但真实对局 36103 出现了这样一手：地主只剩一对 K（2 张）、我 10 张、队友 8 张，共 20 张——
    阈值把求解器挡在外面，于是 DouZero 领出一对 10（把牌权递给只剩两张的地主）把**必胜**
    打丢了；而求解器 2557 个节点、0 毫秒就算穿，赢法是领出任意一张单牌。
    所以现在张数放宽、防爆交给节点预算（约 0.5 秒，超了如实回"没搜完"）。
    """
    assert Limits().max_total == 20
    assert Limits().max_cards >= 10
    # 卡住延迟的是墙钟预算，不是节点数（节点代价差得远）：加到一条建议上的时间不会超过它
    assert Limits().time_budget_ms <= 300
    assert Limits().max_nodes <= 500_000


def test_limits_are_honoured():
    state = endgame_state(["3 4 5 6 7 8", "3 4 5 6 7 8", "9 10"], turn=0, landlord=2)
    assert is_endgame(state, Limits(max_cards=6, max_total=14)) is True
    assert is_endgame(state, Limits(max_cards=5, max_total=14)) is False
    assert is_endgame(state, Limits(max_cards=6, max_total=13)) is False


def test_budget_exhaustion_says_so_instead_of_guessing():
    """搜不完就必须如实说"没搜完"，绝不能把半截结论当结论。"""
    rng = random.Random(4)
    deck = C.full_deck()
    rng.shuffle(deck)
    hands = [tuple(C.sort_cards(deck[i * 6 : i * 6 + 6])) for i in range(3)]
    state = endgame_state(hands, turn=0, landlord=2)
    result = solve(state, limits=Limits(max_nodes=1))
    assert result.exhausted is True
    assert result.winning is None
    assert result.proved is False


def test_deterministic_for_the_same_position():
    state, _ = random_endgame(random.Random(11), total=9)
    first, second = solve(state), solve(state)
    assert (first.winning, first.move.cards if first.move else None) == (
        second.winning, second.move.cards if second.move else None
    )


def test_fast_enough_for_the_advice_budget():
    """残局求解要落在建议的延迟预算里（服务端 deadline 默认 400ms）。"""
    rng = random.Random(2026)
    worst = 0.0
    for _ in range(20):
        state, _ = random_endgame(rng, total=11)
        started = time.perf_counter()
        solve(state)
        worst = max(worst, (time.perf_counter() - started) * 1000.0)
    assert worst < 400, f"最慢一次 {worst:.0f}ms"


def test_a_bomb_beats_a_smaller_pattern_in_the_search():
    """搜索里的合法性与引擎一致：炸弹能压对子，且用了它手牌就少了四张。"""
    state = endgame_state(
        ["3 3 3 3", "4 4", "5"], turn=0, landlord=0,
        to_beat=Pattern("pair", 1), last_seat=2, play_cards=cards_of("4 4"),
    )
    result = solve(state)
    assert result.winning is True
    assert result.move.pattern.type == "bomb"
    assert classify(result.move.cards)[0].type == "bomb"
    assert classify_beating(result.move.cards, state.to_beat) is not None


def test_tiny_budgets_never_leak_the_exhaustion_exception():
    """预算不够时只能"如实说没搜完"，绝不能把内部异常抛出来。

    这个是真漏过的：``_value`` 的调用被 try 包住了，但**根节点排序那一段没有**——
    预算恰好在排根节点着法时用完，``_Exhausted`` 就直接炸到调用方了（服务里会变成
    "引擎出错，已回退启发式"）。这里用一堆很小的预算把每条路径都走一遍。
    """
    rng = random.Random(31337)
    checked = 0
    for _ in range(40):
        state, _ = random_endgame(rng, total=12)
        for budget in (1, 2, 3, 5, 10, 30):
            result = solve(state, limits=Limits(max_nodes=budget))
            assert result.winning is None or result.best_moves is not None
            if result.exhausted:
                assert result.winning is None, "没搜完就不许给结论"
                assert result.best_moves is None
            checked += 1
    assert checked == 240


# --------------------------------------------------------------------- 记忆表复用（C 方案的地基）


def test_a_warm_memo_gives_the_same_answer():
    """拿一张**别人搜过的**记忆表接着搜，结论必须一模一样。

    这是预取方案的地基：预取在后台攒记忆表，我方回合接着搜。记忆表里存的都是"算完"的结论
    （没算完的绝不写进去），所以复用只可能更快、不可能更错——但这条得真测一遍。
    """
    rng = random.Random(4242)
    for _ in range(15):
        state, _ = random_endgame(rng, total=12)
        cold = solve(state)

        # 先解一个"更大的"局面，攒出一张记忆表（它的子树覆盖同一批手牌形态）
        memo: dict = {}
        bigger, _ = random_endgame(random.Random(rng.randrange(10**6)), total=13)
        solve(bigger, memo=memo)
        assert memo, "预取应该攒下东西"

        warm = solve(state, memo=memo)
        assert warm.winning == cold.winning
        assert [m.cards if m else None for m in (warm.best_moves or [])] == \
               [m.cards if m else None for m in (cold.best_moves or [])]


def test_a_warm_memo_makes_the_second_solve_cheaper():
    """同一棵树接着搜，第二次必须明显更省——不然预取就白做了。"""
    rng = random.Random(99)
    cheaper = 0
    for _ in range(10):
        state, _ = random_endgame(rng, total=13)
        memo: dict = {}
        first = solve(state, memo=memo)
        second = solve(state, memo=memo)
        assert second.winning == first.winning
        if second.nodes < first.nodes:
            cheaper += 1
    assert cheaper >= 8, f"复用只便宜了 {cheaper}/10 次，记忆表没起作用"


def test_budget_can_be_overridden_per_call():
    """预取要能用远超正常预算的时间去搜（它跑在后台，不占出建议的延迟）。"""
    state, _ = random_endgame(random.Random(7), total=14)
    tiny = solve(state, time_budget_ms=0.001)
    assert tiny.exhausted and tiny.winning is None      # 预算掐到 1µs，必然没搜完
    full = solve(state, time_budget_ms=5000)
    assert full.proved                                   # 给足就搜完


# --------------------------------------------------------------------- 不依赖队友的赢法（solo）


def test_prefers_a_win_that_does_not_need_the_teammate():
    """能赢的走法里，优先挑"**队友乱打也照样赢**"的那一手。

    局面来自真实对局（我 5 5 8 / 队友 3 3 6 8 / 地主 3 6，我领出）：
    - 甩单张 5：地主要么过要么压，队友（当成对手）也可能抢走牌权——赢了，但要看人脸色；
    - 甩**一对 5**：地主没有对子、队友的一对 3 也压不住 → 两家只能过 → 我拿着 8 走完。
    第二手**谁来都拦不住**，所以要排在前面。证明里的"必胜"是"两个农民配合"的必胜，
    而真人队友不按剧本走——这一条就是给那种情况兜底的。
    """
    state = endgame_state(["3 6", "3 3 6 8", "5 5 8"], turn=2, landlord=0)
    result = solve(state)
    assert result.winning is True
    assert result.solo is True, "这个局面有'不看队友脸色'的赢法，应该挑出来"
    assert sorted(C.rank(c) for c in result.move.cards) == [2, 2], "该出一对 5"

    naive = solve(state, limits=Limits(solo_scan=0))
    assert sorted(C.rank(c) for c in naive.move.cards) == [2], "不挑的话它会先甩那张单 5"


def test_solo_preference_is_deterministic():
    """挑 solo 不能引入不确定性：同一个局面两次必须给同一手。

    （第一版用**时间**预算做这一步，结果同一个局面两次问出不同结果——测试当场抓到。）
    """
    state = endgame_state(["3 6", "3 3 6 8", "5 5 8"], turn=2, landlord=0)
    first, second = solve(state), solve(state)
    assert (first.move.cards, first.solo, first.nodes) == (second.move.cards, second.solo, second.nodes)


def test_solo_check_is_skipped_when_i_am_the_landlord():
    """我是地主时没有队友，这一步直接跳过（省时间）。"""
    state = endgame_state(["5 5 8", "3 3 6 8", "3 6"], turn=0, landlord=0)
    result = solve(state)
    assert result.winning is True
    assert result.solo is False, "地主没有队友，谈不上'不依赖队友'"


# --------------------------------------------------------------------- 看不见别家手牌时


def blind_endgame(hands, *, turn=0, landlord=1):
    """造一个**真实对局形状**的盲局：手里只有自己的牌，别家只知道张数。

    必须补齐 54 张的归属：真实对局里每张牌都在某个人手里或已经打出，所以"未见牌"正好
    等于两家的手牌。而 :func:`endgame_state` 只摆三家手牌、剩下的牌"还在盒子里"，
    那样 ``unseen_for`` 会算出五十几张、和各家张数对不上（实现里会因此拒绝求解）。
    这里把多出来的牌标成"已打出"，未见牌就正好是两家的手牌了。
    """
    state = endgame_state(hands, turn=turn, landlord=landlord)
    used = set()
    for seat in (0, 1, 2):
        used |= set(state.hand_for(seat) or ())
    leftovers = tuple(card for card in C.full_deck() if card not in used)
    return replace(state, all_hands=None, played_by_seat=(leftovers, (), ()))


def test_uncertain_proves_a_win_that_needs_nobody():
    """我拿王炸、两家各剩一两张：**无论牌怎么分、谁怎么打，我都赢**。

    注意首选是**单出小王**而不是王炸：大王在我手里，所以小王出去谁也压不住，下一手走大王——
    比王炸更省。求解器按"代价小"排序，选得对。
    """
    state = blind_endgame(["小王 大王", "3", "4 5"], turn=0, landlord=1)
    assert uncertain_solvable(state) is True
    result = solve_uncertain(state)
    assert result.winning is True
    assert result.worlds >= 2, "应该枚举了多种分配"
    assert set(result.move.cards) == set(cards_of("小王")), "先甩小王，大王留着收尾"
    kinds = {frozenset(m.cards) for m in result.best_moves}
    assert frozenset(cards_of("小王 大王")) in kinds, "王炸当然也赢"


def test_uncertain_refuses_when_a_split_could_lose():
    """只要**有一种**分配能让对手赢，就不算数——这才是"证明"的意思。

    （手里只有一张牌的话，打出去就赢了，跟牌怎么分无关——那是另一个坑，这里特意避开。）
    我 3 4、两家共两张未知牌：无论我先出 3 还是 4，对面总有张更大的接过去，
    而他接着就把最后一张走完。
    """
    state = blind_endgame(["3 4", "5", "6"], turn=0, landlord=1)
    result = solve_uncertain(state)
    assert result.winning is not True, "牌怎么分都有一种能压住我，凭什么说必胜"


def test_uncertain_needs_the_counts_to_add_up():
    """张数对不上（比如底牌还没亮）就不猜——那种局面我们掌握的信息不完整。"""
    state = endgame_state(["3 4", "5 6", "7 8"], turn=0, landlord=1)
    assert uncertain_solvable(replace(state, all_hands=None)) is False


def test_uncertain_respects_the_unseen_gate():
    """未见牌太多就不算（枚举会炸）。"""
    from ddz.engine.endgame import Limits

    state = blind_endgame(["3 4 5 6", "7 8 9 10", "J Q K A"], turn=0, landlord=1)
    assert uncertain_solvable(state, Limits(max_unseen=8)) is True
    assert uncertain_solvable(state, Limits(max_unseen=7)) is False


def test_uncertain_is_deterministic():
    state = blind_endgame(["小王 大王", "3", "4 5"], turn=0, landlord=1)
    first, second = solve_uncertain(state), solve_uncertain(state)
    assert (first.winning, first.worlds, first.nodes) == (second.winning, second.worlds, second.nodes)
    assert (first.move.cards if first.move else None) == (second.move.cards if second.move else None)


def to_blind(state):
    """把任意状态改成"真实对局形状的盲局"：没归属的牌一律算作已打出。"""
    used = set()
    for seat in (0, 1, 2):
        used |= set(state.hand_for(seat) or ())
    leftovers = tuple(card for card in C.full_deck() if card not in used)
    return replace(state, all_hands=None, played_by_seat=(leftovers, (), ()))


def test_uncertain_matches_brute_force_over_all_worlds():
    """**验收**：逐个世界（分配）判一遍，"全都赢"才是必胜——口径必须与它一致。

    两个细节都是踩过的：

    - **枚举按"牌"来**（组合完再按点数去重），而不是复用求解器里那套按点数枚举的实现——
      不然枚举写错了，两边一起错，测试就成了摆设。
    - **语义是"队友也当对手"**（``side={me}``）：不确定求解器要的是"谁也拦不住"，而
      :func:`solve` 默认按"农民队合作"判，两者本来就不同（后者宽松）。
    """
    from itertools import combinations

    from ddz.engine.endgame import _solve_hands

    def worlds_by_cards(unseen_cards, size_first):
        seen = set()
        for combo in combinations(unseen_cards, size_first):
            rest = tuple(card for card in unseen_cards if card not in combo)
            key = (tuple(sorted(C.rank(c) for c in combo)), tuple(sorted(C.rank(c) for c in rest)))
            if key in seen:
                continue
            seen.add(key)
            yield combo, rest

    rng = random.Random(20261007)
    checked = 0
    for _ in range(15):
        state, _ = random_endgame(rng, total=9)
        me = state.advise_seat
        mine = state.hand_for(me)
        others = [s for s in SEATS if s != me]
        blind = to_blind(state)
        unseen_cards = blind.unseen_for(me)
        if not mine or len(unseen_cards) != sum(state.counts[s] for s in others):
            continue
        got = solve_uncertain(blind)

        all_win = True
        for combo, rest in worlds_by_cards(unseen_cards, state.counts[others[0]]):
            by_seat = {me: list(mine), others[0]: list(combo), others[1]: list(rest)}
            hands = tuple(tuple(sorted(by_seat[s])) for s in SEATS)
            solved = _solve_hands(
                hands, frozenset({me}), me, blind.to_beat, blind.turn,
                _passes_behind(blind), limits=Limits(),
            )
            if solved.winning is not True:
                all_win = False
                break
        assert (got.winning is True) == all_win, f"口径不一致：{blind.hand_for(me)}"
        if got.winning:
            for move in got.best_moves or []:
                by_seat = {me: list(mine)}
                for combo, rest in worlds_by_cards(unseen_cards, state.counts[others[0]]):
                    pass
            assert got.worlds >= 1
        checked += 1
    assert checked >= 8, f"样本太少（{checked}）"
