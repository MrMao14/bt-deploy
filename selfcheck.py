#!/usr/bin/env python3
"""自检：不联网、不需要面板，验证签名算法、multipart 编码、zip 打包和重启分发。

运行：python selfcheck.py
"""

from __future__ import annotations

import email
import hashlib
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from btdeploy.api import BtApiError, BtClient
from btdeploy.deploy import (RESTART_LABELS, build_zip, export_targets,
                             merge_targets, restart_service, target_sources)

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
        (web / 'assets' / 'shared.txt').write_bytes(b'from-dist')

        conf = root / 'conf'
        conf.mkdir()
        (conf / 'app.yml').write_bytes(b'port: 8080')
        (conf / 'assets').mkdir()
        # 与 dist 同名的文件：先出现的来源应该赢
        (conf / 'assets' / 'shared.txt').write_bytes(b'from-conf')

        loose = root / 'robots.txt'
        loose.write_bytes(b'User-agent: *')

        with tempfile.TemporaryDirectory() as work:
            zip_path = build_zip([web, conf, loose], Path(work))
            with zipfile.ZipFile(zip_path) as zf:
                names = sorted(zf.namelist())
                assert names == ['app.yml', 'assets/app.js', 'assets/shared.txt',
                                 'index.html', 'robots.txt'], names
                # 必须是正斜杠：Windows 打包、Linux 解压时反斜杠会变成文件名的一部分
                assert not any('\\' in name for name in names), names
                assert zf.read('index.html') == b'<h1>hi</h1>'
                assert zf.read('assets/shared.txt') == b'from-dist', '同名条目应先出现的优先'

            # 只给一个 zip 时原样直传，不再套一层
            with tempfile.TemporaryDirectory() as work2:
                assert build_zip([zip_path], Path(work2)) == zip_path.resolve()

            # zip 和目录混着来，zip 的内容要合并进去
            with tempfile.TemporaryDirectory() as work3:
                merged = build_zip([web, zip_path], Path(work3))
                with zipfile.ZipFile(merged) as zf:
                    assert 'app.yml' in zf.namelist(), 'zip 来源的内容没被合并'
                    assert 'index.html' in zf.namelist()
    print('✅ zip 打包：多路径合并、POSIX 路径、同名优先级、zip 直传')


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
            dialog._save()
            assert dialog.result is not None, '保存没产生结果'

            app.cfg['targets'] = [dialog.result, dict(dialog.result, name='第二个')]
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
        except tk.TclError as exc:
            print(f'  跳过界面检查（无图形环境：{exc}）')
            return
        finally:
            if app is not None:
                app.destroy()
            deploy.CONFIG_DIR, deploy.CONFIG_FILE = saved
    print('✅ 界面构造与部署后动作字段显隐')


def main():
    check_sign()
    check_url_normalizing()
    check_multipart()
    check_zip()
    check_target_sources()
    check_restart_dispatch()
    check_java_path_pick()
    check_site_and_java_parsing()
    check_config_io()
    check_gui_constructs()
    print('\n全部自检通过。')


if __name__ == '__main__':
    main()