"""从帧里读伤害数字。

把「找伤害数字在哪 -> 切字 -> 查模板」这条链路封成一个模块，
tools/ 下的调试脚本和以后的实时识别都用它，避免逻辑两份。

伤害数字的判别特征（实测总结）:
  * 白色实心 + 粉色发光描边 -> 近白阈值能滤掉描边和 UI 彩字
  * 画面上**最大的白色文字**（约 52~56px 高），比界面其他白字大一圈
  * 出现在画面中上部的一条固定纵带里（实测 y 约 560~620）
  * 是一段连续、高度一致、至少四五位的横排
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

from . import segment, templates

#: 伤害数字的字形尺寸范围（在参考分辨率下实测约 52x21~39）
DIGIT_MIN_H, DIGIT_MAX_H = 40, 90
DIGIT_MIN_W, DIGIT_MAX_W = 12, 60

#: 模板和坐标都是按这个分辨率定的。游戏窗口尺寸会变（见过 2048x1152 和
#: 1920x1095），所以识别前要按实际宽度缩一遍，否则尺寸过滤会静默失效。
REFERENCE_WIDTH = 2048

#: 伤害数字出现的纵向范围（参考分辨率）。实测很稳定，可以当锚点用。
DEFAULT_Y_BAND = (440, 780)
#: 横向范围，避开左侧队伍栏
DEFAULT_X_BAND = (900, 1950)

DEFAULT_TEMPLATES = Path(__file__).resolve().parent / "templates.json"


def scale_band(band: Tuple[int, int], scale: float) -> Tuple[int, int]:
    return (int(round(band[0] * scale)), int(round(band[1] * scale)))


def looks_like_digit(glyph: segment.Glyph, scale: float = 1.0) -> bool:
    """按尺寸和宽高比判断是不是数字字形，滤掉小后缀字和细长条。

    scale 是当前帧相对参考分辨率的比例，尺寸门槛要跟着缩放，
    否则窗口一小就什么都卡掉了。
    """
    min_h, max_h = DIGIT_MIN_H * scale, DIGIT_MAX_H * scale
    min_w, max_w = DIGIT_MIN_W * scale, DIGIT_MAX_W * scale
    if not (min_h <= glyph.height <= max_h):
        return False
    if not (min_w <= glyph.width <= max_w):
        return False
    return 0.18 <= glyph.aspect <= 0.95


def split_by_height(
    glyphs: List[segment.Glyph], tolerance: float = 4.0
) -> List[List[segment.Glyph]]:
    """把按 x 排好序的字形切成若干"高度一致"的连续段。

    `BREAK!` 这类覆盖层和伤害数字**高度差很明显**（实测 67~68 vs 51~53），
    但横向间隔有时不够大，会被归进同一组，然后高度方差检查把整组否决 ——
    连带里面本来读得好好的数字一起丢掉（实测 `32774` 就是这么丢的）。
    按高度再切一刀，数字那一段就独立出来了。
    """
    if not glyphs:
        return []
    out: List[List[segment.Glyph]] = [[glyphs[0]]]
    for glyph in glyphs[1:]:
        heights = [g.height for g in out[-1]] + [glyph.height]
        if max(heights) - min(heights) <= tolerance:
            out[-1].append(glyph)
        else:
            out.append([glyph])
    return out


def extract_damage_lines(
    mask: np.ndarray,
    y_band: Tuple[int, int] = DEFAULT_Y_BAND,
    x_band: Tuple[int, int] = DEFAULT_X_BAND,
    min_height: int = 35,
    min_run: int = 4,
    min_row_pixels: int = 40,
    scale: float = 1.0,
) -> List[Tuple[int, int, List[segment.Glyph]]]:
    """在指定纵带里找"横排大号白字"，返回 [(y0, y1, 字形列表), ...]。

    先按行分离数字上方的标签，再交给 `segment.find_glyph_groups`
    按列分离与数字竖直重叠的 `BREAK!`。两个方向的分组缺一不可。

    几道过滤缺一不可:
      min_run        伤害数字至少这么多个字；不足的说明是别的 UI 文字
      高度方差       同一组数字高度应几乎一致，混进别的东西就会散开
      尺寸           `looks_like_digit` 滤掉 `+115.9%` 那种小字后缀和细长条

    scale 用来适配不同的窗口尺寸，见 REFERENCE_WIDTH。
    """
    y0, y1 = y_band
    x0, x1 = x_band
    band_mask = np.zeros_like(mask)
    band_mask[y0:y1, x0:x1] = mask[y0:y1, x0:x1]

    scaled_min_height = max(1, int(round(min_height * scale)))

    # 两道分组缺一不可，因为它们解决的是两个正交的问题：
    #   按行  —— 把标签「合计伤害」（在数字上方）和数字分开
    #   按列  —— 把 BREAK! 这类覆盖层（和数字竖直重叠）和数字分开
    # 只用行分不开 BREAK!，只用列分不开标签。
    rows = segment.find_text_rows(
        band_mask,
        # 门槛要给小：3 位数的伤害每行墨迹本来就不多，
        # 用原来的 40 会把整行判成"没有文字"，短数字全部漏掉。
        min_pixels=max(4, int(round(10 * scale))),
        min_height=scaled_min_height,
    )

    picked = []
    for ry0, ry1 in rows:
        row = np.zeros_like(band_mask)
        row[ry0:ry1] = band_mask[ry0:ry1]

        for glyphs in segment.find_glyph_groups(
            row,
            min_col_pixels=max(2, int(round(3 * scale))),
            min_width=max(2, int(round(3 * scale))),
            merge_gap=max(2, int(round(6 * scale))),
            split_gap=max(20, int(round(70 * scale))),
            min_height=scaled_min_height,
        ):
            glyphs = [g for g in glyphs if looks_like_digit(g, scale)]

            # 组内再按高度切：`BREAK!` 和数字高度差明显，但横向可能挨得近
            for run in split_by_height(glyphs, tolerance=max(2.0, 4.0 * scale)):
                if len(run) < min_run:
                    continue
                heights = np.array([g.height for g in run])
                if heights.std() > 3.0 * scale:
                    continue

                gy0 = min(g.y0 for g in run)
                gy1 = max(g.y1 for g in run)
                picked.append((gy0, gy1, run))

    # 按纵向位置排序，保证输出稳定
    picked.sort(key=lambda item: (item[0], item[2][0].x0))
    return picked


@dataclass
class DamageRead:
    """一次伤害数字的读取结果。"""

    value: int
    text: str
    confidence: float
    box: Tuple[int, int, int, int]
    digits: int
    unresolved: int = 0          # 读成 '?' 的位数
    label: str = "未知"           # "合计" / "平均" / "未知"
    label_score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.unresolved == 0

    @property
    def is_total(self) -> bool:
        """标签明确是「合计伤害」—— 这才是可以当总和用的读数。"""
        return self.label == "合计"

    @property
    def is_average(self) -> bool:
        return self.label == "平均"

    def __str__(self) -> str:
        flag = "" if self.ok else f"  [{self.unresolved} 位没认出来]"
        return (
            f"{self.label}伤害 {self.value:,}  "
            f"(数字 {self.confidence:.2f} / 标签 {self.label_score:.2f}){flag}"
        )


def _as_rgb(source) -> np.ndarray:
    """既接受已经载入的 RGB 数组，也接受帧文件路径。

    做成兼容的是因为传错类型（把 Path 当数组）报出来的是 numpy 的类型错误，
    信息量很低，不如直接在这里兜住。
    """
    if isinstance(source, np.ndarray):
        return source
    return segment.load_rgb(source)


class DamageReader:
    """伤害数字读取器。"""

    def __init__(
        self,
        store: Optional[templates.TemplateStore] = None,
        min_score: float = 0.45,
        y_band: Tuple[int, int] = DEFAULT_Y_BAND,
        x_band: Tuple[int, int] = DEFAULT_X_BAND,
        min_run: int = 4,
        min_row_pixels: int = 40,
        short_run_confidence: float = 0.70,
        scale: float = 1.0,
        label_store=None,
    ):
        if store is None:
            store = templates.TemplateStore.load(DEFAULT_TEMPLATES)
        self.store = store
        self.min_score = min_score
        self.y_band = y_band
        self.x_band = x_band
        self.min_run = min_run
        self.min_row_pixels = min_row_pixels
        #: 只有 1~2 个字时要求的最低置信度。短数字（小额伤害，比如 `8`）
        #: 确实是真实存在的，但 1 个字太容易和别的 UI 文字撞上，
        #: 所以对短串要更严 —— 实测真的 `8` 置信 0.94，`BREAK!` 误读出的 `8` 只有 0.52。
        self.short_run_confidence = short_run_confidence
        self.scale = scale

        # 标签识别器（合计伤害 vs 平均伤害）。
        # 优先用 RapidOCR；没装就退回模板匹配；都没有就一律「未知」。
        # 两条路都不会让数字本身的读取失败。
        if label_store is None:
            from . import label_ocr

            label_store = label_ocr.make_label_reader()
        self.label_store = label_store

    def for_frame(self, width: int, height: int) -> "DamageReader":
        """按实际帧尺寸派生一个缩放过的识别器。

        模板和坐标都是按 2048 宽定的，游戏窗口尺寸会变，不缩放的话
        尺寸过滤会静默失效 —— 表现就是"什么都读不到"，很难查。
        """
        scale = width / REFERENCE_WIDTH
        if abs(scale - 1.0) < 0.01:
            return self
        return DamageReader(
            store=self.store,
            min_score=self.min_score,
            y_band=scale_band(self.y_band, scale),
            x_band=scale_band(self.x_band, scale),
            min_run=self.min_run,
            min_row_pixels=max(1, int(round(self.min_row_pixels * scale))),
            short_run_confidence=self.short_run_confidence,
            scale=scale,
            label_store=self.label_store,
        )

    def read_band(self, rgb_band: np.ndarray) -> List[DamageRead]:
        """识别一个**已经裁到伤害带**的图。

        实时抓帧时用这个：整帧识别要 31ms，只对伤害带识别只要 4.4ms。
        """
        height, width = rgb_band.shape[:2]
        return self._read_with_bands(rgb_band, (0, height), (0, width))

    def read(self, source) -> List[DamageRead]:
        """从一整帧里读出所有伤害数字（一帧可能有多个）。"""
        rgb = _as_rgb(source)
        return self._read_with_bands(rgb, self.y_band, self.x_band)

    def _read_with_bands(self, rgb, y_band, x_band) -> List[DamageRead]:
        mask = segment.near_white_mask(rgb)
        lines = extract_damage_lines(
            mask, y_band, x_band,
            min_height=max(1, int(round(35 * self.scale))),
            min_run=self.min_run,
            min_row_pixels=self.min_row_pixels,
            scale=self.scale,
        )

        results: List[DamageRead] = []
        for _, _, glyphs in lines:
            scored = self.store.read_scored(glyphs, self.min_score)
            text = "".join(char for char, _, _ in scored)
            if not any(char.isdigit() for char in text):
                continue
            values = [score for _, score, _ in scored]
            unresolved = sum(1 for char, _, _ in scored if char == "?")
            digits_text = "".join(char for char, _, _ in scored if char.isdigit())
            if not digits_text:
                continue
            # 未认出来的字太多说明这根本不是数字串。
            # 实测 `BREAK!` 会被读成 `8????` —— 只要求"含数字"的话，
            # 它会被当成伤害值 8 存下来，是纯假阳性。
            if unresolved * 4 > len(scored):
                continue

            # 短串（1~2 位）要求更高的置信度，见 short_run_confidence
            worst = min(values) if values else 0.0
            if len(scored) < 3 and worst < self.short_run_confidence:
                continue
            x0 = min(g.x0 for g in glyphs)
            y0 = min(g.y0 for g in glyphs)
            x1 = max(g.x1 for g in glyphs)
            y1 = max(g.y1 for g in glyphs)

            # 标签决定这个数字是"总和"还是"平均值"，含义天差地别，必须一起读。
            # 注意：标签是「平均伤害」时，总和 = 平均值 × 怪物数。
            label_result = self.label_store.read(rgb, (x0, y0, x1, y1))
            results.append(
                DamageRead(
                    value=int(digits_text),
                    text=text,
                    confidence=min(values) if values else 0.0,
                    box=(x0, y0, x1, y1),
                    digits=len(scored),
                    unresolved=unresolved,
                    label=label_result.name,
                    label_score=label_result.score,
                )
            )
        return results

    def read_one(self, source) -> Optional[DamageRead]:
        """只取置信度最高的那个。"""
        found = self.read(source)
        if not found:
            return None
        return max(found, key=lambda r: r.confidence)

    def read_file(self, path) -> List[DamageRead]:
        """读文件。参数也可以直接给数组。"""
        return self.read(path)
