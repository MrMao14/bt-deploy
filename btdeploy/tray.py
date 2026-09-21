"""Windows 通知区（托盘）图标。ctypes 直调 Shell_NotifyIcon，不引入第三方依赖。

托盘消息只有 Win32 消息循环能收，Tk 的主循环管不着，所以另起一条线程：自己建一个
永不显示的窗口收消息，动作塞进队列，界面线程用 after 轮询取走 —— 跟日志一个套路。
非 Windows 系统 AVAILABLE 为 False，调用方自行降级成任务栏最小化。
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading

AVAILABLE = sys.platform == 'win32'

APP_TITLE = '宝塔部署助手'
TRAY_CLASS = 'BtDeployTray'                 # 固定类名：第二个实例靠它找到托盘窗口
MUTEX_NAME = 'Local\\BtDeployAssistant'     # 单实例互斥体名

# 菜单项 id
MENU_SHOW = 1
MENU_EXIT = 2

if AVAILABLE:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    WM_NULL = 0x0000
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_APP = 0x8000
    WM_TRAY = WM_APP + 1
    WM_TRAY_SHOW = WM_APP + 2

    NIM_ADD, NIM_DELETE = 0, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
    IDI_APPLICATION = 32512
    MF_STRING = 0x0000
    TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
    SW_RESTORE = 9
    ERROR_ALREADY_EXISTS = 183

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ('style', wintypes.UINT),
            ('lpfnWndProc', WNDPROC),
            ('cbClsExtra', ctypes.c_int),
            ('cbWndExtra', ctypes.c_int),
            ('hInstance', wintypes.HINSTANCE),
            ('hIcon', wintypes.HANDLE),
            ('hCursor', wintypes.HANDLE),
            ('hbrBackground', wintypes.HANDLE),
            ('lpszMenuName', wintypes.LPCWSTR),
            ('lpszClassName', wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD),
            ('hWnd', wintypes.HWND),
            ('uID', wintypes.UINT),
            ('uFlags', wintypes.UINT),
            ('uCallbackMessage', wintypes.UINT),
            ('hIcon', wintypes.HANDLE),
            ('szTip', wintypes.WCHAR * 128),
            ('dwState', wintypes.DWORD),
            ('dwStateMask', wintypes.DWORD),
            ('szInfo', wintypes.WCHAR * 256),
            ('uVersion', wintypes.UINT),
            ('szInfoTitle', wintypes.WCHAR * 64),
            ('dwInfoFlags', wintypes.DWORD),
            ('guidItem', ctypes.c_byte * 16),
            ('hBalloonIcon', wintypes.HANDLE),
        ]

    # 不声明 argtypes 的话，64 位下指针参数会被按 c_int 截断，返回的句柄也是
    _user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
    _user32.RegisterClassW.restype = wintypes.WORD
    _user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID)
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                      wintypes.LPARAM)
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT)
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    _user32.LoadIconW.argtypes = (wintypes.HINSTANCE, ctypes.c_void_p)
    _user32.LoadIconW.restype = wintypes.HANDLE
    _user32.CreatePopupMenu.restype = wintypes.HANDLE
    _user32.AppendMenuW.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t,
                                   wintypes.LPCWSTR)
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    _user32.TrackPopupMenu.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       ctypes.c_void_p)
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.DestroyMenu.argtypes = (wintypes.HANDLE,)
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    _kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM)
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.DestroyWindow.argtypes = (wintypes.HWND,)
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.PostQuitMessage.argtypes = (ctypes.c_int,)
    _user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)


_mutex = None   # 句柄留着引用，进程退出系统自己回收


def acquire_single_instance() -> bool:
    """第一个实例返回 True；已经有同名互斥体（程序已经在跑）返回 False。"""
    global _mutex
    if not AVAILABLE:
        return True
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True                     # 互斥体都建不出来就别拦着人启动
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _mutex = handle
    return True


def wake_existing():
    """把已经在跑的那个窗口叫到前面来。"""
    if not AVAILABLE:
        return
    hwnd = _user32.FindWindowW(TRAY_CLASS, None)
    if hwnd:
        # 缩在托盘里：托盘窗口收到消息会自己 deiconify，不必硬改窗口状态
        _user32.PostMessageW(hwnd, WM_TRAY_SHOW, 0, 0)
        return
    hwnd = _user32.FindWindowW(None, APP_TITLE)
    if hwnd:
        _user32.ShowWindow(hwnd, SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)


class TrayIcon:
    """通知区里的一个小图标：双击还原窗口，右键出「显示/退出」菜单。"""

    def __init__(self, tooltip: str = APP_TITLE):
        self.tooltip = tooltip[:127]
        self.events: queue.Queue[str] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._data = None
        self._proc = None      # 回调必须留着引用，被 GC 掉就是进程崩溃
        self._ok = False

    def start(self) -> bool:
        """启动托盘线程并等它就位。建不出来（没权限/非 Windows）返回 False。"""
        if not AVAILABLE:
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._ready.wait(3)
        return self._ok

    def stop(self):
        """撤掉图标并结束线程。没启动过就什么都不做。"""
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        self._thread = None
        self._ok = False

    # ------------------------------------------------------------ 托盘线程内部

    def _run(self):
        try:
            self._create()
        except Exception:
            self._ready.set()
            return
        self._ok = True
        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    @staticmethod
    def _icon():
        """优先用 exe 里打包好的图标（PyInstaller 写进去的资源 id 1），

        源码直接跑时没有这个资源，退回系统默认图标。"""
        if getattr(sys, 'frozen', False):
            handle = _user32.LoadIconW(_kernel32.GetModuleHandleW(None), ctypes.c_void_p(1))
            if handle:
                return handle
        return _user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _create(self):
        instance = _kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = instance
        if not _user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError('注册托盘窗口类失败')

        self._hwnd = _user32.CreateWindowExW(
            0, wc.lpszClassName, '', 0, 0, 0, 0, 0, None, None, instance, None)
        if not self._hwnd:
            raise OSError('创建托盘窗口失败')

        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon()
        data.szTip = self.tooltip
        if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise OSError('添加托盘图标失败')
        self._data = data

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # 回调里漏异常会直接掀翻 Win32 调用栈，一律吞掉
        try:
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.events.put('show')
                elif lparam == WM_RBUTTONUP:
                    self._popup_menu(hwnd)
            elif msg == WM_TRAY_SHOW:
                self.events.put('show')
            elif msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
            elif msg == WM_DESTROY:
                self._remove_icon()
                _user32.PostQuitMessage(0)
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd):
        menu = _user32.CreatePopupMenu()
        _user32.AppendMenuW(menu, MF_STRING, MENU_SHOW, '显示主窗口')
        _user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, '退出程序')

        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        # 不先设成前台窗口的话，菜单点到别处不会消失（Windows 的老毛病）
        _user32.SetForegroundWindow(hwnd)
        command = _user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
        _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        _user32.DestroyMenu(menu)

        if command == MENU_SHOW:
            self.events.put('show')
        elif command == MENU_EXIT:
            self.events.put('exit')

    def _remove_icon(self):
        if self._data is not None:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data))
            self._data = None
            # ponytail: explorer 重启后图标会留个空位，需要注册 TaskbarCreated
            # 消息重加图标才彻底干净 —— 触发条件罕见，先不做