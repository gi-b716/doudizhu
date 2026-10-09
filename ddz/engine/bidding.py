"""叫分：牌力公式 + 阈值映射，夹到合法集里。

PLAN §3.3 的公式（见 :func:`ddz.engine.decomposer.hand_strength`）::

    hand_strength = 2.2·#王 + 1.4·#2 + 0.8·#A + 3.0·#炸 + 4.0·王炸
                    + (17 − 3.2·手数) + 0.5·顺子加成

阈值按**期望收益**定，再用竞技场验证（见 :data:`THRESHOLDS` 的注释）。旧版是按「叫分频率」
校准的（让约 45% 的手牌够叫 1 分），目标本身就错：叫 1、2 分的地主实际只赢 34% / 39%。

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

__all__ = ["THRESHOLDS", "BidThresholds", "as_thresholds", "parse_thresholds", "bid_for", "strength_of"]

#: (叫 1 / 2 / 3 分) 的牌力阈值。**三档必须相同**，理由与实测：
#:
#: - 叫分的期望收益是 ``2 · 叫分 · 倍数 · (2P − 1)``，P 为地主胜率。叫分只是乘数，
#:   符号完全由 P 决定；P > 0.5 时叫 3 分的收益永远是叫 1 分的 3 倍，所以 1、2 分没有保留的理由。
#: - 竞技场实测（旧阈值三家自对弈 6000 局）：叫 1 分的地主胜率 34%、叫 2 分 39%，地主 ADP 分别
#:   −0.72 / −1.00，都是明显负期望；叫 3 分的地主胜率约 49%。叫 3 分的门槛用 6 时，牌力 [10,12)
#:   段的地主 ADP 才明显转正（+1.2），所以门槛取 10。
#: - 相对旧阈值 (−2, 2, 6) 的 ADP 增益（配对种子）：1 个候选 vs 2 个旧版 +0.33 ±0.07（换种子 9000
#:   为 +0.43 ±0.07）；2 个候选 vs 1 个旧版 +0.35 ±0.05。门槛 8～12 是平台期，取偏保守的 10。
THRESHOLDS: tuple[float, float, float] = (10.0, 10.0, 10.0)


@dataclass(frozen=True, slots=True)
class BidThresholds:
    one: float = THRESHOLDS[0]
    two: float = THRESHOLDS[1]
    three: float = THRESHOLDS[2]


def as_thresholds(value) -> BidThresholds | None:
    """统一成 :class:`BidThresholds`：接受 ``None``、现成的对象、``(一, 二, 三)`` 三元组或字典。"""
    if value is None or isinstance(value, BidThresholds):
        return value
    if isinstance(value, dict):
        return BidThresholds(**{key: float(item) for key, item in value.items()})
    one, two, three = (float(item) for item in value)
    return BidThresholds(one, two, three)


def parse_thresholds(text: str) -> BidThresholds:
    """命令行写法：``10`` 表示三档都是 10；``6,8,10`` 表示依次是叫 1 / 2 / 3 分的门槛。"""
    parts = [part.strip() for part in str(text).split(",") if part.strip()]
    if len(parts) == 1:
        parts = parts * 3
    if len(parts) != 3:
        raise ValueError(f"叫分门槛要么一个数，要么三个数（叫 1/2/3 分）：{text!r}")
    return as_thresholds(parts)


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
