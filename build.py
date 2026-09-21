#!/usr/bin/env python3
"""用 PyInstaller 打包成单文件可执行程序。

    pip install -r requirements.txt
    python build.py

产物：
    Windows   dist/bt-deploy.exe
    macOS     dist/bt-deploy.app（可执行文件在 .app/Contents/MacOS/bt-deploy）

注意：PyInstaller 不能交叉编译，Windows 包必须在 Windows 上打，mac 包必须在 mac 上打。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认不是 UTF-8（CI 的 runner 上甚至是 cp1252），直接打印中文会 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent

# 单文件体积基本由内置的 tcl/tk 决定，能砍的主要是这些用不到的第三方大库
EXCLUDES = [
    'numpy', 'pandas', 'matplotlib', 'scipy', 'PIL', 'cv2',
    'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'jupyter',
    'pytest', 'sphinx', 'setuptools',
]


def human_size(path: Path) -> str:
    if path.is_dir():
        total = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    else:
        total = path.stat().st_size
    return f'{total / 1024 / 1024:.1f} MB'


def main() -> int:
    if importlib.util.find_spec('PyInstaller') is None:
        print('未检测到 PyInstaller，请先执行：pip install -r requirements.txt')
        return 1

    args = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean',
        '--onefile',
        '--windowed',
        '--name', 'bt-deploy',
    ]
    for module in EXCLUDES:
        args += ['--exclude-module', module]
    args.append('main.py')

    print('执行：', ' '.join(args), flush=True)
    subprocess.check_call(args, cwd=ROOT)

    dist = ROOT / 'dist'
    print('\n打包完成，产物在 dist/：')
    for item in sorted(dist.iterdir()):
        if item.name.endswith('.spec'):
            continue
        print(f'  {item.name}  {human_size(item)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())