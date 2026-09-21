"""Tkinter 图形界面。标准库 GUI，PyInstaller 打包体积最小。"""

from __future__ import annotations

import copy
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .api import BtApiError, BtClient
from .deploy import (CLOSE_ASK, CLOSE_CHOICES, CLOSE_LABELS, NEW_TARGET,
                     RESTART_CHOICES, RESTART_LABELS, deploy, export_targets,
                     load_config, merge_targets, new_panel, save_config,
                     target_sources)
from .tray import TrayIcon
PAD = 6


def center_on(master, window):
    """按内容定尺寸并居中到父窗口 —— Tk 默认会把 Toplevel 丢到屏幕左上角。"""
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    x = master.winfo_rootx() + (master.winfo_width() - width) // 2
    y = master.winfo_rooty() + (master.winfo_height() - height) // 2
    # 别跑到屏幕外面去
    x = max(0, min(x, window.winfo_screenwidth() - width))
    y = max(0, min(y, window.winfo_screenheight() - height))
    window.geometry(f'{width}x{height}+{x}+{y}')


class TargetDialog(tk.Toplevel):
    """新增/编辑单个部署目标。"""

    def __init__(self, master, target: dict, data_loader=None):
        super().__init__(master)
        self.title('部署目标')
        self.transient(master)
        self.resizable(False, False)
        self.result: dict | None = None
        self.data = copy.deepcopy(target)
        self.data_loader = data_loader
        self._loading = False
        self._payload: dict | None = None
        self._java_paths: dict[str, str] = {}

        self.body = ttk.Frame(self, padding=12)
        self.body.grid(sticky='nsew')
        self.body.columnconfigure(1, weight=1)

        self.var_name = tk.StringVar(value=self.data.get('name', ''))
        self.var_dst = tk.StringVar(value=self.data.get('remote_dir', ''))
        self.var_tmp = tk.StringVar(value=self.data.get('temp_dir', '/tmp'))

        restart = self.data.get('restart') or {}
        self.var_kind = tk.StringVar(value=RESTART_LABELS.get(restart.get('type', 'none'), ''))
        self.var_project = tk.StringVar(value=restart.get('project_name', ''))
        self.var_service = tk.StringVar(value=restart.get('service_name', ''))

        row = 0
        self._row(row, '名称', ttk.Entry(self.body, textvariable=self.var_name, width=46))
        row += 1

        self._row(row, '本地路径', self._build_sources())
        row += 1
        self.var_keep_root = tk.BooleanVar(value=bool(self.data.get('keep_root', True)))
        self._row(row, '', ttk.Checkbutton(
            self.body, variable=self.var_keep_root,
            text='保留目录名（选 lib 传到 …/admin/lib；不勾则铺到 …/admin）'))
        row += 1

        # 可下拉可手输：面板上已配置的网站根目录会作为候选项
        self.combo_dst = ttk.Combobox(self.body, textvariable=self.var_dst, width=46)
        self._row(row, '远程目录', self.combo_dst)
        row += 1
        self._row(row, '临时目录', ttk.Entry(self.body, textvariable=self.var_tmp, width=46))
        row += 1

        kind_combo = ttk.Combobox(self.body, textvariable=self.var_kind, state='readonly',
                                  values=[label for _, label in RESTART_CHOICES], width=43)
        kind_combo.bind('<<ComboboxSelected>>', lambda _e: self._sync_restart_fields())
        self._row(row, '部署后动作', kind_combo)
        row += 1

        # 可下拉可手输：拉得到面板上的项目就直接选，拉不到就自己填
        self.combo_project = ttk.Combobox(self.body, textvariable=self.var_project, width=43)
        self.combo_project.bind('<<ComboboxSelected>>', self._on_project_selected)
        self.row_project = self._row(row, 'Java 项目名', self.combo_project)
        row += 1
        self.row_service = self._row(row, '服务名',
                                     ttk.Entry(self.body, textvariable=self.var_service, width=46))
        row += 1
        self._sync_restart_fields()
        self._load_panel_data()

        ttk.Separator(self.body).grid(row=row, column=0, columnspan=2, sticky='ew', pady=10)
        row += 1

        buttons = ttk.Frame(self.body)
        buttons.grid(row=row, column=0, columnspan=2, sticky='e')
        ttk.Button(buttons, text='取消', command=self.destroy).pack(side='left', padx=(0, PAD))
        ttk.Button(buttons, text='保存', command=self._save).pack(side='left')

        self.bind('<Return>', lambda _e: self._save())
        self.bind('<Escape>', lambda _e: self.destroy())
        self.resize_to_content(master)
        # 必须等窗口真正 map 出来再 grab，否则 Tcl 会报 "grab failed: window not viewable"
        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def resize_to_content(self, master):
        center_on(master, self)

    def _row(self, row: int, text: str, widget: tk.Widget):
        """一行 = 标签 + 控件。两个都返回，隐藏整行时要一起处理。"""
        label = ttk.Label(self.body, text=text, width=11, anchor='w')
        label.grid(row=row, column=0, sticky='w', padx=(0, 8), pady=3)
        widget.grid(row=row, column=1, sticky='ew', pady=3)
        return label, widget

    def _build_sources(self) -> ttk.Frame:
        """本地路径能加多个：目录、单个文件、已有的 zip 都行。"""
        box = ttk.Frame(self.body)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.list_src = tk.Listbox(box, height=5, selectmode='extended', activestyle='none')
        self.list_src.grid(row=0, column=0, sticky='nsew')

        vscroll = ttk.Scrollbar(box, orient='vertical', command=self.list_src.yview)
        vscroll.grid(row=0, column=1, sticky='ns')
        hscroll = ttk.Scrollbar(box, orient='horizontal', command=self.list_src.xview)
        hscroll.grid(row=1, column=0, sticky='ew')
        self.list_src.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.list_src.bind('<<ListboxSelect>>', self._show_full_path)

        for item in target_sources(self.data):
            self.list_src.insert('end', item)

        # 路径长的时候横向也看不全，选中哪条就在下面把完整路径折行显示出来。
        # 这里必须用 Text 而不是 Label：Label 的请求尺寸会跟着文字一起涨，长路径会把
        # 对话框的布局整个撑大，而窗口不可调整大小，保存/取消就被顶到可视区外面点不到了。
        # Text 的宽高写死（1 字符宽，靠 sticky 撑满），长路径在里面折行，滚轮继续看。
        self.txt_full = tk.Text(box, height=3, width=1, wrap='char', state='disabled',
                                relief='flat', borderwidth=0, highlightthickness=0,
                                background=self.cget('background'), foreground='#555')
        self.txt_full.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(3, 0))
        # 只读（disabled）的 Text 自己不吃滚轮，长路径得手动接一下
        self.txt_full.bind('<MouseWheel>', lambda event: self.txt_full.yview_scroll(
            -1 if event.delta > 0 else 1, 'units'))

        buttons = ttk.Frame(box)
        buttons.grid(row=0, column=2, sticky='n', padx=(6, 0))
        ttk.Button(buttons, text='添加目录…', command=self._add_dir).pack(fill='x')
        ttk.Button(buttons, text='添加文件…', command=self._add_files).pack(fill='x', pady=(4, 0))
        ttk.Button(buttons, text='移除选中', command=self._remove_sources).pack(fill='x', pady=(4, 0))
        return box

    def _add_dir(self):
        chosen = filedialog.askdirectory(title='选择要部署的目录')
        if chosen:
            self._append_sources([str(Path(chosen))])

    def _add_files(self):
        chosen = filedialog.askopenfilenames(title='选择要部署的文件（可多选）')
        if chosen:
            self._append_sources([str(Path(item)) for item in chosen])

    def _append_sources(self, items):
        existing = set(self.list_src.get(0, 'end'))
        for item in items:
            if item not in existing:
                self.list_src.insert('end', item)
                existing.add(item)

    def _remove_sources(self):
        for index in reversed(self.list_src.curselection()):
            self.list_src.delete(index)
        self._show_full_path()

    def _show_full_path(self, _event=None):
        picked = self.list_src.curselection()
        self.txt_full.configure(state='normal')
        self.txt_full.delete('1.0', 'end')
        self.txt_full.insert('1.0', self.list_src.get(picked[0]) if picked else '')
        self.txt_full.configure(state='disabled')

    def _sync_restart_fields(self):
        kind = self._kind()
        for widget in self.row_project + self.row_service:
            widget.grid_remove()
        # grid_remove 会记住原位置，重新 grid 即可，顺序不会乱
        if kind == 'java_restart':
            target = self.row_project
        elif kind == 'service_restart':
            target = self.row_service
        else:
            target = ()
        for widget in target:
            widget.grid()

    def _load_panel_data(self):
        """后台拉一次面板数据（网站根目录 + Java 项目名）填进下拉。

        工作线程只写结果，after 轮询一律由主线程发起 —— Tkinter 不是线程安全的。
        """
        if self._loading or self.data_loader is None:
            return
        self._loading = True
        self._payload = None

        def worker():
            try:
                self._payload = self.data_loader() or {}
            except Exception:
                self._payload = {}

        threading.Thread(target=worker, daemon=True).start()
        self._poll_panel_data()

    def _poll_panel_data(self):
        if not self.winfo_exists():
            return
        if self._payload is None:
            self.after(100, self._poll_panel_data)
            return

        payload, self._payload = self._payload, None
        self._loading = False

        self.combo_dst.configure(values=list(payload.get('dirs') or []))

        projects = [item for item in (payload.get('java_projects') or [])
                    if isinstance(item, dict) and item.get('name')]
        self._java_paths = {item['name']: item.get('path') or '' for item in projects}

        names = [item['name'] for item in projects]
        current = self.var_project.get().strip()
        if current and current not in names:
            names.insert(0, current)
        self.combo_project.configure(values=names)

        # 打开对话框时已经选好 Java 项目的话，顺手把目录也带出来
        if not self.var_dst.get().strip():
            self._on_project_selected()

    def _on_project_selected(self, _event=None):
        """选了 Java 项目就把它的目录填进远程目录，省得手打。"""
        path = self._java_paths.get(self.var_project.get().strip(), '')
        if path:
            self.var_dst.set(path)

    def _kind(self) -> str:
        for value, label in RESTART_CHOICES:
            if label == self.var_kind.get():
                return value
        return 'none'

    def _save(self):
        name = self.var_name.get().strip()
        sources = list(self.list_src.get(0, 'end'))
        dst = self.var_dst.get().strip()
        if not name:
            messagebox.showwarning('提示', '请填写名称', parent=self)
            return
        if not sources:
            messagebox.showwarning('提示', '请至少添加一个本地路径', parent=self)
            return
        missing = [item for item in sources if not Path(item).expanduser().exists()]
        if missing:
            messagebox.showwarning('提示', '这些路径已经不存在了：\n' + '\n'.join(missing), parent=self)
            return
        if not dst:
            messagebox.showwarning('提示', '请填写远程目标目录', parent=self)
            return

        kind = self._kind()
        restart = {'type': kind}
        if kind == 'java_restart':
            project = self.var_project.get().strip()
            if not project:
                messagebox.showwarning('提示', '请填写 Java 项目名称', parent=self)
                return
            restart['project_name'] = project
        elif kind == 'service_restart':
            service = self.var_service.get().strip()
            if not service:
                messagebox.showwarning('提示', '请填写服务名，例如 nginx', parent=self)
                return
            restart['service_name'] = service

        self.result = {
            'name': name,
            'source_dirs': sources,
            'keep_root': bool(self.var_keep_root.get()),
            'remote_dir': dst,
            'temp_dir': self.var_tmp.get().strip() or '/tmp',
            'restart': restart,
        }
        self.destroy()

