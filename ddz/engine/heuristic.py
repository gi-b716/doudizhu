"""启发式 agent：Phase 2–3 的主力，之后永远是兜底。

打分（PLAN §3.3，每一项都对应 :mod:`ddz.explain` 里的一句中文理由）::

    score(m) = +W_RED ·(手数减少)   − W_COST ·(牌力消耗)  + W_CTRL ·(保留控制)
               − W_SPLIT·(拆结构)   − W_BOMB ·(炸弹代价)  + W_TEMPO·(抢回出牌权)
               + W_THREAT·(压住快走完的对手) + W_COOP·(农民配合)
    score(pass) = 0

硬规则（同名单测，不参与打分，直接覆盖）：

1. 无牌可管 → 不出；
2. 能一把走完 → 立刻走；
3. 对手只剩 ≤2 张而我在跟牌 → **必须拦**（挑最好的能压的牌；一张都压不上则给出警告）；
4. 上家是队友 → **默认放行，但不是一律不出**。三种情况会压：
   ① 队友 ≤2 张 → 铁律，绝不压；② **顺手打废牌**——队友还有 5 张以上、我这一手不伤结构、
   不动控制牌、而且本来就是拆解里要打的一手（比如手里孤零零一张 6）→ 压上去把废牌放掉；
   ③ 我 ≤3 手就走完 → 接管牌权（不再顾虑）；其余情况压队友要付 ``W_COOP`` 的"抢牌权"代价；
5. 炸弹纪律：只用于赢牌、拦 ≤2 张的对手，或评分比最优非炸弹方案高出 ``bomb_margin``；
   绝不炸队友。

权重都是 :class:`Weights` 的字段，方便用对局库/竞技场调（PLAN §3.7）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .. import cards as C
from ..explain import (
    CONTROL_NAMES, bomb_ranks_text, control_rank_text, rank_list_text,
    reason_block, reason_bomb, reason_bomb_as_plain, reason_bomb_hold, reason_feed,
    reason_grabbing_partner, reason_keeps_control, reason_partner_dump, reason_quiet,
    reason_last_control, reason_must_block, reason_no_beat, reason_no_hand,
    reason_not_worth, reason_partner_lead, reason_partner_low, reason_plays_saved,
    reason_plays_wasted, reason_splits, reason_take_over, reason_team, reason_tempo,
    reason_win,
    seat_label,
)
from ..moves import legal_beats, legal_leads, winning_move
from ..patterns import SINGLE, is_bomb
from ..state import PHASE_FINISHED, PHASE_PLAYING, SEATS, GameState
from .base import Bid, Candidate
from .decomposer import best_decomposition, control_ranks, n_plays

__all__ = ["Weights", "HeuristicAgent", "analyze_hand"]


@dataclass(frozen=True, slots=True)
class Weights:
    """打分权重（PLAN §3.3 的默认值）。"""

    red: float = 3.0
    cost: float = 0.30
    ctrl: float = 1.6
    split: float = 0.9
    bomb: float = 6.0
    tempo: float = 4.0
    threat: float = 2.5
    coop: float = 3.0
    #: 炸弹纪律：要"评分显著占优"才允许动炸弹
    bomb_margin: float = 2.0
    #: 我剩几手以内就不再顾忌队友的牌权（我马上要赢了，抢过来更值）
    take_over_my_plays: int = 3
    #: 队友还有这么多张以上时，才考虑"顺手把废牌打掉"（他快走完就让他走）
    partner_dump_min_cards: int = 5


DEFAULT_WEIGHTS = Weights()

#: 每次决策最多评估多少个候选着法（20 张手牌的 canonical 领出实测最多 64 个）。
_MAX_CANDIDATES = 128

#: 理由最多给几条（界面只展示前几条）。
_MAX_REASONS = 3


def spends_whole_bomb(counts_before, spent) -> bool:
    """这一手是不是把整整一个炸弹当普通牌打掉了（四带二把炸弹当主体之类）。"""
    return any(counts_before[r] == 4 and spent[r] == 4 for r in range(C.N_RANKS))


def _card_value(card: int) -> float:
    """单张牌的"牌力"（0.07 ~ 1.0），用于 W_COST 项。"""
    rank = C.rank(card) if card < 52 else 13 + (card - 52)
    return (rank + 1) / 15.0


def analyze_hand(hand) -> dict:
    """整手牌的分析（服务响应的 ``hand_analysis``）。"""
    cards = C.sort_cards(hand)
    if not cards:
        return {"n_plays": 0, "control": [], "bombs": [], "rockets": 0}
    decomp = best_decomposition(cards)
    return {
        "n_plays": decomp.n_plays,
        "control": control_rank_text(cards, decomp.control),
        "bombs": bomb_ranks_text(decomp.bombs),
        "rockets": decomp.rockets,
        "exact": decomp.exact,
        "plays": [p.describe() for p in decomp.patterns],
    }


class HeuristicAgent:
    """启发式出牌 agent（同时用它兜底：任何异常都退回这里，见 Phase 4）。"""

    name = "heuristic"

    def __init__(self, weights: Weights | None = None, thresholds=None) -> None:
        self.weights = weights or DEFAULT_WEIGHTS
        #: 叫分阈值。``None`` = 库默认的**宽档**（阈值低、叫得勤，竞技场校准的基准）；
        #: 产品（浮窗/服务）默认走**严档**，由调用方显式传进来——
        #: 用 :func:`ddz.engine.bidding.thresholds_of_tier` 拿，别自己拼 ``BidThresholds()``。
        self.thresholds = thresholds

    # ----------------------------------------------------------------- 出牌

    def act(self, state: GameState) -> Candidate:
        """决策入口：记好各阶段耗时再返回（服务会把 timing 透传到 ``timing_ms``）。"""
        started = time.perf_counter()
        timing: dict[str, float] = {}
        candidate = self._decide(state, timing)
        candidate.source = candidate.source or self.name
        candidate.timing = {**timing, "total": round((time.perf_counter() - started) * 1000.0, 3)}
        return candidate

    def _decide(self, state: GameState, timing: dict[str, float]) -> Candidate:
        seat = state.advise_seat
        hand = state.advise_hand
        source = self.name
        if seat is None or not hand:
            return Candidate(source=source, reasons=[reason_no_hand()])
        hand = tuple(hand)
        to_beat = state.to_beat

        # 硬规则 2：能一把走完就走
        finisher = winning_move(hand, to_beat)
        if finisher is not None:
            return Candidate(
                finisher, score=float("inf"), reasons=[reason_win()], tags=["hard:win"], source=source
            )

        gen_started = time.perf_counter()
        if to_beat is None:
            moves = legal_leads(hand, canonical=True)[:_MAX_CANDIDATES]
        else:
            moves = legal_beats(hand, to_beat, canonical=True)[:_MAX_CANDIDATES]
            # 硬规则 1：无牌可管（领出时必然有牌可出，所以只在跟牌时判）
            if not moves:
                return Candidate(None, 0.0, [reason_no_beat()], ["hard:no_beat"], source)
        moves_ms = (time.perf_counter() - gen_started) * 1000.0

        partner = state.partner_seat
        last_seat = state.last.seat if state.last is not None else None
        counts = state.counts
        opponents = [s for s in SEATS if s != seat and s != partner]
        low_opponents = [s for s in opponents if 0 < counts[s] <= 2]

        # 硬规则 4：上家是队友。只有"队友马上走完"才是铁的（绝不压）；
        # 其余情况走打分，但压队友要付一份抢牌权的代价——不然会出现"队友一出牌就一律不出"。
        following_partner = last_seat is not None and last_seat == partner and to_beat is not None
        takeover_reason = None
        partner_penalty = 0.0
        if following_partner:
            partner_cards = counts[last_seat]
            if partner_cards <= 2:
                return Candidate(
                    None, 0.0,
                    [reason_partner_low(partner_cards)],
                    ["hard:partner_low"], source,
                )
            # 队友快走完了就让他走；但我要是在 3 手内就能走完，还是把废牌放掉抢牌权
            closing_in = partner_cards < self.weights.partner_dump_min_cards
            if not closing_in or n_plays(hand) <= self.weights.take_over_my_plays:
                dump = self._free_dump(hand, moves)
                if dump is not None:
                    return Candidate(
                        dump,
                        self._card_costs(dump),
                        [reason_partner_dump(C.card_name(max(dump.cards)), seat_label(last_seat, seat))],
                        ["hard:partner_dump"], source,
                    )
            if n_plays(hand) <= self.weights.take_over_my_plays:
                takeover_reason = reason_take_over(partner_cards)
            else:
                partner_penalty = self.weights.coop

        scored = [
            self._score(state, seat, hand, move, partner, opponents, low_opponents, partner_penalty)
            for move in moves
        ]
        scored.sort(key=lambda item: -item[1])
        scoring_ms = (time.perf_counter() - gen_started) * 1000.0 - moves_ms
        timing["moves"] = round(moves_ms, 3)
        timing["score"] = round(scoring_ms, 3)

        # 硬规则 5：炸弹纪律
        scored = self._apply_bomb_discipline(scored, partner, last_seat, low_opponents)
        if not scored:
            return self._pass_candidate([], source)

        best_move, best_score, reasons, tags = scored[0]

        if takeover_reason is not None:
            reasons = [takeover_reason, *reasons]
            tags = [*tags, "hard:take_over"]

        # 硬规则 3：对手 ≤2 张而我在跟牌 → 必须拦
        if low_opponents and to_beat is not None:
            target = min(low_opponents, key=lambda s: counts[s])
            reasons = [reason_must_block(seat_label(target, seat), counts[target]), *reasons]
            tags = [*tags, "hard:must_block"]
        elif not low_opponents and to_beat is not None and best_score < 0:
            # 跟牌且所有能压的牌都亏 → 不出（score(pass) = 0）
            reason = (
                reason_partner_lead(seat_label(last_seat, seat))
                if following_partner
                else reason_not_worth()
            )
            return self._pass_candidate(scored, source, reason)

        alternatives = [
            Candidate(move, score, list(reasons_), list(tags_), source)
            for move, score, reasons_, tags_ in scored[1:4]
        ]
        return Candidate(best_move, best_score, reasons[:_MAX_REASONS], tags, source, alternatives=alternatives)

    def _free_dump(self, hand, moves):
        """"顺手打废牌"：这一手本来就是拆解里要打的、不伤结构、不动控制牌，且牌面最小。

        队友领出时唯一值得抢的便宜——把手里那张孤零零的小牌放掉，牌权还在自家。
        """
        before = n_plays(hand)
        control = set(control_ranks(hand))
        counts_before = C.rank_counts(hand)
        best = None
        best_cost = None
        for move in moves:
            spent = C.rank_counts(move.cards)
            if any(0 < spent[r] < counts_before[r] for r in range(C.N_RANKS)):
                continue                                   # 拆结构
            if any(r in control for r in range(C.N_RANKS) if spent[r]):
                continue                                   # 动控制牌
            after = n_plays([c for c in hand if c not in set(move.cards)])
            if before - 1 - after < 0:
                continue                                   # 打完手数还涨了
            cost = self._card_costs(move)
            if best_cost is None or cost < best_cost:
                best, best_cost = move, cost
        return best

    @staticmethod
    def _card_costs(move) -> float:
        return sum(_card_value(card) for card in move.cards)

    def _pass_candidate(self, scored, source: str, reason: str | None = None) -> Candidate:
        """不出，并列出最接近的几手作为备选。"""
        alternatives = [
            Candidate(move, score, list(reasons), list(tags), source)
            for move, score, reasons, tags in scored[:3]
        ]
        return Candidate(None, 0.0, [reason or reason_not_worth()], ["scored:pass"], source,
                         alternatives=alternatives)

    def _apply_bomb_discipline(self, scored, partner, last_seat, low_opponents):
        """炸弹只在赢牌 / 拦 ≤2 张对手 / 评分显著占优时出手；绝不炸队友。"""
        if len(scored) < 2:
            return scored
        bombs = [item for item in scored if item[0] is not None and _is_bomb_like(item[0], scored)]
        if not bombs:
            return scored
        others = [item for item in scored if item not in bombs]
        if partner is not None and last_seat == partner:
            return others                     # 绝不炸队友（可能为空，调用方会走"不出"）
        if low_opponents:                     # 拦濒临出完的对手：允许
            return scored
        best_bomb = max(item[1] for item in bombs)
        best_other = max((item[1] for item in others), default=float("-inf"))
        if best_bomb > best_other + self.weights.bomb_margin:
            return scored
        kept = [item for item in others if not _is_bomb_like(item[0], scored)]
        for item in kept:
            if "scored:bomb_hold" not in item[3]:
                item[3].append("scored:bomb_hold")
                item[2].append(reason_bomb_hold())
        return kept or others

    # ----------------------------------------------------------------- 打分

    def _score(self, state, seat, hand, move, partner, opponents, low_opponents, partner_penalty=0.0):
        """返回 ``(move, score, reasons, tags)``。"""
        weights = self.weights
        reasons: list[tuple[float, str]] = []
        tags: list[str] = []

        # 手数减少
        before = n_plays(hand)
        after = n_plays([c for c in hand if c not in set(move.cards)])
        saved = before - 1 - after
        if saved > 0:
            reasons.append((weights.red * saved, reason_plays_saved(before, after + 1)))
        elif saved < 0:
            reasons.append((weights.red * saved, reason_plays_wasted(before, after + 1)))
        score = weights.red * saved

        # 牌力消耗（大牌更贵）
        cost = sum(_card_value(card) for card in move.cards)
        score -= weights.cost * cost
        if cost >= 0.55:
            big = [C.card_name(c) for c in move.cards if _card_value(c) >= 0.8]
            reasons.append((-weights.cost * cost, f"用掉了大牌（{' '.join(big)}）" if big else "牌力消耗"))

        # 拆结构：取用了某个点数的一部分（留下孤张）
        counts_before = C.rank_counts(hand)
        spent = C.rank_counts(move.cards)
        split_ranks = [
            r for r in range(C.N_RANKS) if 0 < spent[r] < counts_before[r] and counts_before[r] >= 2
        ]
        if split_ranks:
            score -= weights.split * len(split_ranks)
            names = rank_list_text(split_ranks)
            reasons.append((-weights.split * len(split_ranks), reason_splits(names)))
            tags.append("scored:split")

        # 炸弹代价：动炸弹要付钱；把整整一个炸弹当普通牌打（比如四带二）同样要付
        if is_bomb(move.pattern):
            score -= weights.bomb
            reasons.append((-weights.bomb, reason_bomb()))
            tags.append("scored:bomb")
        elif spends_whole_bomb(counts_before, spent):
            score -= weights.bomb
            reasons.append((-weights.bomb, reason_bomb_as_plain()))
            tags.append("scored:bomb_as_plain")

        # 保留控制
        control = set(control_ranks(hand))
        spent_control = {r for r in control if spent[r]}
        kept_control = control - spent_control
        if spent_control and not kept_control:
            name = CONTROL_NAMES.get(max(spent_control), rank_list_text([max(spent_control)]))
            score -= weights.ctrl
            reasons.append((-weights.ctrl, reason_last_control(name)))
        elif not spent_control and kept_control:
            score += weights.ctrl
            name = CONTROL_NAMES.get(max(kept_control), rank_list_text([max(kept_control)]))
            reasons.append((weights.ctrl, reason_keeps_control(name)))

        # 抢回出牌权：跟牌时把牌权从对手手里拿回来
        last_seat = state.last.seat if state.last is not None else None
        if not state.leading and last_seat is not None and last_seat in opponents:
            score += weights.tempo
            reasons.append((weights.tempo, reason_tempo(seat_label(last_seat, seat))))
            tags.append("scored:tempo")

        # 压住快走完的对手
        if low_opponents:
            target = min(low_opponents, key=lambda s: state.counts[s])
            score += weights.threat
            reasons.append((weights.threat, reason_block(seat_label(target, seat), state.counts[target])))
            tags.append("scored:threat")

        # 抢队友的牌权：默认该让队友走，压他要付代价（只有明显更值时才压）
        last_seat = state.last.seat if state.last is not None else None
        if partner_penalty and partner is not None and last_seat == partner:
            score -= partner_penalty
            reasons.append((-partner_penalty, reason_grabbing_partner()))

        # 农民配合
        if partner is not None:
            if not state.leading and last_seat is not None and last_seat not in (seat, partner):
                score += weights.coop
                reasons.append((weights.coop, reason_team(seat_label(last_seat, seat))))
                tags.append("scored:coop")
            elif (
                state.leading
                and state.counts[partner] <= 2
                and move.pattern.type == SINGLE
                and move.pattern.main_rank <= 11
            ):
                # 喂牌：队友快走完了，出张小单让他能接上。
                # 队友是**下家**时这一手紧接着就轮到他，最有把握；是上家的话地主夹在中间，
                # 可能被截胡，所以只给一半的权重。
                next_seat = (seat + 1) % 3
                reliable = partner == next_seat
                weight = weights.coop if reliable else weights.coop / 2
                score += weight
                reasons.append((weight, reason_feed(state.counts[partner], reliable)))
                tags.append("scored:feed")

        reasons.sort(key=lambda item: -abs(item[0]))
        if not reasons:  # 一项都没触发也得给个说法，不然界面上一片空白
            reasons.append((0.0, reason_quiet()))
        return move, score, [text for _, text in reasons], tags

    # ----------------------------------------------------------------- 叫分

    def bid(self, state: GameState) -> Bid:
        from .bidding import bid_for

        seat = state.advise_seat
        hand = state.advise_hand
        if seat is None or not hand:
            return Bid(0, 0.0, [reason_no_hand()])
        return bid_for(
            hand,
            high_bid=state.high_bid,
            must_bid=state.must_bid,
            legal=state.legal_bids,
            thresholds=self.thresholds,
        )


def _is_bomb_like(move, _scored) -> bool:
    """炸弹纪律管的对象：炸弹/王炸本身，以及"把整个炸弹当普通牌打"的着法。"""
    if is_bomb(move.pattern):
        return True
    spent = C.rank_counts(move.cards)
    return any(spent[r] == 4 for r in range(C.N_RANKS))
