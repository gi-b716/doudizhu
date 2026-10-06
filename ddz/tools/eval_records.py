"""对局库回放评测（PLAN §3.7）：拿**真实对局**回答"到底亏没亏"。

目前做的是其中最有信息量、也唯一能给出**真值**的那一半：残局。

出牌阶段到了残局（三家手牌都看得见、牌又够少），求解器能把局面**算清**——"这一手必胜"
或者"已经必败"不再是估计。于是可以拿它当标尺，量三件事：

1. 人类在能算清的残局里，**保住了几次必胜**（丢掉的，就是本可以赢的局）；
2. 我们的 agent（默认启发式）在同一批点位上保住了几次；
3. 两者差多少——这就是"残局阶段胜率有可测提升"这句话的实测口径。

注意口径是**"保住必胜"**而不是"下得漂亮"：一个点位只要能赢的走法有一堆，走出哪一手都算
保住。求解器 100% 保住是定义使然（它就是从能赢的走法里挑），所以它在这里是**标尺**，
不是被测者。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .. import cards as C
from ..agents.registry import make
from ..data.archive import Archive
from ..engine.endgame import solve as endgame_solve, solvable as endgame_solvable
from ..record import GameRecord, iter_decisions

__all__ = ["Report", "evaluate"]


def action_key(cards) -> tuple[int, ...] | None:
    """一个动作在**点数**上的身份：``None`` 表示不出。

    花色不参与规则，所以 (3♠,3♥) 和 (3♠,3♣) 是同一个动作。三带一也一样——
    带 3 还是带 9 是两个不同的动作（后续手牌不同），这里必须分得开，不能只看牌型 key。
    """
    if not cards:
        return None
    return tuple(sorted(C.rank(card) for card in cards))


@dataclass(slots=True)
class Report:
    games: int = 0
    decisions: int = 0
    solvable_points: int = 0
    proved_win_points: int = 0
    proved_loss_points: int = 0
    unproved_points: int = 0
    human_kept: int = 0
    human_threw: int = 0
    agent_kept: int = 0
    agent_threw: int = 0
    thrown_examples: list[dict] = field(default_factory=list)
    #: 有"本可以赢却输掉"的局
    games_with_a_thrown_win: set[str] = field(default_factory=set)
    agent_name: str = "heuristic"

    def as_dict(self) -> dict:
        return {
            "games": self.games,
            "decisions": self.decisions,
            "solvable_points": self.solvable_points,
            "proved_win_points": self.proved_win_points,
            "proved_loss_points": self.proved_loss_points,
            "unproved_points": self.unproved_points,
            "human": {"kept": self.human_kept, "threw": self.human_threw},
            "agent": {
                "name": self.agent_name, "kept": self.agent_kept, "threw": self.agent_threw,
            },
            "games_with_a_thrown_win": len(self.games_with_a_thrown_win),
        }


def evaluate(
    records: Iterable[GameRecord],
    *,
    agent_name: str = "heuristic",
    keep_examples: int = 5,
    game_ids: Sequence[str | None] | None = None,
) -> Report:
    """回放对局，在能算清的残局点位上比较"人类 / agent"有没有保住必胜。"""
    agent = make(agent_name)
    report = Report(agent_name=agent_name)
    for index, record in enumerate(records):
        game_id = game_ids[index] if game_ids is not None else record.game_id
        report.games += 1
        threw_here = False
        try:
            decisions = list(iter_decisions(record))
        except Exception:  # 记录坏了不该让整批评测挂掉
            continue
        for decision in decisions:
            report.decisions += 1
            state = decision.state
            if not endgame_solvable(state):
                continue
            report.solvable_points += 1
            solved = endgame_solve(state)
            if not solved.proved:
                report.unproved_points += 1
                continue
            if not solved.winning:
                report.proved_loss_points += 1
                continue

            report.proved_win_points += 1
            best = {None if m is None else action_key(m.cards) for m in (solved.best_moves or [])}
            if action_key(decision.move.cards if decision.move else None) in best:
                report.human_kept += 1
            else:
                report.human_threw += 1
                threw_here = True
                if len(report.thrown_examples) < keep_examples:
                    report.thrown_examples.append(
                        {
                            "game_id": game_id,
                            "seat": decision.seat,
                            "hand": list(state.hand_for(decision.seat) or ()),
                            "human": list(decision.move.cards) if decision.move else [],
                            "solver": list(solved.move.cards) if solved.move else [],
                            "description": _describe(state, solved),
                        }
                    )

            try:
                candidate = agent.act(state)
                played = action_key(candidate.cards)
            except Exception:
                continue
            if played in best:
                report.agent_kept += 1
            else:
                report.agent_threw += 1
        if threw_here and game_id:
            report.games_with_a_thrown_win.add(str(game_id))
    return report


def _describe(state, solved) -> str:
    from ..explain import describe_move

    move = solved.move
    if move is None:
        return "不出"
    return describe_move(move)


def _load(path: Path) -> tuple[list[GameRecord], list[str | None]]:
    records, ids = [], []
    for record in Archive(path).records():
        records.append(record)
        ids.append(record.game_id or None)
    return records, ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.tools.eval_records", description="对局库回放评测")
    parser.add_argument("--records", default="data/games.jsonl")
    parser.add_argument("--agent", default="heuristic", help="拿谁跟人类比（默认启发式）")
    parser.add_argument("--examples", type=int, default=5, help="打印几个「丢掉必胜」的例子")
    args = parser.parse_args(argv)

    path = Path(args.records)
    if not path.exists():
        print(f"没有对局库：{path}", file=sys.stderr)
        return 2
    records, ids = _load(path)
    if not records:
        print("对局库是空的。", file=sys.stderr)
        return 2

    report = evaluate(records, agent_name=args.agent, keep_examples=args.examples)
    _print(report, records)
    return 0


def _print(report: Report, records: Sequence[GameRecord]) -> None:
    def rate(kept: int, threw: int) -> str:
        total = kept + threw
        return f"{kept}/{total}（{100.0 * kept / total:.1f}%）" if total else "—"

    print(f"对局 {report.games} 局 / 决策点 {report.decisions} 个")
    print(
        f"其中能算清的残局 {report.solvable_points} 个："
        f"必胜 {report.proved_win_points}、必败 {report.proved_loss_points}、"
        f"没搜完 {report.unproved_points}"
    )
    if report.proved_win_points:
        print(f"  人类    保住了 {rate(report.human_kept, report.human_threw)}")
        print(f"  {report.agent_name:<8}保住了 {rate(report.agent_kept, report.agent_threw)}")
        print("  求解器  定义上 100%（它就是从这里挑「能赢的走法」的那把尺子）")
    if report.games_with_a_thrown_win:
        print(f"\n有 {len(report.games_with_a_thrown_win)} 局出现过「算得出来能赢、却打丢了」的时刻")
    for example in report.thrown_examples:
        hand = " ".join(C.format_cards(example["hand"]))
        human = C.format_cards(example["human"]) or "不出"
        best = C.format_cards(example["solver"]) or "不出"
        print(
            f"  · 第 {example['game_id']} 局 {example['seat']} 号位：手牌 {hand}｜"
            f"实际 {human}｜求解器 {best}（{example['description']}）"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
