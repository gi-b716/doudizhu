"""生成一键包：**自带 Python + 依赖 + 油猴脚本**，拷到没有任何环境的 Windows 机器上双击就能用。

    uv run python packaging/build_bundle.py                 # 默认连 DouZero 一起（torch + 权重）
    uv run python packaging/build_bundle.py --no-douzero    # 只带启发式，包小一大截
    uv run python packaging/build_bundle.py --zip           # 顺手压成 zip

它**不改项目结构**，只是把现有的东西抄一份摆成能独立运行的形状：

    斗地主Copilot/
    ├─ 启动服务.bat          ← 双击这个
    ├─ python/               ← 便携 CPython（uv 下载的 python-build-standalone）
    ├─ vendor/               ← 依赖（从本机 .venv 的 site-packages 抄）
    ├─ ddz/                  ← 应用源码（原样拷，相对路径全都不变）
    ├─ third_party/DouZero/  ← 权重与上游代码（--no-douzero 时不要）
    ├─ userscript/           ← 油猴脚本
    ├─ data/                 ← 运行时写在这里（对局归档）
    └─ 说明.txt

两个关键点，都是踩过才知道的：

1. **一定要排除 ``*.pth``**。本项目是 editable 安装的，site-packages 里有个
   ``_editable_impl_ddz_copilot.pth`` 指向开发机的 ``D:\\doudizhu``；抄过去会让目标机器
   从开发机的路径导入 ``ddz``（那儿根本没有），或者更糟——悄悄导入一份过期的源码。
   包里靠 ``PYTHONPATH`` 找模块，本来就不需要 .pth。
2. **便携 Python 只装一次**：目标机不需要任何环境，也不需要联网，因为解释器、依赖、
   源码全在包里。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUNDLE_NAME = "斗地主Copilot"

#: 抄依赖时不要的东西。``*.pth`` 见模块开头那条注释；``__pycache__`` 只是省体积。
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules"}
SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".pth")
#: torch 及其专属依赖（--no-douzero 时跳过）
TORCH_NAMES = {
    "torch", "torchgen", "functorch", "sympy", "mpmath", "networkx", "filelock",
    "fsspec", "jinja2", "MarkupSafe", "markupsafe",
}


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def tree_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def copy_tree(src: Path, dst: Path, *, skip_names: set[str] = frozenset()) -> int:
    """抄目录（跳过 ``__pycache__``/``*.pth`` 之类），返回抄了多少字节。"""
    copied = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in skip_names]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith(SKIP_FILE_SUFFIXES) or name in skip_names:
                continue
            source = Path(root) / name
            try:
                target = dst / rel / name
                shutil.copy2(source, target)
                copied += source.stat().st_size
            except OSError as exc:  # 个别文件被占用/权限问题不该让整包失败
                print(f"    ! 跳过 {source.name}（{exc}）")
    return copied


def install_python(target: Path) -> Path:
    """把便携 CPython 装进 ``target``，返回 ``python.exe`` 的路径。"""
    existing = sorted(target.glob("*/python.exe")) or sorted(target.glob("python.exe"))
    if existing:
        print(f"  已有便携 Python：{existing[0].relative_to(target.parent)}")
        return existing[0]

    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    print(f"  下载便携 Python {version}（python-build-standalone，目标机不需要联网）...")
    result = subprocess.run(
        ["uv", "python", "install", version, "--install-dir", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"装便携 Python 失败：{result.stderr.strip() or result.stdout.strip()}")
    found = sorted(target.glob("*/python.exe")) or sorted(target.glob("python.exe"))
    if not found:
        raise SystemExit(f"装完了却没找到 python.exe，看看 {target} 里有什么")
    return found[0]


def write_bat(path: Path, python_rel: str, has_douzero: bool) -> None:
    engines = "启发式 + DouZero" if has_douzero else "启发式（本包不含 DouZero）"
    content = f"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 斗地主 Copilot 本地服务

if not exist "{python_rel}" (
    echo [错误] 找不到 {python_rel} —— 这个包不完整，请重新生成或重新拷贝。
    pause
    exit /b 1
)

netstat -ano | findstr ":17876" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo 服务已经在跑了（端口 17876 被占用），不用再启动一个。
    echo 要重启的话：先关掉那个跑着服务的窗口，再双击本文件。
    pause
    exit /b 0
)

echo.
echo   斗地主 Copilot 本地服务
echo   引擎：{engines}
echo.
echo   这个窗口**不要关**（关了浮窗就收不到建议）。
echo   服务地址 http://127.0.0.1:17876 —— 浏览器里打开对局页即可。
echo.
set "PYTHONPATH=%~dp0;%~dp0vendor"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
"{python_rel}" -m ddz.service

echo.
echo 服务已退出（窗口可以关了）。
pause
"""
    path.write_text(content, encoding="utf-8")


