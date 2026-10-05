"""agent 注册表：把命令行/配置里的名字变成实例。

- ``heuristic``（别名 ``heur``）——启发式，Phase 2–3 主力；
- ``random``（别名 ``rand``）——随机基线；
- ``douzero:WP``／``douzero:<权重目录>``——Phase 4 接入，现在明确报"还没到"而不是静默降级。
"""

from __future__ import annotations

from typing import Any

from ..engine.heuristic import HeuristicAgent, Weights
from ..engine.base import Agent
from .random_agent import RandomAgent

__all__ = ["make", "available", "make_many"]

_HEURISTIC_ALIASES = {"heuristic", "heur", "h"}
_RANDOM_ALIASES = {"random", "rand", "r"}


def available() -> list[str]:
    return ["heuristic", "random"]


def make(spec: str, **kwargs: Any) -> Agent:
    """``spec`` → agent 实例。权重等额外参数用 ``kwargs`` 透传。"""
    name = (spec or "").strip()
    lowered = name.lower()
    if lowered in _HEURISTIC_ALIASES:
        weights = kwargs.pop("weights", None)
        if weights is not None and not isinstance(weights, Weights):
            weights = Weights(**weights)
        return HeuristicAgent(weights, **kwargs)
    if lowered in _RANDOM_ALIASES:
        return RandomAgent(**kwargs)
    if lowered.startswith("douzero"):
        raise NotImplementedError(
            f"DouZero（{spec}）在 Phase 4 接入：需要先 clone third_party/DouZero 并放好 "
            "baselines/douzero_WP 权重。当前可用：" + "、".join(available())
        )
    raise ValueError(f"不认识的 agent：{spec!r}（可用：{'、'.join(available())}）")


def make_many(specs: list[str], **kwargs: Any) -> list[Agent]:
    return [make(spec, **kwargs) for spec in specs]
