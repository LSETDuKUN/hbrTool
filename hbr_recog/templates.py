"""模板匹配认字。

思路：游戏的伤害数字是固定字体，所以**不需要通用 OCR**。
把所有帧里切出来的字符聚成若干类，人工标一次，之后就是查表。

匹配用 Jaccard 系数（前景像素的交并比）而不是简单异或：
字体粗细会随描边/缩放略有变化，交并比对笔画粗细更宽容。

模板存成 JSON（每个模板是一串 '0'/'1'），人能直接看懂，出问题好排查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .segment import GRID_H, GRID_W, Glyph


@dataclass
class Template:
    char: str
    bits: np.ndarray  # (GRID_H, GRID_W) bool

    def to_json(self) -> dict:
        flat = "".join("1" if b else "0" for b in self.bits.reshape(-1))
        return {"char": self.char, "w": self.bits.shape[1], "h": self.bits.shape[0], "bits": flat}

    @classmethod
    def from_json(cls, data: dict) -> "Template":
        w, h = int(data["w"]), int(data["h"])
        bits = np.frombuffer(data["bits"].encode("ascii"), dtype=np.uint8) == ord("1")
        return cls(char=str(data["char"]), bits=bits.reshape(h, w).astype(bool))


def jaccard(a: np.ndarray, b: np.ndarray) -> float:
    """前景交并比。1.0 = 完全一样，0.0 = 毫无重叠。"""
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union else 0.0


class TemplateStore:
    def __init__(self, templates: Optional[Sequence[Template]] = None):
        self.templates: List[Template] = list(templates or [])

    def __len__(self) -> int:
        return len(self.templates)

    def chars(self) -> List[str]:
        return sorted({t.char for t in self.templates})

    def classify(self, bits: np.ndarray) -> Tuple[str, float]:
        """返回 (字符, 置信度)。置信度是 Jaccard，0~1。"""
        best_char, best_score = "?", 0.0
        for template in self.templates:
            score = jaccard(bits, template.bits)
            if score > best_score:
                best_char, best_score = template.char, score
        return best_char, best_score

    def read(self, glyphs: Sequence[Glyph], min_score: float = 0.45) -> str:
        """把一串字符读成字符串。置信度太低的字符转成 '?'。"""
        out = []
        for glyph in glyphs:
            char, score = self.classify(glyph.bitmap)
            out.append(char if score >= min_score else "?")
        return "".join(out)

    def read_scored(self, glyphs: Sequence[Glyph], min_score: float = 0.45):
        """同上，但把每字的置信度一并返回，方便排查。"""
        result = []
        for glyph in glyphs:
            char, score = self.classify(glyph.bitmap)
            result.append((char if score >= min_score else "?", score, glyph))
        return result

    # ------------------------------------------------------------ 存取

    def save(self, path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "grid": {"w": GRID_W, "h": GRID_H},
                    "templates": [t.to_json() for t in self.templates],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path) -> "TemplateStore":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Template.from_json(t) for t in data.get("templates", [])])


# ---------------------------------------------------------------- 聚类


def cluster_glyphs(
    glyphs: Sequence[Glyph], threshold: float = 0.72
) -> List[List[int]]:
    """把字形相近的字符聚成类。返回每类的下标列表。

    贪心：每个字符和已有类的"代表"比一次 Jaccard，够像就归进去，否则开新类。
    类内按出现次数降序，所以 [0] 是最常见的那一类。
    """
    clusters: List[Dict] = []

    for index, glyph in enumerate(glyphs):
        placed = False
        for cluster in clusters:
            if jaccard(glyph.bitmap, cluster["rep"]) >= threshold:
                cluster["members"].append(index)
                # 用类内平均字形更新代表，减少噪声影响
                bits = [glyphs[i].bitmap for i in cluster["members"]]
                avg = np.mean(np.stack(bits), axis=0) >= 0.5
                cluster["rep"] = avg
                placed = True
                break
        if not placed:
            clusters.append({"rep": glyph.bitmap.copy(), "members": [index]})

    clusters.sort(key=lambda c: -len(c["members"]))
    return [c["members"] for c in clusters]
