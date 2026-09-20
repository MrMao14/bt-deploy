"""宝塔面板 API 客户端。

只用标准库实现，PyInstaller 打包体积最小、无第三方依赖风险。
接口依据 https://docs.bt.cn/api （面板 v11.7.0 实测）。
"""

from __future__ import annotations

import hashlib
import json
import re
import posixpath
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_TIMEOUT = 600


class BtApiError(Exception):
    """面板返回 status=false，或网络层失败。"""

# Java 项目的目录字段各面板版本叫法不一，按常见名字依次找
JAVA_PATH_KEYS = (
    'project_jar', 'jar_path', 'jar', 'project_path', 'path',
    'root', 'project_root', 'run_path', 'work_dir', 'workdir', 'home',
)


def _as_rows(data):
    """面板的列表接口有时返回 {'data': [...]}，有时直接给数组。"""
    if isinstance(data, dict):
        return data.get('data') or []
    return data if isinstance(data, list) else []


def _pick_java_path(row: dict) -> str:
    """从 project_list 的一行里找出项目目录。

    字段名各面板版本不一样，只能挨个试；都不中就退回从启动命令里抠 jar 路径。
    """
    raw = ''
    for key in JAVA_PATH_KEYS:
        value = row.get(key)
        if value:
            raw = str(value)
            break

    if not raw:
        match = re.search(r'(\S+\.jar)', str(row.get('project_cmd') or ''))
        raw = match.group(1) if match else ''

    # 拿到的是 jar 文件路径时取所在目录 —— 部署目标是目录
    return posixpath.dirname(raw) if raw.endswith('.jar') else raw




