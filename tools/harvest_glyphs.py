"""从所有帧里采集伤害数字字形，聚类，出一张拼图供人工标注。

伤害数字的判别特征：**它是画面上最大的白色文字**（高约 50~60px）。
界面里其他白字（DP 数值、TURN 计数）都明显更小，所以按行高过滤就能分开。

用法:
    python tools/harvest_glyphs.py                 # 看每帧采到什么
    python tools/harvest_glyphs.py --save out.json # 把字形存下来
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbr_recog import segment, templates  # noqa: E402


#: 采集和判别逻辑已经挪进库里（hbr_recog.damage），
#: 这里只是复用它，免得调试脚本和正式代码两份实现走偏。
from hbr_recog.damage import extract_damage_lines, looks_like_digit  # noqa: E402,F401


def harvest(frames_dir: Path, min_height: int, verbose: bool,
            y_band, x_band, min_run, min_row_pixels):
    """遍历帧，返回 (所有字形, 每帧的信息)。"""
    all_glyphs = []
    per_frame = []
    for path in sorted(frames_dir.glob("*.png")):
        rgb = segment.load_rgb(path)
        mask = segment.near_white_mask(rgb)
        picked = extract_damage_lines(
            mask, y_band, x_band, min_height, min_run, min_row_pixels
        )

        for _, _, glyphs in picked:
            all_glyphs.extend(glyphs)

        per_frame.append((path.name, [(r[0], r[1], len(r[2])) for r in picked]))
        if verbose:
            desc = ", ".join(f"y{a}-{b}:{n}字" for a, b, n in per_frame[-1][1])
            print(f"  {path.name}  {desc or '(没找到)'}")

    return all_glyphs, per_frame


def render_sheet(clusters, glyphs, out_path: Path, cell: int = 4, per_row: int = 10,
                 max_rows: int = 30):
    """每个类画一行：代表字形 + 该类的前几个样本。

    max_rows 限制渲染多少类 —— 类多的时候整张图会大到没法看。
    """
    clusters = clusters[:max_rows]
    cw, ch = segment.GRID_W * cell, segment.GRID_H * cell
    rows = len(clusters)
    cols = min(per_row, max((len(c) for c in clusters), default=1))
    label_w = 80
    sheet = Image.new("RGB", (label_w + cw * cols, ch * rows + 8), (18, 18, 26))
    draw = ImageDraw.Draw(sheet)

    for r, members in enumerate(clusters):
        y = r * ch
        draw.text((4, y + ch // 2 - 6), f"#{r} x{len(members)}", fill=(200, 200, 220))
        for c, idx in enumerate(members[:cols]):
            tile = Image.fromarray((glyphs[idx].bitmap * 255).astype(np.uint8))
            tile = tile.convert("RGB").resize((cw - 2, ch - 2), Image.NEAREST)
            sheet.paste(tile, (label_w + c * cw + 1, y + 1))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return sheet.size


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="采集并聚类伤害数字字形")
    parser.add_argument("--frames", default="frames")
    parser.add_argument("--min-height", type=int, default=35,
                        help="只取高度 >= 这个值的文字行（伤害数字约 50+）")
    parser.add_argument("--y-band", type=int, nargs=2, default=[440, 760],
                        metavar=("Y0", "Y1"), help="伤害数字所在的纵向范围")
    parser.add_argument("--x-band", type=int, nargs=2, default=[900, 1950],
                        metavar=("X0", "X1"), help="横向范围，避开左侧队伍栏")
    parser.add_argument("--min-run", type=int, default=4,
                        help="一行至少要有几个字才算伤害数字（默认 4）")
    parser.add_argument("--min-row-pixels", type=int, default=40,
                        help="一行至少要有多少近白像素才算文字行（默认 40）")
    parser.add_argument("--threshold", type=float, default=0.72,
                        help="聚类相似度阈值")
    parser.add_argument("--out", default="_explore/glyph_clusters.png")
    parser.add_argument("--max-clusters", type=int, default=30,
                        help="拼图最多渲染多少个类（默认 30）")
    parser.add_argument("--cell", type=int, default=4, help="每个字形的放大倍数")
    parser.add_argument("--save", help="把字形+聚类结果存成 JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    frames_dir = Path(args.frames)
    paths = sorted(frames_dir.glob("*.png"))
    print(f"扫描 {len(paths)} 帧（行高 >= {args.min_height}）")

    glyphs, per_frame = harvest(
        frames_dir, args.min_height, not args.quiet,
        tuple(args.y_band), tuple(args.x_band), args.min_run, args.min_row_pixels,
    )
    print(f"\n采集到 {len(glyphs)} 个字形")

    if not glyphs:
        print("没采到字形，试试放宽 --min-height")
        return 1

    sizes = Counter((g.width // 5 * 5, g.height // 5 * 5) for g in glyphs)
    print("尺寸分布 (宽,高 取整到5):")
    for (w, h), n in sizes.most_common(8):
        print(f"   {w:>3}x{h:<3}  x{n}")

    clusters = templates.cluster_glyphs(glyphs, args.threshold)
    print(f"\n聚成 {len(clusters)} 类")
    for i, members in enumerate(clusters):
        reps = [glyphs[j] for j in members[:3]]
        dims = " ".join(f"{g.width}x{g.height}" for g in reps)
        print(f"  #{i:<3} {len(members):>4} 个   样例尺寸: {dims}")

    out = Path(args.out)
    w, h = render_sheet(clusters, glyphs, out, cell=args.cell,
                        max_rows=args.max_clusters)
    print(f"\n拼图已存: {out}  ({w}x{h})  —— 只画了前 {min(len(clusters), args.max_clusters)} 类")
    print("每一行是一类，左边 #n xN 表示类号和成员数，右边是该类的前几个样本。")

    if args.save:
        payload = {
            "grid": {"w": segment.GRID_W, "h": segment.GRID_H},
            "clusters": [
                {
                    "index": i,
                    "members": len(members),
                    "sample_boxes": [list(glyphs[j].box) for j in members[:5]],
                    "bitmap": "".join(
                        "1" if b else "0" for b in glyphs[members[0]].bitmap.reshape(-1)
                    ),
                }
                for i, members in enumerate(clusters)
            ],
            "per_frame": [{"file": f, "rows": r} for f, r in per_frame],
        }
        Path(args.save).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"数据已存: {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
