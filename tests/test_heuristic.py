"""启发式 agent：六条硬规则逐条，加打分项的合理性。"""

from __future__ import annotations

import pytest

from ddz import cards as C
from ddz import patterns as P
from ddz.engine.heuristic import DEFAULT_WEIGHTS, HeuristicAgent, Weights, analyze_hand
from helpers import cards_of, game_state, reading


@pytest.fixture()
def agent() -> HeuristicAgent:
    return HeuristicAgent()


# --------------------------------------------------------------------- 硬规则 1/2


def test_hard_rule_no_beat_means_pass(agent):
    state = game_state("3 4 5", to_beat=reading("2"), last_seat=1)  # 手里全小牌，管不上单张 2
    candidate = agent.act(state)
    assert candidate.is_pass
    assert "hard:no_beat" in candidate.tags
    assert "管上" in candidate.reasons[0]


def test_hard_rule_finish_in_one_go(agent):
    state = game_state("34567")
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert candidate.pattern_key == "straight:0:5"
    assert "hard:win" in candidate.tags
    assert candidate.reasons == ["这一手就能把牌走完"]


def test_hard_rule_finish_overrides_partner_rule(agent):
    """上家是队友，但我能一把走完 → 走。"""
    state = game_state("45678", to_beat=reading("34567"), last_seat=2)
    candidate = agent.act(state)
    assert not candidate.is_pass and "hard:win" in candidate.tags


# --------------------------------------------------------------------- 硬规则 3


#: 拆炸弹打单张：这一手本身就很亏，用来把"必须拦"从打分里隔离出来
_COSTLY_BLOCK = "2 2 2 2 3 3 3 3 4 5 6 7 8"


def test_must_block_beats_the_score():
    """对手只剩 1 张：同一手牌、同一上家，只是分数为负也必须拦。

    把 tempo/coop/threat 三项权重清零，好让"打分认为该放弃"和"硬规则要求拦"直接对撞。
    """
    plain = HeuristicAgent(Weights(tempo=0.0, coop=0.0, threat=0.0))
    far = game_state(_COSTLY_BLOCK, to_beat=reading("A"), last_seat=1, counts=[13, 9, 9])
    assert plain.act(far).is_pass, "对手还远，这一手亏就不该出"

    near = game_state(_COSTLY_BLOCK, to_beat=reading("A"), last_seat=1, counts=[13, 1, 9])
    candidate = plain.act(near)
    assert not candidate.is_pass
    assert "hard:must_block" in candidate.tags
    assert "必须拦" in candidate.reasons[0]


def test_must_block_when_partner_is_not_the_last_player(agent):
    """队友也在场时，只要对手快走完，同样必须拦。"""
    state = game_state("2 2 3 4 5", to_beat=reading("A"), last_seat=1, counts=[5, 1, 4])
    candidate = agent.act(state)
    assert not candidate.is_pass and "hard:must_block" in candidate.tags


# --------------------------------------------------------------------- 硬规则 4


def test_partner_lead_defaults_to_letting_them_play(agent):
    """队友领出，而我只能拆结构或动控制牌 → 不出。"""
    state = game_state("2 2 3 3", to_beat=reading("5"), last_seat=2, counts=[4, 9, 8])
    candidate = agent.act(state)
    assert candidate.is_pass
    assert "上家是队友" in candidate.reasons[0]


def test_partner_lead_dumps_junk_when_it_is_free(agent):
    """队友领小牌、我手里有张孤零零的废牌 → 顺手放掉，不再一律不出。"""
    state = game_state("6 8 8 9 9 10 J Q K A", to_beat=reading("3"), last_seat=2, counts=[10, 9, 8])
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert "hard:partner_dump" in candidate.tags
    assert candidate.pattern_key == "single:3:1"  # ♠6
    assert "顺手" in candidate.reasons[0]


