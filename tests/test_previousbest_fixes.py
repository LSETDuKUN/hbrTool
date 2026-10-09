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
from hbr_recog.damage import DamageRead


def frame(number):
    canvas = Image.new('RGBA', (2048, 1152), 'black')
    with Image.open(Path(__file__).parent / f'fixtures/previousbest/{number}.png') as crop:
        canvas.paste(crop.convert('RGBA'), (900, 440))
    return Frame(2048, 1152, canvas.tobytes('raw', 'BGRA'), non_black_ratio=1)


class CountingFixes(unittest.TestCase):
    def test_native_components_preserve_four_neighbour_filter(self):
        from hbr_recog.segment import drop_small_components
        import sys
        rng = np.random.default_rng(4)
        for density in (0, .1, .5, .9, 1):
            mask = rng.random((60, 45)) < density
            accelerated = drop_small_components(mask)
            with patch.dict(sys.modules, {'cv2': None}):
                reference = drop_small_components(mask)
            np.testing.assert_array_equal(accelerated, reference)

    def test_real_persistently_unknown_digit_is_repaired_but_known_digits_are_protected(self):
        from hbr_recog.damage import DamageReader
        from types import SimpleNamespace
        with Image.open(Path(__file__).parent / 'fixtures/previousbest/blocked-digit.png') as im:
            rgb = np.array(im.convert('RGB'))
        reader = DamageReader(min_run=1, repair_unknown=True)
        if getattr(reader.label_store, 'ocr', None) is None:
            self.skipTest('RapidOCR is not available')
        self.assertEqual([(r.value, r.unresolved) for r in reader.read_band(rgb, False)], [(2793129, 0)])
        fake = SimpleNamespace(ocr=SimpleNamespace(_recognise=lambda row: [('9,793,129', .99)]))
        reader.label_store = fake
        self.assertEqual([(r.text, r.unresolved) for r in reader.read_band(rgb, False)], [('279?129', 1)])

    def test_actual_recent_clear_and_damaged_readings_recover_both_directions(self):
        for sequence in [('today-47', 'today-47', 'today-48', 'today-48', 'today-47'),
                         ('today-48', 'today-48', 'today-47', 'today-47')]:
            with tempfile.TemporaryDirectory() as tmp:
                mon, events = self.monitor(Path(tmp))
                for i, n in enumerate(sequence):
                    self.check(mon, frame(n), i * .35)
                ledger = DamageLedger()
                for event in events: ledger.add(event)
                self.assertEqual(ledger.total, 404215)
                self.assertEqual(len(ledger.events), 1)

    def test_occluded_digit_recovers_in_original_event(self):
        from dataclasses import replace
        image = frame(351)
        complete = DamageRead(404215, '404215', .9, (490, 120, 790, 176), 6,
                              label='合计', label_score=.9)
        partial = replace(complete, value=40215, text='40?215', unresolved=1)
        for sequence in ((partial, partial, complete, complete),
                         (complete, complete, partial, partial, complete, complete)):
            with tempfile.TemporaryDirectory() as tmp:
                mon, events = self.monitor(Path(tmp))
                for i, reading in enumerate(sequence):
                    with patch.object(mon, '_read_damage_frame', return_value=[reading]):
                        self.check(mon, image, i * .35)
                ledger = DamageLedger()
                for event in events: ledger.add(event)
                self.assertEqual(ledger.total, 404215)
                self.assertEqual(len(ledger.events), 1)
                self.assertEqual(mon.damage_hits, 1)

    def test_recovery_burst_is_immediate_bounded_and_restored(self):
        mon = Monitor(MonitorConfig(quiet=True))
        normal = 1 / 15
        mon._schedule_recovery(10)
        self.assertEqual(mon._sampling_period(normal, 10), 0)
        self.assertEqual(mon._sampling_period(normal, 10.1), 1 / 30)
        mon._schedule_recovery(10.9)
        self.assertEqual(mon._sampling_period(normal, 11.1), normal)
        mon._damage_visible = True
        mon.observe_no_damage(12)
        mon.observe_no_damage(12.3)
        self.assertIsNone(mon._recovery_until)

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
