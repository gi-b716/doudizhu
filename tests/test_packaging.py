"""一键包生成器：钉住两条踩过的坑，别哪天顺手改回去。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))

import build_bundle  # noqa: E402


def test_pth_files_are_never_copied():
    """**必须**排除 ``*.pth``。

    本项目是 editable 安装的，site-packages 里有个指向开发机 ``D:\doudizhu`` 的
    ``_editable_impl_ddz_copilot.pth``；抄进包里会让目标机器从开发机路径导入 ``ddz``
    （那儿没有），或者更糟——悄悄导入一份过期的源码。包里靠 PYTHONPATH 找模块，不需要它。
    """
    assert ".pth" in build_bundle.SKIP_FILE_SUFFIXES
    assert ".pyc" in build_bundle.SKIP_FILE_SUFFIXES


def test_lite_bundle_skips_torch():
    """``--no-douzero`` 要真的把 torch 剔掉（487MB，包体积差五倍）。"""
    assert "torch" in build_bundle.TORCH_NAMES
    assert any(name.startswith("torch") for name in build_bundle.TORCH_NAMES)
