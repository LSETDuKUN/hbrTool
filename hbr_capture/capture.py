"""帧对象、抓帧、变化检测、落盘。

这一层把 win32.capture_window 的裸字节包成 Frame，并提供:
  * Frame.save()        存成 PNG（或原始 .bgra，给后续识别用，零解码开销）
  * frame_diff()        两帧差异 0.0~1.0，用于"画面变了才存"
  * Grabber             记住目标窗口，窗口重启后能自动重新找到
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import png as png_mod
from . import win32


@dataclass
class Frame:
    width: int
    height: int
    bgra: bytes
    method: str = "?"
    non_black_ratio: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.non_black_ratio > 0.02

    @property
    def size(self) -> tuple:
        return (self.width, self.height)

    def digest(self) -> str:
        return hashlib.md5(self.bgra).hexdigest()

    def save(self, path, fmt: str = "png", level: int = 6) -> int:
        """返回写出的字节数。fmt: "png" | "raw"。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "png":
            return png_mod.write_png(target, self.bgra, self.width, self.height, level)
        if fmt == "raw":
            header = json.dumps(
                {
                    "width": self.width,
                    "height": self.height,
                    "format": "BGRA",
                    "timestamp": self.timestamp,
                }
            )
            blob = self.bgra
            target.with_suffix(target.suffix + ".json").write_text(
                header, encoding="utf-8"
            )
            target.write_bytes(blob)
            return len(blob)
        raise ValueError(f"未知的保存格式: {fmt!r}")


def frame_diff(a: Frame, b: Frame, step: int = 997) -> float:
    """抽样估算两帧的平均像素差，0.0 表示完全一样，1.0 表示天差地别。

    step 是采样步长（单位: 像素）。默认大约每 1000 个像素取一个，
    1280x720 下只比 ~900 次，够快也够灵敏。
    """
    if a.size != b.size:
        return 1.0
    va = memoryview(a.bgra)
    vb = memoryview(b.bgra)
    total = 0
    count = 0
    for offset in range(0, len(va) - 4, step * 4):
        total += (
            abs(va[offset] - vb[offset])
            + abs(va[offset + 1] - vb[offset + 1])
            + abs(va[offset + 2] - vb[offset + 2])
        )
        count += 3
    return (total / count) / 255.0 if count else 0.0


class Grabber:
    """盯住一个窗口。

    可以用匹配串（match）或直接给 hwnd。
    用匹配串是为了游戏重启后能自己找回来；但像 HBR 国服这种
    「游戏和启动器同进程名」的情况，匹配串会撞上，这时候用 hwnd 钉死更稳。
    """

    def __init__(
        self,
        match: Optional[str] = None,
        *,
        hwnd: Optional[int] = None,
        method: str = "auto",
        min_client: int = 200,
    ):
        self.match = match
        self.hwnd = hwnd
        self.method = method
        self.min_client = min_client
        self._info: Optional[win32.WindowInfo] = None

    def _resolve(self) -> win32.WindowInfo:
        if self.hwnd is not None:
            info = win32.describe(self.hwnd)
            if info is None:
                raise LookupError(
                    f"hwnd 0x{self.hwnd:08X} 已经不存在了（游戏关掉了？）"
                )
            return info
        if self.match:
            return win32.find_window(self.match, min_client=self.min_client)
        # 什么都没给 -> 自动识别游戏窗口
        return win32.find_game_window(min_client=max(self.min_client, 400))

    @property
    def info(self) -> win32.WindowInfo:
        if self._info is None or not self._alive():
            self._info = self._resolve()
        return self._info

    def _alive(self) -> bool:
        if self._info is None:
            return False
        try:
            current = win32.describe(self._info.hwnd)
        except Exception:
            return False
        return current is not None

    def refresh(self) -> win32.WindowInfo:
        self._info = self._resolve()
        return self._info

    def grab(self) -> Frame:
        info = self.info
        result = win32.capture_window(info.hwnd, self.method)
        self.method = result.method  # auto 探测到有效方法后固定下来
        return Frame(
            width=result.width,
            height=result.height,
            bgra=result.bgra,
            method=result.method,
            non_black_ratio=result.non_black_ratio,
        )
