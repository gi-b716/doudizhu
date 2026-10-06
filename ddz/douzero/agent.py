"""DouZero agent：把上游模型包成我们的 :class:`~ddz.engine.base.Agent`。

纯增量：模型没就绪（没 torch / 没权重 / 没 clone 仓库）时 :meth:`DouZeroAgent.load` 返回
``False``，全体回退启发式——服务的兜底路径一行都不用改（PLAN §3.4）。

几条工程约定：

- **单锁串行化模型调用**：torch 的模块不是为并发推理准备的，多个浮窗同时问会互相踩；
- **预热**：启动时用假观测跑一次前向，免得第一手建议卡在 cuDNN 初始化上；
- **超时不在这里做**：请求级的 ``deadline_ms`` 由 :mod:`ddz.service.advisor` 的线程池管
  （引擎无关，DouZero 和别的引擎共用同一套兜底）；这里只管"算出来的东西对不对"；
- **叫分永远走启发式**：DouZero 没有叫分模型，也不认识流局（PLAN §0.2）。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ..engine.base import Bid, Candidate
from ..engine.heuristic import HeuristicAgent
from ..state import GameState
from . import bridge, vendored, weights

__all__ = ["DouZeroAgent", "is_ready", "status"]

log = logging.getLogger("ddz.douzero.agent")


def is_ready() -> bool:
    """三个条件齐了才算就绪：torch、vendored 仓库、三份权重。"""
    return (
        vendored.torch_available()
        and vendored.is_installed()
        and weights.resolve() is not None
    )


def cuda_available() -> bool:
    """torch 装了**并且真能把张量放到 GPU 上**。

    注意 ``torch.cuda.is_available()`` 与"装没装 torch"是两回事：CPU 版 torch（``2.x+cpu``）
    装得好好的，但这里恒为 ``False``。以前 ``/health`` 报的是前者，在纯 CPU 机器上会报 cuda:true。

    import 过 torch 就直接问它；没 import 过则先看装没装，免得为了一个布尔值把几百 MB 拖进来。
    """
    import sys

    module = sys.modules.get("torch")
    if module is None:
        if not vendored.torch_available():
            return False
        try:
            import torch as module
        except Exception:  # pragma: no cover - 装了个坏的
            return False
    try:
        return bool(module.cuda.is_available())
    except Exception:  # pragma: no cover - 没有 cuda 模块的怪构建
        return False


def status() -> dict:
    return {
        "ready": is_ready(),
        "torch": vendored.torch_available(),
        "cuda": cuda_available(),
        "repo": vendored.status(),
        "weights": weights.status(),
    }


class DouZeroAgent:
    """``douzero:WP``。加载失败时构造不会抛异常，只是 ``loaded=False``。"""

    name = "douzero:WP"

    def __init__(
        self,
        weights_path: Path | str | None = None,
        *,
        device: str | None = None,
        fallback: HeuristicAgent | None = None,
    ) -> None:
        self.weights_path = Path(weights_path) if weights_path else None
        self.device = device
        self.fallback = fallback or HeuristicAgent()
        self.loaded = False
        self.last_error: str | None = None
        self.load_seconds: float | None = None
        self.models: dict[str, object] = {}
        self._lock = threading.Lock()
        self._modules = None

    # ----------------------------------------------------------------- 装载

    def load(self) -> bool:
        """装载模型；任何一环缺失都返回 ``False``（并把原因记在 ``last_error``）。"""
        if self.loaded:
            return True
        started = time.perf_counter()
        try:
            if not vendored.torch_available():
                raise RuntimeError("没装 torch（运行机执行 uv pip install torch --index-url ...）")
            if not vendored.is_installed():
                raise RuntimeError(f"没有 vendored 的 DouZero 仓库：{vendored.root_dir()}")
            resolved = weights.resolve(self.weights_path)
            if resolved is None:
                raise RuntimeError(
                    f"权重不全：{weights.status(self.weights_path)['missing']} 还缺，"
                    f"放到 {weights.weights_dir()}"
                )
            modules = vendored.load_env()
            if modules is None:  # pragma: no cover - is_installed 已保证
                raise RuntimeError("上游 env 模块装载失败")
            self._modules = modules
            bridge.assert_code_bijection()   # 牌码对不上就别往下走了

            model_modules = vendored.load_model_modules()
            if model_modules is None:  # pragma: no cover
                raise RuntimeError("上游模型模块装载失败")
            _, deep_agent = model_modules
            for position, path in resolved.files.items():
                digest = weights.sha256_of(path)
                log.info("加载 %s：%s（sha256 %s）", position, path, digest[:16])
                self.models[position] = deep_agent._load_model(position, str(path))
            self.loaded = True
            self.load_seconds = time.perf_counter() - started
            self.warmup()
        except Exception as exc:
            self.last_error = str(exc)
            self.loaded = False
            log.warning("DouZero 不可用，回落启发式：%s", exc)
        return self.loaded

    def warmup(self) -> None:
        """用零观测跑一次前向，把第一次的真实延迟从建议里挪走。"""
        import torch

        for position, model in self.models.items():
            width = 373 if position == "landlord" else 484   # 地主与农民的观测维度不同
            try:
                z = torch.zeros((1, 5, 162), dtype=torch.float32)
                x = torch.zeros((1, width), dtype=torch.float32)
                if self.device:
                    z, x = z.to(self.device), x.to(self.device)
                with torch.no_grad():
                    model.forward(z, x, return_value=True)
            except Exception as exc:  # pragma: no cover - 预热失败不影响正确性
                log.debug("预热 %s 失败（不影响正确性）：%s", position, exc)

    # ----------------------------------------------------------------- 决策

    def act(self, state: GameState) -> Candidate:
        seat = state.advise_seat
        if not self.loaded and not self.load():
            return self._fallback(state, reason="DouZero 未就绪")
        if seat is None or state.landlord is None:
            return self._fallback(state, reason="看不出位置")

        try:
            info = bridge.build_infoset(state, seat)
            position = info.player_position
            model = self.models.get(position)
            if model is None:  # pragma: no cover
                raise RuntimeError(f"没有 {position} 的模型")
            with self._lock:   # 串行化：torch 模块不是为并发推理准备的
                action, value = bridge.choose_action(model, info, device=self.device)
            move = bridge.action_to_move(action, state.hand_for(seat) or (), state.to_beat)
        except Exception as exc:
            log.warning("DouZero 推理失败，回落启发式：%s", exc)
            self.last_error = str(exc)
            return self._fallback(state, reason=f"DouZero 推理失败（{exc}）")

        if move is None:
            return Candidate(None, 0.0, ["DouZero：不出"], ["douzero:pass"], self.name, win_prob=value)
        return Candidate(
            move,
            0.0,
            [f"DouZero 估值 {value:.2f}（{position}）"],
            ["douzero"],
            self.name,
            win_prob=value,
        )

    def bid(self, state: GameState) -> Bid:
        """叫分永远走启发式——DouZero 没有叫分模型（PLAN §0.2）。"""
        return self.fallback.bid(state)

    def _fallback(self, state: GameState, *, reason: str) -> Candidate:
        candidate = self.fallback.act(state)
        candidate.source = self.name
        candidate.reasons = [*candidate.reasons, f"{reason}，已回落启发式"]
        candidate.tags = [*candidate.tags, "fallback:heuristic"]
        return candidate

    # ----------------------------------------------------------------- 运维

    def status(self) -> dict:
        return {
            "name": self.name,
            "loaded": self.loaded,
            "device": self.device or ("cuda" if self._cuda() else "cpu"),
            "load_seconds": self.load_seconds,
            "last_error": self.last_error,
            "positions": sorted(self.models),
        }

    @staticmethod
    def _cuda() -> bool:
        return cuda_available()
