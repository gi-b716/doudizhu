"""叫分：牌力公式 + 阈值映射，夹到合法集里。

PLAN §3.3 的公式（见 :func:`ddz.engine.decomposer.hand_strength`）::

    hand_strength = 2.2·#王 + 1.4·#2 + 0.8·#A + 3.0·#炸 + 4.0·王炸
                    + (17 − 3.2·手数) + 0.5·顺子加成

阈值是**用竞技场校准**出来的（见 :data:`THRESHOLDS` 的注释），不是拍脑袋：先按公式在
10 万手随机牌上取分位数，再看自对弈里叫分是否合理。

``must_bid``（连续流局 3 次后本轮最后一位）强制 ≥1，理由里点明原因——rules.md 的规则，
不是我们的偏好。
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import cards as C
from ..explain import (
    reason_bid_forced, reason_bid_strength, reason_no_hand, reason_pass_weak,
)
from .base import Bid
from .decomposer import best_decomposition, hand_strength

__all__ = ["THRESHOLDS", "BidThresholds", "bid_for", "strength_of"]

#: (叫 1 / 2 / 3 分) 的牌力阈值。校准方式：3000 手随机 17 张牌的牌力分位数——
#: p55 ≈ −2.1、p82 ≈ +2.0、p94 ≈ +5.8，取整后让约 45% / 18% / 6% 的手牌够叫
#: 1 / 2 / 3 分（三人全不叫的概率约 17%，与实际对局里偶尔流局同量级）。
THRESHOLDS: tuple[float, float, float] = (-2.0, 2.0, 6.0)


@dataclass(frozen=True, slots=True)
class BidThresholds:
    one: float = THRESHOLDS[0]
    two: float = THRESHOLDS[1]
    three: float = THRESHOLDS[2]


def strength_of(hand) -> float:
    """牌力分（对外暴露，便于调参与测试）。"""
    cards = C.sort_cards(hand)
    return hand_strength(cards, best_decomposition(cards))


def bid_for(
    hand,
    *,
    high_bid: int = 0,
    must_bid: bool = False,
    legal: list[int] | None = None,
    thresholds: BidThresholds | None = None,
) -> Bid:
    """给一手牌定叫分。``legal`` 不给就按 ``{0} ∪ {v > high_bid}``（must_bid 禁 0）算。"""
    cards = C.sort_cards(hand)
    if not cards:
        return Bid(0, 0.0, [reason_no_hand()])

    if legal is None:
        options = [v for v in (1, 2, 3) if v > high_bid]
        legal = options if must_bid else [0, *options]
    legal = sorted(set(legal))
    if not legal:
        return Bid(0, 0.0, [reason_no_hand()])

    levels = thresholds or BidThresholds()
    strength = hand_strength(cards, best_decomposition(cards))
    wanted = 0
    if strength >= levels.three:
        wanted = 3
    elif strength >= levels.two:
        wanted = 2
    elif strength >= levels.one:
        wanted = 1

    # 只在"合法、且牌力确实够"的档位里挑最高的：叫不了自己的档就不硬上更高的一档
    affordable = [0] + [v for v in (1, 2, 3) if strength >= _threshold_of(v, levels)]
    allowed = [v for v in legal if v in affordable]
    value = max(allowed) if allowed else 0

    forced_by_rule = False
    if must_bid and value == 0:
        # rules.md：连续流局 3 次后，本轮最后一位必须叫分
        value = min(v for v in legal if v)
        forced_by_rule = True

    detail = _strength_detail(cards, strength)
    if value == 0:
        reasons = [reason_pass_weak(detail)]
    elif forced_by_rule and strength < _threshold_of(value, levels):
        reasons = [reason_bid_forced(detail)]
    else:
        reasons = [reason_bid_strength(value, detail)]
    return Bid(value, strength, reasons)


def _threshold_of(value: int, levels: BidThresholds) -> float:
    return {1: levels.one, 2: levels.two, 3: levels.three}[value]


def _strength_detail(hand, strength: float) -> str:
    """理由里的细节：王、2、炸弹、预计手数。"""
    counts = C.rank_counts(hand)
    decomp = best_decomposition(hand)
    parts: list[str] = []
    jokers = counts[C.JOKER_RANK] + counts[C.JOKER_RANK + 1]
    if counts[C.JOKER_RANK] and counts[C.JOKER_RANK + 1]:
        parts.append("王炸")
    elif jokers:
        parts.append(f"{jokers} 张王")
    if counts[12]:
        parts.append(f"{counts[12]} 张 2")
    if counts[11]:
        parts.append(f"{counts[11]} 张 A")
    for rank in decomp.bombs:
        parts.append(f"{C.RANK_NAMES[rank]} 炸弹")
    parts.append(f"{decomp.n_plays} 手")
    return f"牌力 {strength:.1f}（{'、'.join(parts)}）"
