"""桥接：我们的 :class:`~ddz.state.GameState` ↔ 上游 DouZero 的 InfoSet / 观测 / 动作。

三件事，都只做翻译、不重写规则：

1. **位置**：``landlord`` / ``landlord_down``（地主下家）/ ``landlord_up``；
2. **InfoSet**：按上游 ``GameEnv.get_infoset`` 的字段语义填一个鸭子类型对象，然后
   **直接调用上游的 ``get_obs()``** 生成 (N,373)/(N,484) 观测——编码器一行都不重写；
3. **动作**：合法着法由 :mod:`ddz.moves` 生成（唯一契约），逐个映射成 DouZero 牌码。

牌码映射（``game.RealCard2EnvCard``）：3..14 = 3..A、17 = '2'、20 = 小王、30 = 大王；
同一个点数出现多次就重复多次（对子 = ``[3, 3]``）。我们的 :func:`ddz.cards.to_env_card`
就是这个映射，:func:`assert_code_bijection` 在装载时复核一遍。

**信息边界**：真实对局里我们只知道自己的手牌和已出的牌，所以
``other_hand_cards = 未见牌``、``all_handcards`` 不填（上游 ``get_obs`` 不读它，已核对）。
"""

from __future__ import annotations

import numpy as np
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from .. import cards as C
from ..moves import Move, legal_moves
from ..patterns import Pattern, classify, is_bomb
from ..state import GameState, PHASE_PLAYING, SEATS

__all__ = [
    "POSITIONS", "position_of", "seat_of", "assert_code_bijection",
    "legal_actions", "build_infoset", "choose_action", "action_to_move",
    "encode_cards", "decode_action", "cards_for_action",
]

#: 上游的三个位置名。
POSITIONS = ("landlord", "landlord_up", "landlord_down")


def position_of(landlord: int, seat: int) -> str:
    """座位 → 上游位置名。地主下家是 ``(seat - landlord) % 3 == 1``。"""
    if landlord is None:
        raise ValueError("还不知道地主是谁，给不出位置")
    if seat == landlord:
        return "landlord"
    return "landlord_down" if (seat - landlord) % 3 == 1 else "landlord_up"


def seat_of(landlord: int, position: str) -> int:
    """上游位置名 → 座位。"""
    if position == "landlord":
        return landlord
    if position == "landlord_down":
        return (landlord + 1) % 3
    if position == "landlord_up":
        return (landlord + 2) % 3
    raise ValueError(f"不认识的位置：{position!r}")


def encode_cards(cards: Iterable[int]) -> list[int]:
    """牌 id → DouZero 牌码（升序，含重复）。"""
    return sorted(C.to_env_card(card) for card in cards)


def cards_for_action(action: Sequence[int], hand: Iterable[int]) -> tuple[int, ...]:
    """把动作（牌码）落到**手牌里的具体牌**上——每个点数按需取几张。

    DouZero 只认点数，所以拿回动作后必须按手牌分配花色；直接取"该点数最小的牌"可能
    取到手上没有的花色。
    """
    buckets: dict[int, list[int]] = {}
    for card in C.sort_cards(hand):
        buckets.setdefault(C.rank(card), []).append(card)
    used: dict[int, int] = {}
    out: list[int] = []
    for code in sorted(action):
        rank = C.env_card_to_rank(code)
        index = used.get(rank, 0)
        used[rank] = index + 1
        available = buckets.get(rank, [])
        if index >= len(available):
            raise ValueError(f"手上没有第 {index + 1} 张 {C.RANK_NAMES[rank]}")
        out.append(available[index])
    return tuple(sorted(out))


def decode_action(codes: Sequence[int]) -> tuple[int, ...]:
    """DouZero 动作（牌码列表）→ 具体牌 id（升序）。

    同一个牌码出现几次就取该点数的第几张牌（DouZero 只看点数，具体花色无意义，
    但牌 id 必须互不相同，否则下游会以为在打同一张牌）。
    """
    used: dict[int, int] = {}
    cards: list[int] = []
    for code in sorted(codes):
        rank = C.env_card_to_rank(code)
        index = used.get(rank, 0)
        used[rank] = index + 1
        if index >= 4 and rank < C.JOKER_RANK:  # pragma: no cover - 同一张牌打了 5 次
            raise ValueError(f"牌码 {code} 出现次数超过 4")
        cards.append(C.first_card_of_rank(rank) + index)
    return tuple(sorted(cards))


