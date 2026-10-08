import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from hbr_capture.damage_events import ActionGate, EventTracker, OcrMailbox
from hbr_capture.damage_stats import DamageLedger, resource_state


def reading(value):
    return dict(value=value, text=str(value), unresolved=0, confidence=.95)


class EventAccountingTests(unittest.TestCase):
    def test_long_animation_and_fragment_revision_count_once(self):
        tracker, ledger = EventTracker(), DamageLedger()
        for frame, value in enumerate([123, 123, 123456, 123456] + [123456] * 300):
            tracker.observe(True, frame / 30)
            event = tracker.result(1, frame, reading(value))
            ledger.add(event.result('test'))
            if frame == 2:
                self.assertEqual(ledger.total, 0)  # contradictory candidate is pending
        self.assertEqual(len(tracker.events), 1)
        self.assertEqual(ledger.total, 123456)
        self.assertEqual(resource_state(100000, 100000, ledger.total).hp, 76544)

    def test_numbers_never_create_boundaries_even_when_identical(self):
        tracker, ledger = EventTracker(), DamageLedger()
        tracker.observe(True, 0)
        for f in [1, 2]:
            ledger.add(tracker.result(1, f, reading(55)).result('test'))
        tracker.observe(False, .1)
        tracker.observe(False, .133)
        tracker.observe(True, .166)
        for f in [5, 6]:
            ledger.add(tracker.result(2, f, reading(55)).result('test'))
        self.assertEqual(ledger.total, 110)
        self.assertEqual(len(tracker.events), 2)

    def test_failures_and_flash_do_not_split_or_erase_confirmed(self):
        tracker = EventTracker()
        tracker.observe(True, 0)
        tracker.result(1, 1, reading(22))
        tracker.result(1, 2, reading(22))
        tracker.result(1, 3, None)
        tracker.observe(False, .1)
        tracker.observe(None, .2)
        tracker.observe(False, .3)
        tracker.observe(True, .4)
        self.assertEqual(tracker.current.event_id, 1)
        self.assertTrue(tracker.current.reading()['confirmed'])

    def test_late_results_sorted_by_frame_and_versioned(self):
        tracker, ledger = EventTracker(), DamageLedger()
        tracker.observe(True, 0)
        tracker.close(.1, 'phase')
        tracker.observe(True, .2)
        old = tracker.result(1, 4, reading(99)).result('test')
        tracker.result(1, 2, reading(5))
        latest = tracker.result(1, 3, reading(99)).result('test')
        ledger.add(latest)
        ledger.add(old)
        self.assertEqual(ledger.total, 99)
        self.assertEqual(tracker.current.event_id, 2)

    def test_one_frame_cannot_confirm_even_if_processed_twice(self):
        tracker = EventTracker()
        tracker.observe(True, 0)
        tracker.result(1, 1, reading(99))
        tracker.result(1, 1, reading(99))
        self.assertFalse(tracker.current.reading()['confirmed'])

    def test_no_maximum_or_longest_preference(self):
        tracker = EventTracker()
        tracker.observe(True, 0)
        for f, value in enumerate([123456, 123, 123]):
            tracker.result(1, f, reading(value))
        self.assertEqual(tracker.current.reading()['value'], 123)
        self.assertTrue(tracker.current.reading()['confirmed'])

    def test_one_short_event_retains_pending_candidate(self):
        tracker, ledger = EventTracker(), DamageLedger()
        tracker.observe(True, 0)
        tracker.result(1, 1, reading(20))
        event = tracker.close(.04, 'visual_disappearance')
        ledger.add(event.result('test'))
        self.assertEqual((ledger.total, ledger.pending), (0, 1))
        self.assertEqual(ledger.readings[0]['value'], 20)

    def test_undetected_gap_is_one_event_not_invented_hits(self):
        tracker = EventTracker()
        for f in range(300):
            tracker.observe(True, f / 30)
            tracker.result(1, f, reading(10))
        self.assertEqual(len(tracker.events), 1)


