"""调试工具：在某一帧的指定区域跑二值化和切分，把结果画出来看。

用法:
    python tools/inspect_damage.py frames/000036.png
    python tools/inspect_damage.py frames/000036.png --roi 1100 520 1700 660
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbr_recog import segment  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="伤害数字分割调试")
    parser.add_argument("frame", help="帧 PNG 路径")
    parser.add_argument(
        "--roi", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
        default=[1100, 520, 1700, 660], help="关注区域",
    )
    parser.add_argument("--thresh", type=int, default=segment.NEAR_WHITE,
                        help="近白阈值（默认 200）")
    parser.add_argument("--min-row-pixels", type=int, default=40,
                        help="一行至少要有多少近白像素才算文字行（要和采集时一致）")
    parser.add_argument("--out", default="_explore/segments.png")
    parser.add_argument("--zoom", type=int, default=2)
    args = parser.parse_args(argv)

    rgb = segment.load_rgb(args.frame)
    x0, y0, x1, y1 = args.roi
    sub = rgb[y0:y1, x0:x1]

    mask = segment.near_white_mask(sub, args.thresh)
    print(f"区域 {x1-x0}x{y1-y0}  近白像素 {mask.sum()} ({mask.mean():.2%})")

    rows = segment.find_text_rows(mask, min_pixels=args.min_row_pixels)
    print(f"检测到 {len(rows)} 行文字: {rows}")

    glyphs = segment.segment_digits(mask, min_row_pixels=args.min_row_pixels)
    print(f"切出 {len(glyphs)} 个字符:")
    for i, g in enumerate(glyphs):
        print(
            f"  [{i}] box=({g.x0},{g.y0},{g.x1},{g.y1})  "
            f"{g.width}x{g.height}  宽高比={g.aspect:.2f}  像素={int(g.bitmap.sum())}"
        )

    # 画出来看
    canvas = sub.copy()
    overlay = Image.fromarray(canvas)
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], outline=(0, 255, 0))
    for g in glyphs:
        draw.rectangle([g.x0, g.y0, g.x1 - 1, g.y1 - 1], outline=(255, 0, 0))
    for ry0, ry1 in rows:
        draw.line([0, ry0, x1 - x0, ry0], fill=(0, 128, 255))
        draw.line([0, ry1 - 1, x1 - x0, ry1 - 1], fill=(0, 128, 255))

    z = args.zoom
    overlay = overlay.resize((overlay.width * z, overlay.height * z), Image.NEAREST)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(out)
    print(f"\n已存标注图: {out}  ({overlay.width}x{overlay.height})")

    # 顺便把每个字符单独放大拼一张，方便肉眼认字
    if glyphs:
        cell_w, cell_h = segment.GRID_W * 4, segment.GRID_H * 4
        sheet = Image.new("RGB", (cell_w * len(glyphs), cell_h + 16), (20, 20, 30))
        sdraw = ImageDraw.Draw(sheet)
        for i, g in enumerate(glyphs):
            tile = Image.fromarray((g.bitmap * 255).astype(np.uint8))
            tile = tile.convert("RGB").resize((cell_w, cell_h), Image.NEAREST)
            sheet.paste(tile, (i * cell_w, 0))
            sdraw.text((i * cell_w + 4, cell_h + 2), f"[{i}]", fill=(200, 200, 220))
        sheet_path = out.with_name("glyphs_sheet.png")
        sheet.save(sheet_path)
        print(f"字符拼图: {sheet_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