def assert_code_bijection() -> None:
    """启动时复核牌码双射：15 个点数 ↔ 15 个牌码，且与上游常量表一致。

    上游没装载时只检查我们这半边（自洽）。装载了就逐项对比
    ``game.RealCard2EnvCard``——两边任何一个改了都会当场炸出来。
    """
    ranks = list(range(C.N_RANKS))
    codes = [C.to_env_card(C.first_card_of_rank(rank)) for rank in ranks]
    if len(set(codes)) != C.N_RANKS:
        raise AssertionError(f"牌码不是双射：{codes}")
    for rank, code in zip(ranks, codes):
        if C.env_card_to_rank(code) != rank:
            raise AssertionError(f"牌码 {code} 反解不回点数 {rank}")

    from .vendored import load_env

    modules = load_env()
    if modules is None:
        return
    # 上游的牌码表是「牌码 → 点数名」（'3'..'A'、'2'、'X'、'D'）。两边点数名不同
    # （我们叫「小王/大王」），所以按**牌码**对齐：我们的 15 个码上游必须都认得，
    # 上游也只能有这 15 个，并且 EnvCard2RealCard ∘ RealCard2EnvCard 要能往返。
    env2real = modules.game.EnvCard2RealCard
    real2env = modules.game.RealCard2EnvCard
    if len(env2real) != C.N_RANKS:
        raise AssertionError(f"上游牌码表有 {len(env2real)} 项，期望 {C.N_RANKS}")
    for code in codes:
        if code not in env2real:
            raise AssertionError(f"牌码 {code} 上游不认识（我们的点数 {C.RANK_NAMES[codes.index(code)]}）")
        if real2env[env2real[code]] != code:
            raise AssertionError(f"牌码 {code} 往返不一致：{real2env[env2real[code]]}")


# --------------------------------------------------------------------- 合法动作


def legal_actions(
    hand: Iterable[int],
    to_beat: Pattern | None,
    *,
    limit: int | None = 2000,
) -> list[list[int]]:
    """我们生成的合法着法 → DouZero 动作列表；跟牌时带上「不出」（``[]``）。

    动作集合**永远由 :mod:`ddz.moves` 说了算**：DouZero 只对这里给出的动作打分。
    """
    moves = legal_moves(hand, to_beat, canonical=False, limit=limit)
    actions = [encode_cards(move.cards) for move in moves]
    if to_beat is not None:
        actions.append([])  # 上游把「不出」也算一个合法动作
    return actions


def action_to_move(action: Sequence[int], hand: Iterable[int], to_beat: Pattern | None) -> Move | None:
    """DouZero 动作 → 我们的 :class:`~ddz.moves.Move`（``[]`` 即不出）。"""
    if not action:
        return None
    cards = cards_for_action(action, hand)   # 落到手上的具体牌
    pattern = classify_beating(cards, to_beat)
    if pattern is None:
        raise ValueError(f"动作 {list(action)} 压不过 {to_beat}")
    return Move(cards, pattern)


def classify_beating(cards: Sequence[int], to_beat: Pattern | None) -> Pattern | None:
    from ..patterns import classify_beating as _beating

    return _beating(cards, to_beat)


# --------------------------------------------------------------------- InfoSet


