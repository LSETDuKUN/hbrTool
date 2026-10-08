"""Broad-band numeric sampling; slow label OCR and evidence writes run separately.

No label template or action-button gate controls acquisition. A continuous
display owns one ID; numerical changes revise it, never create another ID.
"""
from collections import deque
from dataclasses import asdict, dataclass
import threading
import re

import numpy as np

from hbr_recog.damage import DamageReader, DamageRead, extract_damage_lines
from hbr_recog.segment import find_text_rows


@dataclass
class Candidate:
    event_id: int
    frame: object
    diff: float
    rgb: object
    readings: list
    confirmed: bool
    previous_rgb: object = None


class DisplayTracker:
    """False means measured visual absence; None means inconclusive/occluded."""
    def __init__(self, submit):
        self.submit = submit
        self.serial = 0
        self.active = False
        self.blank_frames = 0
        self.key = None
        self.streak = 0
        self.candidate = None
        self.sent = None
        self.last_submit = -1e9

    def sample(self, presence, readings, frame, diff, rgb):
        if presence is None:
            self.blank_frames = 0
            self.streak = 0
            return
        if not presence:
            self.blank_frames += 1
            if self.blank_frames >= 2:
                self.finish()
            return
        self.blank_frames = 0
        if not readings:
            self.streak = 0
            return
        if not self.active:
            self.serial += 1
            self.active = True
            self.key = self.sent = None
            self.streak = 0
            self.last_submit = -1e9
        key = tuple((r.value, r.text, r.unresolved) for r in readings)
        self.streak = self.streak + 1 if key == self.key else 1
        self.key = key
        previous = self.candidate
        self.candidate = Candidate(self.serial, frame, diff, rgb, readings,
                                   self.streak >= 2,
                                   previous.rgb if previous and previous.event_id == self.serial else None)
        # Two separate capture samples, without a 300ms stability delay.
        # Periodic retries allow a previously covered label to become readable.
        if self.streak >= 2 and (key != self.sent or
                                frame.timestamp - self.last_submit >= .5):
            self.submit(self.candidate)
            self.sent = key
            self.last_submit = frame.timestamp

    def finish(self):
        if self.active and self.candidate is not None and self.sent is None:
            self.submit(self.candidate)  # One-frame events are retained as pending.
        self.active = False
        self.candidate = None
        self.blank_frames = 0

    def pause(self):
        # Pausing is not visual disappearance. Retain identity and submitted value.
        self.blank_frames = self.streak = 0


