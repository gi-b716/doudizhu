"""按路径装载上游 DouZero 的叶子模块（不用 pip 安装它）。

**为什么按文件路径装载叶子模块、而不是 import 包**：PyPI 上的 douzero 1.1.0 是无依赖声明的
sdist；更要命的是 ``douzero/dmc/__init__.py`` 会 import ``dmc.py`` → ``file_writer.py`` →
``import git``（GitPython，就是那条出名的死依赖）。我们只要 ``dmc/models.py`` 这一个文件，
所以合成空的包对象再按路径 exec，包 ``__init__`` 一次都不执行。装载后逐个断言模块确实来自这份
vendored 副本（commit 记在 ``COMMIT.txt``）。

分层很关键：

- ``douzero.env.*``（game/move_detector/move_generator/move_selector/utils）**不 import torch**
  → :func:`load_env` 在没有 torch 的机器上也能用，Phase 4 的两道正确性闸就靠它；
- ``douzero.dmc.models`` / ``evaluation.deep_agent`` 要 torch → 只由 :func:`load_model_modules`
  在 :func:`torch_available` 为真时加载。

`DDZ_DOUZERO_DIR` 可以指到别的副本（测试与多版本对照用）。
"""

from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType

__all__ = [
    "DouZeroModules", "root_dir", "commit", "is_installed", "torch_available",
    "load_env", "load_model_modules", "status", "ENV_MODULES", "MODEL_MODULES",
]

#: 只需要 python/numpy 的叶子模块（按依赖顺序）。
ENV_MODULES = (
    "douzero.env.utils",
    "douzero.env.move_detector",
    "douzero.env.move_selector",
    "douzero.env.move_generator",
    "douzero.env.game",
    "douzero.env.env",
)
#: 需要 torch 的模块。
MODEL_MODULES = ("douzero.dmc.models", "douzero.evaluation.deep_agent")


def root_dir() -> Path:
    """vendored DouZero 的位置。"""
    override = os.environ.get("DDZ_DOUZERO_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "third_party" / "DouZero"


def commit() -> str | None:
    """当时 clone 的 commit（``COMMIT.txt`` 里记着）。"""
    marker = root_dir() / "COMMIT.txt"
    if not marker.exists():
        return None
    return marker.read_text(encoding="utf-8").strip() or None


def is_installed() -> bool:
    return (root_dir() / "douzero" / "env" / "game.py").exists()


def torch_available() -> bool:
    """torch 装没装（只探测，不 import——import torch 很慢）。"""
    import importlib.util

    return importlib.util.find_spec("torch") is not None


@dataclass(frozen=True, slots=True)
class DouZeroModules:
    """上游那几个模块的句柄。"""

    root: Path
    env: ModuleType          # douzero.env.env —— get_obs() 在这里
    game: ModuleType         # douzero.env.game —— GameEnv/InfoSet/牌码表
    detector: ModuleType     # move_detector —— get_move_type()
    generator: ModuleType    # move_generator —— MovesGener
    selector: ModuleType     # move_selector
    utils: ModuleType        # env/utils.py —— _cards2array 等

    def get_obs(self, infoset):
        """**直接调用上游的 get_obs**，不重写编码器（PLAN §3.4）。"""
        return self.env.get_obs(infoset)


def _ensure_package(name: str, path: Path) -> ModuleType:
    """注册一个**空**的包对象（只提供 __path__），从而绕开上游的 __init__.py。"""
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
    return module


def _load_leaf(name: str, path: Path) -> ModuleType:
    """按文件路径 exec 一个叶子模块（不触发任何 __init__.py）。"""
    import importlib.util

    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"装载不了 {name}（{path}）")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module      # 先注册再执行：模块内部的绝对 import 才找得到
    spec.loader.exec_module(module)
    return module


def _assert_vendored(module: ModuleType, root: Path) -> None:
    """导入后的模块必须来自 vendored 目录，否则说明被同名包顶掉了。"""
    origin = getattr(module, "__file__", "") or ""
    if not Path(origin).resolve().is_relative_to(root.resolve()):
        raise ImportError(
            f"{module.__name__} 来自 {origin}，不是 vendored 的 {root}——"
            "检查是不是 `pip install douzero` 装了个同名包"
        )


@lru_cache(maxsize=4)
def load_env(root: str | None = None) -> DouZeroModules | None:
    """加载不依赖 torch 的那批模块；没 clone 仓库就返回 ``None``。"""
    base = Path(root) if root else root_dir()
    if not (base / "douzero" / "env" / "game.py").exists():
        return None
    _ensure_package("douzero", base / "douzero")
    _ensure_package("douzero.env", base / "douzero" / "env")
    loaded: dict[str, ModuleType] = {}
    for name in ENV_MODULES:
        leaf = base / Path(*name.split(".")).with_suffix(".py")
        module = _load_leaf(name, leaf)
        _assert_vendored(module, base)
        loaded[name] = module
    return DouZeroModules(
        root=base,
        env=loaded["douzero.env.env"],
        game=loaded["douzero.env.game"],
        detector=loaded["douzero.env.move_detector"],
        generator=loaded["douzero.env.move_generator"],
        selector=loaded["douzero.env.move_selector"],
        utils=loaded["douzero.env.utils"],
    )


@lru_cache(maxsize=4)
def load_model_modules() -> tuple[ModuleType, ModuleType] | None:
    """加载要 torch 的两个模块；没有 torch / 没有仓库返回 ``None``。"""
    if not torch_available() or not is_installed():
        return None
    base = root_dir()
    _ensure_package("douzero", base / "douzero")
    _ensure_package("douzero.dmc", base / "douzero" / "dmc")
    _ensure_package("douzero.evaluation", base / "douzero" / "evaluation")
    _ensure_package("douzero.env", base / "douzero" / "env")
    load_env(str(base) if base != root_dir() else None)   # models 会 import 上游常量
    models = _load_leaf("douzero.dmc.models", base / "douzero" / "dmc" / "models.py")
    deep_agent = _load_leaf(
        "douzero.evaluation.deep_agent", base / "douzero" / "evaluation" / "deep_agent.py"
    )
    _assert_vendored(models, base)
    _assert_vendored(deep_agent, base)
    return models, deep_agent


def status() -> dict:
    """给 ``/health`` 与运维看的一览。"""
    from . import weights

    return {
        "installed": is_installed(),
        "root": str(root_dir()),
        "commit": commit(),
        "torch": torch_available(),
        "weights": weights.status(),
        "env_loaded": bool(load_env()) if is_installed() else False,
    }
