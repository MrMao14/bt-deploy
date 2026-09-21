#!/usr/bin/env python3
"""自检：不联网、不需要面板，验证签名算法、multipart 编码、zip 打包和重启分发。

运行：python selfcheck.py
"""

from __future__ import annotations

import email
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from btdeploy.api import BtApiError, BtClient
from btdeploy.deploy import (CLOSE_LABELS, RESTART_LABELS, _migrate_config, build_zip,
                             export_targets, merge_targets, new_panel,
                             restart_service, target_sources)
from btdeploy import tray, update

# Windows 控制台默认用 GBK，直接打印 emoji 会 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def _body(part) -> bytes:
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else (data or '').encode()


def check_sign():
    """宝塔是双重 MD5：md5(request_time + md5(api_sk))。"""
    client = BtClient('https://panel.example.com:8888', 'ABC123')
    signed = client.sign()

    assert set(signed) == {'request_time', 'request_token'}, signed
    assert signed['request_time'].isdigit(), signed['request_time']
    expected = hashlib.md5(
        (signed['request_time'] + hashlib.md5(b'ABC123').hexdigest()).encode()
    ).hexdigest()
    assert signed['request_token'] == expected, '签名公式不对'
    assert len(signed['request_token']) == 32
    print('✅ 签名：md5(request_time + md5(api_sk))')


def check_url_normalizing():
    assert BtClient('1.2.3.4:8888', 'k').base == 'https://1.2.3.4:8888'
    assert BtClient('http://1.2.3.4:8888/', 'k').base == 'http://1.2.3.4:8888'
    assert BtClient('  https://a.com  ', 'k').base == 'https://a.com'

    for bad_url, bad_key in (('', 'k'), ('   ', 'k'), ('https://a.com', '')):
        try:
            BtClient(bad_url, bad_key)
        except BtApiError:
            continue
        raise AssertionError(f'({bad_url!r}, {bad_key!r}) 应该报错')
    print('✅ 面板地址规范化与参数校验')


def check_multipart():
    """上传接口是 multipart，且签名参数必须放在 FormData 字段里。"""
    client = BtClient('http://127.0.0.1:8888', 'KEY')
    seen = {}

    def fake_open(req):
        seen['body'] = req.data
        seen['ctype'] = req.get_header('Content-type')
        return b'{"status": true, "msg": "ok"}'

    client._open = fake_open
    payload = b'PK\x03\x04fake-zip-bytes'
    client.upload_file('/tmp', 'btdeploy-1.zip', payload)

    assert seen['ctype'].startswith('multipart/form-data; boundary='), seen['ctype']
    raw = f"Content-Type: {seen['ctype']}\r\n\r\n".encode() + seen['body']
    message = email.message_from_bytes(raw)
    assert message.is_multipart(), '请求体不是合法的 multipart'

    parts = {}
    for part in message.walk():
        name = part.get_param('name', header='content-disposition')
        if name:
            parts[name] = part

    assert _body(parts['action']) == b'UploadFile', parts.get('action')
    assert _body(parts['path']) == b'/tmp'
    assert parts['zunfile'].get_filename() == 'btdeploy-1.zip'
    assert _body(parts['zunfile']) == payload, '文件字节被改动了'
    for field in ('request_time', 'request_token'):
        assert field in parts, f'{field} 必须以 FormData 字段方式发送'
    print('✅ multipart 上传体：字段、文件名、二进制内容')