class BtClient:
    def __init__(self, panel_url: str, api_sk: str,
                 verify_ssl: bool = False, timeout: int = DEFAULT_TIMEOUT):
        url = (panel_url or '').strip()
        if not url:
            raise BtApiError('面板地址不能为空')
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        self.base = url.rstrip('/')

        self.api_sk = (api_sk or '').strip()
        if not self.api_sk:
            raise BtApiError('API 密钥不能为空')

        self.timeout = timeout
        # 宝塔面板默认用自签证书，默认不校验；关掉校验就得同时关掉 hostname 检查
        if self.base.startswith('https') and not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = None
        self._ctx = ctx

    # ------------------------------------------------------------------ 认证

    def sign(self) -> dict:
        """宝塔双重 MD5 签名：md5(request_time + md5(api_sk))。"""
        t = str(int(time.time()))
        token = hashlib.md5(
            (t + hashlib.md5(self.api_sk.encode()).hexdigest()).encode()
        ).hexdigest()
        return {'request_time': t, 'request_token': token}

    # ------------------------------------------------------------------ 传输

    def _open(self, req: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode('utf-8', 'replace')
            raise BtApiError(f'HTTP {e.code}：{detail}') from None
        except urllib.error.URLError as e:
            raise BtApiError(f'无法连接面板（{self.base}）：{e.reason}') from None
        except OSError as e:
            raise BtApiError(f'网络错误：{e}') from None

    @staticmethod
    def _parse(raw: bytes):
        text = raw.decode('utf-8', 'replace').strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            raise BtApiError(
                f'面板返回了非 JSON 内容，请检查地址/端口/协议是否正确：{text[:200]}'
            ) from None
        if isinstance(result, dict) and result.get('status') is False:
            raise BtApiError(result.get('msg') or '面板返回失败（未给出原因）')
        return result

    def post_form(self, path: str, params: dict):
        body = urllib.parse.urlencode({**params, **self.sign()}).encode()
        req = urllib.request.Request(self.base + path, data=body, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        return self._parse(self._open(req))

    def get(self, path: str, params: dict | None = None):
        query = urllib.parse.urlencode({**(params or {}), **self.sign()})
        req = urllib.request.Request(f'{self.base}{path}?{query}', method='GET')
        return self._parse(self._open(req))

    def post_multipart(self, path: str, fields: dict, file_field: str,
                       filename: str, file_bytes: bytes):
        """手写 multipart。签名参数必须放在 FormData 字段里，不能放 URL query。"""
        boundary = '----btdeploy' + uuid.uuid4().hex
        bnd = boundary.encode()
        crlf = b'\r\n'

        buf = bytearray()
        for key, value in fields.items():
            buf += b'--' + bnd + crlf
            buf += f'Content-Disposition: form-data; name="{key}"'.encode() + crlf + crlf
            buf += str(value).encode() + crlf
        buf += b'--' + bnd + crlf
        buf += (f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"').encode() + crlf
        buf += b'Content-Type: application/octet-stream' + crlf + crlf
        buf += file_bytes + crlf
        buf += b'--' + bnd + b'--' + crlf

        req = urllib.request.Request(self.base + path, data=bytes(buf), method='POST')
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        return self._parse(self._open(req))

    # ------------------------------------------------------------------ 业务

    def test_connection(self) -> dict:
        """读一次 /tmp 目录，足以验证连通性 + 签名 + 白名单。"""
        return self.post_form('/files', {'action': 'GetDir', 'path': '/tmp'})

    def ensure_dir(self, path: str) -> None:
        """尽力创建远程目录。老面板没有 CreateDir 接口时静默跳过。"""
        try:
            self.post_form('/files', {'action': 'CreateDir', 'path': path})
        except BtApiError:
            pass

    def upload_file(self, remote_dir: str, filename: str, file_bytes: bytes) -> dict:
        fields = {'action': 'UploadFile', 'path': remote_dir}
        fields.update(self.sign())
        return self.post_multipart('/files', fields, 'zunfile', filename, file_bytes)

    def unzip(self, remote_zip: str, dest_dir: str) -> dict:
        return self.post_form('/files', {
            'action': 'mutil_unzip',
            'sfile_list': json.dumps([remote_zip]),
            'dfile': dest_dir,
            'coding': 'utf-8',
            'type1': 'zip',
        })

    def delete(self, path: str) -> dict:
        return self.post_form('/files', {'action': 'DeleteDir', 'path': path})

    def service_admin(self, name: str, op: str) -> dict:
        """name: nginx/httpd/mysqld/redis/tomcat/webserver... op: start/stop/restart/reload"""
        return self.post_form('/system', {'action': 'ServiceAdmin', 'name': name, 'type': op})

    def restart_java_project(self, project_name: str) -> dict:
        """文档标注 GET；先按 GET 调，失败再退回 POST（部分面板版本只收 form）。"""
        path = '/mod/java/project/restart_project/stype'
        params = {'project_name': project_name}
        try:
            return self.get(path, params)
        except BtApiError:
            return self.post_form(path, params)

    def list_sites(self) -> list[dict]:
        """面板上的网站：每项 {'name': 域名, 'path': 网站根目录}。"""
        # 不要带 list=true —— 它会改变返回结构，按文档示例用 type=-1
        data = self.post_form('/data', {
            'action': 'getData', 'table': 'sites', 'type': '-1',
        })
        sites = []
        for row in _as_rows(data):
            if isinstance(row, dict) and row.get('path'):
                sites.append({'name': str(row.get('name') or ''), 'path': str(row['path'])})
        return sites

    def list_java_projects(self) -> list[dict]:
        """面板上的 Java 项目：每项 {'name': 项目名, 'path': 项目目录}。

        path 可能为空 —— project_list 的字段各面板版本不一样，能解析到就用，
        解析不到就留空让用户手填。
        """
        endpoint = '/mod/java/project/project_list/stype'
        try:
            data = self.get(endpoint)
        except BtApiError:
            data = self.post_form(endpoint, {})
        rows = _as_rows(data)

        found: dict[str, str] = {}
        for row in rows or []:
            if isinstance(row, dict):
                name = row.get('name') or row.get('project_name')
                if name:
                    found[str(name)] = _pick_java_path(row)
            elif isinstance(row, str):
                # 有的面板版本按 "name;size;..." 这种分号串返回
                found.setdefault(row.split(';')[0], '')
        return [{'name': name, 'path': found[name]} for name in sorted(found)]

    def raw_panel_snapshot(self) -> dict:
        """诊断用：原样拿回两个列表接口的响应，好确认字段名到底叫什么。"""
        calls = (
            ('/data  table=sites', lambda: self.post_form(
                '/data', {'action': 'getData', 'table': 'sites', 'type': '-1'})),
            ('/mod/java/project/project_list/stype', lambda: self.get(
                '/mod/java/project/project_list/stype')),
        )
        snapshot = {}
        for label, call in calls:
            try:
                snapshot[label] = call()
            except BtApiError as exc:
                snapshot[label] = f'调用失败：{exc}'
        return snapshot