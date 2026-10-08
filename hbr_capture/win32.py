"""Win32 窗口枚举与抓图的 ctypes 封装（纯标准库）。

只做三件事:
  1. 枚举可见窗口，拿到标题 / 类名 / 进程名 / 尺寸
  2. 按标题或进程名子串找到目标窗口
  3. 把窗口的**客户区**抓成 BGRA 像素

抓图提供两种方法，都从窗口自身取内容，不依赖窗口在前台:
  * PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)  —— Win8.1+，对 DirectX/Unity 通常有效
  * BitBlt(GetDC(hwnd))                           —— 老办法，被遮挡时可能拿到花屏

两种都可能在某些游戏上返回全黑，所以 CaptureResult 里有 non_black_ratio，
让上层自己判断哪个方法在这个游戏上真的能用。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# ---------------------------------------------------------------- 原型

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


# ---------------------------------------------------------------- DPI

def set_dpi_aware() -> str:
    """让进程感知 DPI，否则 GetWindowRect 返回的是被缩放过的逻辑坐标。

    必须在任何取坐标的调用之前执行一次。
    """
    # PER_MONITOR_AWARE_V2 = -4
    fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if fn is not None:
        try:
            if fn(ctypes.c_void_p(-4)):
                return "PerMonitorV2"
        except Exception:
            pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return "PerMonitor"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "System"
    except Exception:
        pass
    return "none"


# ---------------------------------------------------------------- 窗口信息


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    rect: Tuple[int, int, int, int]      # 屏幕坐标 left, top, right, bottom
    client_size: Tuple[int, int]         # 客户区宽高
    client_origin: Tuple[int, int]       # 客户区左上角的屏幕坐标
    minimized: bool

    @property
    def window_size(self) -> Tuple[int, int]:
        left, top, right, bottom = self.rect
        return right - left, bottom - top

    @property
    def title_bar_height(self) -> int:
        """客户区相对窗口原点的偏移，用来判断有没有边框/标题栏。"""
        left, top, _, _ = self.rect
        cx, cy = self.client_origin
        return cy - top

    def __str__(self) -> str:
        w, h = self.client_size
        return (
            f"hwnd=0x{self.hwnd:08X}  client={w}x{h}  "
            f"proc={self.process or '?'}  title={self.title!r}"
        )


def _window_text(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def describe(hwnd) -> Optional[WindowInfo]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    client = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    return WindowInfo(
        hwnd=int(hwnd),
        title=_window_text(hwnd),
        class_name=_class_name(hwnd),
        pid=int(pid.value),
        process=_process_name(int(pid.value)),
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        client_size=(client.right - client.left, client.bottom - client.top),
        client_origin=(origin.x, origin.y),
        minimized=bool(user32.IsIconic(hwnd)),
    )


def list_windows(
    *, visible_only: bool = True, min_client: int = 0, with_title_only: bool = False
) -> List[WindowInfo]:
    """枚举顶层窗口。默认只看可见的、客户区不小于 min_client 的。"""
    results: List[WindowInfo] = []

    def callback(hwnd, _lparam):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        info = describe(hwnd)
        if info is None:
            return True
        w, h = info.client_size
        if w < min_client or h < min_client:
            return True
        if with_title_only and not info.title:
            return True
        results.append(info)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return results


def find_windows(match: str, **kwargs) -> List[WindowInfo]:
    """在标题、类名、进程名里做不区分大小写的子串匹配。"""
    needle = match.lower()
    hits = []
    for info in list_windows(**kwargs):
        haystack = f"{info.title}\n{info.class_name}\n{info.process}".lower()
        if needle in haystack:
            hits.append(info)
    return hits


def find_window(match: str, **kwargs) -> WindowInfo:
    hits = find_windows(match, **kwargs)
    if not hits:
        raise LookupError(
            f"没找到匹配 {match!r} 的窗口。用 list-windows 看看实际有哪些。"
        )
    if len(hits) > 1:
        # 优先挑客户区最大的那个 —— 游戏主窗口通常是最大的
        hits.sort(key=lambda i: i.client_size[0] * i.client_size[1], reverse=True)
    return hits[0]


# ---------------------------------------------------------------- 键盘

# 只列常用的。字母 A-Z、数字 0-9 由 parse_hotkey 直接算。
_VK_SPECIAL = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "CONTROL": 0x11,
    "ALT": 0x12,
    "PAUSE": 0x13,
    "CAPSLOCK": 0x14,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "NUMPAD0": 0x60,
    "NUMPAD1": 0x61,
    "NUMPAD2": 0x62,
    "NUMPAD3": 0x63,
    "NUMPAD4": 0x64,
    "NUMPAD5": 0x65,
    "NUMPAD6": 0x66,
    "NUMPAD7": 0x67,
    "NUMPAD8": 0x68,
    "NUMPAD9": 0x69,
}
for _i in range(1, 25):
    _VK_SPECIAL[f"F{_i}"] = 0x6F + _i  # F1=0x70 ... F24=0x87


def parse_hotkey(name: str) -> int:
    """把 "F9" / "A" / "NUMPAD0" 这类名字转成虚拟键码。"""
    key = name.strip().upper()
    if key in _VK_SPECIAL:
        return _VK_SPECIAL[key]
    if len(key) == 1 and key.isalnum():
        return ord(key)
    raise ValueError(f"无法识别的热键名: {name!r}")


def key_down(vk: int) -> bool:
    """按键当前是否处于按下状态（最高位为 1 表示按下）。"""
    return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)


# ---------------------------------------------------------------- 抓图


@dataclass
class CaptureResult:
    width: int
    height: int
    bgra: bytes
    method: str
    non_black_ratio: float

    @property
    def ok(self) -> bool:
        """画面不全黑，就认为这个方法在这个窗口上有效。"""
        return self.non_black_ratio > 0.02

    def __str__(self) -> str:
        return (
            f"{self.width}x{self.height} via {self.method}  "
            f"非黑像素={self.non_black_ratio:.1%}"
            f"{'' if self.ok else '  <-- 可能是黑屏/花屏'}"
        )


def _non_black_ratio(bgra: bytes, step: int = 401) -> float:
    """抽样估算非黑像素比例。step 是采样步长（字节），不是逐像素扫。"""
    total = 0
    lit = 0
    view = memoryview(bgra)
    for offset in range(0, len(view) - 4, step * 4):
        total += 1
        if view[offset] or view[offset + 1] or view[offset + 2]:
            lit += 1
    return lit / total if total else 0.0


def _bitmap_to_bgra(hdc_mem, bitmap, width: int, height: int) -> bytes:
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height          # 负值 = 自上而下，省一次翻转
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB

    buffer = ctypes.create_string_buffer(width * height * 4)
    scanned = gdi32.GetDIBits(
        hdc_mem, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS
    )
    if scanned == 0:
        raise OSError(f"GetDIBits 失败，错误码 {ctypes.get_last_error()}")
    return buffer.raw


def capture_window(hwnd: int, method: str = "auto") -> CaptureResult:
    """抓窗口客户区。

    method:
      "printwindow"  抓窗口自身渲染内容；Unity/DirectX 上常直接失败
      "screendc"     从屏幕 DC 抓该窗口所在的屏幕区域 —— **唯一保证是活动的**，
                     代价是窗口必须没被别的窗口挡住
      "bitblt"       从窗口 DC 抓；普通 GDI 窗口可用，Unity 上会拿到**冻结帧**
      "auto"         依次试 printwindow -> screendc -> bitblt，取第一个像样的

    注意: 单看 non_black_ratio 无法区分「静止画面」和「冻结帧」。
    要判断活动性得用 probe 的两次抓取比对，或者让 Monitor 的看门狗盯着。
    """
    handle = wintypes.HWND(hwnd)
    client = wintypes.RECT()
    if not user32.GetClientRect(handle, ctypes.byref(client)):
        raise OSError(f"GetClientRect 失败，hwnd=0x{hwnd:X}")
    width = client.right - client.left
    height = client.bottom - client.top
    if width <= 0 or height <= 0:
        raise ValueError(
            f"窗口客户区尺寸为 {width}x{height}，无法抓图"
            "（窗口可能被最小化了）。"
        )

    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(handle, ctypes.byref(origin)):
        raise OSError(f"ClientToScreen 失败，hwnd=0x{hwnd:X}")

    if method == "auto":
        attempts = ["printwindow", "screendc", "bitblt"]
    else:
        attempts = [method]

    last_error: Optional[Exception] = None
    for name in attempts:
        if name == "screendc" and method == "auto":
            # screendc 抓的是屏幕像素，被挡住就会把别的窗口抓进来。
            # 自动选择时先看一眼遮挡情况，挡太多就跳过它。
            hidden = occluded_sample(hwnd)
            if hidden > 0.2:
                last_error = RuntimeError(
                    f"窗口有 {hidden:.0%} 被别的窗口挡住，screendc 会抓到遮挡物"
                )
                continue
        try:
            result = _capture_once(handle, width, height, (origin.x, origin.y), name)
        except Exception as exc:  # 换下一个方法
            last_error = exc
            continue
        if result.ok or name == attempts[-1]:
            return result
        last_error = RuntimeError(f"{name} 抓到全黑画面")

    raise RuntimeError(f"抓图失败: {last_error}")


def capture_region(hwnd: int, box) -> CaptureResult:
    """Capture a client-relative ROI directly from the screen DC (no full bitmap)."""
    handle = wintypes.HWND(hwnd)
    client = wintypes.RECT()
    if not user32.GetClientRect(handle, ctypes.byref(client)) or user32.IsIconic(handle):
        raise ValueError('窗口最小化或客户区不可用')
    x0, y0, x1, y1 = map(int, box)
    if not (0 <= x0 < x1 <= client.right and 0 <= y0 < y1 <= client.bottom):
        raise ValueError('区域超出客户区，窗口尺寸可能已改变')
    origin = wintypes.POINT(x0, y0)
    if not user32.ClientToScreen(handle, ctypes.byref(origin)):
        raise OSError('ClientToScreen 失败')
    return _capture_once(handle, x1 - x0, y1 - y0, (origin.x, origin.y), 'screendc')


def _capture_once(
    handle, width: int, height: int, origin: Tuple[int, int], method: str
) -> CaptureResult:
    if method == "screendc":
        # NULL 表示取整个屏幕的 DC；坐标系是屏幕坐标
        hdc_source = user32.GetDC(None)
        release_hwnd = None
    else:
        hdc_source = user32.GetDC(handle)
        release_hwnd = handle
    if not hdc_source:
        raise OSError("GetDC 失败")

    hdc_mem = None
    bitmap = None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_source)
        bitmap = gdi32.CreateCompatibleBitmap(hdc_source, width, height)
        if not hdc_mem or not bitmap:
            raise OSError("创建内存 DC / 位图失败")
        gdi32.SelectObject(hdc_mem, bitmap)

        if method == "printwindow":
            # PW_RENDERFULLCONTENT 才能拿到 DirectX/Unity 渲染出来的内容
            if not user32.PrintWindow(handle, hdc_mem, PW_RENDERFULLCONTENT):
                raise OSError("PrintWindow 返回失败")
        elif method == "bitblt":
            if not gdi32.BitBlt(
                hdc_mem, 0, 0, width, height, hdc_source, 0, 0, SRCCOPY
            ):
                raise OSError("BitBlt 返回失败")
        elif method == "screendc":
            if not gdi32.BitBlt(
                hdc_mem, 0, 0, width, height,
                hdc_source, origin[0], origin[1], SRCCOPY,
            ):
                raise OSError("BitBlt(屏幕) 返回失败")
        else:
            raise ValueError(f"未知的抓图方法: {method!r}")

        bgra = _bitmap_to_bgra(hdc_mem, bitmap, width, height)
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(release_hwnd, hdc_source)

    return CaptureResult(
        width=width,
        height=height,
        bgra=bgra,
        method=method,
        non_black_ratio=_non_black_ratio(bgra),
    )


def get_foreground_window() -> int:
    return int(user32.GetForegroundWindow())


def is_foreground(hwnd: int) -> bool:
    return get_foreground_window() == int(hwnd)


# ---- 自动识别游戏窗口

#: 游戏本体的窗口类名。国服的游戏和启动器**同进程名**，只能靠类名区分。
GAME_CLASS_HINTS = ("UnityWndClass",)
#: 进程名 / 标题里可能出现的线索（小写比较）
GAME_NAME_HINTS = ("heavenburnsred", "heaven burns red", "炽焰天穹", "緋染天空")


def score_game_window(info: "WindowInfo", min_client: int = 400) -> int:
    """给一个窗口打分，判断它有多像游戏本体。

    类名是最可靠的判据 —— 国服的启动器进程名也叫 HeavenBurnsRed.exe，
    只看进程名会把启动器当成游戏。启动器的类名是 CGameLauncherWnd。
    """
    width, height = info.client_size
    if width < min_client or height < min_client:
        return 0

    score = 0
    if info.class_name in GAME_CLASS_HINTS:
        score += 1000
    blob = f"{info.title}\n{info.process}".lower()
    if any(hint in blob for hint in GAME_NAME_HINTS):
        score += 200

    # 没有任何线索就直接出局。**尺寸加分只能加在有线索的窗口上** ——
    # 否则一个大的无关窗口（浏览器之类）也能靠尺寸混进候选，
    # 游戏没开时就会去抓它而不是报错。
    if score == 0:
        return 0

    # 同为候选时，客户区大的优先（游戏本体比启动器/弹窗大）
    score += min(width * height // 100_000, 99)
    return score


def find_game_window(min_client: int = 400) -> "WindowInfo":
    """自动找 HBR 游戏窗口，不需要用户给匹配串。

    找不到就抛 LookupError，调用方可以据此重试（比如游戏还没启动）。
    """
    candidates = []
    for info in list_windows(min_client=min_client):
        score = score_game_window(info, min_client)
        if score > 0:
            candidates.append((score, info))

    if not candidates:
        raise LookupError(
            "没找到像 HBR 游戏窗口的窗口。游戏启动了吗？"
            "（用的是窗口类名 UnityWndClass + 进程名/标题关键词）"
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def game_window_candidates(min_client: int = 400):
    """按像的程度排序列出候选，方便排错时看。"""
    scored = [
        (score_game_window(info, min_client), info)
        for info in list_windows(min_client=min_client)
    ]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


# WindowFromPoint: 判断某个屏幕点当前实际显示的是哪个窗口，用来检测遮挡
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND


def occluded_sample(hwnd: int, samples: int = 5) -> float:
    """在客户区里抽几个点，看有多少比例当前**不是**被这个窗口占着。

    返回 0.0 表示完全没被挡，1.0 表示全被挡住。
    只对 screendc 有意义 —— 因为它抓的是屏幕上的像素。
    """
    info = describe(hwnd)
    if info is None:
        return 1.0
    left, top = info.client_origin
    width, height = info.client_size
    if width <= 0 or height <= 0:
        return 1.0

    points = []
    for i in range(samples):
        fx = (i + 1) / (samples + 1)
        for fy in (0.3, 0.7):
            points.append((int(left + width * fx), int(top + height * fy)))

    hidden = 0
    for x, y in points:
        top_window = user32.WindowFromPoint(wintypes.POINT(x, y))
        if int(top_window) != int(hwnd):
            hidden += 1
    return hidden / len(points) if points else 1.0


def occluding_windows(hwnd: int, grid: int = 6) -> List[dict]:
    """在客户区里打一张网格，统计每个点当前实际压在上面的是哪个窗口。

    返回按覆盖比例降序排列的列表。**只报"挡住了 20%"没用，得说清是谁挡的** ——
    最常见的元凶就是跑这个工具的那个控制台窗口自己。
    """
    info = describe(hwnd)
    if info is None:
        return []
    left, top = info.client_origin
    width, height = info.client_size
    if width <= 0 or height <= 0:
        return []

    counts: dict = {}
    total = 0
    for i in range(grid):
        for j in range(grid):
            x = int(left + width * (i + 0.5) / grid)
            y = int(top + height * (j + 0.5) / grid)
            total += 1
            key = int(user32.WindowFromPoint(wintypes.POINT(x, y)))
            counts[key] = counts.get(key, 0) + 1

    results = []
    for key, hits in sorted(counts.items(), key=lambda kv: -kv[1]):
        is_self = key == int(hwnd)
        entry = {
            "hwnd": key,
            "fraction": hits / total if total else 0.0,
            "self": is_self,
            "title": "",
            "process": "",
        }
        if not is_self and key:
            other = describe(key)
            if other is not None:
                entry["title"] = other.title
                entry["process"] = other.process
        results.append(entry)
    return results


# ---- 控制台窗口自身（用来把自己挪开，免得挡住被截的窗口）

kernel32.GetConsoleWindow.argtypes = []
kernel32.GetConsoleWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL

SW_MINIMIZE = 6
SW_RESTORE = 9


def console_window() -> Optional[int]:
    """当前进程所属的控制台窗口。

    注意: 在 Windows Terminal / VS Code 内置终端里，这里拿到的是一个隐藏的
    伪控制台窗口，最小化它对可见的终端**没有效果**。经典 conhost 窗口才管用。
    """
    hwnd = kernel32.GetConsoleWindow()
    return int(hwnd) if hwnd else None


def minimize_console() -> Optional[int]:
    hwnd = console_window()
    if hwnd:
        user32.ShowWindow(wintypes.HWND(hwnd), SW_MINIMIZE)
    return hwnd


def restore_console(hwnd: Optional[int]) -> None:
    if hwnd:
        user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
