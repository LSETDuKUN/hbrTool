"""Fixed HUD landmarks; visibility is independent of numeric recognition.

Reference crops are from the user's 2048 x 1152 CN client session 20261007-220819.
Only tiny, normalized HUD regions are examined. Scores in the uncertain band
hold the event open instead of pretending an OCR failure is a disappearance.
"""
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

TOTAL_ROI = (1220, 470, 1990, 665)
BUTTON_ROI = (1765, 910, 1980, 990)
DIGITS_ROI = (150, 60, 760, 185)  # relative to TOTAL_ROI


def viewport_box(box, size):
    width, height = size
    scale = min(width / 2048, height / 1152)
    ox, oy = (width - 2048 * scale) / 2, (height - 1152 * scale) / 2
    return tuple(round(v * scale + (ox if i % 2 == 0 else oy))
                 for i, v in enumerate(box))


def normalize(rgb, box):
    return cv2.resize(rgb, (box[2] - box[0], box[3] - box[1]),
                      interpolation=cv2.INTER_LINEAR)


class HudVisibility:
    def __init__(self):
        root = Path(__file__).with_name('visual_refs')
        self.refs = {}
        for name, filename in [('total', 'total-label.png'), ('button', 'action-button.png')]:
            with Image.open(root / filename) as image:
                self.refs[name] = cv2.Canny(np.array(image.convert('RGB')), 80, 160)

    def check(self, rgb, name):
        region = rgb[10:75, 40:250] if name == 'total' else rgb
        edge = cv2.Canny(region, 80, 160)
        score = float(cv2.matchTemplate(edge, self.refs[name], cv2.TM_CCOEFF_NORMED).max())
        # Full-screen flashes/black-outs are not evidence of a new event.
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        flash = float(np.mean(gray > 245)) > .8 or float(np.std(gray)) < 4
        visible = True if score >= .50 else (None if score >= .22 or flash else False)
        return visible, score
