"""配置读写 + 部署流程编排。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

from .api import BtApiError, BtClient

CONFIG_DIR = Path.home() / '.bt-deploy'
CONFIG_FILE = CONFIG_DIR / 'config.json'

DEFAULT_TEMP_DIR = '/tmp'

# (值, 界面显示文本)
RESTART_CHOICES = [
    ('none', '不重启（纯静态文件覆盖）'),
    ('webserver_reload', '重载 Web 服务器（Nginx/Apache）'),
    ('service_restart', '重启系统服务'),
    ('java_restart', '重启 Java 项目'),
]
RESTART_LABELS = dict(RESTART_CHOICES)

DEFAULT_CONFIG = {
    'panel_url': '',
    'api_sk': '',
    'verify_ssl': False,
    'targets': [],
}

NEW_TARGET = {
    'name': '新部署目标',
    'source_dirs': [],
    'remote_dir': '',
    'temp_dir': DEFAULT_TEMP_DIR,
    'restart': {'type': 'none'},
}


# ---------------------------------------------------------------------- 配置

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    if not isinstance(cfg['targets'], list):
        cfg['targets'] = []
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    # 里面存着面板 API 密钥，尽量收紧权限（Windows 上 chmod 基本无效，仅作尽力而为）
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ 配置分享

EXPORT_FORMAT = 'bt-deploy/targets'


def export_targets(targets: list, panel_url: str, verify_ssl: bool) -> dict:
    """打包成可分享的 JSON —— 故意不含 api_sk，别人的密钥让人自己填。"""
    return {
        'format': EXPORT_FORMAT,
        'version': 1,
        'panel_url': panel_url,
        'verify_ssl': bool(verify_ssl),
        'targets': [dict(item) for item in targets],
    }


def merge_targets(existing: list, incoming: list) -> tuple[list, int, int]:
    """按名称合并导入的目标：同名覆盖，其余追加。返回 (新列表, 新增数, 覆盖数)。"""
    merged = [dict(item) for item in existing]
    positions = {item.get('name'): pos for pos, item in enumerate(merged)}

    added = replaced = 0
    for item in incoming:
        if not isinstance(item, dict) or not item.get('name'):
            continue
        name = item['name']
        pos = positions.get(name)
        if pos is None:
            positions[name] = len(merged)
            merged.append(dict(item))
            added += 1
        else:
            merged[pos] = dict(item)
            replaced += 1
    return merged, added, replaced


# ------------------------------------------------------------------ 本地打包

def target_sources(target: dict) -> list[str]:
    """部署目标的本地路径列表。兼容早期只有一个 source_dir 的配置。"""
    sources = target.get('source_dirs')
    if isinstance(sources, list):
        return [str(item).strip() for item in sources if str(item).strip()]
    legacy = str(target.get('source_dir') or '').strip()
    return [legacy] if legacy else []


def _add_source(out: zipfile.ZipFile, source: Path, seen: set[str]) -> None:
    """把一个目录/文件/zip 的内容并进目标 zip，同名条目先出现的优先。"""
    if source.is_dir():
        for root, _dirs, files in os.walk(source):
            for name in files:
                full = Path(root) / name
                # 软链接/无权限文件跳过，避免整个部署因为一个文件失败
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    # 必须用 POSIX 分隔符：Windows 下 relative_to 给出反斜杠，
                    # Linux 侧 unzip 会把 'a\\b.js' 整个当成文件名
                    arc = full.relative_to(source).as_posix()
                    if arc in seen:
                        continue
                    seen.add(arc)
                    out.write(full, arc)
                except OSError:
                    continue
    elif source.is_file():
        if source.suffix.lower() == '.zip':
            # 来源要合并，所以把已有 zip 的内容转存进来，不落盘
            with zipfile.ZipFile(source) as inner:
                for info in inner.infolist():
                    if info.is_dir() or info.filename in seen:
                        continue
                    seen.add(info.filename)
                    out.writestr(info, inner.read(info.filename))
        elif source.name not in seen:
            seen.add(source.name)
            out.write(source, source.name)


def build_zip(sources: list[Path], work_dir: Path) -> Path:
    """把多个目录/文件/zip 合并成一个 zip。"""
    sources = [item.resolve() for item in sources]

    # 只有一个 zip 时直接原样上传，省掉解压再压缩
    if len(sources) == 1 and sources[0].is_file() and sources[0].suffix.lower() == '.zip':
        return sources[0]

    zip_path = work_dir / f'btdeploy-{time.strftime("%Y%m%d-%H%M%S")}.zip'
    seen: set[str] = set()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as out:
        for source in sources:
            _add_source(out, source, seen)
    return zip_path


# ------------------------------------------------------------------ 重启动作

def restart_service(client: BtClient, cfg: dict, log) -> None:
    kind = cfg.get('type') or 'none'

    if kind == 'none':
        log('      按配置跳过重启')
    elif kind == 'webserver_reload':
        client.service_admin('webserver', 'reload')
        log('      已重载 Web 服务器')
    elif kind == 'service_restart':
        name = (cfg.get('service_name') or '').strip()
        if not name:
            raise BtApiError('重启方式为「重启系统服务」时必须填写服务名，例如 nginx')
        client.service_admin(name, 'restart')
        log(f'      已重启服务 {name}')
    elif kind == 'java_restart':
        project = (cfg.get('project_name') or '').strip()
        if not project:
            raise BtApiError('重启方式为「重启 Java 项目」时必须填写项目名称')
        client.restart_java_project(project)
        # 面板接口是异步的，只保证"操作已执行"
        log(f'      已下发重启指令：{project}')
    else:
        raise BtApiError(f'未知的重启方式：{kind}')


# ------------------------------------------------------------------ 部署流程

def deploy(client: BtClient, target: dict, log=print) -> None:
    sources = [Path(item).expanduser() for item in target_sources(target)]
    if not sources:
        raise BtApiError('没有配置本地路径')
    missing = [str(item) for item in sources if not item.exists()]
    if missing:
        raise BtApiError('本地路径不存在：' + '、'.join(missing))

    remote_dir = (target.get('remote_dir') or '').strip().rstrip('/')
    if not remote_dir:
        raise BtApiError('远程目标目录不能为空')
    temp_dir = ((target.get('temp_dir') or '').strip() or DEFAULT_TEMP_DIR).rstrip('/')

    with tempfile.TemporaryDirectory(prefix='bt-deploy-') as work:
        log(f'[1/5] 打包 {len(sources)} 个路径')
        for item in sources:
            log(f'      · {item}')
        zip_path = build_zip(sources, Path(work))
        size_mb = zip_path.stat().st_size / 1024 / 1024
        log(f'      → {zip_path.name}（{size_mb:.2f} MB）')

        log(f'[2/5] 上传到 {temp_dir}')
        client.ensure_dir(temp_dir)
        # ponytail: 整个 zip 一次读进内存，几百 MB 以内没问题；再大就得改分片上传
        # 上传名固定用 ASCII：multipart 的 filename 带中文时部分面板版本会乱码
        upload_name = f'btdeploy-{time.strftime("%Y%m%d-%H%M%S")}.zip'
        client.upload_file(temp_dir, upload_name, zip_path.read_bytes())
        log('      上传成功')

        log(f'[3/5] 解压到 {remote_dir}')
        remote_zip = f'{temp_dir}/{upload_name}'
        result = client.unzip(remote_zip, remote_dir)
        failed = result.get('fail') if isinstance(result, dict) else None
        if failed:
            raise BtApiError(f'解压失败：{failed}')
        log('      解压完成')

        log('[4/5] 清理远程临时文件')
        try:
            client.delete(remote_zip)
            log('      已删除')
        except BtApiError as exc:
            log(f'      删除失败（不影响结果，可手动清理）：{exc}')

        log('[5/5] 重启服务')
        restart_service(client, target.get('restart') or {}, log)

    log('部署完成 ✅')