"""油猴脚本的 nextFrame() 必须在**后台标签页**里也能返回。

这是真实对局里最贵的一条 bug：``selectRecommended()`` 选完每张牌都要 ``await nextFrame()``，
而 ``nextFrame`` 原来是纯 ``requestAnimationFrame``——页面在后台标签里 rAF 一次都不触发，
于是点击序列永远停在第一张牌上，平台 15 秒（叫分）/ 30 秒（出牌）计时走完就把我们**托管**，
剩下整局由平台机器人代打（托管不会自己结束）。用户归档的 120 局里有 12 局是这样：净 −44 分、
−3.67/局，同期自己打的 108 局只有 −0.72/局；12 局的 auto_on 全部带 timeout，卡住的间隔
精确等于 30.0 秒 / 15.0 秒。

没有 node 时跳过（与 tests/test_platform_parity.py 同一套约定）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "userscript_frame.js"
USERSCRIPT = ROOT / "userscript" / "ddz-copilot.user.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="没装 node，跳过油猴脚本的对照")


def run_frame(mode: str) -> dict:
    """在 node 里跑一次 nextFrame()，取回它有没有 resolve、用了多久、rAF 被调了几次。"""
    out = subprocess.run(
        [NODE, str(HARNESS), str(USERSCRIPT), mode],
        capture_output=True, text=True, timeout=20, check=True,
    )
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert "error" not in data, data
    return data


def test_hidden_tab_still_resolves():
    """后台标签页（rAF 不触发）：nextFrame 必须靠兜底定时器返回，而不是永远挂着。"""
    data = run_frame("hidden")
    assert data["mode"] == "hidden"
    assert data["rafCalls"] >= 1, "应该先试 rAF（前台时它更快）"
    assert data["resolved"] is True, "后台标签页里 nextFrame 卡死 = 整局会被平台托管"
    assert data["ms"] < 500, data


def test_visible_tab_resolves_fast():
    """前台：rAF 触发，行为与改动前一致。"""
    data = run_frame("visible")
    assert data["resolved"] is True
    assert data["ms"] < 500, data
