"""用 RapidOCR 读伤害数字上方的标签。

模板匹配那套（`label.py`）在已知样本上 12/12 全对，但**最小类间差只有 0.02**，
也就是说区分度很薄、换个场景就可能错。而一个真实的 OCR 读「合计伤害 / 平均伤害」
这种印刷体是碾压级的任务，没必要硬撑模板匹配。

这个模块是**可选增强**：
  * 装了 rapidocr-onnxruntime  -> 用 OCR
  * 没装                        -> 自动退回模板匹配（label.LabelStore）

这样即使换台机器没装 OCR，整条链路照样能跑，只是标签会有较多「未知」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from . import label as label_mod

#: 关键词。OCR 可能把「伤害」读成「伤害」之外的近似字，所以只要前两个字匹配就够了。
TOTAL_KEYWORD = "合计"
AVERAGE_KEYWORD = "平均"

#: OCR 前把标签区域放大，小字识别率高很多
UPSCALE = 3


def rapidocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


class OcrLabelReader:
    """用 RapidOCR 认标签。接口和 `label.label.Store` 一致（有 read 方法）。

    **只调 text_rec，不走完整流程。** 因为标签位置是我们自己按数字框裁出来的，
    根本不需要文字检测 —— 而 text_det 正是耗时大头（实测 967ms vs 121ms）。
    """

    def __init__(self, upscale: int = UPSCALE):
        from rapidocr_onnxruntime import RapidOCR

        self.engine = RapidOCR()
        self.upscale = upscale

    def _recognise(self, region: np.ndarray):
        """对一小块图跑识别，返回 [(文本, 置信度), ...]。"""
        from PIL import Image

        image = Image.fromarray(region)
        if self.upscale > 1:
            image = image.resize(
                (image.width * self.upscale, image.height * self.upscale),
                Image.LANCZOS,
            )
        # RapidOCR 走的是 OpenCV 那套，期望 BGR
        array = np.asarray(image)[:, :, ::-1].copy()

        rec = getattr(self.engine, "text_rec", None)
        try:
            if rec is not None:
                result, _ = rec(array)
            else:
                result, _ = self.engine(array)
        except Exception:
            return []
        if not result:
            return []

        texts = []
        for item in result:
            try:
                text, score = item[0], item[1]
            except (IndexError, TypeError):
                continue
            if text:
                texts.append((str(text), float(score)))
        return texts

    def read(self, rgb: np.ndarray, box, min_score: float = 0.4):
        region = label_mod.label_region(rgb, box)
        if region is None:
            return label_mod.LabelResult("未知", 0.0)

        for text, score in self._recognise(region):
            if score < min_score:
                continue
            if TOTAL_KEYWORD in text:
                return label_mod.LabelResult(label_mod.LABEL_TOTAL, score)
            if AVERAGE_KEYWORD in text:
                return label_mod.LabelResult(label_mod.LABEL_AVERAGE, score)
        return label_mod.LabelResult("未知", 0.0)


class FallbackLabelReader:
    """优先 OCR，不可用就退回模板匹配。

    两条路都不通时返回「未知」—— 这个字段上"说不知道"永远优于"猜"。
    """

    def __init__(self, template_path=None, prefer_ocr: bool = True):
        self.template = label_mod.LabelStore.load(
            template_path or label_mod.DEFAULT_TEMPLATES
        )
        self.ocr: Optional[OcrLabelReader] = None
        self.ocr_error: Optional[str] = None

        if prefer_ocr and rapidocr_available():
            try:
                self.ocr = OcrLabelReader()
            except Exception as exc:       # 模型下载失败之类
                self.ocr_error = f"{type(exc).__name__}: {exc}"

    @property
    def backend(self) -> str:
        if self.ocr is not None:
            return "rapidocr"
        if len(self.template):
            return "template"
        return "none"

    def read(self, rgb: np.ndarray, box, min_score: float = 0.4):
        if self.ocr is not None:
            result = self.ocr.read(rgb, box, min_score)
            if result.name != "未知":
                return result
            # OCR 没读出来，模板再试一次
        return self.template.read(rgb, box)


_shared_reader: Optional["FallbackLabelReader"] = None


def make_label_reader(prefer_ocr: bool = True, template_path=None):
    """工厂：返回一个带 read(rgb, box) 的标签读取器。

    **结果会缓存。** 初始化 RapidOCR 要 ~2 秒（加载 ONNX 模型），
    每次 new 一个 DamageReader 都重来一遍的话，测试和实时循环都受不了。
    """
    global _shared_reader
    if _shared_reader is None:
        _shared_reader = FallbackLabelReader(template_path, prefer_ocr=prefer_ocr)
    return _shared_reader


def reset_shared_reader() -> None:
    """丢掉缓存的读取器（测试用，或者想换配置时）。"""
    global _shared_reader
    _shared_reader = None