class PhaseAndQueueTests(unittest.TestCase):
    def test_button_debounce(self):
        gate = ActionGate()
        for _ in range(3):
            gate.observe(True)
        self.assertTrue(gate.idle)
        gate.observe(False)
        gate.observe(None)
        gate.observe(False)
        self.assertTrue(gate.idle)
        gate.observe(False)
        self.assertFalse(gate.idle)

    def test_overload_keeps_short_event_and_bounds_memory(self):
        mailbox = OcrMailbox(capacity=6)
        for f in range(10):
            mailbox.put(SimpleNamespace(event_id=1, frame_id=f))
        mailbox.put(SimpleNamespace(event_id=2, frame_id=10))
        mailbox.put(SimpleNamespace(event_id=2, frame_id=11))
        self.assertLessEqual(len(mailbox.jobs), 6)
        self.assertEqual([j.frame_id for j in mailbox.jobs if j.event_id == 1][:2], [0, 1])
        mailbox.close()
        drained = []
        while (job := mailbox.get()) is not None:
            drained.append(job)
        self.assertEqual(len([j for j in drained if j.event_id == 2]), 2)

    def test_queue_rejects_explicitly_when_no_redundancy_left(self):
        mailbox = OcrMailbox(capacity=2)
        for f in range(2):
            mailbox.put(SimpleNamespace(event_id=1, frame_id=f))
        job = SimpleNamespace(event_id=2, frame_id=3)
        self.assertIs(mailbox.put(job), job)
        self.assertEqual(len(mailbox.jobs), 2)


class ActualSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1] / 'results/20261007-220819'
        if not (cls.root / 'index.jsonl').exists():
            raise unittest.SkipTest('Local user session not distributed with source')
        cls.records = {r['seq']: r for r in map(json.loads,
            (cls.root / 'index.jsonl').read_text(encoding='utf-8').splitlines())}

    def region(self, seq, box):
        with Image.open(self.root / self.records[seq]['file']) as image:
            return np.array(image.convert('RGB').crop(box))

    def test_real_label_rejects_individual_hit_numbers(self):
        from hbr_recog.visibility import HudVisibility, TOTAL_ROI, BUTTON_ROI
        detector = HudVisibility()
        for seq in (46, 118, 119, 335, 336, 337, 350, 351):
            self.assertIs(detector.check(self.region(seq, TOTAL_ROI), 'total')[0], True, seq)
        self.assertIs(detector.check(self.region(97, TOTAL_ROI), 'total')[0], False)
        self.assertIs(detector.check(self.region(43, BUTTON_ROI), 'button')[0], True)

    def test_real_crops_worker_late_revision_and_archiving(self):
        from hbr_capture.event_monitor import EventMonitor, OcrJob
        from hbr_capture.monitor import Monitor, MonitorConfig
        from hbr_capture.session import archive_run
        from hbr_recog.visibility import TOTAL_ROI
        ledger = DamageLedger()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = MonitorConfig(outdir=root / 'frames', on_damage=ledger.add, quiet=True)
            cfg.outdir.mkdir()
            monitor = Monitor(cfg)
            pipeline = EventMonitor(monitor, None)
            pipeline.audit = (cfg.outdir / f'session-{monitor.run_id}-events.jsonl').open('w', encoding='utf-8')
            pipeline.tracker.observe(True, 0)
            pipeline.tracker.close(.5, 'test_end')
            # Replay candidate frames inside ONE known event; this is not a claim
            # that sparse saved screenshots reveal actual intervening boundaries.
            for f, seq in enumerate([350, 351, 351]):
                pipeline.mailbox.put(OcrJob(1, f, f / 30, f, self.region(seq, TOTAL_ROI), TOTAL_ROI, .8))
            pipeline.mailbox.close()
            worker = threading.Thread(target=pipeline._work)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(pipeline.worker_error)
            pipeline.drain()
            pipeline.audit.close()
            self.assertEqual(ledger.total, 420483989)
            self.assertEqual(len(ledger.events), 1)
            dest = archive_run(cfg.outdir, monitor.run_id, root / 'results')
            self.assertEqual(len(list(dest.glob('*-input.png'))), 3)
            self.assertTrue((dest / f'session-{monitor.run_id}-events.jsonl').exists())
            records = [json.loads(line) for line in (dest / f'session-{monitor.run_id}-events.jsonl').read_text(encoding='utf-8').splitlines()]
            ocr = [r for r in records if r['type'] == 'ocr']
            self.assertEqual(len(ocr), 3)
            self.assertTrue(ocr[0]['raw_glyphs'])
            self.assertEqual(sum(r['delta'] for r in records if r['type'] == 'event'), 420483989)
            from tools.replay_damage_events import replay
            self.assertEqual(replay(dest)['frames'], 3)


