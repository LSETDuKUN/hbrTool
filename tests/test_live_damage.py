import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from hbr_capture.capture import Frame
from hbr_capture.damage_stats import DamageLedger, resource_state
from hbr_capture.live_damage import Candidate, DisplayTracker, LiveDamage
from hbr_capture.monitor import Monitor, MonitorConfig
from hbr_recog.damage import DamageRead


def reading(value):
    return DamageRead(value, str(value), .95, (400, 130, 750, 185), len(str(value)))


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.tracker = DisplayTracker(self.jobs.append)
        self.t = 0

    def sample(self, value=None, presence=True):
        self.t += 1 / 30
        self.tracker.sample(presence, [] if value is None else [reading(value)],
                            SimpleNamespace(timestamp=self.t), 0, object())

    def test_partial_then_full_revises_one_id(self):
        for value in (123, 123, 123456, 123456):
            self.sample(value)
        self.assertEqual([j.event_id for j in self.jobs], [1, 1])
        ledger = DamageLedger()
        for j in self.jobs:
            ledger.add(dict(run='r', event_id=j.event_id,
                            readings=[dict(value=j.readings[0].value, label='合计', confirmed=True)]))
        self.assertEqual(ledger.total, 123456)
        self.assertEqual(resource_state(100000, 100000, ledger.total).hp, 76544)

    def test_long_animation_does_not_create_ids(self):
        for i in range(600):
            self.sample(444)
        self.assertEqual({j.event_id for j in self.jobs}, {1})

    def test_two_blank_frames_allow_equal_damage_twice(self):
        for v in (444, 444): self.sample(v)
        self.sample(presence=False)
        self.sample(presence=False)
        for v in (444, 444): self.sample(v)
        self.assertEqual([j.event_id for j in self.jobs], [1, 2])

    def test_ocr_failure_and_flash_do_not_split(self):
        for v in (444, 444): self.sample(v)
        for _ in range(30): self.sample(presence=None)
        for v in (444, 444): self.sample(v)
        self.assertEqual({j.event_id for j in self.jobs}, {1})

    def test_single_frame_damage_is_retained_pending(self):
        self.sample(123)
        self.sample(presence=False)
        self.sample(presence=False)
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.jobs[0].confirmed)

    def test_pause_does_not_reset_event_or_reuse_pre_pause_confirmation(self):
        self.sample(123)
        self.tracker.pause()
        self.sample(123)
        self.assertEqual(self.jobs, [])
        self.sample(123)
        self.assertEqual(self.jobs[0].event_id, 1)

    def test_pending_never_deducts_resources(self):
        ledger = DamageLedger()
        ledger.add(dict(event_id=1, readings=[dict(value=123, label='合计', confirmed=False)]))
        self.assertEqual(ledger.total, 0)

    def test_numeric_parser_rejects_ambiguous_suffix(self):
        import numpy as np
        for text, expected in [('2,709,29453.8', 2709294), ('395,095', 395095),
                               ('123 456', None), ('420483989', 420483989)]:
            ocr = SimpleNamespace(_recognise=lambda *a, **kw: [(text, .99)])
            result = LiveDamage.read_number_row(ocr, np.zeros((200, 500, 3), 'uint8'), (40, 60, 300, 120))
            self.assertEqual(result[0] if result else None, expected)


class RealImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hbr_recog.label_ocr import make_label_reader
        cls.labels = make_label_reader()
        if cls.labels.ocr is None:
            raise unittest.SkipTest('RapidOCR is not installed')

    def test_recent_failed_frames_are_read_without_label_gating_or_truncated_crop(self):
        from PIL import Image
        fixtures = Path(__file__).parent / 'fixtures/live_damage'
        rows = json.loads((fixtures / 'samples.json').read_text())
        for i in range(0, len(rows), 2):
            with self.subTest(value=rows[i]['value']):
                events = []
                monitor = Monitor(MonitorConfig(quiet=True, on_damage=events.append))
                monitor._save = Mock(return_value=True)
                flow = LiveDamage(monitor)
                flow.close()
                jobs = []
                flow.tracker.submit = jobs.append
                for n, row in enumerate(rows[i:i+2]):
                    image = Image.new('RGBA', (2048, 1152), 'black')
                    x, y, scale = row['pose']
                    self.assertEqual(scale, 1.0)  # These source pixels were not resized.
                    with Image.open(fixtures / row['file']) as source:
                        image.paste(source.convert('RGBA'), (int(x + 900), int(y + 400)))
                    flow.sample(Frame(2048, 1152, image.tobytes('raw', 'BGRA'),
                                      timestamp=n / 30, non_black_ratio=1), 0)
                flow.tracker.finish()
                for job in jobs: flow.publish(job, self.labels)
                ledger = DamageLedger()
                for event in events: ledger.add(event)
                self.assertEqual(ledger.total, rows[i]['value'])

    def test_slow_label_ocr_cannot_block_sampler(self):
        monitor = Monitor(MonitorConfig(quiet=True))
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(5)
        with patch.object(LiveDamage, 'publish', side_effect=blocked):
            flow = LiveDamage(monitor)
            try:
                first = Candidate(1, None, 0, None, [reading(1)], True)
                flow.submit(first)
                self.assertTrue(entered.wait(5))
                # Submission completes while the OCR worker is still blocked.
                for n in range(2, 10):
                    flow.submit(Candidate(n, None, 0, None, [reading(n)], True))
                self.assertEqual(len(flow.pending), 8)
            finally:
                release.set()
                flow.close()

    def test_old_long_battle_partial_read_is_corrected_without_extra_leading_digit(self):
        from PIL import Image
        monitor = Monitor(MonitorConfig(quiet=True))
        monitor._save = Mock(return_value=True)
        events = []
        monitor.cfg.on_damage = events.append
        flow = LiveDamage(monitor)
        flow.close()
        jobs = []
        flow.tracker.submit = jobs.append
        for i, n in enumerate((350, 350, 351, 351)):
            image = Image.new('RGBA', (2048, 1152), 'black')
            with Image.open(Path(__file__).parent / f'fixtures/live_damage/old-{n}.png') as source:
                image.paste(source.convert('RGBA'), (900, 440))
            flow.sample(Frame(2048, 1152, image.tobytes('raw', 'BGRA'),
                              timestamp=i / 30, non_black_ratio=1), 0)
        for job in jobs: flow.publish(job, self.labels)
        ledger = DamageLedger()
        for event in events: ledger.add(event)
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(ledger.total, 420483989)


if __name__ == '__main__':
    unittest.main()