def test_no_dump_while_the_partner_is_closing_in(agent):
    """队友只剩 4 张、马上要走完 → 还是让他走，别抢。"""
    state = game_state("6 8 8 9 9 10 J Q K A", to_beat=reading("3"), last_seat=2, counts=[10, 9, 4])
    assert agent.act(state).is_pass


def test_beating_the_partner_pays_a_price(agent):
    """没有便宜可占时，压队友的候选都带着"抢出牌权"的扣分项。"""
    from ddz.moves import legal_beats

    hand = "3 5 7 9 J K 2 4 6 8 10 Q A"
    state = game_state(hand, to_beat=reading("9"), last_seat=2, counts=[13, 9, 4])
    scored = [
        agent._score(state, 0, tuple(state.hand), m, 2, [1], [], agent.weights.coop)
        for m in legal_beats(state.hand, state.to_beat)
    ]
    assert scored and all(any("抢掉队友" in reason for reason in item[2]) for item in scored)


def test_hard_rule_never_overtake_a_partner_with_two_cards(agent):
    """队友只剩 2 张：这是唯一的铁律——绝不压。"""
    state = game_state("2 2 3 4 5", to_beat=reading("3"), last_seat=2, counts=[5, 9, 2])
    candidate = agent.act(state)
    assert candidate.is_pass and "hard:partner_low" in candidate.tags

    state = game_state("3 4 5 6 7 8 9", to_beat=reading("3"), last_seat=2, counts=[7, 9, 1])
    assert agent.act(state).is_pass


def test_take_over_when_i_am_close_to_finishing(agent):
    """队友只剩 4 张（快走完了），但我三手内就能走完 → 照样把废牌放掉抢牌权。"""
    near = game_state("6 88 99", to_beat=reading("3"), last_seat=2, counts=[5, 9, 4])
    candidate = agent.act(near)
    assert not candidate.is_pass and "hard:partner_dump" in candidate.tags

    # 对照组：我手上还有一堆结构（5 手）→ 让队友走
    far = game_state("6 7 88 99 10 J", to_beat=reading("3"), last_seat=2, counts=[7, 9, 4])
    assert agent.act(far).is_pass


# --------------------------------------------------------------------- 硬规则 5


def test_bomb_is_kept_unless_it_is_needed(agent):
    """领出时不会把炸弹当普通牌打掉（四带二那种），也不会白扔炸弹。"""
    state = game_state("3333 4 5 6 7 8 9 10 J")
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert candidate.move is not None and not P.is_bomb(candidate.move.pattern)
    assert "scored:bomb_as_plain" not in candidate.tags
    assert "scored:bomb_hold" in candidate.tags


def test_finishing_with_a_bomb_is_always_right(agent):
    """整手就是一个炸弹：能一把走完，硬规则 2 直接出手（哪怕上家是队友）。"""
    state = game_state("3333", to_beat=reading("5"), last_seat=2, counts=[4, 9, 6])
    candidate = agent.act(state)
    assert not candidate.is_pass and "hard:win" in candidate.tags


def test_never_bomb_a_partner(agent):
    """只有炸弹能管上队友 → 不出（炸弹绝不用来压自己人）。"""
    state = game_state("3333 7", to_beat=reading("9"), last_seat=2, counts=[5, 9, 6])
    candidate = agent.act(state)
    assert candidate.is_pass


def test_dumping_over_the_partner_is_never_a_bomb(agent):
    """顺手放废牌可以，但绝不是把炸弹甩出去。"""
    state = game_state("3333 7", to_beat=reading("5"), last_seat=2, counts=[5, 9, 6])
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert not P.is_bomb(candidate.move.pattern)
    assert candidate.pattern_key == "single:4:1"  # 只可能是那张 7


def test_bomb_allowed_to_stop_an_opponent_at_one_card(agent):
    state = game_state("3333 7", to_beat=reading("5"), last_seat=1, counts=[5, 1, 8])
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert "hard:must_block" in candidate.tags


