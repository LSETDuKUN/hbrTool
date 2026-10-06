"""从已知帧构建「合计伤害 / 平均伤害」标签模板，并自检。

标签的含义不同（总和 vs 平均值），混用会严重低估伤害，
所以这张模板表和数字模板一样重要。

用法:
    python tools/build_label_templates.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbr_recog import damage as dmg  # noqa: E402
from hbr_recog import label as label_mod  # noqa: E402

#: 我逐帧放大目视确认过的标签归属
KNOWN = {
    "合计": ["frames/000014", "frames/000015", "frames/000028", "frames/000030",
             "frames/000033", "frames/000035", "frames/000036", "frames2/000024"],
    "平均": ["frames/000037", "frames/000038", "frames/000039", "frames/000042"],
}


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="构建伤害标签模板并自检")
    parser.add_argument("--out", default=str(root / "hbr_recog" / "label_templates.json"))
    parser.add_argument("--montage", default=str(root / "_diag" / "label_bitmaps.png"))
    args = parser.parse_args(argv)

    reader = dmg.DamageReader()
    samples = {name: [] for name in KNOWN}

    for name, rels in KNOWN.items():
        for rel in rels:
            path = root / f"{rel}.png"
            if not path.exists():
                print(f"  跳过（不存在）: {rel}")
                continue
            rgb = np.asarray(Image.open(path).convert("RGB"))
            found = reader.read(rgb)
            if not found:
                print(f"  跳过（读不到数字）: {rel}")
                continue
            region = label_mod.label_region(rgb, found[0].box)
            if region is None:
                print(f"  跳过（标签区越界）: {rel}")
                continue
            samples[name].append(label_mod.label_bitmap(region))
            print(f"  {rel:22} -> {name}  位图 {int(label_mod.label_bitmap(region).sum())} 像素")

    store = label_mod.LabelStore()
    for name, bitmaps in samples.items():
        if not bitmaps:
            print(f"  !! {name} 一个样本都没有")
            continue
        # 用平均位图当模板，比单个样本抗噪
        avg = np.mean(np.stack(bitmaps), axis=0) >= 0.5
        store.templates[name] = avg.astype(bool)

    print(f"\n生成 {len(store)} 个标签模板: {list(store.templates)}")
    store.save(args.out)
    print(f"已保存: {args.out}")

    # ---------------- 自检 ----------------
    print("\n=== 自检：回读所有已知帧 ===")
    loaded = label_mod.LabelStore.load(args.out)
    ok = bad = 0
    worst_margin = 1.0
    for name, rels in KNOWN.items():
        for rel in rels:
            path = root / f"{rel}.png"
            if not path.exists():
                continue
            rgb = np.asarray(Image.open(path).convert("RGB"))
            found = reader.read(rgb)
            if not found:
                continue
            region = label_mod.label_region(rgb, found[0].box)
            bitmap = label_mod.label_bitmap(region) if region is not None else None
            scores = {
                cls: label_mod.jaccard(bitmap, bits)
                for cls, bits in loaded.templates.items()
            }
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            got = ranked[0][0]
            margin = ranked[0][1] - ranked[1][1] if len(ranked) > 1 else ranked[0][1]
            worst_margin = min(worst_margin, margin)
            mark = "OK " if got == name else "错!"
            if got == name:
                ok += 1
            else:
                bad += 1
            detail = "  ".join(f"{k}={v:.2f}" for k, v in ranked)
            print(
                f"  [{mark}] {rel:22} 期望 {name}  实得 {got}  "
                f"差 {margin:+.2f}   {detail}"
            )

    print(f"\n{ok} 对 / {bad} 错   最小类间差 {worst_margin:+.2f}")
    if worst_margin < 0.10:
        print("  !! 类间差偏小，说明模板区分度不够，换场景可能出错")

    # ---------------- 拼图 ----------------
    if store.templates:
        from PIL import ImageDraw

        cell_h, cell_w = label_mod.GRID_H * 4, label_mod.GRID_W * 4
        names = list(store.templates)
        sheet = Image.new("RGB", (cell_w, cell_h * len(names) + 20 * len(names)), (18, 18, 26))
        draw = ImageDraw.Draw(sheet)
        y = 0
        for name in names:
            bits = store.templates[name]
            tile = Image.fromarray((bits * 255).astype(np.uint8)).convert("RGB")
            tile = tile.resize((cell_w, cell_h), Image.NEAREST)
            draw.text((4, y + 2), name, fill=(200, 220, 200))
            sheet.paste(tile, (0, y + 18))
            y += cell_h + 20
        montage = Path(args.montage)
        montage.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(montage)
        print(f"模板拼图: {montage}")

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