class CloseChoiceDialog(tk.Toplevel):
    """第一次关窗口时问一句：退出程序，还是缩到通知区。"""

    def __init__(self, master):
        super().__init__(master)
        self.title('关闭窗口时')
        self.transient(master)
        self.resizable(False, False)
        self.result: tuple[str, bool] | None = None

        body = ttk.Frame(self, padding=14)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='点关闭按钮之后，程序该怎么做？').pack(anchor='w')
        ttk.Label(body, text='之后可以在主界面右下角随时改。',
                  foreground='#666').pack(anchor='w', pady=(4, 0))

        self.var_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text='记住我的选择，以后不再询问',
                        variable=self.var_remember).pack(anchor='w', pady=(12, 14))

        buttons = ttk.Frame(body)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='最小化到托盘',
                   command=lambda: self._pick('tray')).pack(side='right')
        ttk.Button(buttons, text='退出程序',
                   command=lambda: self._pick('exit')).pack(side='right', padx=(0, PAD))

        self.protocol('WM_DELETE_WINDOW', self.destroy)   # 关掉 = 这次算了，什么都不改
        self.bind('<Escape>', lambda _e: self.destroy())
        center_on(master, self)
        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _pick(self, action: str):
        self.result = (action, bool(self.var_remember.get()))
        self.destroy()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('宝塔部署助手')
        self.geometry('920x680')
        self.minsize(760, 520)

        self.cfg = load_config()
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._busy = False
        self._picked: set[int] = set()
        self._tray: TrayIcon | None = None

        self._build_panel()
        self._build_targets()
        self._build_log()
        self._load_active_panel()

        self.after(120, self._drain_log)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ------------------------------------------------------------- 界面搭建

    def _build_panel(self):
        box = ttk.LabelFrame(self, text=' 面板连接 ', padding=PAD)
        box.pack(fill='x', padx=PAD * 2, pady=(PAD * 2, 0))
        box.columnconfigure(1, weight=1)

        self.var_panel = tk.StringVar()
        self.var_url = tk.StringVar()
        self.var_key = tk.StringVar()
        self.var_ssl = tk.BooleanVar()

        ttk.Label(box, text='面板').grid(row=0, column=0, sticky='w', padx=(0, 8))
        pick = ttk.Frame(box)
        pick.grid(row=0, column=1, sticky='ew')
        pick.columnconfigure(0, weight=1)
        self.combo_panel = ttk.Combobox(pick, textvariable=self.var_panel,
                                        state='readonly')
        self.combo_panel.grid(row=0, column=0, sticky='ew')
        self.combo_panel.bind('<<ComboboxSelected>>', self._on_panel_switch)
        ttk.Button(pick, text='新增', command=self._add_panel).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(pick, text='重命名', command=self._rename_panel).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(pick, text='删除', command=self._remove_panel).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(box, text='面板地址').grid(row=1, column=0, sticky='w', padx=(0, 8), pady=(4, 0))
        ttk.Entry(box, textvariable=self.var_url).grid(row=1, column=1, sticky='ew', pady=(4, 0))

        ttk.Label(box, text='API 密钥').grid(row=2, column=0, sticky='w', padx=(0, 8), pady=(4, 0))
        key_row = ttk.Frame(box)
        key_row.grid(row=2, column=1, sticky='ew', pady=(4, 0))
        key_row.columnconfigure(0, weight=1)
        self.entry_key = ttk.Entry(key_row, textvariable=self.var_key, show='*')
        self.entry_key.grid(row=0, column=0, sticky='ew')
        ttk.Checkbutton(key_row, text='显示', command=self._toggle_key).grid(row=0, column=1, padx=(6, 0))

        opts = ttk.Frame(box)
        opts.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        ttk.Checkbutton(opts, text='校验 SSL 证书（面板用自签证书时不要勾选）',
                        variable=self.var_ssl).pack(side='left')
        ttk.Button(opts, text='测试连接', command=self._test_connection).pack(side='right')
        ttk.Button(opts, text='诊断面板数据', command=self._diagnose).pack(side='right', padx=(0, PAD))

    def _build_targets(self):
        box = ttk.LabelFrame(self, text=' 部署目标 ', padding=PAD)
        box.pack(fill='both', expand=True, padx=PAD * 2, pady=(PAD * 2, 0))

        # 第一列是手写的勾选标记 —— Treeview 没有原生复选框
        columns = ('pick', 'name', 'source', 'remote', 'restart')
        self.tree = ttk.Treeview(box, columns=columns, show='headings', height=7)
        for key, text, width in (('pick', '', 34), ('name', '名称', 150),
                                 ('source', '本地目录', 240), ('remote', '远程目录', 210),
                                 ('restart', '部署后动作', 130)):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, stretch=key != 'pick',
                             anchor='center' if key == 'pick' else 'w')
        self.tree.grid(row=0, column=0, sticky='nsew')
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<Double-1>', lambda _e: self._edit_target())

        scroll = ttk.Scrollbar(box, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        actions = ttk.Frame(box)
        actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(PAD, 0))
        ttk.Button(actions, text='新增', command=self._add_target).pack(side='left')
        ttk.Button(actions, text='编辑', command=self._edit_target).pack(side='left', padx=(PAD, 0))
        ttk.Button(actions, text='删除', command=self._remove_target).pack(side='left', padx=(PAD, 0))
        ttk.Button(actions, text='↑ 上移', command=lambda: self._move_target(-1)).pack(
            side='left', padx=(PAD * 3, 0))
        ttk.Button(actions, text='↓ 下移', command=lambda: self._move_target(1)).pack(
            side='left', padx=(PAD, 0))
        ttk.Button(actions, text='导入配置', command=self._import_config).pack(
            side='left', padx=(PAD * 3, 0))
        self.btn_deploy = ttk.Button(actions, text='开始部署', command=self._start_deploy)
        self.btn_deploy.pack(side='right')

    def _build_log(self):
        box = ttk.LabelFrame(self, text=' 日志 ', padding=PAD)
        box.pack(fill='both', expand=True, padx=PAD * 2, pady=PAD * 2)

        self.txt = tk.Text(box, height=14, wrap='word', state='disabled',
                           background='#1c1c1c', foreground='#dcdcdc', insertbackground='#dcdcdc')
        self.txt.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(box, orient='vertical', command=self.txt.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.txt.configure(yscrollcommand=scroll.set)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        footer = ttk.Frame(box)
        footer.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(PAD, 0))
        ttk.Button(footer, text='清空日志', command=self._clear_log).pack(side='left')

        # 关闭行为是全局设置，不属于任何一个面板
        self.combo_close = ttk.Combobox(footer, state='readonly', width=13,
                                        values=[label for _, label in CLOSE_CHOICES])
        self.combo_close.pack(side='right')
        self.combo_close.bind('<<ComboboxSelected>>', self._on_close_action_changed)
        ttk.Label(footer, text='关闭窗口时：').pack(side='right', padx=(0, PAD))
        self._sync_close_action()

    # ------------------------------------------------------------- 小工具

    def _toggle_key(self):
        self.entry_key.configure(show='' if self.entry_key.cget('show') else '*')

    def _log(self, message: str):
        self._log_queue.put(message)

    def _drain_log(self):
        while True:
            try:
                message = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.txt.configure(state='normal')
            self.txt.insert('end', message + '\n')
            self.txt.see('end')
            self.txt.configure(state='disabled')
        self.after(120, self._drain_log)

    def _clear_log(self):
        self.txt.configure(state='normal')
        self.txt.delete('1.0', 'end')
        self.txt.configure(state='disabled')

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.btn_deploy.configure(state='disabled' if busy else 'normal')

    def panel(self) -> dict:
        """当前面板。索引越界就夹回 0，保证永远有一个能用的。"""
        panels = self.cfg['panels']
        index = self.cfg.get('active_panel', 0)
        if not isinstance(index, int) or not 0 <= index < len(panels):
            index = 0
            self.cfg['active_panel'] = 0
        return panels[index]

    def _targets(self) -> list:
        """当前面板的部署目标列表。"""
        return self.panel().setdefault('targets', [])

    def _persist_panel(self):
        """把界面上的连接信息写回**当前**面板，别的面板不动。"""
        panel = self.panel()
        panel['panel_url'] = self.var_url.get().strip()
        panel['api_sk'] = self.var_key.get().strip()
        panel['verify_ssl'] = bool(self.var_ssl.get())
        save_config(self.cfg)

    def _client(self) -> BtClient:
        """把界面上的连接信息写回配置，再按当前面板构造客户端。"""
        self._persist_panel()
        panel = self.panel()
        return BtClient(panel['panel_url'], panel['api_sk'], panel['verify_ssl'])

    def _refresh_panel_choices(self):
        names = [item.get('name', '') for item in self.cfg['panels']]
        self.combo_panel.configure(values=names)
        self.var_panel.set(names[self.cfg['active_panel']])

    def _load_active_panel(self):
        """把当前面板的连接信息填进输入框，并刷新目标列表。"""
        panel = self.panel()
        self.var_url.set(panel.get('panel_url', ''))
        self.var_key.set(panel.get('api_sk', ''))
        self.var_ssl.set(bool(panel.get('verify_ssl')))
        self._picked.clear()
        self._refresh_panel_choices()
        self._refresh_tree()

    def _on_panel_switch(self, _event=None):
        self._persist_panel()   # 先存下刚编辑的内容，再切走
        name = self.var_panel.get()
        for index, item in enumerate(self.cfg['panels']):
            if item.get('name') == name:
                self.cfg['active_panel'] = index
                break
        save_config(self.cfg)
        self._load_active_panel()

    def _add_panel(self):
        name = (simpledialog.askstring('新增面板', '给这个面板起个名字：', parent=self) or '').strip()
        if not name:
            return
        if any(item.get('name') == name for item in self.cfg['panels']):
            messagebox.showwarning('提示', f'已经有一个叫「{name}」的面板了')
            return
        self._persist_panel()
        self.cfg['panels'].append(new_panel(name))
        self.cfg['active_panel'] = len(self.cfg['panels']) - 1
        save_config(self.cfg)
        self._load_active_panel()
        self._log(f'已新增面板「{name}」，填好地址和密钥后点「测试连接」')

    def _rename_panel(self):
        panel = self.panel()
        name = (simpledialog.askstring('重命名面板', '新的名称：', parent=self,
                                       initialvalue=panel.get('name', '')) or '').strip()
        if not name or name == panel.get('name'):
            return
        if any(item is not panel and item.get('name') == name for item in self.cfg['panels']):
            messagebox.showwarning('提示', f'已经有一个叫「{name}」的面板了')
            return
        panel['name'] = name
        save_config(self.cfg)
        self._load_active_panel()

    def _remove_panel(self):
        if len(self.cfg['panels']) <= 1:
            messagebox.showinfo('提示', '至少要保留一个面板')
            return
        panel = self.panel()
        count = len(panel.get('targets') or [])
        if not messagebox.askyesno(
                '确认', f'删除面板「{panel.get("name", "")}」及其 {count} 个部署目标？'):
            return
        del self.cfg['panels'][self.cfg['active_panel']]
        self.cfg['active_panel'] = max(0, self.cfg['active_panel'] - 1)
        save_config(self.cfg)
        self._load_active_panel()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for index, target in enumerate(self._targets()):
            restart = target.get('restart') or {}
            label = RESTART_LABELS.get(restart.get('type', 'none'), restart.get('type', ''))
            extra = restart.get('project_name') or restart.get('service_name')
            sources = target_sources(target)
            if len(sources) > 1:
                shown = f'{sources[0]} 等 {len(sources)} 项'
            else:
                shown = sources[0] if sources else ''
            mark = '☑' if index in self._picked else '☐'
            self.tree.insert('', 'end', iid=str(index), values=(
                mark, target.get('name', ''), shown,
                target.get('remote_dir', ''), f'{label}{"：" + extra if extra else ""}'))

    def _on_tree_click(self, event):
        """点第一列切换勾选。只有点在这一列上才处理，其余交给默认行为。"""
        if self.tree.identify('region', event.x, event.y) != 'cell':
            return None
        if self.tree.identify_column(event.x) != '#1':
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self._picked.symmetric_difference_update({int(iid)})
        self._refresh_tree()
        return 'break'

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo('提示', '请先选择一个部署目标')
            return None
        return int(selection[0])

    # ------------------------------------------------------------- 目标管理

    def _add_target(self):
        dialog = TargetDialog(self, copy.deepcopy(NEW_TARGET), self._load_panel_data)
        self.wait_window(dialog)
        if dialog.result:
            self._targets().append(dialog.result)
            save_config(self.cfg)
            self._refresh_tree()

    def _edit_target(self):
        index = self._selected_index()
        if index is None:
            return
        dialog = TargetDialog(self, self._targets()[index], self._load_panel_data)
        self.wait_window(dialog)
        if dialog.result:
            self._targets()[index] = dialog.result
            save_config(self.cfg)
            self._refresh_tree()

    def _remove_target(self):
        index = self._selected_index()
        if index is None:
            return
        name = self._targets()[index].get('name', '')
        if not messagebox.askyesno('确认', f'删除部署目标「{name}」？'):
            return
        del self._targets()[index]
        self._picked.clear()  # 索引会往前挪，勾选状态直接作废重来
        save_config(self.cfg)
        self._refresh_tree()

    def _move_target(self, delta: int):
        """上下移动部署目标。批量部署按列表顺序跑，所以顺序是有意义的。"""
        index = self._selected_index()
        if index is None:
            return
        targets = self._targets()
        new_index = index + delta
        if not 0 <= new_index < len(targets):
            return

        targets[index], targets[new_index] = targets[new_index], targets[index]

        # 勾选状态是按下标存的，位置换了要跟着换，否则勾的会变成另一条
        was_a, was_b = index in self._picked, new_index in self._picked
        self._picked.discard(index)
        self._picked.discard(new_index)
        if was_a:
            self._picked.add(new_index)
        if was_b:
            self._picked.add(index)

        save_config(self.cfg)
        self._refresh_tree()
        self.tree.selection_set(str(new_index))
        self.tree.focus(str(new_index))
    def _export_config(self):
        """导出成 JSON 给别人用。故意不带 API 密钥。"""
        if not self._targets():
            messagebox.showinfo('提示', '还没有可导出的部署目标')
            return
        path = filedialog.asksaveasfilename(
            title='导出配置', defaultextension='.json',
            initialfile='bt-deploy-targets.json',
            filetypes=[('JSON 配置', '*.json'), ('所有文件', '*.*')],
        )
        if not path:
            return

        payload = export_targets(self._targets(), self.var_url.get().strip(),
                                 bool(self.var_ssl.get()))
        try:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
        except OSError as exc:
            messagebox.showerror('导出失败', str(exc))
            return
        self._log(f'✅ 已导出面板「{self.panel().get("name", "")}」的 '
                  f'{len(self._targets())} 个部署目标到 {path}（不含 API 密钥）')

    def _import_config(self):
        """导入别人分享的 JSON：同名覆盖，其余追加。"""
        path = filedialog.askopenfilename(
            title='导入配置', filetypes=[('JSON 配置', '*.json'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror('导入失败', f'读不了这个文件：{exc}')
            return

        if isinstance(payload, list):
            incoming, meta = payload, {}
        elif isinstance(payload, dict):
            incoming, meta = payload.get('targets'), payload
        else:
            incoming, meta = None, {}

        if not isinstance(incoming, list):
            messagebox.showerror('导入失败', '文件里没有 targets 数组')
            return

        merged, added, replaced = merge_targets(self._targets(), incoming)
        self.panel()['targets'] = merged
        if meta.get('panel_url'):
            self.var_url.set(str(meta['panel_url']))
        if 'verify_ssl' in meta:
            self.var_ssl.set(bool(meta['verify_ssl']))

        self._picked.clear()
        save_config(self.cfg)
        self._refresh_tree()
        self._log(f'✅ 已导入：新增 {added} 个，覆盖 {replaced} 个。API 密钥需要自己填。')

    # ------------------------------------------------------------- 任务执行

    def _run_async(self, title: str, work):
        if self._busy:
            return
        self._set_busy(True)
        self._log(f'—— {title} ——')

        def runner():
            try:
                work()
            except BtApiError as exc:
                self._log(f'❌ {exc}')
            except Exception as exc:  # 兜底，别让线程静默死掉
                self._log(f'❌ 未预期的错误：{type(exc).__name__}: {exc}')
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=runner, daemon=True).start()

    def _test_connection(self):
        try:
            client = self._client()
        except BtApiError as exc:
            messagebox.showerror('配置错误', str(exc))
            return

        def work():
            client.test_connection()
            self._log('✅ 连接成功，密钥与 IP 白名单均正常')

        self._run_async('测试连接', work)

    def _diagnose(self):
        """把列表接口的原始响应打到日志区 —— 面板字段名只能靠这个确认。"""
        try:
            client = self._client()
        except BtApiError as exc:
            messagebox.showerror('配置错误', str(exc))
            return

        def work():
            snapshot = client.raw_panel_snapshot()
            for label, payload in snapshot.items():
                self._log(f'—— {label} ——')
                self._log(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])

        self._run_async('诊断面板数据', work)

    def _load_panel_data(self) -> dict:
        """给编辑对话框拉候选：网站根目录、Java 项目名。拉不到就给空的，用户手输。"""
        try:
            client = self._client()
        except BtApiError:
            return {'dirs': [], 'java_projects': []}

        dirs: list[str] = []
        projects: list[dict] = []
        try:
            dirs = [item['path'] for item in client.list_sites()]
        except BtApiError:
            pass
        try:
            projects = client.list_java_projects()
        except BtApiError:
            pass
        return {'dirs': dirs, 'java_projects': projects}

    def _start_deploy(self):
        # 勾了多个就按顺序批量跑；一个都没勾就用当前高亮的那一个
        targets = self._targets()
        picked = sorted(index for index in self._picked if 0 <= index < len(targets))
        if picked:
            batch = [(index, targets[index]) for index in picked]
        else:
            index = self._selected_index()
            if index is None:
                return
            batch = [(index, targets[index])]

        try:
            client = self._client()
        except BtApiError as exc:
            messagebox.showerror('配置错误', str(exc))
            return

        if len(batch) == 1:
            target = batch[0][1]
            self._run_async(f'部署「{target.get("name", "")}」',
                            lambda: deploy(client, target, log=self._log))
            return

        def work():
            total = len(batch)
            for order, (_index, target) in enumerate(batch, 1):
                self._log(f'===== ({order}/{total}) {target.get("name", "")} =====')
                deploy(client, target, log=self._log)
                self._log('')
            self._log(f'全部完成，共 {total} 个部署目标')

        self._run_async(f'按顺序部署 {len(batch)} 个目标', work)

    # ------------------------------------------------------------- 退出

    def _close_action(self) -> str:
        """当前配置的关闭行为；存着不认识的值就按「每次询问」处理。"""
        action = self.cfg.get('close_action')
        return action if action in CLOSE_LABELS else CLOSE_ASK

    def _sync_close_action(self):
        self.combo_close.set(CLOSE_LABELS[self._close_action()])

    def _on_close_action_changed(self, _event=None):
        for value, label in CLOSE_CHOICES:
            if label == self.combo_close.get():
                self.cfg['close_action'] = value
                save_config(self.cfg)
                return

    def _on_close(self):
        action = self._close_action()
        if action == CLOSE_ASK:
            action = self._ask_close_action()
            if action is None:
                return      # 对话框被关掉了，这次不关窗口
        if action == 'tray':
            self._hide_to_tray()
            return
        if self._busy and not messagebox.askyesno('确认', '正在部署中，确定要退出吗？'):
            return
        self._quit()

    def _ask_close_action(self) -> str | None:
        """没定过关闭行为时问一次；勾了「记住」就写进配置，以后不再问。"""
        dialog = CloseChoiceDialog(self)
        self.wait_window(dialog)
        if dialog.result is None:
            return None
        action, remember = dialog.result
        if remember:
            self.cfg['close_action'] = action
            save_config(self.cfg)
            self._sync_close_action()
        return action

    def _quit(self):
        self._stop_tray()
        self._persist_panel()
        self.destroy()

    # ------------------------------------------------------------- 通知区图标

    def _hide_to_tray(self):
        """缩到通知区。图标建不出来（非 Windows 等）就退回任务栏最小化。"""
        if self._tray is None:
            icon = TrayIcon('宝塔部署助手')
            if icon.start():
                self._tray = icon
                self.after(120, self._drain_tray)
                self._log('已缩到右下角通知区：双击图标恢复窗口，右键可以退出')
            else:
                self._log('这个系统不支持通知区图标，改为最小化到任务栏')
        if self._tray is not None:
            self.withdraw()
        else:
            self.iconify()

    def _drain_tray(self):
        """托盘线程只往队列里塞动作，取走和动界面都留在这个线程。"""
        if self._tray is None:
            return
        while True:
            try:
                action = self._tray.events.get_nowait()
            except queue.Empty:
                break
            if action == 'show':
                self.deiconify()
                self.lift()
                self.focus_force()
            elif action == 'exit':
                self._quit()
                return
        self.after(120, self._drain_tray)

    def _stop_tray(self):
        if self._tray is not None:
            self._tray.stop()
            self._tray = None


def run():
    App().mainloop()