def test_bomb_allowed_when_clearly_better_and_tagged(agent):
    """评分显著占优的分支存在：把炸弹代价调到很低时，炸弹会被选中并打上 bomb 标记。"""
    cheap_bomb = HeuristicAgent(Weights(bomb=0.0, bomb_margin=0.0))
    state = game_state("3333 4", to_beat=reading("5"), last_seat=1, counts=[5, 6, 6])
    candidate = cheap_bomb.act(state)
    assert not candidate.is_pass and candidate.move is not None
    assert P.is_bomb(candidate.move.pattern)


# --------------------------------------------------------------------- 打分项


def test_tempo_reason_when_taking_back_the_lead(agent):
    state = game_state("2 3 4 5 6", to_beat=reading("5"), last_seat=1, counts=[5, 8, 8])
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert any("拿回出牌权" in reason for reason in candidate.reasons)


def test_split_is_penalised(agent):
    """手牌 33 55 要压单张 4，只能拆对子 55 → 给出"拆开"的理由。"""
    state = game_state("3 3 5 5", to_beat=reading("4"), last_seat=1, counts=[4, 8, 8])
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert "scored:split" in candidate.tags
    assert candidate.pattern_key == "single:2:1"


def test_leading_dumps_junk_before_breaking_the_big_structure(agent):
    """34567 88 9 10 J Q K 是"一条 11 张的长顺 + 一张散 8"。

    领出时应当先把散 8 打掉（保留长顺），而不是为了打长顺把对子拆了。
    """
    hand = "34567 88 9 10 J Q K"
    candidate = agent.act(game_state(hand))
    assert not candidate.is_pass
    assert candidate.pattern_key == "single:5:1"


def test_feed_the_partner_when_he_is_almost_out(agent):
    """我领出、队友只剩 1 张 → 给一手小单张。"""
    state = game_state("3 4 5 6 7 8 9 10 J Q K A 2 2", counts=[15, 9, 1])
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert "scored:feed" in candidate.tags
    assert candidate.move is not None and candidate.move.pattern.type == P.SINGLE


def test_alternatives_are_listed_for_the_ui(agent):
    state = game_state("34567 88 9 10 J")
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert all(alt.move is not None for alt in candidate.alternatives)
    assert len(candidate.alternatives) <= 3


def test_weights_are_injectable():
    cautious = HeuristicAgent(Weights(bomb=20.0, cost=1.0))
    assert cautious.weights.bomb == 20.0
    assert DEFAULT_WEIGHTS.bomb == 6.0 and DEFAULT_WEIGHTS.red == 3.0


def test_no_hand_means_no_advice(agent):
    from ddz.state import GameState

    state = GameState(seat=None, phase="playing", hand=None)
    candidate = agent.act(state)
    assert candidate.is_pass and "看不到手牌" in candidate.reasons[0]


# --------------------------------------------------------------------- 手牌分析


def test_analyze_hand_reports_plays_control_and_bombs():
    report = analyze_hand(cards_of("3333 小王 2 456789"))
    assert report["n_plays"] >= 2
    assert "3333" in report["bombs"]
    assert any("大王" == name or "小王" == name for name in report["control"])
    assert isinstance(report["plays"], list) and report["plays"]


def test_analyze_hand_of_empty_hand():
    assert analyze_hand([])["n_plays"] == 0


def test_decisions_are_always_legal(agent):
    """随机局面下蹦一圈：给出的着法必须是合法的，pass 前必须真的管不上或主动放弃。"""
    import random

    from ddz.moves import has_any_beat
    from helpers import random_hand

    rng = random.Random(4)
    for _ in range(30):
        hand = random_hand(rng, rng.randint(8, 17), max_count=4)
        to_beat = reading(rng.choice(["3", "5", "AA", "34567", "333444"]))
        state = game_state(hand, to_beat=to_beat, last_seat=1, counts=[len(hand), 6, 7])
        candidate = agent.act(state)
        if candidate.is_pass:
            continue
        assert set(candidate.cards) <= set(hand)
        assert candidate.pattern_key in {p.key() for p in P.classify(candidate.cards)}
        assert P.beats(candidate.move.pattern, to_beat)


