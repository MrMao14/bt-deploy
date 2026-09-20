# 宝塔部署助手

本地一键把打包产物发到宝塔服务器：**打包 → 上传 → 解压 → 重启**，全程走宝塔面板 API，不需要在服务器上开 SSH。

## 特性

- 运行时**零第三方依赖**：HTTP 用 `urllib`、压缩用 `zipfile`、界面用 `tkinter`，全是标准库
- 跨平台图形界面，Windows / macOS 都能跑
- PyInstaller 打包后是**单文件**，约 12–15 MB，不需要目标机器装 Python
- 支持配置多套部署目标，配置存在本机

## 前置条件

### 服务器端：开启面板 API

1. 登录宝塔面板 → **面板设置** → **API 接口**
2. 打开 API 开关，点「生成密钥」，复制保存
3. 建议配置 **IP 白名单**，把本机公网 IP 加进去（不配的话面板默认允许所有 IP，不安全）
4. 确认面板端口在云服务器安全组 / 系统防火墙里已放行

### 本机

- 直接跑源码：Python 3.9+（Windows 上建议勾选 `tcl/tk` 组件，Tkinter 界面需要）
- 只想用打好的 exe / app：不需要装任何东西

## 使用

1. 启动程序，填入**面板地址**（如 `https://1.2.3.4:8888`）和 **API 密钥**
2. 点「**测试连接**」确认密钥和白名单没问题
3. 点「**新增**」配置一个部署目标
4. 点「**开始部署**」—— 列表**第一列可以勾选**，勾了一个以上就按从上到下的顺序批量部署；一个都不勾则只部署当前高亮的那一个
5. 日志区会实时输出每一步结果

### 多选批量部署

列表最左边一列是勾选框（☐ / ☑），点一下切换。勾选多个后点「开始部署」，会**按列表顺序依次部署**：

```
===== (1/3) 官网静态站 =====
…… 五步流程 ……
===== (2/3) 后端 API =====
…… 五步流程 ……
```

任何一个失败就整体停下，不会继续跑后面的 —— 后面的目标常常依赖前面的结果。

### 分享配置给别人

- 「**导出配置**」把当前所有部署目标存成一个 JSON 文件。**导出文件里不含 API 密钥**，可以放心发出去
- 「**导入配置**」读入别人给的 JSON：同名的目标覆盖，新目标追加，不会清空你已有的配置
- 导入后会一并带上对方的面板地址，**API 密钥需要自己填**


### 部署目标字段

| 字段 | 说明 |
| --- | --- |
| 名称 | 随便起，只用于在列表里区分 |
| 本地路径 | **可以加多个**：目录、单个文件、已有的 `.zip`。所有来源会合并成一个 zip 再上传 |
| 远程目录 | 目标绝对路径。下拉会自动列出面板上已有网站的根目录；**选中 Java 项目时也会自动带出该项目的目录**，都能手改 |
| 临时目录 | zip 先上传到这里再解压，默认 `/tmp` |
| 部署后动作 | 见下表 |

### 部署后动作怎么选

| 选项 | 适用场景 | 实际调用的接口 |
| --- | --- | --- |
| 不重启 | 纯静态文件覆盖，无需任何动作 | — |
| 重载 Web 服务器 | 静态站更新后刷新 Nginx/Apache 配置缓存 | `/system?action=ServiceAdmin` `name=webserver` `type=reload` |
| 重启系统服务 | 需要重启 `nginx` / `mysqld` / `redis` / `tomcat` 等 | `/system?action=ServiceAdmin` `type=restart` |
| 重启 Java 项目 | 宝塔「Java 项目管理器」里的 Spring Boot / Tomcat 项目 | `/mod/java/project/restart_project/stype` |

选中「重启 Java 项目」时，项目名下拉会自动从面板拉取；选中某个项目后，远程目录会自动填上该项目的目录。拉不到（面板没装 Java 项目管理器、或密钥没权限）也可以手输。

如果下拉是空的，点主界面的「**诊断面板数据**」按钮 —— 日志区会原样打印两个列表接口的响应，把那段贴出来就能定位字段名。

## 打包成单文件

```bash
pip install -r requirements.txt
python build.py
```

产物：

- Windows → `dist/bt-deploy.exe`
- macOS → `dist/bt-deploy.app`

PyInstaller **不能交叉编译**，Windows 包必须在 Windows 上打，mac 包必须在 mac 上打。

## 自检

不需要网络和面板，验证签名算法、multipart 编码、zip 路径和重启分发逻辑：

```bash
python selfcheck.py
```

## 配置文件

`~/.bt-deploy/config.json`

- Windows：`C:\Users\<用户名>\.bt-deploy\config.json`
- macOS：`/Users/<用户名>/.bt-deploy/config.json`

API 密钥以明文存在里面（保存时会尝试设成 `600` 权限，Windows 上该设置无效）。别把这个文件提交到仓库。

## 已知限制

- **解压是覆盖式的**，远程目录里多余的旧文件不会被删除。需要完全干净的话，先手动清空目标目录再部署。
- 多个本地来源合并成一个 zip 时，**同名条目以列表里先出现的为准**（先加的不会被后加的覆盖）。
- 面板 API 没有执行任意 shell 命令的能力，所有动作都受限于面板已有的接口。
- zip 一次性读进内存后上传，几百 MB 以内没问题，再大需要改成分片上传。
- Java 项目重启接口是**异步**的，返回「操作已执行」只代表指令已下发，不代表进程已经起来。
- 宝塔官方声明 API 接口可能随面板版本变化，不保证长期稳定。本项目的接口依据 [docs.bt.cn/api](https://docs.bt.cn/api)（面板 v11.7.0）。

## 接口对照

| 用途 | 路由 | 关键参数 |
| --- | --- | --- |
| 上传文件 | `POST /files`（multipart） | `action=UploadFile`、`path`、`zunfile` |
| 批量解压 | `POST /files` | `action=mutil_unzip`、`sfile_list`、`dfile`、`coding`、`type1` |
| 删除文件 | `POST /files` | `action=DeleteDir`、`path` |
| 服务管理 | `POST /system` | `action=ServiceAdmin`、`name`、`type` |
| 重启 Java 项目 | `/mod/java/project/restart_project/stype` | `project_name` |
| 网站列表（取根目录） | `POST /data` | `action=getData`、`table=sites`、`type=-1`，返回每项的 `name`、`path` |

认证是双重 MD5：`request_token = md5(request_time + md5(api_sk))`，每次请求都要带 `request_time` 和 `request_token`。上传接口是唯一的例外——签名参数必须放在 FormData 字段里，不能放 URL query。

## 目录结构

```
main.py               入口
btdeploy/api.py       宝塔 API 客户端（签名、三种请求、业务封装）
btdeploy/deploy.py    配置读写、本地打包、五步部署流程
btdeploy/gui.py       Tkinter 界面
selfcheck.py          自检脚本
build.py              PyInstaller 打包脚本
```