"""残局求解 agent：**能算清就按算出来的打，算不清就走启发式**。

它和启发式的唯一区别就在残局那几手——所以拿它跟启发式对打，测出来的差值就是
"完美信息残局求解"这件事本身值多少（PLAN §4 Phase 5 的验收：残局阶段胜率有可测提升）。

前提是**看得到三家手牌**：真实平台下发的 state 里带 ``hands``（我们归档的第一局真实对局
就有），所以线上用得上；竞技场默认不给（
:class:`~ddz.arena.simulator.Simulator` 的 ``oracle=True`` 才发），要测这个才打开。

叫分仍然走启发式：残局求解只解决出牌，不管叫分。
"""

from __future__ import annotations

from ..engine.base import Bid, Candidate
from ..engine.endgame import (
    DEFAULT_LIMITS, Limits, candidate_from, solve as endgame_solve,
    solve_uncertain, solvable as endgame_solvable, uncertain_solvable,
)
from ..engine.heuristic import HeuristicAgent
from ..explain import reason_solver_lost
from ..state import GameState

__all__ = ["EndgameAgent"]


class EndgameAgent:
    """``endgame``。"""

    name = "endgame"

    def __init__(self, fallback: HeuristicAgent | None = None, *, limits: Limits = DEFAULT_LIMITS):
        self.fallback = fallback or HeuristicAgent()
        self.limits = limits
        #: 统计：算过多少残局、其中多少是算出来必胜的（竞技场报告与自查用）
        self.solved = 0
        self.proved_wins = 0
        self.proved_losses = 0
        #: 多少结论是"看不见手牌也算出来"的（实盘那种）
        self.blind_wins = 0

    def act(self, state: GameState) -> Candidate:
        if state.all_hands is None:
            # **真实对局就是这样**：只看得到自己的牌。走"对一切可能的分配都赢"那条路。
            if not uncertain_solvable(state, self.limits):
                return self.fallback.act(state)
            result = solve_uncertain(state, limits=self.limits)
        else:
            if not endgame_solvable(state, self.limits):
                return self.fallback.act(state)
            result = endgame_solve(state, limits=self.limits)
        if not result.proved:
            # 没搜完就当没这个功能（绝不拿半截结论下结论）
            return self.fallback.act(state)

        self.solved += 1
        if result.winning:
            self.proved_wins += 1
            if state.all_hands is None:
                self.blind_wins += 1
            return candidate_from(result)

        # 必败：着法照旧尽量打，但结论要说实话——玩家可以改为等对方失误
        self.proved_losses += 1
        candidate = self.fallback.act(state)
        candidate.win_prob = 0.0
        candidate.reasons = [reason_solver_lost(), *candidate.reasons]
        candidate.tags = [*candidate.tags, "solver:lost"]
        return candidate

    def bid(self, state: GameState) -> Bid:
        """叫分走启发式（残局求解不管叫分）。"""
        return self.fallback.bid(state)
