"""把采集到的字形聚类 + 人工标注，固化成模板库。

流程:
    1. 复用 harvest_glyphs 的采集逻辑，拿到所有伤害数字字形
    2. 聚类
    3. 套用 digit_labels.json 的标注
    4. 存成 hbr_recog/templates.json
    5. **自检**：拿已知帧回读，把识别结果显示出来对答案

第 5 步是这个工具存在的意义 —— 模板库不验证等于没建。

用法:
    python tools/build_templates.py
    python tools/build_templates.py --out hbr_recog/templates.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbr_recog import segment, templates  # noqa: E402
from tools import harvest_glyphs as hv  # noqa: E402


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="构建数字模板库并自检")
    parser.add_argument("--frames", default=str(root / "frames"))
    parser.add_argument("--labels", default=str(root / "hbr_recog" / "digit_labels.json"))
    parser.add_argument("--out", default=str(root / "hbr_recog" / "templates.json"))
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--min-run", type=int, default=4)
    parser.add_argument("--min-row-pixels", type=int, default=40)
    parser.add_argument("--y-band", type=int, nargs=2, default=[440, 780])
    parser.add_argument("--min-cluster-size", type=int, default=2,
                        help="样本数少于这个的类丢掉（多半是碎片）")
    args = parser.parse_args(argv)

    frames_dir = Path(args.frames)
    paths = sorted(frames_dir.glob("*.png"))
    if not paths:
        print(f"没有帧: {frames_dir}")
        return 1

    print(f"从 {len(paths)} 帧采集字形...")
    glyphs = []
    for path in paths:
        rgb = segment.load_rgb(path)
        mask = segment.near_white_mask(rgb)
        for _, _, picked in hv.extract_damage_lines(
            mask, tuple(args.y_band), (900, 1950), 35, args.min_run, args.min_row_pixels
        ):
            glyphs.extend(g for g in picked if hv.looks_like_digit(g))
    print(f"采集到 {len(glyphs)} 个字形")

    if not glyphs:
        print("没采到字形。检查 --y-band / --min-run")
        return 1

    clusters = templates.cluster_glyphs(glyphs, args.threshold)
    print(f"聚成 {len(clusters)} 类")

    label_data = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    labels = label_data.get("labels", {})
    if len(labels) != len(clusters):
        print(
            f"  !! 警告: 标注文件里有 {len(labels)} 条，但聚出 {len(clusters)} 类。\n"
            "     索引会漂 —— 重跑 tools/harvest_glyphs.py 对着拼图更新 digit_labels.json。"
        )

    store = templates.TemplateStore()
    skipped = 0
    for index, members in enumerate(clusters):
        char = labels.get(str(index))
        if char is None:
            skipped += 1
            continue
        if len(members) < args.min_cluster_size:
            skipped += 1
            continue
        # 用类内平均字形当模板，比单个样本抗噪
        avg = np.mean(np.stack([glyphs[i].bitmap for i in members]), axis=0) >= 0.5
        store.templates.append(templates.Template(char=char, bits=avg.astype(bool)))

    print(f"生成 {len(store)} 个模板，覆盖字符: {store.chars()}  (跳过 {skipped} 类)")
    missing = set("0123456789") - set(store.chars())
    if missing:
        print(f"  !! 缺字符: {sorted(missing)} —— 读这些数字会出错")

    store.save(args.out)
    print(f"已保存: {args.out}")

    # ---------------- 自检 ----------------
    print("\n=== 自检：回读每一帧 ===")
    hits = 0
    for path in paths:
        rgb = segment.load_rgb(path)
        mask = segment.near_white_mask(rgb)
        lines = hv.extract_damage_lines(
            mask, tuple(args.y_band), (900, 1950), 35, args.min_run, args.min_row_pixels
        )
        if not lines:
            continue
        hits += 1
        for y0, y1, picked in lines:
            text = store.read(picked)
            scores = [s for _, s, _ in store.read_scored(picked)]
            worst = min(scores) if scores else 0.0
            print(f"  {path.name}  y{y0}-{y1}  ->  {text}   最低置信 {worst:.2f}")

    print(f"\n共 {hits} 帧有可读的伤害数字。")
    print("人工核对上面这些数字和帧里实际显示的是否一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
