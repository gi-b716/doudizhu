"""叫分门槛 A/B：候选门槛 vs 基线门槛，同一桌上 1 个候选 + 2 个基线，轮换座位、配对种子。

用法::

    # 启发式（任何机器都能跑）
    python -m ddz.tools.bid_ab --candidate 10 --baseline -2,2,6 --games 6000 --seed 5000

    # DouZero 模式（需要本机装好 torch + 权重；DouZero 只管出牌，叫分门槛作用于它的兜底启发式）
    python -m ddz.tools.bid_ab --engine douzero:WP --candidate 10 --baseline -2,2,6 --games 3000

结果口径：候选那一座位每局得分（rules.md 的口径）的平均值，即 ADP；括号里是 95% 置信区间。
同一个 ``--seed`` 下两种门槛看到的是同一批牌（配对），所以差值的方差远小于两组独立比较。
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

from ..agents.registry import make
from ..arena.simulator import Simulator
from ..engine.bidding import parse_thresholds

__all__ = ["run_ab", "main"]


def run_ab(engine: str, candidate, baseline, games: int, seed: int) -> list[int]:
    """返回候选每局的得分（长度 ``games``）。agent 只建一次：DouZero 装载很慢，而它无状态。"""
    cand = make(engine, bid_thresholds=candidate)
    base = make(engine, bid_thresholds=baseline)
    scores: list[int] = []
    for index in range(games):
        offset = index % 3                       # 第 index 局候选坐在 offset 号位
        agents = [base, base, base]
        agents[offset] = cand
        outcome = Simulator(agents, seed=seed + index, strict=True).play(index)
        scores.append(outcome.deltas[offset])
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.tools.bid_ab", description="叫分门槛 A/B（配对种子）")
    parser.add_argument("--engine", default="heuristic", help="heuristic（默认）或 douzero:WP")
    parser.add_argument("--candidate", required=True, help="候选门槛：一个数（三档相同）或 '6,8,10'")
    parser.add_argument("--baseline", default="-2,2,6", help="基线门槛，默认旧版 -2,2,6")
    parser.add_argument("--games", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=5000)
    args = parser.parse_args(argv)

    try:
        candidate = parse_thresholds(args.candidate)
        baseline = parse_thresholds(args.baseline)
    except ValueError as exc:
        parser.error(str(exc))

    started = time.perf_counter()
    scores = run_ab(args.engine, candidate, baseline, args.games, args.seed)
    n = len(scores)
    mean = statistics.fmean(scores)
    half = 1.96 * statistics.stdev(scores) / math.sqrt(n) if n > 1 else float("nan")
    win = sum(1 for s in scores if s > 0) / n
    print(
        f"[{args.engine}] 候选 {args.candidate} vs 基线 {args.baseline}（1 候选 + 2 基线，轮换座位）："
        f"ADP {mean:+.3f} ±{half:.3f}（95%）  候选胜率 {win:.1%}  局 {n}  种子 {args.seed}"
        f"  用时 {time.perf_counter() - started:.0f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
