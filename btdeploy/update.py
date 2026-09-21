"""查 GitHub Release 有没有新版本，并把新包下下来。

只用标准库：查 API、下载都走 urllib。Windows 上系统不允许覆盖正在运行的 exe，
所以覆盖这一步交给一个等本进程退出的 PowerShell —— 脚本用 -EncodedCommand 以
UTF-16 传进去，绕开 .cmd 在中文路径上的代码页问题；覆盖失败会弹窗，不然程序
会无声无息地消失。macOS 的 .app 换不了自己，只下载 dmg 交给用户手动装。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .tray import APP_TITLE

REPO = 'MrMao14/bt-deploy'
API_LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
RELEASE_PAGE = f'https://github.com/{REPO}/releases/latest'
TIMEOUT = 30
ASSET_SUFFIX = '.exe' if sys.platform == 'win32' else '.dmg'
USER_AGENT = 'bt-deploy'    # GitHub API 不带 UA 会 403


class UpdateError(Exception):
    """网络不通、release 里没有本平台的包，或者下下来的文件不对劲。"""


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' → (1, 2, 3)；v 前缀和 -beta 之类的后缀直接丢掉。"""
    return tuple(int(item) for item in re.findall(r'\d+', text or ''))


def is_newer(latest: str, current: str = __version__) -> bool:
    """latest 确实比 current 新才返回 True。'v1.2' 与 '1.2.0' 视作同一个版本。"""
    new, old = parse_version(latest), parse_version(current)
    size = max(len(new), len(old))
    return new + (0,) * (size - len(new)) > old + (0,) * (size - len(old))


def _read(url: str) -> bytes:
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'application/vnd.github+json',
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:    # 403 限流、404 没发过 release
        raise UpdateError(f'GitHub 返回 {exc.code}（{exc.reason}）') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f'连不上 GitHub：{exc}') from exc


def latest_release() -> dict:
    """最新 release：{'version': tag 名, 'url': 本平台的包, 'page': 发布页网址}。

    release 里没有本平台的包时 url 为空字符串 —— 调用方提示用户自己去发布页下。
    """
    try:
        payload = json.loads(_read(API_LATEST))
    except json.JSONDecodeError as exc:
        raise UpdateError('GitHub 返回的不是 JSON，可能被网络中间设备拦了') from exc

    version = str(payload.get('tag_name') or '').strip()
    if not version:
        raise UpdateError('这个 release 没有版本号')

    url = ''
    for item in payload.get('assets') or []:
        if str(item.get('name') or '').endswith(ASSET_SUFFIX):
            url = str(item.get('browser_download_url') or '')
            break
    return {'version': version, 'url': url, 'page': str(payload.get('html_url') or RELEASE_PAGE)}


def can_self_update() -> bool:
    """只有 Windows 上跑打包好的 exe 才能自己换掉自己。"""
    return sys.platform == 'win32' and bool(getattr(sys, 'frozen', False))


def _update_path() -> Path:
    """新包下到哪儿：能自更新就放 exe 同目录（同卷才谈得上原子替换），否则放临时目录。"""
    if can_self_update():
        target = Path(sys.executable)
        return target.with_name(f'{target.stem}.update{target.suffix}')
    return Path(tempfile.gettempdir()) / f'bt-deploy{ASSET_SUFFIX}'


def download(url: str) -> Path:
    """下载本平台的新包，返回落盘路径。"""
    dest = _update_path()
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(dest, 'wb') as out:
            shutil.copyfileobj(response, out)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f'下载失败：GitHub 返回 {exc.code}') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f'下载失败：{exc}') from exc

    if dest.stat().st_size == 0:
        raise UpdateError('下载到的是空文件')
    if dest.suffix.lower() == '.exe':
        with dest.open('rb') as fp:
            magic = fp.read(2)
        if magic != b'MZ':
            raise UpdateError('下载到的不是可执行文件（内容可能被网络中间设备换掉了）')
    return dest


def _quote(path: Path) -> str:
    """PowerShell 单引号字符串：内部的单引号要写成两个。"""
    return "'" + str(path).replace("'", "''") + "'"


def apply_update(new_file: Path) -> None:
    """本进程退出后由 PowerShell 把新包覆盖到 exe 位置并重新启动。

    覆盖成不成功都在这条命令里了：失败（比如放在 Program Files 没写权限）会弹窗
    告诉用户新包在哪儿，免得程序就这么没了。调用方发完这行必须立刻退出，否则
    文件一直被占着，替换不会开始。
    """
    target = Path(sys.executable)
    script = (
        f'Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue\n'
        'try {\n'
        f'  Copy-Item -LiteralPath {_quote(new_file)} -Destination {_quote(target)}'
        ' -Force -ErrorAction Stop\n'
        f'  Start-Process -FilePath {_quote(target)}\n'
        '} catch {\n'
        '  Add-Type -AssemblyName System.Windows.Forms\n'
        '  [System.Windows.Forms.MessageBox]::Show('
        f'"替换失败：" + $_.Exception.Message + "`n`n新版本已经下载在：`n{new_file}", "{APP_TITLE}")\n'
        '}\n'
    )
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    subprocess.Popen(
        ['powershell', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
         '-EncodedCommand', encoded],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        close_fds=True,
    )


def open_download(path: Path) -> None:
    """macOS 上把 dmg 挂起来给用户拖；其他平台什么都不做。"""
    if sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
