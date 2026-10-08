"""Audit sparse saved frames without inventing unsaved appearance boundaries.

Usage: python -m tools.replay_damage_events results/20261007-220819 --output report.json
Each row is independent. Event accounting is tested separately with timed frames.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from hbr_recog.damage import DamageReader
from hbr_recog.visibility import (BUTTON_ROI, DIGITS_ROI, TOTAL_ROI,
                                  HudVisibility, normalize, viewport_box)


def replay(root):
    detector = HudVisibility()
    reader = DamageReader(min_run=1, label_store=False)
    rows = []
    for record in map(json.loads, (root / 'index.jsonl').read_text(encoding='utf-8').splitlines()):
        with Image.open(root / record['file']) as image:
            rgb = np.array(image.convert('RGB'))
        start = time.perf_counter()
        regions = {}
        if record.get('trigger') == 'ocr_sample':
            regions['total'] = normalize(rgb, TOTAL_ROI)
        else:
            for name, box in [('total', TOTAL_ROI), ('button', BUTTON_ROI)]:
                x, y, X, Y = viewport_box(box, (rgb.shape[1], rgb.shape[0]))
                regions[name] = normalize(rgb[y:Y, x:X], box)
        visible, score = detector.check(regions['total'], 'total')
        button, button_score = (detector.check(regions['button'], 'button')
                                if 'button' in regions else (None, None))
        detection_ms = (time.perf_counter() - start) * 1000
        readings = []
        if visible:
            x, y, X, Y = DIGITS_ROI
            readings = [asdict(r) for r in reader.read_total_band(regions['total'][y:Y, x:X])]
        rows.append(dict(seq=record['seq'], file=record['file'], trigger=record['trigger'],
                         total_visible=visible, total_score=round(score, 4),
                         button_visible=button, button_score=round(button_score, 4) if button_score is not None else None,
                         readings=readings, detection_ms=round(detection_ms, 3)))
    return dict(source=str(root), kind='sparse_frame_audit', frames=len(rows),
                limitation='触发截图不连续；不能据此计算真实事件数、漏抓率或两事件间的空白时长。', rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.session)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    times = [r['detection_ms'] for r in report['rows']]
    print(json.dumps(dict(frames=report['frames'], visible=sum(r['total_visible'] is True
          for r in report['rows']), detection_median_ms=float(np.median(times)),
          detection_p95_ms=float(np.percentile(times, 95))), ensure_ascii=False))