# --------------------------------------------------------------------- 带队友走（喂牌）


def test_feed_the_partner_with_the_lowest_single(agent):
    """队友只剩 2 张、轮到我领出 → 出手里最小的单张喂他（"带队友走"）。"""
    state = game_state("6 88 99 10 J Q K A", counts=[10, 2, 9], landlord=2)  # 队友是下家
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert candidate.pattern_key == "single:3:1"          # ♠6：手里最小的单张
    assert "scored:feed" in candidate.tags
    assert any("喂他" in reason for reason in candidate.reasons)


def test_feed_is_discounted_when_the_landlord_sits_between(agent):
    """队友是上家时地主夹在中间，喂牌权重减半，理由里也写明白。"""
    state = game_state("6 88 99 10 J Q K A", counts=[10, 9, 2], landlord=1)
    candidate = agent.act(state)
    assert "scored:feed" in candidate.tags
    assert any("未必接得上" in reason for reason in candidate.reasons)


def test_take_the_lead_back_when_the_partner_is_about_to_finish(agent):
    """队友 2 张、地主刚出牌 → 接过来拿回牌权，别让地主一直领。"""
    state = game_state("6 88 99 10 J Q K A", to_beat=reading("5"), last_seat=1, counts=[10, 9, 2], landlord=1)
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert "scored:coop" in candidate.tags


def test_feed_does_not_apply_without_a_single(agent):
    """手里全是对子：喂不了单张，就按常规出最小的对子。"""
    state = game_state("2 2 A A K K", counts=[6, 2, 9], landlord=2)
    candidate = agent.act(state)
    assert not candidate.is_pass
    assert candidate.move.pattern.type == P.PAIR
    assert "scored:feed" not in candidate.tags


def test_every_recommendation_carries_a_reason(agent):
    """任何推荐都得带理由——界面上不能出现"有建议但没说法"。"""
    import random

    from helpers import random_hand

    rng = random.Random(11)
    for _ in range(40):
        hand = random_hand(rng, rng.randint(5, 17), max_count=4)
        state = game_state(hand, counts=[len(hand), rng.randint(1, 9), rng.randint(1, 9)],
                           landlord=rng.choice([1, 2]))
        candidate = agent.act(state)
        assert candidate.reasons, f"没有理由：{hand}"
        assert all(isinstance(text, str) and text for text in candidate.reasons)

# --------------------------------------------------------------------- 硬规则 6：别送对手一把走完
#
# 下面两个局面是从真实对局库（2026-10-05 ~ 10-07，120 局）里 `ddz.tools.review` 抓出来的
# "求解器证明能赢、实际却打丢"的两处，牌 id 原样照抄。对局库本身不入库（gitignore），
# 所以这里把局面写死；求解器现场重算，不写死结论。

#: 局 31971 第 54 手：0 号[农民] 3 5 7 9 J、1 号[地主] Q、2 号[农民·我] 3 3 4 4 5 5 10 Q，我领出
REPLAY_31971 = [(0, 9, 18, 25, 35), (38,), (2, 3, 5, 6, 10, 11, 31, 37)]

#: 局 32080 第 32 手：0 号[农民·我] 3 3 3 4 4 7 9 9 K A 2、1 号[地主] 9、2 号[农民] A（刚领出单张 4）
REPLAY_32080 = [(0, 1, 3, 5, 7, 18, 24, 27, 42, 47, 48), (25,), (44, 6)]


def _replayed(hands, **kwargs):
    from ddz.state import from_platform_state
    from helpers import complete_payload

    return from_platform_state(complete_payload(hands, **kwargs))


def _solver_best(state):
    from ddz.engine.endgame import Limits, solve, solvable
    from ddz.tools.eval_records import action_key

    limits = Limits(time_budget_ms=5000.0, rank_budget_ms=1000.0)
    assert solvable(state, limits), "复盘出来的残局应当算得清"
    solved = solve(state, limits=limits)
    assert solved.proved and solved.winning, "这两局当时都是我方必胜"
    return {None if m is None else action_key(m.cards) for m in (solved.best_moves or [])}