def check_zip():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        web = root / 'dist'
        (web / 'assets').mkdir(parents=True)
        (web / 'index.html').write_bytes(b'<h1>hi</h1>')
        (web / 'assets' / 'app.js').write_bytes(b'console.log(1)')

        lib = root / 'lib'
        (lib / 'nested').mkdir(parents=True)
        (lib / 'a.jar').write_bytes(b'jar-a')
        (lib / 'nested' / 'b.jar').write_bytes(b'jar-b')

        loose = root / 'robots.txt'
        loose.write_bytes(b'User-agent: *')

        # 默认保留目录名：选 .../lib 就打进 lib/...，解压后是 目标目录/lib/...
        with tempfile.TemporaryDirectory() as work:
            zip_path = build_zip([lib, loose], Path(work))
            with zipfile.ZipFile(zip_path) as zf:
                names = sorted(zf.namelist())
                assert names == ['lib/a.jar', 'lib/nested/b.jar', 'robots.txt'], names
                # 必须是正斜杠：Windows 打包、Linux 解压时反斜杠会变成文件名的一部分
                assert not any('\\' in name for name in names), names
                assert zf.read('lib/nested/b.jar') == b'jar-b'

        # 关掉 keep_root 就只把目录里的内容铺到目标目录
        with tempfile.TemporaryDirectory() as work:
            zip_path = build_zip([lib, loose], Path(work), keep_root=False)
            with zipfile.ZipFile(zip_path) as zf:
                names = sorted(zf.namelist())
                assert names == ['a.jar', 'nested/b.jar', 'robots.txt'], names

        # 多个来源各有各的顶层目录，同名文件天然不会撞
        with tempfile.TemporaryDirectory() as work:
            both = build_zip([web, lib], Path(work))
            with zipfile.ZipFile(both) as zf:
                names = sorted(zf.namelist())
                assert names == ['dist/assets/app.js', 'dist/index.html',
                                 'lib/a.jar', 'lib/nested/b.jar'], names

        # 展开模式下同名条目以先出现的来源为准
        conf = root / 'conf'
        (conf / 'assets').mkdir(parents=True)
        (conf / 'assets' / 'app.js').write_bytes(b'from-conf')
        (conf / 'index.html').write_bytes(b'<h1>conf</h1>')
        with tempfile.TemporaryDirectory() as work:
            flat = build_zip([web, conf], Path(work), keep_root=False)
            with zipfile.ZipFile(flat) as zf:
                assert zf.read('assets/app.js') == b'console.log(1)', '先出现的来源应该赢'
                assert zf.read('index.html') == b'<h1>hi</h1>'

        # 单个 zip 原样直传，不再套一层
        with tempfile.TemporaryDirectory() as work:
            zip_path = build_zip([lib], Path(work))
            with tempfile.TemporaryDirectory() as work2:
                assert build_zip([zip_path], Path(work2)) == zip_path.resolve()

            # zip 和目录混着来，zip 的内容要合并进去
            with tempfile.TemporaryDirectory() as work3:
                merged = build_zip([web, zip_path], Path(work3))
                with zipfile.ZipFile(merged) as zf:
                    names = sorted(zf.namelist())
                    assert 'dist/index.html' in names, names
                    assert 'lib/a.jar' in names, 'zip 来源的内容没被合并'
    print('✅ zip 打包：保留目录名 / 展开两种模式、POSIX 路径、zip 直传')


def check_target_sources():
    assert target_sources({'source_dirs': ['a', ' b ', '']}) == ['a', 'b']
    assert target_sources({'source_dir': '/old/path'}) == ['/old/path']
    assert target_sources({'source_dirs': ['new'], 'source_dir': '/old'}) == ['new']
    assert target_sources({}) == []
    assert target_sources({'source_dirs': 'not-a-list'}) == []
    print('✅ 本地路径列表：多路径与旧配置兼容')


def check_java_path_pick():
    from btdeploy.api import _pick_java_path

    # 拿到 jar 文件路径时要取所在目录 —— 部署目标是目录
    assert _pick_java_path({'project_jar': '/opt/app/app.jar'}) == '/opt/app'
    assert _pick_java_path({'jar_path': '/srv/x.jar'}) == '/srv'
    # 给的本来就是目录时原样返回
    assert _pick_java_path({'path': '/www/wwwroot/demo'}) == '/www/wwwroot/demo'
    assert _pick_java_path({'project_root': '/opt/demo'}) == '/opt/demo'
    # 只给了启动命令时，从命令里把 jar 路径抠出来再取目录
    assert _pick_java_path(
        {'project_cmd': '/www/server/java/jdk1.8/bin/java -jar /opt/demo/app.jar'}
    ) == '/opt/demo'
    assert _pick_java_path({}) == ''
    assert _pick_java_path({'project_cmd': 'java -jar app.jar'}) == ''
    print('✅ Java 项目目录字段解析')

