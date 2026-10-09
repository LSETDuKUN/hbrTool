"""Regression coverage for the two localized previousbest counting fixes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from hbr_capture.capture import Frame
from hbr_capture.damage_stats import DamageLedger, resource_state
from hbr_capture.monitor import Monitor, MonitorConfig


def frame(number):
    canvas = Image.new('RGBA', (2048, 1152), 'black')
    with Image.open(Path(__file__).parent / f'fixtures/previousbest/{number}.png') as crop:
        canvas.paste(crop.convert('RGBA'), (900, 440))
    return Frame(2048, 1152, canvas.tobytes('raw', 'BGRA'), non_black_ratio=1)


class CountingFixes(unittest.TestCase):
    def monitor(self, path):
        events = []
        mon = Monitor(MonitorConfig(outdir=path, damage_trigger=True, quiet=True,
                                    on_damage=events.append))
        return mon, events

    def check(self, mon, image, when):
        with patch('hbr_capture.monitor.time.time', return_value=when):
            mon._check_damage(image, 0)

    def test_real_partial_then_complete_replaces_total_and_dp_hp(self):
        with tempfile.TemporaryDirectory() as tmp:
            mon, events = self.monitor(Path(tmp))
            short, complete = frame(350), frame(351)
            for t, image in [(0, short), (.31, short), (.4, complete), (.71, complete), (8, complete)]:
                self.check(mon, image, t)
            self.assertEqual([e['readings'][0]['value'] for e in events], [420483, 420483989])
            self.assertEqual([e['event_id'] for e in events], [1, 1])
            self.assertTrue(events[1]['update'])
            self.assertEqual(mon.damage_hits, 1)
            ledger = DamageLedger()
            for e in events: ledger.add(e)
            self.assertEqual(ledger.total, 420483989)
            self.assertEqual(resource_state(200000000, 500000000, ledger.total).hp, 279516011)
            records = [json.loads(s) for s in (Path(tmp) / 'index.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([r['trigger'] for r in records], ['damage', 'damage_update'])
            self.assertEqual([r['damage_event_id'] for r in records], [1, 1])
            from tools.read_frames import superseded_damage_files, main
            self.assertEqual(superseded_damage_files(Path(tmp), list(Path(tmp).glob('*.png'))),
                             {records[0]['file']})
            import contextlib
            import io
            output = Path(tmp) / 'readings.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--frames', tmp, '--json', str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['total'], 420483989)

    def test_ocr_dropout_with_unchanged_pixels_does_not_restart_long_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            mon, events = self.monitor(Path(tmp))
            image = frame(118)
            for t in [0, .31]: self.check(mon, image, t)
            image = frame(119)  # Same digits over a different real animation frame.
            with patch.object(mon, '_read_damage_frame', return_value=[]):
                for t in [1, 2, 5, 10, 15]: self.check(mon, image, t)
            for t in [16, 16.4]: self.check(mon, image, t)
            self.assertEqual(len(events), 1)
            self.assertEqual(mon.damage_hits, 1)

    def test_actual_blank_allows_another_identical_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            mon, events = self.monitor(Path(tmp))
            image = frame(351)
            for t in [0, .31]: self.check(mon, image, t)
            blank = Frame(2048, 1152, bytes(2048 * 1152 * 4), non_black_ratio=1)
            for t in [1, 1.3]: self.check(mon, blank, t)
            for t in [2, 2.31]: self.check(mon, image, t)
            self.assertEqual([e['event_id'] for e in events], [1, 2])
            from tools.read_frames import superseded_damage_files
            self.assertEqual(superseded_damage_files(Path(tmp), list(Path(tmp).glob('*.png'))), set())

    def test_completion_after_real_blank_is_a_new_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            mon, events = self.monitor(Path(tmp))
            for t in [0, .31]: self.check(mon, frame(350), t)
            blank = Frame(2048, 1152, bytes(2048 * 1152 * 4), non_black_ratio=1)
            for t in [1, 1.3]: self.check(mon, blank, t)
            for t in [2, 2.31]: self.check(mon, frame(351), t)
            self.assertEqual([e['event_id'] for e in events], [1, 2])

    def test_flash_is_not_treated_as_confirmed_disappearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            mon, events = self.monitor(Path(tmp))
            image = frame(351)
            for t in [0, .31]: self.check(mon, image, t)
            white = Frame(2048, 1152, bytes([255]) * (2048 * 1152 * 4), non_black_ratio=1)
            with patch.object(mon, '_read_damage_frame', return_value=[]):
                for t in [1, 1.5, 2]: self.check(mon, white, t)
            for t in [3, 3.4]: self.check(mon, image, t)
            self.assertEqual(len(events), 1)

    def test_background_change_does_not_break_digit_reference(self):
        mon = Monitor(MonitorConfig(quiet=True))
        image = frame(351)
        readings = mon._read_damage_frame(image)
        mon._remember_digits(image, readings)
        pixels = np.frombuffer(image.bgra, np.uint8).reshape(1152, 2048, 4).copy()
        pixels[:300, :, :3] = 255
        changed = Frame(2048, 1152, pixels.tobytes(), non_black_ratio=1)
        self.assertTrue(mon._digits_still_visible(changed))
        self.assertFalse(mon._digits_still_visible(Frame(100, 100, bytes(40000))))