class LiveDamage:
    def __init__(self, monitor):
        self.monitor = monitor
        # Avoid loading/running the heavyweight label model in the capture loop.
        self.reader = DamageReader(min_run=monitor.cfg.damage_min_run, label_store=False)
        self.size = None
        self.scaled = self.reader
        self.boxes = []
        self.condition = threading.Condition()
        self.pending = deque()
        self.closed = False
        self.dropped = 0
        self.published = {}
        self.tracker = DisplayTracker(self.submit)
        self.frames = deque()
        self.capture_condition = threading.Condition()
        self.capture_closed = False
        self.samples_dropped = 0
        self.sampler = threading.Thread(target=self._sample_work, name="damage-samples", daemon=True)
        self.worker = threading.Thread(target=self._work, name="damage-labels", daemon=True)
        self.worker.start()
        self.sampler.start()

    def enqueue(self, frame, diff):
        with self.capture_condition:
            if len(self.frames) >= 16:
                self.frames.popleft()
                self.samples_dropped += 1
                if self.samples_dropped == 1 or self.samples_dropped % 30 == 0:
                    self.monitor._say(f"[警告] 数字采样积压，丢帧 {self.samples_dropped}；极近事件可能合并")
            self.frames.append((frame, diff))
            self.capture_condition.notify()

    def pause(self):
        # Queue a boundary marker after already captured frames. No new capture
        # occurs while paused; only that finite backlog is finished.
        with self.capture_condition:
            self.frames.append((None, 0))
            self.capture_condition.notify()

    def _sample_work(self):
        while True:
            with self.capture_condition:
                self.capture_condition.wait_for(lambda: self.frames or self.capture_closed)
                if not self.frames:
                    return
                frame, diff = self.frames.popleft()
            try:
                if frame is None:
                    self.tracker.pause()
                else:
                    self.sample(frame, diff)
            except Exception as exc:
                self.monitor._say(f"[错误] 数字采样失败: {exc}")

    def sample(self, frame, diff):
        if frame.size != self.size:
            self.size = frame.size
            self.scaled = self.reader.for_frame(*frame.size)
            self.boxes = []
        reader = self.scaled
        (y0, y1), (x0, x1) = reader.y_band, reader.x_band
        arr = np.frombuffer(frame.bgra, np.uint8).reshape(frame.height, frame.width, 4)
        rgb = arr[y0:min(y1, frame.height), x0:min(x1, frame.width), 2::-1].copy()
        if not rgb.size:
            return
        readings = reader.read_band(rgb, recognize_labels=False)
        boxes = reader.visual_boxes
        if not readings:
            # White backgrounds can swallow the white digit cores. Pink outlines
            # still supply geometry; they do not decide the number or its label.
            r, g, b = [rgb[:, :, i].astype(np.int16) for i in range(3)]
            pink = (r > 180) & (r - g > 25) & (b > 100)
            lines = extract_damage_lines(pink, (0, rgb.shape[0]), (0, rgb.shape[1]),
                                         min_run=1, scale=reader.scale)
            outlines = [(min(g.x0 for g in gs), a, max(g.x1 for g in gs), z)
                        for a, z, gs in lines]
            if not outlines:
                # Glow joins neighbouring outlines into a single wide shape.
                # Keep its complete row for OCR instead of splitting it into digits.
                for a, z in find_text_rows(pink, min_pixels=max(4, int(10 * reader.scale)),
                                           min_height=max(1, int(60 * reader.scale))):
                    if z - a <= 120 * reader.scale:
                        xs = np.flatnonzero(pink[a:z].any(axis=0))
                        if len(xs) and xs[-1] - xs[0] > 40 * reader.scale:
                            pad = int(10 * reader.scale)
                            outlines.append((int(xs[0]), a + pad, int(xs[-1]) + 1, z - pad))
            boxes = boxes or outlines
            readings = [DamageRead(0, "?", 0, box, 0, unresolved=1) for box in boxes]
        presence = bool(readings)
        if not readings and self.tracker.active:
            # Inspect geometry, not OCR success, at the previous digits' position.
            for a, b, c, d in self.boxes:
                for x, y, xx, yy in boxes:
                    if min(c, xx) > max(a, x) and min(d, yy) > max(b, y):
                        presence = None
                # A partly occluded digit core still means uncertain, not absent.
                patch = rgb[b:d, a:c]
                if patch.size and np.all(patch >= 235, axis=2).mean() > .12:
                    presence = None
        if self.monitor.cfg.reject_flash and self.monitor._bright_ratio(frame) >= self.monitor.cfg.flash_bright_ratio:
            presence = None
            self.monitor.flash_skipped += 1
        if readings:
            self.boxes = [r.box for r in readings]
        self.tracker.sample(presence, readings, frame, diff, rgb)
        self.monitor._damage_visible = self.tracker.active

    def submit(self, candidate):
        with self.condition:
            # Coalesce redundant work for this event; preserve different events.
            for i, old in enumerate(self.pending):
                if old.event_id == candidate.event_id:
                    self.pending[i] = candidate
                    self.condition.notify()
                    return
            if len(self.pending) >= 12:
                self.pending.popleft()
                self.dropped += 1
                self.monitor._say("[警告] 标签识别积压，已丢弃一个待处理事件；本次统计可能不完整")
            self.pending.append(candidate)
            self.condition.notify()

    def _work(self):
        from hbr_recog.label_ocr import make_label_reader
        try:
            labels = make_label_reader()
        except Exception as exc:
            labels = None
            self.monitor._say(f"标签识别初始化失败，数字保留为未知: {exc}")
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.closed)
                if not self.pending:
                    return
                job = self.pending.popleft()
            try:
                self.publish(job, labels)
            except Exception as exc:
                self.monitor._say(f"[错误] 伤害事件 {job.event_id} 处理失败: {exc}")

    def publish(self, job, labels):
        readings = []
        diagnostics, images = [], []
        for r in job.readings:
            label = labels.read(job.rgb, r.box) if labels is not None else None
            if label is not None and label.name == '未知':
                x, y, xx, yy = r.box
                # Outline rows can start at the decorative dot left of the number.
                # Retry a wider label window so that its right half is not cropped.
                label = labels.read(job.rgb, (x + 100, y, xx, yy))
            readings.append(dict(asdict(r), label=label.name if label else "未知",
                                 label_score=label.score if label else 0,
                                 confirmed=job.confirmed))
        # Verify the entire numeric row, including its left edge. This catches
        # comma segmentation and partial-template reads without choosing max/longest.
        ocr = getattr(labels, "ocr", None)
        if ocr is not None:
            for reading in readings:
                if reading['label'] not in ('合计', '平均'):
                    continue
                expected = reading['value'] if not reading['unresolved'] else None
                first = self.read_number_row(ocr, job.rgb, reading['box'], diagnostics, images, expected)
                second = (self.read_number_row(ocr, job.previous_rgb, reading['box'], diagnostics, images, expected)
                          if job.previous_rgb is not None else None)
                if first is not None:
                    reading.update(value=first[0], text=str(first[0]), unresolved=0,
                                   confidence=first[1], confirmed=second is not None and first[0] == second[0])
                else:
                    reading['confirmed'] = False
        # Multiple geometry fragments may describe the same row. One row contributes once.
        unique = []
        for reading in readings:
            if not any(abs(reading['box'][1] - prev['box'][1]) < 12
                       and reading['value'] == prev['value'] and reading['label'] == prev['label']
                       for prev in unique):
                unique.append(reading)
        readings = unique
        old = self.published.get(job.event_id)
        # Failures in a later label attempt do not erase known readings.
        if old and len(old) == len(readings):
            readings = [prev if (prev.get("confirmed") and prev["label"] != "未知"
                        and (cur["label"] == "未知" or cur["unresolved"] or not cur["confirmed"]))
                        else cur for prev, cur in zip(old, readings)]
        elif old and not any(r['confirmed'] and not r['unresolved'] and
                             r['label'] in ('合计', '平均') for r in readings):
            readings = old
        signature = lambda rs: [(r['value'], r['text'], r['label'], r['unresolved'], r['confirmed']) for r in rs]
        if old is not None and signature(old) == signature(readings):
            return
        event = dict(run=self.monitor.run_id, event_id=job.event_id, readings=readings,
                     update=old is not None, captured_at=job.frame.timestamp,
                     replace_group=True, recognition=diagnostics)
        self.monitor._save(job.frame, "damage" if old is None else "damage_update",
                           job.diff, damage_event=event, ocr_images=images)
        if self.monitor.cfg.on_damage:
            self.monitor.cfg.on_damage(event)
        self.published[job.event_id] = readings
        if old is None:
            self.monitor.damage_hits += 1

    @staticmethod
    def read_number_row(ocr, rgb, box, diagnostics=None, images=None, expected=None):
        if rgb is None:
            return None
        x, y, xx, yy = box
        # Keep generous horizontal margins: the template box may contain only
        # the tail of a partially segmented number. Include the suffix, then parse it.
        row = rgb[max(0, y - 10):min(rgb.shape[0], yy + 12),
                  max(0, x - 260):min(rgb.shape[1], xx + 200)]
        if not row.size:
            return None
        raw = ocr._recognise(row, upscale=1)
        if diagnostics is not None:
            diagnostics.append(dict(box=box, raw=raw))
        if images is not None:
            images.append(row)
        # If a tighter crop and the independent digit templates agree, prefer
        # that agreement over OCR turning animated background into a leading digit.
        if expected is not None:
            tight = rgb[max(0, y - 10):min(rgb.shape[0], yy + 12),
                        max(0, x - 10):min(rgb.shape[1], xx + 200)]
            tight_raw = ocr._recognise(tight, upscale=1)
            if diagnostics is not None:
                diagnostics.append(dict(box=box, raw=tight_raw, variant='tight'))
            if images is not None:
                images.append(tight)
            for text, score in tight_raw:
                value = LiveDamage.parse_number(text, score)
                if value is not None and value[0] == expected:
                    return value
        for text, score in raw:
            value = LiveDamage.parse_number(text, score)
            if value is not None:
                return value
        return None

    @staticmethod
    def parse_number(text, score):
        if score < .8:
            return None
        # Only one damage token at the beginning; optional percentage is not damage.
        match = re.fullmatch(r'[^0-9]*([0-9]{1,3}(?:[,，][0-9]{3})+)'
                                 r'(?:[+＋.:：·]?[0-9]+\.[0-9]+%?)?[^0-9]*', text.strip())
        if match is None:
            match = re.fullmatch(r'[^0-9]*([0-9]+)'
                                     r'(?:[+＋.:：·][0-9]+(?:\.[0-9]+)?%?)?[^0-9]*', text.strip())
        if match:
            return int(match[1].replace(',', '').replace('，', '')), score
        return None

    def close(self):
        with self.capture_condition:
            self.capture_closed = True
            self.capture_condition.notify()
        self.sampler.join()
        self.tracker.finish()
        with self.condition:
            self.closed = True
            self.condition.notify()
        self.worker.join()
