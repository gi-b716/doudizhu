"""叫分阈值 A/B：候选 vs 基线，同一桌上 1 个候选 + 2 个基线，轮换座位、配对种子。

用法（``--candidate`` / ``--baseline`` 都接受档位名 ``strict`` / ``loose``，或 ``10``、``4,8,12`` 这种阈值）::

    # 产品默认档 vs 旧式宽档（任何机器都能跑）
    python -m ddz.tools.bid_ab --candidate strict --baseline loose --games 6000 --seed 5000

    # 想比两种严档谁更好，直接写阈值
    python -m ddz.tools.bid_ab --candidate 10 --baseline 4,8,12 --games 20000 --seed 5000

    # DouZero 模式（需要本机装好 torch + 权重；DouZero 只管出牌，叫分走兜底启发式）
    python -m ddz.tools.bid_ab --engine douzero:WP --candidate strict --games 3000

为什么这么摆：**同一桌 1 个候选 + 2 个基线**，候选座位按 ``index % 3`` 轮换，所以候选坐地主、
坐地主上家、坐地主下家的局数各占三分之一；同一个 ``--seed`` 下两种阈值看到的是同一批牌
（配对），差值的方差远小于两组独立比较。

口径：候选那一座位每局得分（rules.md：地主 ±2×底分×倍数，农民 ∓底分×倍数）的平均值 = ADP，
括号里是 95% 置信区间。除了总账还给**角色 × 底分**的切分——只看总账会得出"运气不好"，
切开才知道钱是在哪一格输的（和 :mod:`ddz.tools.report` 同一个道理）。
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from ..agents.registry import make
from ..arena.simulator import Simulator
from ..engine.bidding import parse_thresholds

__all__ = ["Sample", "run_ab", "summarize", "main"]


@dataclass(frozen=True, slots=True)
class Sample:
    """候选座位在一局里的结果。"""

    delta: int
    won: bool
    was_landlord: bool
    base: int
    multiplier: int
    #: 候选这一局坐哪个位（``index % 3``）——用它验证"轮换座位"真的轮起来了
    seat: int
    #: 同场两个基线座位的得分（三家之和为 0，用来校验口径没写错）
    baseline_deltas: tuple[int, int]


def run_ab(engine: str, candidate, baseline, games: int, seed: int) -> list[Sample]:
    """跑 ``games`` 局，返回候选座位每局的结果。agent 只建一次：DouZero 装载很慢，而它无状态。"""
    cand = make(engine, thresholds=candidate)
    base = make(engine, thresholds=baseline)
    samples: list[Sample] = []
    for index in range(games):
        offset = index % 3                       # 第 index 局候选坐在 offset 号位
        agents = [base, base, base]
        agents[offset] = cand
        outcome = Simulator(agents, seed=seed + index, strict=True).play(index)
        others = tuple(outcome.deltas[seat] for seat in range(3) if seat != offset)
        samples.append(
            Sample(
                delta=outcome.deltas[offset],
                won=outcome.deltas[offset] > 0,
                was_landlord=outcome.landlord == offset,
                base=outcome.base,
                multiplier=outcome.multiplier,
                seat=offset,
                baseline_deltas=others,
            )
        )
    return samples


def _mean_ci(values: Sequence[int]) -> tuple[float, float]:
    """平均值与 95% 置信区间的半宽（少于 2 个样本时半宽是 nan）。"""
    if not values:
        return float("nan"), float("nan")
    mean = statistics.fmean(values)
    half = 1.96 * statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else float("nan")
    return mean, half


def summarize(samples: Sequence[Sample]) -> dict:
    """总账 + 角色 × 底分的切分（``main`` 与测试都用它，口径只有一份）。"""
    adp, half = _mean_ci([s.delta for s in samples])
    base_adp, _ = _mean_ci([sum(s.baseline_deltas) / 2 for s in samples])
    groups: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups["地主" if sample.was_landlord else "农民"].append(sample)
        groups[f"底分{sample.base}"].append(sample)
        groups[f"{'地主' if sample.was_landlord else '农民'}·底分{sample.base}"].append(sample)
    return {
        "games": len(samples),
        "adp": adp,
        "ci95": half,
        "winrate": (sum(1 for s in samples if s.won) / len(samples)) if samples else float("nan"),
        "landlord_share": (sum(1 for s in samples if s.was_landlord) / len(samples)) if samples else float("nan"),
        "avg_multiplier": statistics.fmean([s.multiplier for s in samples]) if samples else float("nan"),
        "baseline_adp": base_adp,
        "groups": {
            name: {
                "games": len(group),
                "adp": _mean_ci([s.delta for s in group])[0],
                "winrate": sum(1 for s in group if s.won) / len(group),
                "landlord_winrate": (
                    sum(1 for s in group if s.was_landlord and s.won) / max(1, sum(1 for s in group if s.was_landlord))
                ),
            }
            for name, group in groups.items()
        },
    }


def _fmt_group(name: str, stats: dict) -> str:
    return (
        f"    {name:<12} {stats['games']:>6} 局   胜率 {stats['winrate']:>6.1%}"
        f"   ADP {stats['adp']:+.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.tools.bid_ab", description="叫分阈值 A/B（配对种子）")
    parser.add_argument("--engine", default="heuristic", help="heuristic（默认）或 douzero:WP")
    parser.add_argument("--candidate", required=True,
                        help="候选：档位名（strict/loose）或阈值（'10' 三档相同、'4,8,12' 逐档）")
    parser.add_argument("--baseline", default="loose", help="基线，默认宽档 loose（= 旧版 -2,2,6）")
    parser.add_argument("--games", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=5000)
    parser.add_argument("--quiet", action="store_true", help="只打总账那一行，不打切分表")
    args = parser.parse_args(argv)

    try:
        candidate = parse_thresholds(args.candidate)
        baseline = parse_thresholds(args.baseline)
    except ValueError as exc:
        parser.error(str(exc))

    started = time.perf_counter()
    samples = run_ab(args.engine, candidate, baseline, args.games, args.seed)
    stats = summarize(samples)
    print(
        f"[{args.engine}] 候选 {args.candidate} vs 基线 {args.baseline}（1 候选 + 2 基线，轮换座位）："
        f"ADP {stats['adp']:+.3f} ±{stats['ci95']:.3f}（95%）  候选胜率 {stats['winrate']:.1%}"
        f"  局 {stats['games']}  种子 {args.seed}  用时 {time.perf_counter() - started:.0f}s",
        flush=True,
    )
    if not args.quiet and samples:
        print(
            f"  候选当地主 {stats['landlord_share']:.1%} 的局；平均倍数 {stats['avg_multiplier']:.2f}；"
            f"同场两个基线座位各 {stats['baseline_adp']:+.3f}（三家之和应为 0）",
            flush=True,
        )
        order = ["地主", "农民", "底分1", "底分2", "底分3",
                 "农民·底分1", "农民·底分2", "农民·底分3", "地主·底分3"]
        print("  切分（角色 / 底分）：", flush=True)
        for name in order:
            if name in stats["groups"]:
                print(_fmt_group(name, stats["groups"][name]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