README = """斗地主 Copilot —— 一键包
================================

一、启动服务
   双击「启动服务.bat」，会弹出一个黑窗口，里面出现
       Uvicorn running on http://127.0.0.1:17876
   就是好了。**这个窗口不要关**，关了浮窗就没有建议了。

二、装浮窗（只需要做一次）
   1. 浏览器装 Tampermonkey（篡改猴）扩展；
   2. 点 Tampermonkey 图标 → 「添加新脚本」→ 把 userscript 文件夹里的
      ddz-copilot.user.js 用记事本打开、内容整个粘进去 → Ctrl+S 保存；
   3. 打开斗地主对局页，右下角会出现浮窗。

三、怎么用
   · 「自动打牌」管叫分和出牌，「自动续局」管终局后自己点「再来一局」，
     两个开关各自独立，默认都开。
   · 浮窗标题栏有版本号，用来确认脚本更新成功了没有。
     **改完脚本要刷新对局页才生效。**
   · 浮窗默认用启发式；点「DouZero」切到更强的引擎（本包是否含 DouZero 见下）。
   · 「帮我选中」只替你在手牌上做选择，点不点出牌由你自己决定。

四、本包内容
   {douzero_line}

五、常见问题
   · 浮窗显示「服务未启动」→ 那个黑窗口是不是被关了？重新双击「启动服务.bat」。
   · 浮窗一直显示「未连接」→ 说明探活失败，同上。
   · 端口被占用 → 上一份服务还在跑，找到那个窗口关掉，或者直接用着。
   · 换台电脑：整个文件夹拷过去就行，不需要装 Python、不需要联网。
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_bundle", description="生成一键包")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    parser.add_argument("--no-douzero", action="store_true", help="不带 torch 与 DouZero（包小很多）")
    parser.add_argument("--zip", action="store_true", help="打完压成 zip")
    parser.add_argument("--force", action="store_true", help="输出目录已存在也重建")
    args = parser.parse_args(argv)

    has_douzero = not args.no_douzero
    douzero_src = ROOT / "third_party" / "DouZero"
    if has_douzero and not douzero_src.exists():
        print("! 本机没有 third_party/DouZero，这次按 --no-douzero 处理")
        has_douzero = False

    out_root = Path(args.out).resolve()
    bundle = out_root / BUNDLE_NAME
    if bundle.exists():
        if not args.force:
            raise SystemExit(f"{bundle} 已存在；要重建请加 --force（会先删掉它）")
        print(f"清掉旧的 {bundle} ...")
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    started = time.perf_counter()
    print(f"生成到 {bundle}\n")

    print("[1/6] 便携 Python")
    python = install_python(bundle / "python")
    python_rel = str(python.relative_to(bundle))

    print("[2/6] 依赖（从本机 .venv 抄）")
    site_packages = Path(sysconfig.get_paths()["purelib"])
    skip = set() if has_douzero else {name.lower() for name in TORCH_NAMES}
    size = copy_tree(site_packages, bundle / "vendor", skip_names=skip)
    print(f"  {human(size)}{'（已跳过 torch 及其专属依赖）' if skip else ''}")

    print("[3/6] 应用源码")
    size = copy_tree(ROOT / "ddz", bundle / "ddz")
    print(f"  {human(size)}")

    if has_douzero:
        print("[4/6] DouZero（上游代码 + 权重）")
        size = copy_tree(douzero_src, bundle / "third_party" / "DouZero")
        print(f"  {human(size)}")

    print("[5/6] 油猴脚本")
    shutil.copytree(ROOT / "userscript", bundle / "userscript",
                    ignore=shutil.ignore_patterns(*SKIP_DIRS))
    (bundle / "data").mkdir(exist_ok=True)

    print("[6/6] 启动器与说明")
    write_bat(bundle / "启动服务.bat", python_rel, has_douzero)
    (bundle / "说明.txt").write_text(
        README.format(
            douzero_line=("· 含 DouZero（torch + 权重），浮窗上可以切过去。"
                          if has_douzero else
                          "· **不含 DouZero**：服务只用启发式，功能照常，只是棋力弱一些。")
        ),
        encoding="utf-8",
    )

    total = tree_size(bundle)
    print(f"\n完成：{bundle}")
    print(f"体积：{human(total)}（{sum(1 for _ in bundle.rglob('*') if _.is_file())} 个文件）")
    print("目标机：整个文件夹拷过去 → 双击「启动服务.bat」→ 按 说明.txt 装浮窗")

    if args.zip:
        archive = out_root / f"{BUNDLE_NAME}.zip"
        print(f"\n打包 {archive} ...")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(out_root))
        print(f"完成：{archive}（{human(archive.stat().st_size)}）")

    print(f"\n用时 {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
