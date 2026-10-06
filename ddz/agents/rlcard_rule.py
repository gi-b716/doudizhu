"""RLCard 规则 agent —— **别人的启发式**，竞技场里的第三方参照。

是谁：DouZero 官方 README 里跟 ``random`` 并列的那个基线（``douzero/evaluation/rlcard_agent.py``，
183 行、自包含），RLCard 项目里的规则 agent。

**跑的是他们未经修改的原代码**，不是我照着论文重写的版本——重写很容易不小心把它写强或写弱，
那就失去了参照的意义。接法跟我们接 DouZero 是同一套：用 :func:`ddz.douzero.bridge.build_infoset`
把我们的 ``GameState`` 包成它认识的 InfoSet，再把返回的动作（牌码）落回手上的具体牌。

它的策略其实就是两句话：

- **领出**：自己拆一遍牌（``combine_cards``），甩掉含最小牌的那一组；
- **跟牌**：从 ``legal_actions`` 里挑「同型里最小」的一手；要是没有同型能压，且上家不是地主、
  自己也不是地主，就不出（不压队友）；**否则在全部合法着法里随机抓一个**——包括炸弹。
  这个随机分支是它明显偏弱的地方，我们照原样保留。

两处**有意**的偏离（别当成 bug）：

- **叫分走我们的启发式**：上游这个基线只管出牌，没有叫分逻辑（DouZero 自己也没有）。跟
  ``DouZeroAgent`` 一样委托给 ``HeuristicAgent.bid``——顺带让竞技场的地主选择在各桌之间完全一致，
  于是各桌是在**同一副牌**上比的。
- **合法着法由我们给**：它挑的就是 ``infoset.legal_actions``，所以我们喂 ``ddz.moves``（已对平台
  自己的引擎验证过）生成的着法。它交出来的东西不可能非法。

依赖：``uv pip install rlcard``（两个纯 python 包，不碰 torch）＋ ``third_party/DouZero``。
缺了就不可用——这是评测用的参照物，不进产品的引擎列表（``registry.available()`` 里没有它）。
"""

from __future__ import annotations

from ..douzero import bridge, vendored
from ..engine.base import Bid, Candidate
from ..engine.heuristic import HeuristicAgent
from ..explain import (
    describe_pattern, reason_rlcard_follow, reason_rlcard_lead, reason_rlcard_teammate,
)
from ..state import GameState

__all__ = ["RLCardRuleAgent", "is_available"]


def is_available() -> bool:
    """rlcard 装了、vendored 仓库也在，才谈得上这个参照。"""
    return vendored.load_rlcard_agent() is not None


class RLCardRuleAgent:
    """``rlcard-rule``。缺依赖时构造不抛，第一次决策才回落到启发式（和 DouZero 一个路子）。"""

    name = "rlcard-rule"

    def __init__(self, fallback: HeuristicAgent | None = None) -> None:
        self.fallback = fallback or HeuristicAgent()
        #: 上游的 agent 要把「我是哪个位置」记在实例里，所以按位置缓存三个实例
        self._players: dict[str, object] = {}
        self.last_error: str | None = None

    def _player(self, position: str):
        player = self._players.get(position)
        if player is None:
            module = vendored.load_rlcard_agent()
            if module is None:
                raise RuntimeError("没装 rlcard（uv pip install rlcard）或没有 third_party/DouZero")
            player = module.RLCardAgent(position)
            self._players[position] = player
        return player

    # ----------------------------------------------------------------- 决策

    def act(self, state: GameState) -> Candidate:
        seat = state.advise_seat
        hand = state.advise_hand
        if seat is None or not hand or state.landlord is None:
            return Candidate(source=self.name, reasons=[reason_rlcard_follow("")], tags=["rlcard"])

        info = bridge.build_infoset(state, seat)
        try:
            action = self._player(info.player_position).act(info)
        except Exception as exc:  # 参照物坏了不该让整局跑不下去
            self.last_error = str(exc)
            return self._fallback(state, reason=str(exc))

        if not action:
            return Candidate(None, 0.0, [reason_rlcard_teammate()], ["rlcard:pass"], self.name)

        move = bridge.action_to_move(action, hand, state.to_beat)
        if move is None:  # pragma: no cover - 只有「空动作」才为 None，上面已经拦掉
            return Candidate(None, 0.0, [reason_rlcard_teammate()], ["rlcard:pass"], self.name)

        label = describe_pattern(move.pattern)
        reason = reason_rlcard_lead(label) if state.to_beat is None else reason_rlcard_follow(label)
        return Candidate(move, 0.0, [reason], ["rlcard"], self.name)

    def bid(self, state: GameState) -> Bid:
        """叫分永远走启发式（上游基线没有叫分逻辑）。"""
        return self.fallback.bid(state)

    def _fallback(self, state: GameState, *, reason: str) -> Candidate:
        candidate = self.fallback.act(state)
        candidate.source = self.name
        candidate.reasons = [*candidate.reasons, f"RLCard 基线出错，已回落启发式（{reason}）"]
        candidate.tags = [*candidate.tags, "fallback:heuristic"]
        return candidate