class PortableCropTests(unittest.TestCase):
    def test_single_digit_and_partial_then_full(self):
        from hbr_recog.damage import DamageReader
        from hbr_recog.visibility import DIGITS_ROI, HudVisibility, TOTAL_ROI, normalize
        import cv2
        root = Path(__file__).parent / 'fixtures/damage_events'
        reader, detector = DamageReader(min_run=1, label_store=False), HudVisibility()
        for seq, expected in [(63, '6'), (350, '420483'), (351, '420483989')]:
            with Image.open(root / f'total-{seq}.png') as image:
                rgb = np.array(image.convert('RGB'))
            self.assertTrue(detector.check(rgb, 'total')[0])
            x, y, X, Y = DIGITS_ROI
            found = reader.read_total_band(rgb[y:Y, x:X])
            self.assertEqual([r.text for r in found], [expected])
            # Landmark remains recognizable after common window-size scaling.
            for width in [1280, 1600, 1920, 2560]:
                small = cv2.resize(rgb, (round(rgb.shape[1]*width/2048), round(rgb.shape[0]*width/2048)))
                self.assertTrue(detector.check(normalize(small, TOTAL_ROI), 'total')[0], (seq, width))
        with Image.open(root / 'total-97.png') as image:
            self.assertFalse(detector.check(np.array(image.convert('RGB')), 'total')[0])


class ProducerTests(unittest.TestCase):
    def test_idle_avoids_ocr_and_capture_continues_while_worker_is_slow(self):
        from hbr_capture.event_monitor import EventMonitor
        from hbr_capture.monitor import Monitor, MonitorConfig
        with tempfile.TemporaryDirectory() as directory:
            cfg = MonitorConfig(outdir=Path(directory), duration=.38, quiet=True)
            monitor = Monitor(cfg)
            grabber = SimpleNamespace(refresh=lambda: SimpleNamespace(client_size=(2048, 1152), hwnd=1))
            pipeline = EventMonitor(monitor, grabber)
            captures = []
            def capture(box, size):
                captures.append(time.monotonic())
                return np.zeros((box[3] - box[1], box[2] - box[0], 3), np.uint8), box
            pipeline.capture = capture
            pipeline.visibility.check = lambda rgb, name: (True, .99)
            # Button is already visible -> wait mode starts before the fourth tick.
            summary = pipeline.run()
            self.assertTrue(pipeline.gate.idle)
            self.assertLessEqual(summary['grabbed'], 7)
            self.assertLessEqual(pipeline.processed, 2)
            self.assertTrue(summary['stopped_by_limit'])

    def test_slow_worker_does_not_block_region_sampling(self):
        from hbr_capture.event_monitor import EventMonitor
        from hbr_capture.monitor import Monitor, MonitorConfig
        with tempfile.TemporaryDirectory() as directory:
            cfg = MonitorConfig(outdir=Path(directory), duration=.3, quiet=True)
            monitor = Monitor(cfg)
            grabber = SimpleNamespace(refresh=lambda: SimpleNamespace(client_size=(2048, 1152), hwnd=1))
            pipeline = EventMonitor(monitor, grabber)
            pipeline.capture = lambda box, size: (np.zeros((box[3]-box[1], box[2]-box[0], 3), np.uint8), box)
            pipeline.visibility.check = lambda rgb, name: (name == 'total', .99)
            done = []
            def slow():
                while (job := pipeline.mailbox.get()) is not None:
                    time.sleep(.15)
                    done.append(job.frame_id)
            pipeline._work = slow
            summary = pipeline.run()
            self.assertGreaterEqual(summary['grabbed'], 6)
            self.assertGreaterEqual(len(done), 2)
            self.assertIsNotNone(pipeline.tracker.events[1].ended)


if __name__ == '__main__':
    unittest.main()
