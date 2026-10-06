"""最小 PNG 编码器（纯标准库：zlib + struct）。

为什么不用 Pillow: 抓屏这一层保持零第三方依赖，随便哪个 Python 都能跑。
BGRA -> RGB 用 bytearray 的步长切片赋值完成，不需要 numpy，速度也够
（1280x720 大约 30ms 一张，回合制场景绰绰有余）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def bgra_to_scanlines(bgra: bytes, width: int, height: int) -> bytes:
    """BGRA 缓冲区 -> PNG 需要的扫描行（每行前置一个 filter type 0）。"""
    expected = width * height * 4
    if len(bgra) != expected:
        raise ValueError(
            f"像素缓冲区大小不对: 期望 {expected} 字节，收到 {len(bgra)}"
        )

    stride = width * 4
    view = memoryview(bgra)
    out = bytearray()

    for y in range(height):
        row = view[y * stride : (y + 1) * stride]
        out.append(0)  # filter type: None
        rgb = bytearray(width * 3)
        rgb[0::3] = bytes(row[2::4])  # R
        rgb[1::3] = bytes(row[1::4])  # G
        rgb[2::3] = bytes(row[0::4])  # B（第 4 字节是 alpha，丢掉）
        out += rgb

    return bytes(out)


def encode_png(bgra: bytes, width: int, height: int, level: int = 6) -> bytes:
    raw = bgra_to_scanlines(bgra, width, height)
    return (
        PNG_MAGIC
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, level))
        + _chunk(b"IEND", b"")
    )


def write_png(path, bgra: bytes, width: int, height: int, level: int = 6) -> int:
    data = encode_png(bgra, width, height, level)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)
