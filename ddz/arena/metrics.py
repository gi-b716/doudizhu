"""竞技场指标：分座位/角色的胜率、ADP、平均倍数、延迟分位、错误计数（PLAN §3.7）。

ADP = Average Delta in Points：某个 agent 每局的平均得分（DouZero 论文里的口径）。
同名 agent（比如两个 ``random``）在 ``by_agent`` 里合并统计，在 ``by_seat`` 里分座位看。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .simulator import GameOutcome

__all__ = ["AgentStats", "Metrics", "percentile"]

_ROLES = ("landlord", "farmer")


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数（q 取 0..1）；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


@dataclass(slots=True)
class AgentStats:
    name: str
    seats: set[int] = field(default_factory=set)
    games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    delta_sum: int = 0
    role_games: dict[str, int] = field(default_factory=lambda: {r: 0 for r in _ROLES})
    role_wins: dict[str, int] = field(default_factory=lambda: {r: 0 for r in _ROLES})
    multiplier_sum: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    fallbacks: int = 0

    def summary(self) -> dict:
        def rate(wins: int, games: int) -> float:
            return round(wins / games, 4) if games else 0.0

        return {
            "name": self.name,
            "seats": sorted(self.seats),
            "games": self.games,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "win_rate": rate(self.wins, self.games),
            "adp": round(self.delta_sum / self.games, 3) if self.games else 0.0,
            "landlord_win_rate": rate(self.role_wins["landlord"], self.role_games["landlord"]),
            "farmer_win_rate": rate(self.role_wins["farmer"], self.role_games["farmer"]),
            "avg_multiplier": round(self.multiplier_sum / self.games, 3) if self.games else 0.0,
            "latency_ms": {
                "p50": round(percentile(self.latencies, 0.50), 3),
                "p95": round(percentile(self.latencies, 0.95), 3),
                "p99": round(percentile(self.latencies, 0.99), 3),
                "max": round(max(self.latencies), 3) if self.latencies else 0.0,
                "mean": round(sum(self.latencies) / len(self.latencies), 3) if self.latencies else 0.0,
            },
            "errors": self.errors,
            "fallbacks": self.fallbacks,
        }


class Metrics:
    """把一局局 :class:`GameOutcome` 累积成报表。"""

    def __init__(self, players: list[str] | None = None) -> None:
        self.players = list(players or [])
        self.by_agent: dict[str, AgentStats] = {}
        #: 每个座位的明细（座位固定时用来区分两个同名 agent）
        self.by_seat: list[AgentStats] = [AgentStats(name="") for _ in range(3)]
        self.games: list[dict] = []
        self.multiplier_sum = 0
        self.landlord_wins = 0
        self.springs = 0
        self.anti_springs = 0
        self.redeals = 0

    def add(self, outcome: GameOutcome) -> None:
        self.multiplier_sum += outcome.multiplier
        self.landlord_wins += int(outcome.landlord_won)
        self.springs += int(outcome.spring)
        self.anti_springs += int(outcome.anti_spring)
        self.redeals += outcome.redeals

        for seat, name in enumerate(outcome.seats):
            delta = outcome.deltas[seat]
            role = "landlord" if seat == outcome.landlord else "farmer"
            for stats in (self.by_agent.setdefault(name, AgentStats(name=name)), self.by_seat[seat]):
                stats.name = name
                stats.seats.add(seat)
                stats.games += 1
                stats.delta_sum += delta
                stats.wins += int(delta > 0)
                stats.losses += int(delta < 0)
                stats.draws += int(delta == 0)
                stats.role_games[role] += 1
                stats.role_wins[role] += int(delta > 0)
                stats.multiplier_sum += outcome.multiplier
                stats.errors += outcome.errors
            latency = outcome.latency_by_seat[seat]
            self.by_seat[seat].latencies.extend(latency)
            self.by_agent[name].latencies.extend(latency)

        self.games.append(
            {
                "index": outcome.index,
                "seats": list(outcome.seats),
                "landlord": outcome.landlord,
                "landlord_won": outcome.landlord_won,
                "spring": outcome.spring,
                "anti_spring": outcome.anti_spring,
                "base": outcome.base,
                "multiplier": outcome.multiplier,
                "bombs": outcome.bombs,
                "deltas": list(outcome.deltas),
                "steps": outcome.steps,
                "redeals": outcome.redeals,
                "errors": outcome.errors,
            }
        )

    def summary(self, *, meta: dict | None = None, include_games: bool = False) -> dict:
        games = len(self.games)
        out = {
            "meta": {
                "players": self.players,
                "games": games,
                "landlord_win_rate": round(self.landlord_wins / games, 4) if games else 0.0,
                "avg_multiplier": round(self.multiplier_sum / games, 3) if games else 0.0,
                "springs": self.springs,
                "anti_springs": self.anti_springs,
                "redeals": self.redeals,
                **(meta or {}),
            },
            "by_agent": {name: stats.summary() for name, stats in sorted(self.by_agent.items())},
            "by_seat": [
                {**stats.summary(), "seat": seat} for seat, stats in enumerate(self.by_seat) if stats.games
            ],
        }
        if include_games:
            out["games"] = self.games
        return out


