"""把文件送进回收站，而不是永久删除。

为什么单独做一个模块: `Path.unlink()` 是**永久删除**，误删就没了。
抓帧的会话清理会一次性删掉几十个文件，用永久删除太危险 ——
用户在资源管理器里按 Delete 都会进回收站，工具没理由比他更狠。

实现走 Shell 的 SHFileOperationW + FOF_ALLOWUNDO，效果和资源管理器按 Delete 一致。
（这个 API 官方标记为 deprecated，但 IFileOperation COM 要写几百行，不值当。）
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Iterable, List

FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


class SHFILEOPSTRUCTW(ctypes.Structure):
    # 注意字段顺序和类型必须和 Win32 一致，否则 shell32 会读到垃圾
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def recycle(paths: Iterable) -> bool:
    """把文件/目录送进回收站。返回是否成功。

    传入的路径会先过滤掉不存在的，不会因为一个文件没了就整体失败。
    """
    existing: List[str] = [str(p) for p in paths if Path(p).exists()]
    if not existing:
        return True

    # pFrom 必须是双 null 结尾的字符串
    joined = "\0".join(existing) + "\0\0"

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = joined
    op.pTo = None
    op.fFlags = (
        FOF_ALLOWUNDO
        | FOF_NOCONFIRMATION
        | FOF_SILENT
        | FOF_NOERRORUI
        | FOF_WANTNUKEWARNING
    )

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    result = shell32.SHFileOperationW(ctypes.byref(op))

    if result != 0:
        return False
    if op.fAnyOperationsAborted:
        return False
    # 真的没了吗？顺手确认一下
    return not any(Path(p).exists() for p in existing)
