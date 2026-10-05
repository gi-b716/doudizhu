"""启发式 agent：五条硬规则逐条，加打分项的合理性。"""

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


def test_hard_rule_let_the_partner_lead(agent):
    state = game_state("2 2 3 4 5", to_beat=reading("5"), last_seat=2, counts=[5, 9, 6])
    candidate = agent.act(state)
    assert candidate.is_pass
    assert "hard:partner_lead" in candidate.tags
    assert "上家是队友" in candidate.reasons[0]


def test_hard_rule_never_overtake_a_partner_with_two_cards(agent):
    state = game_state("2 2 3 4 5", to_beat=reading("3"), last_seat=2, counts=[5, 9, 2])
    candidate = agent.act(state)
    assert candidate.is_pass and "hard:partner_low" in candidate.tags


def test_hard_rule_take_over_when_partner_is_far_and_we_are_close(agent):
    """队友还有 8 张、我两手持完 → 不再"放行"，改为正常打分（可能压，也可能仍然不出）。"""
    close = game_state("34567 88", to_beat=reading("3"), last_seat=2, counts=[7, 9, 8])
    candidate = agent.act(close)
    assert "hard:partner_lead" not in candidate.tags, "该接管时不该直接放行"

    # 对照组：队友只剩 6 张（没到" > 6 张"的门槛）→ 照常放行
    not_so_close = game_state("34567 88", to_beat=reading("3"), last_seat=2, counts=[7, 9, 6])
    assert "hard:partner_lead" in agent.act(not_so_close).tags


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
    """手里还有别的牌，只能靠炸弹管上队友 → 不出。"""
    state = game_state("3333 7", to_beat=reading("5"), last_seat=2, counts=[5, 9, 6])
    candidate = agent.act(state)
    assert candidate.is_pass
    assert "hard:partner_lead" in candidate.tags


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