def check_multi_panel():
    """多面板结构 + v1 单面板配置的自动迁移。"""
    # 老版本：整个文件就是一个面板
    migrated = _migrate_config({
        'panel_url': 'https://old:8888', 'api_sk': 'k', 'verify_ssl': True,
        'targets': [{'name': 'a'}],
    })
    assert migrated['active_panel'] == 0, migrated
    assert len(migrated['panels']) == 1, migrated
    panel = migrated['panels'][0]
    assert panel['panel_url'] == 'https://old:8888', panel
    assert panel['api_sk'] == 'k', panel
    assert panel['verify_ssl'] is True, panel
    assert panel['targets'] == [{'name': 'a'}], panel

    # 已经是新版就原样保留
    v2 = _migrate_config({
        'version': 2, 'active_panel': 1,
        'panels': [{'name': 'p1'}, {'name': 'p2', 'targets': [{'name': 't'}]}],
    })
    assert [item['name'] for item in v2['panels']] == ['p1', 'p2'], v2
    assert v2['active_panel'] == 1, v2
    assert v2['panels'][1]['targets'] == [{'name': 't'}], v2

    # 空配置和垃圾配置也得保证有一个能用的面板
    for bad in ({}, None, 'junk', {'panels': []}, {'panels': 'junk'}):
        cfg = _migrate_config(bad)
        assert len(cfg['panels']) == 1, (bad, cfg)
        assert cfg['active_panel'] == 0, (bad, cfg)

    # active_panel 越界要夹回 0
    assert _migrate_config(
        {'panels': [{'name': 'a'}], 'active_panel': 9})['active_panel'] == 0

    assert new_panel('x')['name'] == 'x'
    print('✅ 多面板结构 / v1 单面板配置迁移')


def check_config_io():
    """导出的 JSON 不能带 API 密钥；导入按名称合并。"""
    payload = export_targets([{'name': 'a', 'remote_dir': '/x'}], 'https://1.2.3.4:8888', False)
    assert payload['format'] == 'bt-deploy/targets', payload
    assert payload['targets'] == [{'name': 'a', 'remote_dir': '/x'}], payload
    assert payload['panel_url'] == 'https://1.2.3.4:8888'
    # 关键：密钥绝不能进导出文件
    assert 'api_sk' not in json.dumps(payload), '导出文件里混进了 API 密钥'

    merged, added, replaced = merge_targets(
        [{'name': 'a', 'remote_dir': '/old'}, {'name': 'b'}],
        [{'name': 'a', 'remote_dir': '/new'}, {'name': 'c'}, 'garbage', {}, 42],
    )
    assert [item['name'] for item in merged] == ['a', 'b', 'c'], merged
    assert merged[0]['remote_dir'] == '/new', '同名目标应该被覆盖'
    assert (added, replaced) == (1, 1), (added, replaced)

    # 传进来的列表不该被就地改动
    original = [{'name': 'a', 'remote_dir': '/old'}]
    merge_targets(original, [{'name': 'a', 'remote_dir': '/new'}])
    assert original[0]['remote_dir'] == '/old', 'merge 改动了入参'
    print('✅ 导出不含密钥 / 导入按名称合并')


def check_close_action():
    """关闭窗口行为：老配置按「每次询问」，坏值也得兜住。"""
    assert set(CLOSE_LABELS) == {'ask', 'exit', 'tray'}, CLOSE_LABELS
    assert _migrate_config({})['close_action'] == 'ask'
    assert _migrate_config({'panel_url': 'https://old:8888'})['close_action'] == 'ask'
    for action in ('exit', 'tray'):
        assert _migrate_config({'close_action': action})['close_action'] == action
    # 手改配置或旧文件里塞了怪值，不能把界面带崩
    for bad in ('nonsense', None, 42, ['tray'], {'a': 1}):
        assert _migrate_config({'close_action': bad})['close_action'] == 'ask', bad
    print('✅ 关闭窗口行为的默认值与兜底')


