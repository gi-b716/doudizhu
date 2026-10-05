"""中文文案集中处。

平台客户端的 ``window.DoudizhuRules`` 没有副本（见 PLAN §0.1），平台的
``choice``/``patternKey`` 具体格式不可知，所以牌型描述与理由文案一律自产，全部
收在本模块，便于统一口径与日后润色。

本模块只依赖 :mod:`ddz.cards`，不 import :mod:`ddz.patterns`（避免循环导入）：
``describe_pattern`` 对传入对象只读 ``type`` / ``main_rank`` / ``length`` 三个属性。
"""

from __future__ import annotations

from typing import Any

from .cards import RANK_NAMES

__all__ = [
    "PATTERN_NAMES", "SEAT_LABELS", "describe_pattern", "rank_text", "seat_label",
    "threat_opponent_low", "threat_partner_close", "threat_possible_bomb",
    "threat_rocket_possible", "threat_top_card_pressed",
]

#: 牌型名（与 rules.md 的用词一致）。
PATTERN_NAMES: dict[str, str] = {
    "single": "单张",
    "pair": "对子",
    "triple": "三张",
    "triple_single": "三带一",
    "triple_pair": "三带一对",
    "straight": "顺子",
    "pair_straight": "连对",
    "plane": "飞机",
    "plane_single": "飞机带单",
    "plane_pair": "飞机带对",
    "four_two": "四带二",
    "four_two_pair": "四带两对",
    "bomb": "炸弹",
    "rocket": "王炸",
}

#: 只有单个主体的牌型（描述里不带链条区间）。
_SOLO_TYPES = frozenset({
    "single", "pair", "triple", "triple_single", "triple_pair",
    "four_two", "four_two_pair", "bomb",
})


def rank_text(r: int) -> str:
    return RANK_NAMES[r]


def describe_pattern(pattern: Any) -> str:
    """牌型的中文描述，如 ``对子 3`` / ``顺子 3-7`` / ``飞机带单 5-7``。"""
    name = PATTERN_NAMES[pattern.type]
    if pattern.type == "rocket":
        return name
    head = RANK_NAMES[pattern.main_rank]
    if pattern.type in _SOLO_TYPES:
        return f"{name} {head}"
    tail = RANK_NAMES[pattern.main_rank + pattern.length - 1]
    return f"{name} {head}-{tail}"


# --------------------------------------------------------------------- 座位称呼

#: 相对称呼：出牌顺序是 seat+1 → 我之后的那家叫「下家」，seat+2 叫「上家」。
SEAT_LABELS = ("你", "下家", "上家")


def seat_label(seat: int, me: int | None) -> str:
    """把绝对座位翻译成相对称呼（你 / 下家 / 上家）。"""
    if me is None:
        return f"座位 {seat}"
    if seat == me:
        return SEAT_LABELS[0]
    return SEAT_LABELS[1] if seat == (me + 1) % 3 else SEAT_LABELS[2]


# --------------------------------------------------------------------- 威胁提示


def threat_opponent_low(label: str, count: int, urgent: bool) -> str:
    if urgent:
        return f"{label}只剩 {count} 张，必须拦下这一手"
    return f"{label}只剩 {count} 张，别让他顺出去"


def threat_partner_close(count: int) -> str:
    return f"队友只剩 {count} 张，尽量出小牌让他走"


def threat_possible_bomb(names: str) -> str:
    return f"{names} 的 4 张都还没出现，小心炸弹"


def threat_rocket_possible() -> str:
    return "大小王都还没露面，小心王炸"


def threat_top_card_pressed(top_name: str, count: int) -> str:
    return f"你最大的单牌是 {top_name}，还有 {count} 张未见牌比它大"
