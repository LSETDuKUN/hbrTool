"""Window discovery only. No screen capture and no game input."""
import ctypes as c
from ctypes import wintypes as w


def set_dpi_aware():
    try:
        c.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        c.windll.user32.SetProcessDPIAware()


def game_pid():
    from . import runtime as r
    user = c.WinDLL('user32', use_last_error=True)
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    candidates = set()

    def visit(hwnd, _):
        name = c.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        if name.value != 'UnityWndClass':
            return True
        pid = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(pid))
        handle = r.op(0x1000, False, pid.value)
        if handle:
            try:
                path = c.create_unicode_buffer(32768)
                length = w.DWORD(len(path))
                if r.qp(handle, 0, path, c.byref(length)) and path.value.lower().rsplit(chr(92), 1)[-1] == 'heavenburnsred.exe':
                    candidates.add(pid.value)
            finally:
                r.close(handle)
        return True

    if not user.EnumWindows(callback_type(visit), 0):
        raise OSError(c.get_last_error(), '无法枚举游戏窗口')
    if len(candidates) != 1:
        raise RuntimeError('请先启动游戏本体，并停在战斗指令选择画面。')
    return candidates.pop()