def test_lead_does_not_hand_a_one_card_opponent_the_win():
    """真实对局 31971：地主只剩 1 张（Q），旧版领单张 10 → 他 Q 走完，必胜局变输。"""
    from ddz.tools.eval_records import action_key

    state = _replayed(REPLAY_31971, turn=2, landlord=1)
    best = _solver_best(state)

    fixed = HeuristicAgent().act(state)
    assert action_key(fixed.move.cards) in best, f"领 {C.format_cards(fixed.move.cards)} 不在必胜集合里"
    assert "hard:walkout_guard" in fixed.tags

    old = HeuristicAgent(Weights(walkout_guard=False)).act(state)
    assert action_key(old.move.cards) not in best, "对照：关掉规则 6 就该复现当初那手败着"
    assert old.move.cards == (31,), "当初打的是单张 10"


def test_overtakes_partner_when_the_opponent_would_walk_out():
    """真实对局 32080：队友 1 张 A 领单张 4、地主 1 张 9 在我下家——放行就是让地主 9 走完。"""
    from ddz.tools.eval_records import action_key

    state = _replayed(REPLAY_32080, turn=0, landlord=1, to_beat=reading("4"),
                      last_seat=2, play_cards=[6])
    best = _solver_best(state)

    fixed = HeuristicAgent().act(state)
    assert not fixed.is_pass, "这里绝不能放行"
    assert action_key(fixed.move.cards) in best
    assert "hard:overtake_partner" in fixed.tags and "hard:walkout_guard" in fixed.tags
    assert "必须先抢下来" in fixed.reasons[0]

    old = HeuristicAgent(Weights(walkout_guard=False)).act(state)
    assert old.is_pass and "hard:partner_low" in old.tags, "对照：关掉规则 6 就复现当初的放行"


def test_partner_low_rule_still_holds_when_no_opponent_is_close():
    """对手还有一大把牌时，"队友 ≤2 张绝不压"照旧是铁律（规则 6 不该改变它）。"""
    state = game_state("3 4 5 6 7 8 9", to_beat=reading("3"), last_seat=2, counts=[7, 9, 1])
    candidate = HeuristicAgent().act(state)
    assert candidate.is_pass and "hard:partner_low" in candidate.tags


def test_lead_guard_is_knowledge_free():
    """看不见别家的牌也成立：对手只剩 1 张时，手里有对子就不该领单张（1 张牌压不过对子）。"""
    state = game_state("3 3 4 5 6 7", counts=[6, 1, 9], landlord=1)   # 我领出，下家只剩 1 张
    candidate = HeuristicAgent().act(state)
    assert len(candidate.move.cards) > 1, f"领了 {C.format_cards(candidate.move.cards)}：单张会被他一把压过"
    assert "hard:walkout_guard" in candidate.tags


def test_lead_guard_falls_back_to_the_biggest_single():
    """全是单张可领时，领最大的那张——赌他手里那张更小（总比领最小的送他走要好）。"""
    state = game_state("3 7 K", counts=[3, 1, 9], landlord=1)
    candidate = HeuristicAgent().act(state)
    assert candidate.move.cards == (cards_of("K")[0],)
    assert "赌他压不过" in candidate.reasons[-1]


def test_lead_guard_leaves_a_two_card_opponent_alone_on_singles():
    """对手剩 2 张时，单张不是一把手走完的牌型（他压过还剩 1 张）；该避开的是对子。"""
    pair_risk = game_state("3 3 5 7 9", counts=[5, 2, 9], landlord=1)
    candidate = HeuristicAgent().act(pair_risk)
    assert len(candidate.move.cards) != 2 or candidate.move.pattern.type != P.PAIR, \
        "对手剩 2 张时不该领对子：他一对压过来就走完了"
