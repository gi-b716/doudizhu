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

from ..agents.registry import _HEURISTIC_ALIASES, make
from ..engine.bidding import parse_thresholds
from ..state import RULES_TAG
from .metrics import Metrics
from .simulator import Simulator

__all__ = ["main", "run_match", "default_out_dir"]


def default_out_dir(players: Sequence[str], seed: int) -> Path:
    slug = "_vs_".join(p.replace(":", "-").replace("/", "-") for p in players)
    return Path("runs") / f"{slug}_{seed}"


def _make_agent(name: str, agent_kwargs: dict | None, cache: dict):
    """每局新建 agent，但 DouZero 例外：它装载三份模型很慢，同一配置只装一次（无状态，可复用）。"""
    kwargs = {} if agent_kwargs is None else agent_kwargs.get(name, {})
    if not name.strip().lower().startswith("douzero"):
        return make(name, **kwargs)
    key = (name, repr(sorted(kwargs.items())))
    if key not in cache:
        cache[key] = make(name, **kwargs)
    return cache[key]


def _takes_thresholds(name: str) -> bool:
    """叫分阈值只对启发式和 DouZero（它的兜底启发式）有意义；random 不接受这个参数。"""
    lowered = name.strip().lower()
    return lowered in _HEURISTIC_ALIASES or lowered.startswith("douzero")


def run_match(
    players: Sequence[str],
    *,
    games: int = 100,
    seed: int = 0,
    rotate: bool = False,
    strict: bool = True,
    progress: bool = False,
    agent_kwargs: dict | None = None,
    oracle: bool = False,
) -> tuple[Metrics, float]:
    """跑 ``games`` 局，返回 (指标, 用时秒)。"""
    metrics = Metrics(list(players))
    started = time.perf_counter()
    cache: dict = {}
    for index in range(games):
        # --rotate：第 index 局让 players[0] 去坐 (index % 3) 号座位
        offset = index % 3 if rotate else 0
        order = [players[(seat - offset) % 3] for seat in range(3)]
        agents = [_make_agent(name, agent_kwargs, cache) for name in order]
        outcome = Simulator(agents, seed=seed + index, strict=strict, oracle=oracle).play(index)
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
    parser.add_argument(
        "--oracle", action="store_true",
        help="把三家手牌也发给 agent（只有残局求解这类需要完美信息的东西用得上；"
             "启发式/随机/DouZero 都不读它，所以对它们是中性的）。报告里必须写明用了它。",
    )
    parser.add_argument(
        "--bid-thresholds", default=None,
        help="叫分门槛（叫 1/2/3 分的牌力线）：一个数表示三档相同（如 10），或 '6,8,10' 逐档指定。"
             "只作用于启发式与 DouZero 的兜底叫分（DouZero 本身不叫分）；默认用 bidding.THRESHOLDS。",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    players = [p.strip() for p in args.players.split(",") if p.strip()]
    if len(players) != 3:
        parser.error("--players 需要正好三个（逗号分隔）")

    agent_kwargs = None
    if args.bid_thresholds:
        try:
            thresholds = parse_thresholds(args.bid_thresholds)
        except ValueError as exc:
            parser.error(str(exc))
        agent_kwargs = {
            name: {"thresholds": thresholds} for name in players if _takes_thresholds(name)
        }

    started = time.time()
    metrics, elapsed = run_match(
        players,
        games=args.games,
        seed=args.seed,
        rotate=args.rotate,
        strict=not args.lenient,
        progress=not args.quiet,
        oracle=args.oracle,
        agent_kwargs=agent_kwargs,
    )
    summary = metrics.summary(
        meta={
            "seed": args.seed,
            "rotate": args.rotate,
            "bid_thresholds": args.bid_thresholds or "default",
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