def check_single_instance():
    """单实例互斥体 + 唤醒已有窗口。用自检专名，免得撞上真在跑的本程序。"""
    tray.MUTEX_NAME = 'Local\\BtDeploySelfCheck'
    assert tray.acquire_single_instance() is True, '第一个实例应该拿得到锁'
    if tray.AVAILABLE:
        assert tray.acquire_single_instance() is False, '第二个实例不该拿到锁'
        tray.wake_existing()     # 没有窗口可叫也不能抛
    print('✅ 单实例互斥体 / 唤醒已有窗口')


def check_tray_icon():
    """托盘曾经整条线是死的：窗口类漏了类名，注册必然失败，界面只看到「不支持」。

    没 explorer 的机器（CI）到挂图标那步才会失败，窗口那步过了就算数。"""
    if not tray.AVAILABLE:
        print('  跳过托盘检查（非 Windows）')
        return
    icon = tray.TrayIcon('自检')
    if icon.start():
        icon.stop()
    else:
        assert '添加托盘图标失败' in str(icon.error), f'托盘窗口没建起来：{icon.error}'
    print('✅ 托盘窗口类 / 通知区图标')


def check_version_compare():
    """版本号比较：v 前缀、位数不齐、两位数都不能判错。"""
    assert update.parse_version('v1.2.3') == (1, 2, 3)
    assert update.parse_version('1.2-beta') == (1, 2)
    assert update.parse_version('') == ()
    assert update.is_newer('v1.0.1', '1.0.0')
    assert update.is_newer('v1.10.0', '1.9.9'), '两位数不能按字符串比'
    assert not update.is_newer('v1.2', '1.2.0'), '位数不齐要按 1.2.0 处理'
    assert update.is_newer('v1.2.1', '1.2')
    assert not update.is_newer('v1.0.0', '1.0.0')
    assert not update.is_newer('', '1.0.0'), '拿不到 tag 不能算有新版本'
    assert update.is_newer('v99.0.0'), '默认要拿当前版本比'
    print('✅ 版本号解析与比较')


def check_site_and_java_parsing():
    """用伪造的面板响应验证字段解析 —— 真实面板的字段名没法离线确认。"""
    client = BtClient('http://127.0.0.1:8888', 'KEY')

    client.post_form = lambda path, params: {
        'data': [
            {'id': 1, 'name': 'a.example.com', 'path': '/www/wwwroot/a.example.com'},
            {'id': 2, 'name': 'no-path.example.com'},
            'garbage',
        ]
    }
    assert client.list_sites() == [
        {'name': 'a.example.com', 'path': '/www/wwwroot/a.example.com'}
    ], client.list_sites()

    # 有的面板版本直接返回裸数组，也得能解析
    client.post_form = lambda path, params: [
        {'name': 'b.example.com', 'path': '/www/wwwroot/b.example.com'},
    ]
    assert client.list_sites() == [
        {'name': 'b.example.com', 'path': '/www/wwwroot/b.example.com'}
    ], client.list_sites()

    client.get = lambda path, params=None: {
        'data': [{'name': 'demo', 'project_jar': '/opt/demo/app.jar'}, {'name': 'plain'}],
    }
    assert client.list_java_projects() == [
        {'name': 'demo', 'path': '/opt/demo'},
        {'name': 'plain', 'path': ''},
    ], client.list_java_projects()
    print('✅ 网站 / Java 项目列表字段解析')

def check_restart_dispatch():
    calls = []

    class FakeClient:
        def service_admin(self, name, op):
            calls.append(('service', name, op))

        def restart_java_project(self, project):
            calls.append(('java', project))

    def run(cfg):
        calls.clear()
        restart_service(FakeClient(), cfg, lambda _m: None)
        return list(calls)

    assert run({'type': 'none'}) == []
    assert run({'type': 'webserver_reload'}) == [('service', 'webserver', 'reload')]
    assert run({'type': 'service_restart', 'service_name': 'nginx'}) == \
        [('service', 'nginx', 'restart')]
    assert run({'type': 'java_restart', 'project_name': 'demo'}) == [('java', 'demo')]
    assert run({}) == []

    for bad in ({'type': 'java_restart'}, {'type': 'service_restart'}, {'type': 'nope'}):
        try:
            restart_service(FakeClient(), bad, lambda _m: None)
        except BtApiError:
            continue
        raise AssertionError(f'{bad} 应该报错')
    print('✅ 重启动作分发与缺参校验')


