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
    "PATTERN_NAMES", "SEAT_LABELS", "PASS_TEXT", "describe_pattern", "describe_move",
    "rank_text", "seat_label",
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

#: 「不出」的说法——服务响应里 pass 的 description 就是它。
PASS_TEXT = "不出"

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


def describe_move(move: Any) -> str:
    """一手着法的中文描述；``move is None`` 即"不出"。"""
    return PASS_TEXT if move is None else describe_pattern(move.pattern)


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


# --------------------------------------------------------------------- 手牌与牌面

#: 控制牌的中文名（点数 → 名字），用于"保留大王作为控制牌"这类理由。
CONTROL_NAMES = {11: "A", 12: "2", 13: "小王", 14: "大王"}


def rank_list_text(ranks) -> str:
    return "、".join(RANK_NAMES[r] for r in ranks)


def bomb_ranks_text(ranks) -> str:
    return "、".join(f"{RANK_NAMES[r]}{RANK_NAMES[r]}{RANK_NAMES[r]}{RANK_NAMES[r]}" for r in ranks)


def control_rank_text(hand, control_ranks) -> list[str]:
    """控制牌的显示名：炸弹显示成 ``9999``，其余显示点数。"""
    counts = {}
    for card in hand:
        rank = card >> 2 if card < 52 else 13 + (card - 52)
        counts[rank] = counts.get(rank, 0) + 1
    out: list[str] = []
    for rank in control_ranks:
        if counts.get(rank, 0) >= 4 and rank not in CONTROL_NAMES:
            out.append(RANK_NAMES[rank] * 4)
        else:
            out.append(RANK_NAMES[rank])
    return out


# --------------------------------------------------------------------- 出牌理由


def reason_plays_saved(before: int, after: int) -> str:
    return f"手数从 {before} 手降到 {after} 手"


def reason_plays_wasted(before: int, after: int) -> str:
    return f"会把手牌拆散：手数从 {before} 手涨到 {after} 手"


def reason_keeps_control(name: str) -> str:
    return f"保留{name}作为控制牌"


def reason_last_control(name: str) -> str:
    return f"{name}是你最后一张控制牌，打出去就压不住场了"


def reason_splits(names: str) -> str:
    return f"要拆开{names}"


def reason_bomb() -> str:
    return "这一手要动用炸弹"


def reason_tempo(label: str) -> str:
    return f"从{label}手里拿回出牌权"


def reason_block(label: str, count: int) -> str:
    return f"压住只剩 {count} 张的{label}"


def reason_feed(count: int, reliable: bool = True) -> str:
    if reliable:
        return f"队友只剩 {count} 张，出张小牌喂他"
    return f"队友只剩 {count} 张，出张小牌喂他（地主夹在中间，未必接得上）"


def reason_quiet() -> str:
    return "这一手不拆结构、不动大牌，按拆解走最省"


def reason_team(label: str) -> str:
    return f"替队友压住{label}"


def reason_win() -> str:
    return "这一手就能把牌走完"


def reason_no_beat() -> str:
    return "没有牌能管上，只能不出"


def reason_partner_lead(label: str) -> str:
    return f"上家是队友{label}，让他继续走"


def reason_partner_low(count: int) -> str:
    return f"队友只剩 {count} 张，绝不能压他"


def reason_partner_dump(card: str, label: str) -> str:
    return f"{label}领的是小牌，顺手把 {card} 放掉，牌权还在自家"


def reason_grabbing_partner() -> str:
    return "这一手会抢掉队友的出牌权"


def reason_take_over(count: int) -> str:
    return f"队友还有 {count} 张，你更接近走完，接管这一手"


def reason_must_block(label: str, count: int) -> str:
    return f"{label}只剩 {count} 张，必须拦下这一手"


def reason_cannot_block(label: str, count: int) -> str:
    return f"拦不住：{label}只剩 {count} 张，你手里没有能压的牌"


def reason_bomb_as_plain() -> str:
    return "这会把整个炸弹当普通牌打掉"


def reason_bomb_hold() -> str:
    return "炸弹留着，这一手不值得动它"


def reason_no_hand() -> str:
    return "看不到手牌（旁观且没有管理员权限）"


def reason_not_worth() -> str:
    return "这一手不划算，先不出，把大牌留着"


def reason_random() -> str:
    return "随机挑一手合法牌"


# --------------------------------------------------------------------- 叫分理由


def reason_bid_strength(value: int, detail: str) -> str:
    return f"{detail} → 叫 {value} 分"


def reason_pass_weak(detail: str) -> str:
    return f"{detail}，牌力不够，不叫"


def reason_bid_forced(detail: str) -> str:
    return f"已连续流局，本轮必须叫分（{detail}）"
