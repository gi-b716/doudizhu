"""叫分：牌力公式 + 阈值映射，夹到合法集里。

PLAN §3.3 的公式（见 :func:`ddz.engine.decomposer.hand_strength`）::

    hand_strength = 2.2·#王 + 1.4·#2 + 0.8·#A + 3.0·#炸 + 4.0·王炸
                    + (17 − 3.2·手数) + 0.5·顺子加成

阈值是**用竞技场校准**出来的（见 :data:`THRESHOLDS` 与 :data:`CONSERVATIVE_THRESHOLDS`
的注释），不是拍脑袋：先按公式在 10 万手随机牌上取分位数，再看自对弈里叫分是否合理。

两个档位只有两个词：**宽**（阈值低、叫得勤）与**严**（阈值高、几乎不叫）——
名字说的是阈值高低，不是"态度"。中文只从 :func:`tier_label` 出，别处别再起名字。

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

__all__ = [
    "THRESHOLDS", "CONSERVATIVE_THRESHOLDS", "BID_TIERS", "BID_TIER_ALIASES",
    "DEFAULT_BID_TIER", "BidThresholds", "bid_for", "strength_of",
    "thresholds_of_tier", "tier_of", "tier_label",
]

#: **宽档**（阈值低、叫得勤）：20000 手随机 17 张实测 57.4% 不叫、24.5% 叫 1、12.9% 叫 2、5.2% 叫 3。
#:
#: 定档方式：先按公式在 10 万手随机牌上取分位数——p55 ≈ −2.1、p82 ≈ +2.0、p94 ≈ +5.8，
#: 取整后让约 45% / 18% / 6% 的手牌够叫 1 / 2 / 3 分（三人全不叫的概率约 17%，
#: 与实际对局里偶尔流局同量级）。
#:
#: 它是**库的默认**（`BidThresholds()`、竞技场自对弈都跑这一档），**不是产品的默认**——
#: 产品（浮窗/服务）默认跑 :data:`CONSERVATIVE_THRESHOLDS`。两个词各指一档，别混用：
#: 宽 = 阈值低 = 叫得勤，严 = 阈值高 = 几乎不叫。
THRESHOLDS: tuple[float, float, float] = (-2.0, 2.0, 6.0)


#: **严档**（阈值整体上移，几乎不主动叫地主）：89.5% 不叫、7.9% 叫 1、2.1% 叫 2、0.4% 叫 3。
#: **浮窗与服务默认这一档**（面板上写「叫分 严」）。
#:
#: **是量出来的，不是拍的**：一桌一个候选叫分者 vs 两个默认叫分者，轮换座位 800 局比 ADP——
#:
#:     (-2, 2, 6) 宽档   +0.033 分/局   当地主时胜率 42.8%
#:     ( 0, 4, 8)        +0.236         47.2%
#:     ( 2, 6,10)        +0.275         50.4%
#:     ( 4, 8,12) 严档   +0.309         52.6%   ← 平台期，取它
#:     ( 8,12,16)        +0.318         40.7%
#:
#: **反方向（阈值再降、叫得更勤）是单调变差的**：(-4,0,4) → −0.219、(-6,-1,3) → −0.507。
#: 原因很直白：**在这个打法下当地主本来就低于五成胜率**（42.8%），多抢地主就是多送分。
#: 顺带一提，用户真实对局里当地主也是 42%（5/12），和这个数字吻合。
CONSERVATIVE_THRESHOLDS: tuple[float, float, float] = (4.0, 8.0, 12.0)

#: 档位的**唯一命名处**：库里的 id（``loose``/``strict``）↔ 面板上的字（「宽」/「严」）↔ 旧脚本发过的别名。
#:
#: 别在别处再写一份中文：叫分档位曾经在两个文件里被叫成「稳健」和「保守」——
#: 两个词在中文里是近义词，浮窗上两个按钮看着一模一样。**宽/严对应的是阈值高低**，
#: 一眼能分，也就不会再有人问"这俩有区别吗"。
BID_TIERS: dict[str, str] = {"loose": "宽", "strict": "严"}

#: 旧脚本（≤0.7.3）发的是 ``safe`` / ``conservative``，**必须一直认**：
#: 浏览器里装着的旧浮窗不会跟着仓库升级，服务端装作看不见就等于档位失灵
#: （历史上就这么失灵过一次，见 tests/test_service_advise.py 的 HTTP 那条）。
BID_TIER_ALIASES: dict[str, str] = {
    "loose": "loose", "safe": "loose", "wide": "loose",
    "strict": "strict", "conservative": "strict",
}

#: 认不出或没给时的档位——产品的默认档，也是这里唯一允许写死的默认值。
DEFAULT_BID_TIER = "strict"


def tier_of(value: object) -> str:
    """任意写法（``loose``/``strict``/``safe``/``conservative``/带空格的大小写）→ 档位 id。

    认不出的一律落到 :data:`DEFAULT_BID_TIER`——不报错：这条路上报错等于浮窗叫不了分。
    """
    return BID_TIER_ALIASES.get(str(value or "").strip().lower(), DEFAULT_BID_TIER)


def tier_label(value: object) -> str:
    """档位 → 面板上的字（「宽」/「严」）。中文只在这里生成一份，别处不许再写。"""
    return BID_TIERS[tier_of(value)]


def thresholds_of_tier(value: object) -> "BidThresholds":
    """档位 → 阈值组。**每个档位都要显式给阈值**，别靠 ``BidThresholds()`` 的默认——
    库默认是宽档，产品默认是严档，靠默认会静默错一档。"""
    return BidThresholds(*(CONSERVATIVE_THRESHOLDS if tier_of(value) == "strict" else THRESHOLDS))


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