def check_gui_constructs():
    """构造一遍主窗口和编辑对话框，抓布局管理器冲突这类低级错误。"""
    # CI 上没有桌面会话时 Tk 会卡在等窗口可见，用这个开关只跳过界面部分
    if os.environ.get('BTDEPLOY_SKIP_GUI'):
        print('⏭  跳过界面检查（BTDEPLOY_SKIP_GUI）')
        return
    try:
        import tkinter as tk
    except ImportError as exc:
        print(f'⏭  跳过界面检查（{exc}）')
        return

    from btdeploy import deploy
    from btdeploy.gui import App, TargetDialog

    saved = deploy.CONFIG_DIR, deploy.CONFIG_FILE
    with tempfile.TemporaryDirectory() as tmp:
        deploy.CONFIG_DIR = Path(tmp)
        deploy.CONFIG_FILE = Path(tmp) / 'config.json'
        app = None
        try:
            app = App()
            app.update()
            dialog = TargetDialog(app, {
                'name': '检查',
                'source_dirs': [str(Path(__file__).parent), tempfile.gettempdir()],
                'remote_dir': '/tmp/check', 'temp_dir': '/tmp',
                'restart': {'type': 'java_restart', 'project_name': 'demo'},
            }, data_loader=lambda: {
                'dirs': ['/www/wwwroot/site-a', '/www/wwwroot/site-b'],
                'java_projects': [{'name': 'demo', 'path': '/opt/demo'},
                                  {'name': 'other', 'path': ''}],
            })
            dialog.update()
            assert dialog.list_src.get(0, 'end') == \
                (str(Path(__file__).parent), tempfile.gettempdir()), \
                f'本地路径列表不对：{dialog.list_src.get(0, "end")}'
            assert dialog.row_project[1].winfo_manager() == 'grid', 'Java 项目名行应显示'
            assert dialog.row_service[1].winfo_manager() == '', '服务名行应隐藏'

            # 后台线程拉完项目名后，下拉里应该有可选项目
            for _ in range(50):
                dialog.update()
                if dialog.combo_project.cget('values'):
                    break
                time.sleep(0.02)
            assert 'demo' in dialog.combo_project.cget('values'), \
                f'Java 项目下拉没被填充：{dialog.combo_project.cget("values")}'
            assert dialog.combo_dst.cget('values') == ('/www/wwwroot/site-a', '/www/wwwroot/site-b'), \
                f'远程目录候选没被填充：{dialog.combo_dst.cget("values")}'

            # 选中 Java 项目应自动把它的目录填进远程目录
            dialog.var_project.set('demo')
            dialog._on_project_selected()
            assert dialog.var_dst.get() == '/opt/demo', dialog.var_dst.get()

            dialog.var_kind.set(RESTART_LABELS['webserver_reload'])
            dialog._sync_restart_fields()
            dialog.update()
            assert dialog.row_project[1].winfo_manager() == '', '切换动作后该行应隐藏'
            assert dialog.row_service[1].winfo_manager() == '', '切换动作后该行应隐藏'

            # _save 会销毁对话框，顺便拿到 result 做列表渲染检查
            assert dialog.var_keep_root.get() is True, '默认应该保留目录名'
            dialog.var_keep_root.set(False)
            dialog._save()
            assert dialog.result is not None, '保存没产生结果'
            assert dialog.result['keep_root'] is False, dialog.result

            app.panel()['targets'] = [dialog.result, dict(dialog.result, name='第二个')]
            app._refresh_tree()
            app.update()

            rows = app.tree.get_children()
            assert len(rows) == 2, rows
            assert app.tree.item(rows[0], 'values')[0] == '☐', app.tree.item(rows[0], 'values')
            assert app.tree.item(rows[0], 'values')[1] == '检查', app.tree.item(rows[0], 'values')

            # 勾选切换
            app._picked.add(0)
            app._refresh_tree()
            app.update()
            assert app.tree.item(rows[0], 'values')[0] == '☑', app.tree.item(rows[0], 'values')
            assert app.tree.item(rows[1], 'values')[0] == '☐', app.tree.item(rows[1], 'values')

            # 上移 / 下移：列表顺序就是批量部署的顺序
            assert [t['name'] for t in app.panel()['targets']] == ['检查', '第二个']
            app.tree.selection_set('1')          # 选中第二条，把它挪上去
            app._move_target(-1)
            app.update()
            assert [t['name'] for t in app.panel()['targets']] == ['第二个', '检查'], \
                app.panel()['targets']
            # 勾选是按下标记的，位置一换就得跟着走，否则勾的会变成另一条
            assert app._picked == {1}, app._picked
            assert app.tree.item(app.tree.get_children()[1], 'values')[0] == '☑'

            # 已经在头尾就不动
            app.tree.selection_set('0')
            app._move_target(-1)
            assert [t['name'] for t in app.panel()['targets']] == ['第二个', '检查']
            app.tree.selection_set('1')
            app._move_target(1)
            assert [t['name'] for t in app.panel()['targets']] == ['第二个', '检查']

            # 多面板：切换面板后列表和输入框都要跟着换
            app.var_url.set('https://panel-1:8888')
            app._persist_panel()
            app.cfg['panels'].append(new_panel('第二台'))
            app.cfg['active_panel'] = 1
            app._load_active_panel()
            app.update()
            assert app.var_panel.get() == '第二台', app.var_panel.get()
            assert app.var_url.get() == '', '新面板的地址应该是空的'
            assert app.tree.get_children() == (), '新面板不该有部署目标'

            app.var_url.set('https://panel-2:9999')
            app._persist_panel()
            assert app.cfg['panels'][1]['panel_url'] == 'https://panel-2:9999'
            assert app.cfg['panels'][0]['panel_url'] == 'https://panel-1:8888', \
                '写串面板了 —— 面板配置必须互相独立'

            # 切回第一个面板，内容应该原样还在
            app.var_panel.set('默认面板')
            app._on_panel_switch()
            app.update()
            assert app.var_url.get() == 'https://panel-1:8888', app.var_url.get()
            assert len(app.tree.get_children()) == 2, app.tree.get_children()

            # 关闭窗口行为：默认「每次询问」，下拉框改完要落到配置里
            assert app.combo_close.get() == '每次询问', app.combo_close.get()
            app.combo_close.set('最小化到托盘')
            app._on_close_action_changed()
            assert app.cfg['close_action'] == 'tray', app.cfg
            app._sync_close_action()
            assert app.combo_close.get() == '最小化到托盘', app.combo_close.get()

            # 选「缩到托盘」时关窗口不该退出。真建图标要 Windows 桌面，这里只验分支
            hidden = []
            app._hide_to_tray = lambda: hidden.append(True)
            app._on_close()
            assert hidden == [True], '关窗口没走托盘分支'
            assert app.winfo_exists(), '缩到托盘时窗口不该销毁'

            # 选「退出程序」时关窗口要真的走销毁 —— 这条放最后，之后窗口就没了
            app.cfg['close_action'] = 'exit'
            app._on_close()
            try:
                alive = bool(app.winfo_exists())
            except tk.TclError:
                alive = False
            assert not alive, '关窗口没退出'
        except tk.TclError as exc:
            print(f'  跳过界面检查（无图形环境：{exc}）')
            return
        finally:
            if app is not None:
                try:
                    app.destroy()     # 上面「退出程序」那条已经销毁过一次了
                except tk.TclError:
                    pass
            deploy.CONFIG_DIR, deploy.CONFIG_FILE = saved
    print('✅ 界面构造 / 部署后动作字段显隐 / 关闭窗口行为')


def main():
    check_sign()
    check_url_normalizing()
    check_multipart()
    check_zip()
    check_target_sources()
    check_restart_dispatch()
    check_java_path_pick()
    check_site_and_java_parsing()
    check_multi_panel()
    check_config_io()
    check_close_action()
    check_single_instance()
    check_tray_icon()
    check_version_compare()
    check_gui_constructs()
    print('\n全部自检通过。')


if __name__ == '__main__':
    main()