def build_infoset(state: GameState, seat: int) -> SimpleNamespace:
    """按上游字段语义填一个 InfoSet（鸭子类型，``get_obs`` 只读属性）。

    与 ``GameEnv.get_infoset`` 逐项对齐：``last_move`` 取「最后一条，若为空则取倒数第二条」，
    ``played_cards`` 按位置累计，``other_hand_cards`` 是未见牌（我之外的两家之和）。
    """
    landlord = state.landlord
    if landlord is None:
        raise ValueError("地主还没定，DouZero 用不了")
    position = position_of(landlord, seat)
    hand = state.hand_for(seat)
    if hand is None:
        raise ValueError(f"看不到 {seat} 号座位的手牌")

    events = [event for event in state.events if event.get("kind") in ("play", "pass")]
    sequence: list[list[int]] = [
        encode_cards(event.get("cards") or ()) if event.get("kind") == "play" else []
        for event in events
    ]

    played_by_position: dict[str, list[int]] = {name: [] for name in POSITIONS}
    last_move_dict: dict[str, list[int]] = {name: [] for name in POSITIONS}
    for event in events:
        who = event.get("seat")
        if who not in SEATS:
            continue
        name = position_of(landlord, who)
        action = encode_cards(event.get("cards") or ()) if event.get("kind") == "play" else []
        last_move_dict[name] = list(action)
        if action:
            played_by_position[name].extend(action)

    counts = {name: state.counts[seat_of(landlord, name)] for name in POSITIONS}
    bottom = _landlord_bottom(state, landlord)

    info = SimpleNamespace(
        player_position=position,
        player_hand_cards=encode_cards(hand),
        num_cards_left_dict=counts,
        three_landlord_cards=list(bottom),
        card_play_action_seq=sequence,
        other_hand_cards=encode_cards(state.unseen_for(seat)),
        legal_actions=legal_actions(hand, state.to_beat),
        last_move=_last_move(sequence),
        last_two_moves=_last_two(sequence),
        last_move_dict=last_move_dict,
        played_cards=played_by_position,
        bomb_num=int(state.bombs),
        last_pid=_last_pid(landlord, events),
    )
    return info


def _landlord_bottom(state: GameState, landlord: int) -> tuple[int, ...]:
    """底牌里还没被打出去的那些，**返回牌码**。

    逐字对应上游 ``step`` 的做法：地主每打出一张牌，就从 ``three_landlord_cards`` 里移除
    **一张**同点数的牌（上游只有点数没有花色，所以这里是按点数计数而不是按牌 id）。
    """
    bottom = [C.to_env_card(card) for card in (state.bottom or ())]
    if not bottom:
        return ()
    remaining = sorted(bottom)
    for card in state.played_by_seat[landlord]:
        code = C.to_env_card(card)
        if code in remaining:
            remaining.remove(code)
    return tuple(remaining)


def _last_move(sequence: Sequence[Sequence[int]]) -> list[int]:
    if not sequence:
        return []
    if not sequence[-1]:
        return list(sequence[-2]) if len(sequence) >= 2 else []
    return list(sequence[-1])


def _last_two(sequence: Sequence[Sequence[int]]) -> list[list[int]]:
    tail = list(sequence[-2:])
    while len(tail) < 2:
        tail.insert(0, [])
    return [list(item) for item in tail]


def _last_pid(landlord: int, events: Sequence[dict]) -> str:
    """最后一个**真出了牌**的位置名（不出不算）。

    逐字对齐上游：``step()`` 里只有 ``len(action) > 0`` 才更新 ``last_pid``，而它初始化成
    ``'landlord'``——所以还没人出牌时是 ``'landlord'``，不是"没有"。上游的 RLCard 基线会读这个
    字段来判断"上家是不是队友"，差一个 ``None`` 就会让它在该不出的时候乱出。
    """
    for event in reversed(events):
        if event.get("kind") == "play" and event.get("seat") in SEATS:
            return position_of(landlord, event["seat"])
    return "landlord"


# --------------------------------------------------------------------- 推理


def choose_action(model, infoset, *, device: str | None = None, modules=None) -> tuple[list[int], float]:
    """让模型挑一个动作，返回 ``(动作, 估值)``。

    ``估值 = (v + 1) / 2``，界面上标注为「DouZero 估值」（不是严格概率）。
    """
    import torch

    legal = infoset.legal_actions
    if len(legal) == 1:  # 只有一个选择就别浪费一次前向
        return list(legal[0]), 0.5

    if modules is None:
        from .vendored import load_env

        modules = load_env()
    obs = modules.get_obs(infoset)

    z = torch.from_numpy(np.asarray(obs["z_batch"])).float()
    x = torch.from_numpy(np.asarray(obs["x_batch"])).float()
    if device:
        z, x = z.to(device), x.to(device)
    with torch.no_grad():
        values = model.forward(z, x, return_value=True)["values"]
    values = values.detach().cpu().numpy().reshape(-1)
    index = int(np.argmax(values))
    return list(legal[index]), float((values[index] + 1.0) / 2.0)
