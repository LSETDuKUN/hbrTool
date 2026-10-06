"""HBR 窗口抓帧工具（纯标准库）。

    from hbr_capture import Grabber, Monitor, MonitorConfig

这一层只负责「把游戏窗口的画面拿到手」，不负责看懂它。
识别、模板匹配、数字 OCR 都建立在它之上，所以两者解耦:
以后换抓图方式（比如上 DXGI）不用动识别代码。

零第三方依赖：ctypes 调 Win32，PNG 用 zlib 自己编。
"""

from .capture import Frame, Grabber, frame_diff
from .monitor import Monitor, MonitorConfig
from .session import (
    archive_run,
    current_run_id,
    discard_run,
    read_index,
    remove_run_from_index,
    run_records,
)
from .win32 import (
    CaptureResult,
    WindowInfo,
    capture_window,
    console_window,
    describe,
    find_window,
    find_windows,
    get_foreground_window,
    is_foreground,
    key_down,
    list_windows,
    minimize_console,
    occluded_sample,
    occluding_windows,
    parse_hotkey,
    restore_console,
    set_dpi_aware,
)

__all__ = [
    "CaptureResult",
    "Frame",
    "Grabber",
    "Monitor",
    "MonitorConfig",
    "WindowInfo",
    "archive_run",
    "capture_window",
    "console_window",
    "current_run_id",
    "describe",
    "discard_run",
    "find_window",
    "find_windows",
    "frame_diff",
    "get_foreground_window",
    "is_foreground",
    "key_down",
    "list_windows",
    "minimize_console",
    "occluded_sample",
    "occluding_windows",
    "parse_hotkey",
    "read_index",
    "remove_run_from_index",
    "restore_console",
    "run_records",
    "set_dpi_aware",
]
