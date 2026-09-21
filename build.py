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
import tempfile
import time
from pathlib import Path

from btdeploy import __author__, __version__

# Windows 控制台默认不是 UTF-8（CI 的 runner 上甚至是 cp1252），直接打印中文会 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent

NAME = 'bt-deploy'
PRODUCT = '宝塔部署助手'
ICON = ROOT / 'assets' / 'icon.png'    # 由 assets/icon.svg 导出，PyInstaller 负责转成 ico / icns

# 单文件体积基本由内置的 tcl/tk 决定，能砍的主要是这些用不到的第三方大库
EXCLUDES = [
    'numpy', 'pandas', 'matplotlib', 'scipy', 'PIL', 'cv2',
    'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'jupyter',
    'pytest', 'sphinx', 'setuptools',
]


VERSION_TEMPLATE = '''VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v4}, prodvers={v4},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', '{author}'),
        StringStruct('FileDescription', '{product}'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{name}'),
        StringStruct('LegalCopyright', '\u00a9 {year} {author}'),
        StringStruct('OriginalFilename', '{name}.exe'),
        StringStruct('ProductName', '{product}'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)'''


def write_version_file() -> Path:
    """给 Windows exe 写版本资源：右键属性里的署名、版本号、版权就来自这里。"""
    numbers = [int(item) for item in __version__.split('.') if item.isdigit()][:4]
    numbers += [0] * (4 - len(numbers))
    text = VERSION_TEMPLATE.format(
        v4=tuple(numbers), version=__version__, author=__author__,
        product=PRODUCT, name=NAME, year=time.strftime('%Y'),
    )
    path = Path(tempfile.gettempdir()) / f'{NAME}-version.txt'
    path.write_text(text, encoding='utf-8')
    return path


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

    if not ICON.exists():
        print(f'缺少图标：{ICON}（先用 assets/icon.svg 导出 PNG，见 README）')
        return 1

    args = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean',
        '--onefile',
        '--windowed',
        '--name', NAME,
        '--icon', str(ICON),
    ]
    if sys.platform == 'win32':
        args += ['--version-file', str(write_version_file())]
    elif sys.platform == 'darwin':
        args += ['--osx-bundle-identifier', f'com.{__author__}.{NAME}']
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