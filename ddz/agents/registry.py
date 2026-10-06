"""agent 注册表：把命令行/配置里的名字变成实例。

- ``heuristic``（别名 ``heur``）——启发式，Phase 2–3 主力；
- ``random``（别名 ``rand``）——随机基线；
- ``douzero:WP``（或 ``douzero:<权重目录>``）——上游 DouZero。**没装好不算错误**：构造出来的
  agent ``loaded=False``，每次决策都回落到启发式，并在理由里写明原因（服务的兜底路径不变）。
- ``rlcard-rule``——上游 DouZero 仓库里那个 RLCard 规则基线（**别人的启发式**），只为竞技场
  的第三方参照存在，**不进** :func:`available`（那是产品的引擎列表，只有启发式与 DouZero）。
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
    """可用引擎。DouZero 装好了就列进去（浮窗/服务的 ``auto`` 都按这个判断）。"""
    names = ["heuristic", "random"]
    from ..douzero.agent import is_ready

    if is_ready():
        names.insert(1, "douzero:WP")
    return names


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
    if lowered in ("rlcard-rule", "rlcard", "rlcard_rule"):
        from .rlcard_rule import RLCardRuleAgent

        return RLCardRuleAgent(**kwargs)
    if lowered.startswith("douzero"):
        from ..douzero.agent import DouZeroAgent

        # "douzero" / "douzero:WP" 用默认权重目录；"douzero:<路径>" 指定目录
        _, _, tail = name.partition(":")
        weights_path = None if tail in ("", "WP", "wp") else tail
        agent = DouZeroAgent(weights_path, **kwargs)
        agent.load()   # 尽力装载；失败也不抛，决策时回落启发式
        return agent
    raise ValueError(f"不认识的 agent：{spec!r}（可用：{'、'.join(available())}）")


def make_many(specs: list[str], **kwargs: Any) -> list[Agent]:
    return [make(spec, **kwargs) for spec in specs]
