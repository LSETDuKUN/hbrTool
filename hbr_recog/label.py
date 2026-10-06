"""识别伤害数字上方的标签：`合计伤害` 还是 `平均伤害`。

为什么必须区分：这两个词的含义完全不同 ——
  合计伤害 = 一次动作的**总和**
  平均伤害 = **平均值**
把平均值当总和用会严重低估伤害，而且看起来"数字都读对了"，很难发现。

提取办法：**局部对比度（高通）**。
标签文字是"亮粉色芯 + 暗色描边"，局部对比度高；
而火焰、蓝天这类背景是平滑的，高通之后基本归零。
这比按颜色阈值稳 —— 按颜色在橙色火焰上会彻底失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

from .templates import jaccard

#: 标签相对于数字框的位置。实测 8 帧一致（数字框左边缘稳定在 x≈1390），
#: 但 ROI 要留足余量 —— 太窄会把第一个字切掉，而归一化是按墨迹包围盒做的，
#: 一旦切掉一角，整个位图就跟着错位。
ROI_DX0, ROI_DX1 = -240, 80
ROI_DY0, ROI_DY1 = -96, -8

#: 归一化网格。四个汉字是扁宽的，所以横向格子比数字多。
GRID_H, GRID_W = 24, 72

#: 高通阈值（灰度 0~255）
BLUR_KERNEL = 9
HIGHPASS_THRESHOLD = 12.0

#: 稳健包围盒的门槛：一列/一行至少要有这么多像素才算"文字"。
#: 不做这一步的话，零星几个离群像素会把包围盒撑歪几十个像素，
#: 归一化之后整个位图错位 —— 实测这让分类从 12/12 掉到 6/12。
MIN_COL_PIXELS = 4
MIN_ROW_PIXELS = 3

DEFAULT_TEMPLATES = Path(__file__).resolve().parent / "label_templates.json"

LABEL_TOTAL = "合计"
LABEL_AVERAGE = "平均"


def _mean_filter(a: np.ndarray, k: int) -> np.ndarray:
    """盒子均值滤波（用滑窗，够快且没有 scipy 依赖）。"""
    if k <= 1:
        return a
    pad = k // 2
    padded = np.pad(a, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (k, k))
    return windows.mean(axis=(-1, -2))


def label_region(rgb: np.ndarray, box) -> Optional[np.ndarray]:
    """按数字框裁出标签区域。越界就返回 None。"""
    height, width = rgb.shape[:2]
    x0, y0, x1, y1 = box
    lx0 = max(0, x0 + ROI_DX0)
    lx1 = min(width, x0 + ROI_DX1)
    ly0 = max(0, y0 + ROI_DY0)
    ly1 = min(height, y0 + ROI_DY1)
    if lx1 - lx0 < 20 or ly1 - ly0 < 10:
        return None
    return rgb[ly0:ly1, lx0:lx1]


def label_mask(region: np.ndarray) -> np.ndarray:
    """标签区域 -> 布尔掩码。

    两个判据相乘，缺一不可：
      * **粉色**：R 明显大于 G，且 R-B 不大（火焰的 R-B 很大，蓝天 R<G）
      * **高局部对比度**：文字是"亮芯 + 暗描边"；背景里的粉色区域是平滑的
    只用粉色会在火焰/粉色场景上崩；只用对比度会在水晶、粒子上崩。
    """
    r = region[:, :, 0].astype(np.int32)
    g = region[:, :, 1].astype(np.int32)
    b = region[:, :, 2].astype(np.int32)

    pink = (r - g > 30) & (r - b < 120) & (r > 130) & (g > 60)

    gray = region.astype(np.float32).mean(axis=-1)
    highpass = gray - _mean_filter(gray, BLUR_KERNEL)

    return pink & (highpass > HIGHPASS_THRESHOLD)


def robust_bbox(mask: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """稳健包围盒：忽略只有零星像素的行列。

    直接用 `mask.any()` 求包围盒会被离群像素带偏几十个像素，
    归一化之后文字就错位了。
    """
    if not mask.any():
        return None
    col_counts = mask.sum(axis=0)
    row_counts = mask.sum(axis=1)
    cols = np.where(col_counts >= MIN_COL_PIXELS)[0]
    rows = np.where(row_counts >= MIN_ROW_PIXELS)[0]
    if not len(cols) or not len(rows):
        return None
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def label_bitmap(region: np.ndarray) -> np.ndarray:
    """标签区域 -> 归一化的二值位图。"""
    mask = label_mask(region)
    box = robust_bbox(mask)
    if box is None:
        return np.zeros((GRID_H, GRID_W), dtype=bool)

    y0, y1, x0, x1 = box
    mask = mask[y0:y1, x0:x1]

    src_h, src_w = mask.shape
    ys = (np.arange(GRID_H) * src_h // GRID_H).clip(0, src_h - 1)
    xs = (np.arange(GRID_W) * src_w // GRID_W).clip(0, src_w - 1)
    return mask[np.ix_(ys, xs)]


@dataclass
class LabelResult:
    name: str
    score: float

    @property
    def is_total(self) -> bool:
        return self.name == LABEL_TOTAL

    @property
    def is_average(self) -> bool:
        return self.name == LABEL_AVERAGE


class LabelStore:
    def __init__(self, templates: Optional[Dict[str, np.ndarray]] = None):
        self.templates: Dict[str, np.ndarray] = dict(templates or {})

    def __len__(self) -> int:
        return len(self.templates)

    def classify(self, bitmap: np.ndarray, min_margin: float = 0.08) -> LabelResult:
        """返回最像的标签。

        **类间差不够大就返回「未知」而不是硬猜。**
        这个字段猜错的代价极高（平均值当总和用会差十几倍），
        而实测在已知样本上最小类间差只有 0.02 —— 硬猜迟早出错。
        宁可说不知道。
        """
        if not self.templates:
            return LabelResult("未知", 0.0)

        ranked = sorted(
            ((jaccard(bitmap, bits), name) for name, bits in self.templates.items()),
            reverse=True,
        )
        best_score, best_name = ranked[0]
        if len(ranked) > 1:
            margin = best_score - ranked[1][0]
            if margin < min_margin:
                return LabelResult("未知", best_score)
        return LabelResult(best_name, best_score)

    def read(self, rgb: np.ndarray, box, min_score: float = 0.30,
             min_margin: float = 0.08) -> LabelResult:
        region = label_region(rgb, box)
        if region is None:
            return LabelResult("未知", 0.0)
        result = self.classify(label_bitmap(region), min_margin)
        if result.score < min_score:
            return LabelResult("未知", result.score)
        return result

    # ------------------------------------------------------------ 存取

    def save(self, path) -> None:
        import json

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "grid": {"h": GRID_H, "w": GRID_W},
                    "templates": {
                        name: "".join("1" if b else "0" for b in bits.reshape(-1))
                        for name, bits in self.templates.items()
                    },
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path) -> "LabelStore":
        import json

        source = Path(path)
        if not source.exists():
            return cls()
        data = json.loads(source.read_text(encoding="utf-8"))
        h = int(data.get("grid", {}).get("h", GRID_H))
        w = int(data.get("grid", {}).get("w", GRID_W))
        templates = {}
        for name, bits in data.get("templates", {}).items():
            arr = np.frombuffer(bits.encode("ascii"), dtype=np.uint8) == ord("1")
            templates[name] = arr.reshape(h, w).astype(bool)
        return cls(templates)
