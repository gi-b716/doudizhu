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
from ddz.engine.endgame import Limits, SolveResult, is_endgame, solvable, solve
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
    """默认阈值是**实测**定的，不是拍的：总 14 张最慢 114ms，16 张最慢 725ms，
    而服务端 deadline 默认 400ms。要改就重新量一遍再改。"""
    assert Limits().max_total == 14
    assert Limits().max_nodes <= 200_000


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
