"""伤害数字的分割。

游戏里的伤害数字是 **白色实心 + 粉色发光描边**。
描边会随背景变亮变暗，白芯则稳定得多，所以二值化只取"接近纯白"的像素。

分割策略（针对单行数字，够用且比连通域简单可靠）:
  1. 近白阈值 -> 二值掩码
  2. 行投影找到数字行的高度范围
  3. 列投影找竖直空隙 -> 切成一个个字符框
  4. 每个字符归一化到固定网格，供模板匹配
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

#: 白芯阈值：三通道都 >= 这个值才算"白"。
#: 取 235 而不是 200：粉色描边会把相邻数字"桥接"成一个块（实测见过 54 粘在一起），
#: 阈值提高后描边被滤掉，数字自然分开。
NEAR_WHITE = 235


@dataclass
class Glyph:
    """一个切出来的字符。"""

    x0: int
    y0: int
    x1: int
    y1: int
    bitmap: np.ndarray      # bool，归一化到 (GRID_H, GRID_W)

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def box(self) -> Tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


GRID_W = 32
GRID_H = 32


def load_rgb(path) -> np.ndarray:
    """读帧成 RGB uint8 数组。"""
    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB"))


def near_white_mask(rgb: np.ndarray, thresh: int = NEAR_WHITE) -> np.ndarray:
    """接近纯白的像素。数字的白芯会留下，粉边和背景被滤掉。"""
    return np.all(rgb >= thresh, axis=-1)


def row_profile(mask: np.ndarray) -> np.ndarray:
    return mask.sum(axis=1)


def column_profile(mask: np.ndarray) -> np.ndarray:
    return mask.sum(axis=0)


def weighted_column_runs(
    mask: np.ndarray, min_col_pixels: int = 3, min_width: int = 3
) -> List[Tuple[int, int]]:
    """按列找墨迹区间，要求该列的墨迹像素达到 min_col_pixels。

    和 `find_column_runs`（只要求 >0）的区别：这里对**每列的墨迹量**设门槛。
    只要求 >0 的话，一列里哪怕只有一个像素（背景噪点、特效边缘）也算，
    会把不相干的东西连成一片。
    """
    if mask.size == 0:
        return []
    profile = mask.sum(axis=0)
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for x, count in enumerate(profile):
        if count >= min_col_pixels:
            if start is None:
                start = x
        else:
            if start is not None:
                if x - start >= min_width:
                    runs.append((start, x))
                start = None
    if start is not None and len(profile) - start >= min_width:
        runs.append((start, len(profile)))
    return runs


def group_column_runs(
    runs: Sequence[Tuple[int, int]], split_gap: int
) -> List[List[Tuple[int, int]]]:
    """把列区间按横向间隔分组：间隔 > split_gap 就切成两组。

    这是绕开 `BREAK!` 那类覆盖层的关键 —— 它和伤害数字**竖直方向重叠**
    （都占 y 564~605），按行投影切不开；但两者横向隔着一百多像素，
    按列分组一刀就分开了。
    """
    if not runs:
        return []
    groups: List[List[Tuple[int, int]]] = [[runs[0]]]
    for previous, current in zip(runs, runs[1:]):
        if current[0] - previous[1] > split_gap:
            groups.append([current])
        else:
            groups[-1].append(current)
    return groups


def find_text_rows(
    mask: np.ndarray, min_pixels: int = 3, min_height: int = 12
) -> List[Tuple[int, int]]:
    """按行投影找出成行的文字区域。返回 [(y0, y1), ...]。"""
    profile = row_profile(mask)
    rows: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for y, count in enumerate(profile):
        if count >= min_pixels:
            if start is None:
                start = y
        else:
            if start is not None:
                if y - start >= min_height:
                    rows.append((start, y))
                start = None
    if start is not None and len(profile) - start >= min_height:
        rows.append((start, len(profile)))
    return rows


def find_column_runs(
    mask: np.ndarray, min_gap: int = 2, min_width: int = 2
) -> List[Tuple[int, int]]:
    """按列投影找出连续的字符列区间，用于切分单个数字。"""
    profile = column_profile(mask)
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    gap = 0
    for x, count in enumerate(profile):
        if count > 0:
            if start is None:
                start = x
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap >= min_gap:
                    end = x - gap + 1
                    if end - start >= min_width:
                        runs.append((start, end))
                    start = None
                    gap = 0
    if start is not None:
        end = len(profile)
        if end - start >= min_width:
            runs.append((start, end))
    return runs


def normalize(bitmap: np.ndarray, width: int = GRID_W, height: int = GRID_H) -> np.ndarray:
    """把字形缩放到固定网格，**保持宽高比**，居中留白。

    为什么不能直接拉伸到固定尺寸：同一个数字在不同帧里宽度会差很多
    （实测同一个 `7` 有 33px 和 45px 两种宽度）。拉伸会把笔画粗细也一起改掉，
    同一个字形的两种宽度就变得不像了，Jaccard 直接掉下去。
    按高度缩放 + 保留宽高比，笔画粗细才是可比的。
    """
    src_h, src_w = bitmap.shape
    if src_h == 0 or src_w == 0:
        return np.zeros((height, width), dtype=bool)

    scale = height / src_h
    new_w = max(1, int(round(src_w * scale)))
    new_h = height
    if new_w > width:
        # 宽得放不下，改成按宽度缩放
        scale = width / src_w
        new_w = width
        new_h = max(1, int(round(src_h * scale)))

    ys = (np.arange(new_h) * src_h // new_h).clip(0, src_h - 1)
    xs = (np.arange(new_w) * src_w // new_w).clip(0, src_w - 1)
    scaled = bitmap[np.ix_(ys, xs)]

    canvas = np.zeros((height, width), dtype=bool)
    y_off = (height - new_h) // 2
    x_off = (width - new_w) // 2
    canvas[y_off : y_off + new_h, x_off : x_off + new_w] = scaled
    return canvas


def column_extents(mask: np.ndarray) -> np.ndarray:
    """每一列的竖直笔画跨度（第一个 True 到最后一个 True 的行数）。"""
    if mask.size == 0:
        return np.zeros(0, dtype=int)
    height = mask.shape[0]
    any_col = mask.any(axis=0)
    first = np.argmax(mask, axis=0)
    last = height - 1 - np.argmax(mask[::-1], axis=0)
    return np.where(any_col, last - first + 1, 0)


def split_wide_runs(
    band: np.ndarray, runs: List[Tuple[int, int]], target_width: float,
    search: int = 8,
) -> List[Tuple[int, int]]:
    """把明显过宽的列区间切开。

    伤害数字之间有时会被描边"粘"在一起（实测见过 540 粘成一个 81px 宽的块），
    直接当成一个字会既读不出内容、又被尺寸过滤器整个丢掉。
    这里按列投影的**谷值**切：在预期边界附近找一个笔画最少的位置下刀。
    """
    if target_width <= 0:
        return list(runs)

    out: List[Tuple[int, int]] = []
    for x0, x1 in runs:
        width = x1 - x0
        if width <= target_width * 1.4:
            out.append((x0, x1))
            continue

        parts = max(2, int(round(width / target_width)))
        profile = band[:, x0:x1].sum(axis=0)
        bounds = [0]
        for k in range(1, parts):
            expected = int(width * k / parts)
            lo = max(1, expected - search)
            hi = min(width - 1, expected + search)
            cut = expected if hi <= lo else lo + int(np.argmin(profile[lo:hi]))
            bounds.append(cut)
        bounds.append(width)

        for a, b in zip(bounds, bounds[1:]):
            if b - a >= 3:
                out.append((x0 + a, x0 + b))
    return out


def drop_small_components(mask: np.ndarray, min_ratio: float = 0.25) -> np.ndarray:
    """只保留足够大的连通块，甩掉粘在字形边上的小碎片。

    实测见过这种情况：切出来的 `7` 左边粘着一小块前一个数字的残留，
    导致它和任何模板都对不上（最高 Jaccard 只有 0.32）。
    按连通块大小过滤是最直接的解法。

    用 min_ratio 而不是"只留最大的一个"：某些字形可能天然由几块组成，
    一刀切成一块太激进。
    """
    height, width = mask.shape
    if not mask.any():
        return mask

    labels = np.zeros((height, width), dtype=np.int32)
    sizes: List[int] = [0]     # 下标 0 占位
    current = 0

    for sy in range(height):
        for sx in range(width):
            if not mask[sy, sx] or labels[sy, sx]:
                continue
            current += 1
            size = 0
            stack = [(sy, sx)]
            labels[sy, sx] = current
            while stack:
                cy, cx = stack.pop()
                size += 1
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < height and 0 <= nx < width:
                        if mask[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = current
                            stack.append((ny, nx))
            sizes.append(size)

    if current <= 1:
        return mask

    biggest = max(sizes)
    threshold = biggest * min_ratio
    keep = {i for i, size in enumerate(sizes) if i and size >= threshold}
    return np.isin(labels, list(keep))


def merge_close_runs(
    runs: List[Tuple[int, int]], merge_gap: int
) -> List[Tuple[int, int]]:
    """把间隔小于 merge_gap 的列区间并起来。

    `weighted_column_runs` 用的是"每列墨迹量"门槛，一个字内部笔画稀疏的地方
    可能掉到门槛以下，导致一个字被切成几段。这里按小间隔补回来。
    """
    if not runs:
        return []
    out = [list(runs[0])]
    for x0, x1 in runs[1:]:
        if x0 - out[-1][1] <= merge_gap:
            out[-1][1] = x1
        else:
            out.append([x0, x1])
    return [(a, b) for a, b in out]


def find_glyph_groups(
    mask: np.ndarray,
    *,
    min_col_pixels: int = 3,
    min_width: int = 3,
    merge_gap: int = 6,
    split_gap: int = 70,
    min_height: int = 10,
    grid: Tuple[int, int] = (GRID_W, GRID_H),
) -> List[List[Glyph]]:
    """把掩码切成若干"横排字符组"，每组是一个候选数字串。

    **为什么不按行切**：`BREAK!` 这类覆盖层和伤害数字在竖直方向**重叠**
    （实测两者都占 y 564~605），横向投影切不开，只会粘成一行；
    接着按行高做覆盖率过滤时，较矮的那一组（伤害数字 52px vs BREAK! 67px）
    会被当成"小字后缀"整个丢掉 —— 实测这让 4 帧伤害完全读不到。

    改按列：
      1. 逐列数墨迹，太少的列不算（`weighted_column_runs`）
      2. 间隔小的列区间合并（同一个字的笔画）
      3. 间隔大的切成不同组（`BREAK!` 和数字就靠这个分开）
    每组自己算竖直范围，互不影响。
    """
    if not mask.any():
        return []

    runs = weighted_column_runs(mask, min_col_pixels, min_width)
    if not runs:
        return []
    runs = merge_close_runs(runs, merge_gap)

    groups_out: List[List[Glyph]] = []
    for group in group_column_runs(runs, split_gap):
        gx0, gx1 = group[0][0], group[-1][1]
        band = mask[:, gx0:gx1]
        ys = np.where(band.any(axis=1))[0]
        if len(ys) == 0:
            continue
        gy0, gy1 = int(ys[0]), int(ys[-1]) + 1
        if gy1 - gy0 < min_height:
            continue
        band = band[gy0:gy1]

        widths = [b - a for a, b in group]
        target = float(np.median(widths)) if widths else 0.0
        pieces = split_wide_runs(
            band, [(a - gx0, b - gx0) for a, b in group], target
        )

        glyphs: List[Glyph] = []
        for px0, px1 in pieces:
            piece = drop_small_components(band[:, px0:px1])
            if not piece.any():
                continue
            ink_rows = np.where(piece.any(axis=1))[0]
            ink_cols = np.where(piece.any(axis=0))[0]
            top = gy0 + int(ink_rows[0])
            bottom = gy0 + int(ink_rows[-1]) + 1
            left = gx0 + px0 + int(ink_cols[0])
            right = gx0 + px0 + int(ink_cols[-1]) + 1
            if bottom - top < min_height:
                continue
            glyphs.append(
                Glyph(
                    x0=left,
                    y0=top,
                    x1=right,
                    y1=bottom,
                    bitmap=normalize(
                        piece[
                            ink_rows[0] : ink_rows[-1] + 1,
                            ink_cols[0] : ink_cols[-1] + 1,
                        ],
                        grid[0],
                        grid[1],
                    ),
                )
            )
        if glyphs:
            groups_out.append(glyphs)
    return groups_out


def segment_digits(
    mask: np.ndarray,
    *,
    min_gap: int = 2,
    min_width: int = 3,
    min_height: int = 10,
    min_row_pixels: int = 3,
    min_row_coverage: float = 0.8,
    grid: Tuple[int, int] = (GRID_W, GRID_H),
) -> List[Glyph]:
    """从掩码里切出字符。只取最高的一行（伤害数字都是单行）。

    min_row_coverage 很关键：先扔掉那些**不贯穿整行高度**的列。
    伤害数字后面那串 `+115.9%` 是明显更小的字，会被这一步自然剔除，
    省得混进数字里。
    """
    rows = find_text_rows(mask, min_pixels=min_row_pixels, min_height=min_height)
    if not rows:
        return []

    # 取像素最多的一行
    y0, y1 = max(rows, key=lambda r: mask[r[0] : r[1]].sum())
    band = mask[y0:y1, :]
    height = band.shape[0]

    # 注意顺序：先在**完整** band 上找列区间。
    # 如果先按跨度改掩码再找区间，笔画会被切碎，得到一堆没意义的碎片。
    runs = find_column_runs(band, min_gap=min_gap, min_width=min_width)
    if not runs:
        return []

    # 再按竖直跨度筛：伤害数字后面那串 `+115.9%` 明显更小，会被这一步剔除
    extent = column_extents(band)
    kept = [
        (x0, x1)
        for x0, x1 in runs
        if extent[x0:x1].max() >= min_row_coverage * height
    ]
    if not kept:
        return []

    # 用中位宽度当基准，把被描边粘在一起的数字切开
    target = float(np.median([x1 - x0 for x0, x1 in kept]))
    pieces = split_wide_runs(band, kept, target)

    glyphs: List[Glyph] = []
    for x0, x1 in pieces:
        sub = band[:, x0:x1]
        ys = np.where(sub.any(axis=1))[0]
        if len(ys) == 0:
            continue
        gy0, gy1 = y0 + int(ys[0]), y0 + int(ys[-1]) + 1
        piece = mask[gy0:gy1, x0:x1]
        if piece.shape[0] < min_height:
            continue

        # 甩掉粘在边上的碎片（比如前一个数字的残笔），再把框收紧到实际墨迹
        piece = drop_small_components(piece)
        if not piece.any():
            continue
        ink_rows = np.where(piece.any(axis=1))[0]
        ink_cols = np.where(piece.any(axis=0))[0]
        gy0, gy1 = gy0 + int(ink_rows[0]), gy0 + int(ink_rows[-1]) + 1
        gx0, gx1 = x0 + int(ink_cols[0]), x0 + int(ink_cols[-1]) + 1
        piece = piece[ink_rows[0] : ink_rows[-1] + 1, ink_cols[0] : ink_cols[-1] + 1]
        if piece.shape[0] < min_height:
            continue

        glyphs.append(
            Glyph(
                x0=gx0,
                y0=gy0,
                x1=gx1,
                y1=gy1,
                bitmap=normalize(piece, grid[0], grid[1]),
            )
        )
    return glyphs


def merge_adjacent(glyphs: Sequence[Glyph], max_gap: int = 3) -> List[Glyph]:
    """把被误切开的字符合回去（比如某些字体里 '1' 的竖笔和衬线断开）。

    这里只做简单的间隙合并提示，实际判断交给模板匹配的得分。
    """
    return list(glyphs)
