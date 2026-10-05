"""竞技场 CLI：让几个 agent 互相对打，输出胜率/ADP/延迟报表。

    python -m ddz.arena.match --players heuristic,random,random --games 200 --seed 7
    python -m ddz.arena.match --players douzero:WP,heuristic,heuristic --games 300 \\
        --seed 11 --rotate --out runs/dz_vs_heur

``--rotate`` 让每个 agent 依次坐 0/1/2 号座位（每局轮换），这样"分座位胜率"才有意义；
不加就是固定座位。结果写到 ``<out>/summary.json``（不加 ``--out`` 时是
``runs/<players>_<seed>/``）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from ..agents.registry import make
from ..state import RULES_TAG
from .metrics import Metrics
from .simulator import Simulator

__all__ = ["main", "run_match", "default_out_dir"]


def default_out_dir(players: Sequence[str], seed: int) -> Path:
    slug = "_vs_".join(p.replace(":", "-").replace("/", "-") for p in players)
    return Path("runs") / f"{slug}_{seed}"


def run_match(
    players: Sequence[str],
    *,
    games: int = 100,
    seed: int = 0,
    rotate: bool = False,
    strict: bool = True,
    progress: bool = False,
    agent_kwargs: dict | None = None,
) -> tuple[Metrics, float]:
    """跑 ``games`` 局，返回 (指标, 用时秒)。"""
    metrics = Metrics(list(players))
    started = time.perf_counter()
    for index in range(games):
        # --rotate：第 index 局让 players[0] 去坐 (index % 3) 号座位
        offset = index % 3 if rotate else 0
        order = [players[(seat - offset) % 3] for seat in range(3)]
        agents = [
            make(name, **({} if agent_kwargs is None else agent_kwargs.get(name, {}))) for name in order
        ]
        outcome = Simulator(agents, seed=seed + index, strict=strict).play(index)
        metrics.add(outcome)
        if progress and (index + 1) % max(1, games // 10) == 0:
            print(f"  ... {index + 1}/{games}", file=sys.stderr)
    return metrics, time.perf_counter() - started


def _fmt_table(summary: dict) -> str:
    lines = []
    header = f"{'agent':<16}{'座位':<10}{'局数':>6}{'胜率':>9}{'ADP':>9}{'地主胜率':>10}{'农民胜率':>10}{'倍数':>7}{'p95(ms)':>9}{'错误':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, stats in summary["by_agent"].items():
        seats = ",".join(str(s) for s in stats["seats"])
        lines.append(
            f"{name:<16}{seats:<10}{stats['games']:>6}{stats['win_rate']:>9.1%}"
            f"{stats['adp']:>+9.2f}{stats['landlord_win_rate']:>10.1%}{stats['farmer_win_rate']:>10.1%}"
            f"{stats['avg_multiplier']:>7.2f}{stats['latency_ms']['p95']:>9.2f}{stats['errors']:>6}"
        )
    meta = summary["meta"]
    lines.append(
        f"共 {meta['games']} 局：地主胜率 {meta['landlord_win_rate']:.1%}，平均倍数 {meta['avg_multiplier']:.2f}，"
        f"春天 {meta['springs']}、反春 {meta['anti_springs']}、流局 {meta['redeals']}"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    parser = argparse.ArgumentParser(prog="ddz.arena.match", description="自对弈竞技场")
    parser.add_argument("--players", required=True, help="逗号分隔，正好三个，如 heuristic,random,random")
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--rotate", action="store_true", help="每局轮换座位，让每个 agent 坐遍 0/1/2")
    parser.add_argument("--out", default=None, help="输出目录（默认 runs/<players>_<seed>）")
    parser.add_argument("--dump", action="store_true", help="summary.json 里带上每一局的明细")
    parser.add_argument("--lenient", action="store_true", help="非法着法只计数不抛异常")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    players = [p.strip() for p in args.players.split(",") if p.strip()]
    if len(players) != 3:
        parser.error("--players 需要正好三个（逗号分隔）")

    started = time.time()
    metrics, elapsed = run_match(
        players,
        games=args.games,
        seed=args.seed,
        rotate=args.rotate,
        strict=not args.lenient,
        progress=not args.quiet,
    )
    summary = metrics.summary(
        meta={
            "seed": args.seed,
            "rotate": args.rotate,
            "rules": RULES_TAG,
            "scoring": "rules.md：地主 ±2×底分×倍数，农民 ∓底分×倍数",
            "created_at": started,
            "elapsed_s": round(elapsed, 3),
        },
        include_games=args.dump,
    )

    out_dir = Path(args.out) if args.out else default_out_dir(players, args.seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not args.quiet:
        print(_fmt_table(summary))
        print(f"用时 {elapsed:.1f}s（{elapsed / max(1, args.games) * 1000:.0f} ms/局）→ {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
