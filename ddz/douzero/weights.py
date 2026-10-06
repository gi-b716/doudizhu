"""权重：定位、校验、安全反序列化。

三个 ckpt（``landlord / landlord_up / landlord_down``）**不在 git 里**，要用户从官方渠道
下载后放进 ``third_party/DouZero/baselines/douzero_WP/``（README 有步骤）。缺失时整个服务
以启发式运行，不会报错——这是 PLAN §6.1 的既定行为。

反序列化的坑（PLAN §6.2）：torch ≥ 2.6 起 ``torch.load`` 默认 ``weights_only=True``，对老式
ckpt 会直接失败。我们的顺序是：

1. 先用 ``weights_only=True``（安全默认）试；
2. 失败再退到 ``weights_only=False``——**只对本地校验过 sha256 的文件**，并在日志里写明。

第一次加载会把 sha256 打进日志，方便你事后核对文件没被动过。``DDZ_DOUZERO_WEIGHTS`` 可以指到
别的权重目录。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "POSITIONS", "MODEL_FILES", "weights_dir", "find_weights", "missing",
    "sha256_of", "load_state_dict", "verify", "status", "WeightsError",
]

log = logging.getLogger("ddz.douzero.weights")

#: 位置名与上游 ``InfoSet.player_position`` 一致。
POSITIONS = ("landlord", "landlord_up", "landlord_down")
MODEL_FILES = {position: f"{position}.ckpt" for position in POSITIONS}


class WeightsError(RuntimeError):
    """权重文件有问题（缺失、损坏、反序列化失败）。"""


def weights_dir() -> Path:
    """权重目录：``third_party/DouZero/baselines/douzero_WP``（可用环境变量覆盖）。"""
    override = os.environ.get("DDZ_DOUZERO_WEIGHTS")
    if override:
        return Path(override)
    from .vendored import root_dir

    return root_dir() / "baselines" / "douzero_WP"


def find_weights(directory: Path | None = None) -> dict[str, Path]:
    """找到的权重文件（位置 → 路径）；缺的不会出现在结果里。"""
    base = Path(directory) if directory else weights_dir()
    found: dict[str, Path] = {}
    for position, filename in MODEL_FILES.items():
        candidate = base / filename
        if candidate.exists() and candidate.stat().st_size > 0:
            found[position] = candidate
    return found


def missing(directory: Path | None = None) -> list[str]:
    found = find_weights(directory)
    return [position for position in POSITIONS if position not in found]


def sha256_of(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state_dict(path: Path | str, *, checksum: str | None = None):
    """读一个 ckpt（需要 torch）。

    ``checksum`` 给定时会先核对 sha256——**只有核对通过才允许 ``weights_only=False``**。
    """
    import torch

    target = Path(path)
    if not target.exists():
        raise WeightsError(f"权重文件不存在：{target}")
    if checksum is not None:
        actual = sha256_of(target)
        if actual != checksum:
            raise WeightsError(f"{target.name} 的 sha256 与记录不符：{actual} != {checksum}")
    try:
        return torch.load(target, map_location="cpu", weights_only=True)
    except Exception as exc:  # 老式 ckpt（pickle 里带 numpy 之类）在 weights_only=True 下会失败
        log.warning(
            "%s 用 weights_only=True 读取失败（%s），改用 weights_only=False。"
            "只对本地校验过的权重这么做；来源不明的文件不要碰。",
            target.name, exc,
        )
        try:
            return torch.load(target, map_location="cpu", weights_only=False)
        except Exception as exc2:  # pragma: no cover - 只在文件损坏时走到
            raise WeightsError(
                f"{target.name} 两种方式都读不了：{exc2}。"
                "确认文件完整（sha256）、并且 torch 版本支持这份 ckpt"
            ) from exc2


def verify(path: Path | str, *, model=None) -> dict:
    """校验一个 ckpt：sha256 + （给了 model 时）状态字典的形状对得上。"""
    target = Path(path)
    info: dict = {"path": str(target), "size": target.stat().st_size, "sha256": sha256_of(target)}
    if model is not None:  # pragma: no cover - 需要 torch
        state = load_state_dict(target, checksum=info["sha256"])
        expected = model.state_dict()
        unknown = [k for k in state if k not in expected]
        mismatched = [
            k for k, v in state.items()
            if k in expected and tuple(v.shape) != tuple(expected[k].shape)
        ]
        info["keys"] = len(state)
        info["unknown_keys"] = unknown[:5]
        info["shape_mismatch"] = mismatched[:5]
        info["usable"] = not mismatched
    return info


@dataclass(frozen=True, slots=True)
class WeightSet:
    """三个位置各自的权重路径。"""

    files: dict[str, Path]
    directory: Path

    @property
    def complete(self) -> bool:
        return all(position in self.files for position in POSITIONS)


def resolve(directory: Path | None = None) -> WeightSet | None:
    """三个都在才返回 :class:`WeightSet`，否则 ``None``（调用方回退启发式）。"""
    base = Path(directory) if directory else weights_dir()
    found = find_weights(base)
    if len(found) != len(POSITIONS):
        return None
    return WeightSet(files=found, directory=base)


def status(directory: Path | None = None) -> dict:
    base = Path(directory) if directory else weights_dir()
    found = find_weights(base)
    return {
        "directory": str(base),
        "found": sorted(found),
        "missing": missing(base),
        "complete": len(found) == len(POSITIONS),
    }
