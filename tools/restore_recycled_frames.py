"""从回收站把被抓帧工具删掉的帧恢复回来。

为什么要写这个：如果 frames/ 里的帧被误删（不管是人删的还是别的工具删的），
它们会躺在回收站里。这个脚本按 $I 元数据里记录的原路径把它们放回去。

回收站里每个被删文件有两个条目:
  $I<id><ext>  元数据：原始路径、大小、删除时间
  $R<id><ext>  实际内容

$I 的格式（Windows 10）:
  0-7    int64  版本号（1 或 2）
  8-15   int64  文件大小
  16-23  int64  删除时间（FILETIME）
  24-27  int32  路径长度（字符数，仅版本 2）
  之后     UTF-16LE 的原始路径，以 \\0 结尾

用法:
    python tools/restore_recycled_frames.py                  # 只看，不还原
    python tools/restore_recycled_frames.py --apply          # 真的还原
    python tools/restore_recycled_frames.py --apply --dir path\\to\\frames
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sys
from pathlib import Path

# 双 null 结尾的路径列表，给 SHFileOperationW 用
FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
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


def recycle(paths) -> bool:
    """把文件送进回收站（可撤销），而不是永久删除。

    session.py 的 discard_run 原本用 Path.unlink()，那是永久删除 ——
    误删就没了。这里走 Shell 的 FO_DELETE + FOF_ALLOWUNDO，
    和你在资源管理器里按 Delete 是一个效果。
    """
    paths = [str(p) for p in paths if Path(p).exists()]
    if not paths:
        return True
    # 必须双 null 结尾
    joined = "\0".join(paths) + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = joined
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.WinDLL("shell32", use_last_error=True).SHFileOperationW(
        ctypes.byref(op)
    )
    return result == 0 and not op.fAnyOperationsAborted


def find_recycle_bins():
    """枚举所有盘的回收站目录（按当前用户 SID）。"""
    bins = []
    for letter in "CDEFGHIJ":
        root = Path(f"{letter}:\\$RECYCLE.BIN")
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name.startswith("S-1-5-21-"):
                    bins.append(child)
        except (PermissionError, OSError):
            continue
    return bins


def parse_i_file(path: Path):
    """读 $I 元数据，返回 (版本, 大小, 原始路径)。"""
    data = path.read_bytes()
    if len(data) < 28:
        return None
    version = int.from_bytes(data[0:8], "little")
    size = int.from_bytes(data[8:16], "little")
    if version >= 2:
        length = int.from_bytes(data[24:28], "little")
        raw = data[28 : 28 + length * 2]
    else:
        raw = data[24 : 24 + 520]
    original = raw.decode("utf-16-le", errors="replace").split("\x00")[0]
    return version, size, original


def scan(target_dir: Path):
    """找出回收站里原本位于 target_dir 的文件。"""
    hits = []
    for bin_dir in find_recycle_bins():
        for i_file in bin_dir.glob("$I*"):
            try:
                parsed = parse_i_file(i_file)
            except Exception:
                continue
            if not parsed:
                continue
            _, size, original = parsed
            if not original:
                continue
            original_path = Path(original)
            if original_path.parent != target_dir:
                continue
            r_file = bin_dir / ("$R" + i_file.name[2:])
            if r_file.exists():
                hits.append((original_path, r_file, size))
    return sorted(hits, key=lambda item: item[0].name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="从回收站恢复被抓帧工具删掉的帧"
    )
    parser.add_argument(
        "--dir",
        default=str(Path(__file__).resolve().parent.parent / "frames"),
        help="目标目录（默认是项目下的 frames/）",
    )
    parser.add_argument("--apply", action="store_true", help="真的执行恢复")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的同名文件")
    args = parser.parse_args(argv)

    target = Path(args.dir).resolve()
    print(f"目标目录: {target}")
    print(f"回收站扫描中...")

    hits = scan(target)
    if not hits:
        print("回收站里没有找到属于该目录的文件。")
        return 0

    total_mb = sum(size for _, _, size in hits) / 1024 / 1024
    print(f"找到 {len(hits)} 个文件，共约 {total_mb:.1f} MB")
    for original, _, size in hits:
        print(f"  {original.name}  {size / 1024:,.0f} KB")

    if not args.apply:
        print()
        print("这是预览。要真的恢复，加 --apply")
        return 0

    print()
    restored = 0
    skipped = 0
    for original, r_file, size in hits:
        if original.exists() and not args.force:
            skipped += 1
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r_file, original)
            restored += 1
        except Exception as exc:
            print(f"  恢复失败 {original.name}: {exc}")

    print(f"已恢复 {restored} 个，跳过 {skipped} 个（已存在）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
