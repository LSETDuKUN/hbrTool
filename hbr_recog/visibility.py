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
SEARCH_ROI = (900, 400, 2048, 800)
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
        self.scaled_total = []
        self.pose = (320., 70., 1.)
        self.last_search = 0.
        for name, filename in [('total', 'total-label.png'), ('button', 'action-button.png')]:
            with Image.open(root / filename) as image:
                rgb = np.array(image.convert('RGB'))
                self.refs[name] = cv2.Canny(rgb, 80, 160)
                if name == 'total':
                    self.scaled_total = [(scale, cv2.Canny(cv2.resize(rgb, None,
                        fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR), 80, 160))
                        for scale in (.65, .8, 1., 1.2, 1.5)]

    def check(self, rgb, name):
        region = rgb[10:75, 40:250] if name == 'total' else rgb
        edge = cv2.Canny(region, 80, 160)
        score = float(cv2.matchTemplate(edge, self.refs[name], cv2.TM_CCOEFF_NORMED).max())
        # Full-screen flashes/black-outs are not evidence of a new event.
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        flash = float(np.mean(gray > 245)) > .8 or float(np.std(gray)) < 4
        visible = True if score >= .48 else (None if score >= .22 or flash else False)
        return visible, score

    def prepare_total(self, rgb):
        """Locate the label inside a wider HUD band and align the numeric input.

        The old fixed ROI is the initial pose. Reacquire periodically at several
        label sizes, so a layout/scale change does not silently disable all OCR.
        Visibility is still entirely visual, never inferred from OCR success.
        """
        import time
        def aligned(pose):
            x, y, scale = pose
            matrix = np.array([[1/scale, 0, -x/scale], [0, 1/scale, -y/scale]], np.float32)
            return cv2.warpAffine(rgb, matrix, (770, 195))
        canonical = aligned(self.pose)
        visible, score = self.check(canonical, 'total')
        now = time.monotonic()
        if visible is not True and now - self.last_search >= .1:
            self.last_search = now
            edge = cv2.Canny(rgb, 80, 160)
            best = (score, None)
            for scale, template in self.scaled_total:
                _, candidate, _, location = cv2.minMaxLoc(cv2.matchTemplate(
                    edge, template, cv2.TM_CCOEFF_NORMED))
                pose = (location[0]-60*scale, location[1]-22*scale, scale)
                if candidate >= .30:
                    # Recheck after alignment: font rasterization at a smaller
                    # size changes Canny pixels, while normalized text agrees.
                    _, verified = self.check(aligned(pose), 'total')
                    candidate = max(candidate, verified)
                if candidate > best[0]:
                    best = (candidate, pose)
            if best[1] is not None and best[0] >= .48:
                self.pose = best[1]
                canonical = aligned(self.pose)
                visible, score = True, float(best[0])
            elif best[0] >= .22:
                visible, score = None, float(best[0])
        return canonical, visible, score, self.pose
