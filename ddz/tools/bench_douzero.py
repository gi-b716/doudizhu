"""DouZero 推理延迟基准（PLAN §3.7：``bench_douzero`` 要 p95 < 50ms）。

在随机打出来的真实局面上跑，而不是造零观测——真实局面的动作数（= 前向的 batch size）
才是延迟的主导因素。

    python -m ddz.tools.bench_douzero --games 20 --repeat 3 [--device cuda]

没有 torch / 权重时不会假装有数据：直接说清楚缺什么并退出码 2。
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from typing import Sequence

from .. import cards as C
from ..arena.metrics import percentile
from ..douzero import agent as agent_module
from ..douzero import bridge, vendored, weights


def _use_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def sample_infosets(games: int = 10, seed: int = 0):
    """从随机对局里取真实局面（每个决策点的 infoset）。"""
    import sys as _sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from gamegen import play_game              # 只在基准里用，运行时不依赖
    from ..record import from_finished_state, replay

    out = []
    for index in range(games):
        record = from_finished_state(play_game(random.Random(seed + index))[-1])
        if len(record.deals) != 1:
            continue
        for k, event in enumerate(record.events):
            if event.kind not in ("play", "pass"):
                continue
            state = replay(record, k)
            if state.landlord is None:
                continue
            if state.hand_for(event.seat) is None:
                continue
            out.append(bridge.build_infoset(state, event.seat))
    return out


def bench(games: int = 10, repeat: int = 1, *, device: str | None = None, seed: int = 0) -> dict:
    """跑基准，返回统计结果（``ready=False`` 表示环境没准备好）。"""
    if not agent_module.is_ready():
        return {"ready": False, "status": agent_module.status()}

    agent = agent_module.DouZeroAgent(device=device)
    if not agent.load():
        return {"ready": False, "status": agent.status()}

    infosets = sample_infosets(games, seed)
    if not infosets:
        return {"ready": False, "reason": "取不到可用局面"}

    latencies, sizes = [], []
    for _ in range(max(1, repeat)):
        for info in infosets:
            model = agent.models[info.player_position]
            started = time.perf_counter()
            with agent._lock:
                bridge.choose_action(model, info, device=device)
            latencies.append((time.perf_counter() - started) * 1000.0)
            sizes.append(len(info.legal_actions))

    return {
        "ready": True,
        "device": agent.status()["device"],
        "load_seconds": agent.load_seconds,
        "samples": len(latencies),
        "actions_mean": round(statistics.fmean(sizes), 1),
        "actions_max": max(sizes),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "p99": round(percentile(latencies, 0.99), 2),
            "max": round(max(latencies), 2),
            "mean": round(statistics.fmean(latencies), 2),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(prog="bench_douzero", description="DouZero 推理延迟基准")
    parser.add_argument("--games", type=int, default=10, help="取几局真实局面")
    parser.add_argument("--repeat", type=int, default=1, help="同一批局面重复几轮")
    parser.add_argument("--device", default=None, help="cuda / cpu（默认自动）")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = bench(args.games, args.repeat, device=args.device, seed=args.seed)
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ready") else 2

    if not result.get("ready"):
        status = result.get("status", {})
        print("DouZero 还没就绪，先补齐：")
        print(f"  torch      : {vendored.torch_available()}")
        print(f"  仓库       : {vendored.root_dir()}（{vendored.commit() or '缺 COMMIT.txt'}）")
        print(f"  权重       : {weights.status()}")
        if status.get("last_error"):
            print(f"  最近错误   : {status['last_error']}")
        return 2

    lat = result["latency_ms"]
    print(f"设备 {result['device']}｜装载 {result['load_seconds']:.2f}s｜样本 {result['samples']}"
          f"（平均 {result['actions_mean']} 个候选，最多 {result['actions_max']}）")
    print(f"延迟 p50 {lat['p50']}ms  p95 {lat['p95']}ms  p99 {lat['p99']}ms  max {lat['max']}ms")
    verdict = "通过" if lat["p95"] < 50 else "未达标（PLAN 要求 p95 < 50ms）"
    print(f"验收（p95 < 50ms）：{verdict}")
    return 0 if lat["p95"] < 50 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
