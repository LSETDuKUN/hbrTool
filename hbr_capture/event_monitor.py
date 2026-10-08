"""Regional capture producer + single independent OCR/audit worker.

Only the producer owns EventTracker and publishes revisions. Worker results may
arrive after closure; their event IDs are never reassigned to a newer event.
"""
from collections import deque
from dataclasses import asdict, dataclass
import json
import queue
import threading
import time

import numpy as np
import cv2
from PIL import Image

from . import win32
from .capture import Frame
from .damage_events import EventTracker, OcrMailbox
from .damage_stats import DamageLedger
from hbr_recog.visibility import (SEARCH_ROI, DIGITS_ROI, TOTAL_ROI,
                                  HudVisibility, normalize, viewport_box)


@dataclass
class OcrJob:
    event_id: int
    frame_id: int
    timestamp: float
    captured_at: float
    rgb: np.ndarray
    roi: tuple
    visibility_score: float
    pose: tuple | None = None


class EventMonitor:
    def __init__(self, monitor, grabber):
        self.monitor, self.cfg, self.grabber = monitor, monitor.cfg, grabber
        self.tracker = EventTracker()
        self.ledger = DamageLedger()
        self.visibility = HudVisibility()
        self.mailbox, self.results = OcrMailbox(), queue.Queue()
        self.ring = deque(maxlen=30)
        self.frame_id = self.processed = self.dropped = 0
        self.started = time.monotonic()
        self.last_sample = self.last_submit = self.last_preview = 0
        self.last_rgb = None
        self.submitted = 0
        self.audit = None
        self.worker = None
        self.worker_error = None
        self.diagnostic_at = 0
        self.diagnostic_count = 0

    def diagnostic(self, rgb, roi, score, pose, now):
        if now - self.diagnostic_at < 5 or self.diagnostic_count >= 60:
            return
        self.diagnostic_at = now
        self.diagnostic_count += 1
        dropped = self.mailbox.put(OcrJob(0, self.diagnostic_count, now,
                           time.time(), rgb, roi, score, pose))
        if dropped:
            self.write('queue_drop', event_id=dropped.event_id, frame_id=dropped.frame_id)

    def write(self, kind, **values):
        self.audit.write(json.dumps(dict(type=kind, run=self.monitor.run_id, **values),
                                    ensure_ascii=False) + '\n')
        self.audit.flush()

    def publish(self, event):
        payload = event.result(self.monitor.run_id)
        before = self.ledger.total
        self.ledger.add(payload)
        self.write('event', total=self.ledger.total, delta=self.ledger.total - before,
                   **{k: v for k, v in payload.items() if k != 'run'})
        if self.cfg.on_damage:
            self.cfg.on_damage(payload)

    def capture(self, box, size):
        actual = viewport_box(box, size)
        result = win32.capture_region(self.hwnd, actual)
        rgb = np.frombuffer(result.bgra, np.uint8).reshape(result.height, result.width, 4)
        return normalize(rgb[:, :, 2::-1], box), actual

    def _work(self):
        try:
            from hbr_recog.damage import DamageReader
            from hbr_recog.segment import near_white_mask
            # Label recognition is provided by the fixed visual landmark, not OCR.
            reader = DamageReader(min_run=1, label_store=False)
            while True:
                job = self.mailbox.get()
                if job is None:
                    return
                if job.event_id == 0:
                    name = f'session-{self.monitor.run_id}-diagnostic-{job.frame_id:03d}.png'
                    Image.fromarray(job.rgb).save(self.cfg.outdir / name)
                    self.results.put((job, None, dict(file=name, pose=job.pose)))
                    continue
                stem = f'session-{self.monitor.run_id}-ocr-{job.frame_id:07d}'
                source = stem + '.png'
                input_file, mask_file = stem + '-input.png', stem + '-mask.png'
                x0, y0, x1, y1 = DIGITS_ROI
                image = job.rgb[y0:y1, x0:x1].copy()
                Image.fromarray(job.rgb).save(self.cfg.outdir / source)
                Image.fromarray(image).save(self.cfg.outdir / input_file)
                Image.fromarray(near_white_mask(image).astype(np.uint8) * 255).save(
                    self.cfg.outdir / mask_file)
                begin = time.monotonic()
                found = reader.read_total_band(image)
                raw = [asdict(r) for r in found]
                # Multiple fragments are ambiguous; never pick the largest/longest.
                reading = dict(raw[0], label='合计') if len(raw) == 1 else None
                self.results.put((job, reading, dict(file=source, input_file=input_file,
                    mask_file=mask_file, raw=raw, raw_glyphs=reader.last_diagnostics,
                    ocr_ms=round((time.monotonic() - begin) * 1000, 3))))
        except Exception as exc:
            self.worker_error = exc

    def drain(self):
        while True:
            try:
                job, reading, evidence = self.results.get_nowait()
            except queue.Empty:
                break
            diagnostic = job.event_id == 0
            if not diagnostic:
                self.processed += 1
            self.write('diagnostic' if diagnostic else 'ocr', event_id=job.event_id, frame_id=job.frame_id,
                       timestamp=job.timestamp, captured_at=job.captured_at,
                       roi=job.roi, visibility_score=job.visibility_score,
                       parsed=reading, **evidence)
            # Index each OCR source region so normal archive/recycle owns it too.
            m = self.monitor
            with m._save_lock:
                m.seq += 1
                trigger = 'diagnostic' if diagnostic else 'ocr_sample'
                m.counts[trigger] = m.counts.get(trigger, 0) + 1
                record = dict(run=m.run_id, seq=m.seq, trigger=trigger,
                              file=evidence['file'], event_id=job.event_id,
                              frame_id=job.frame_id, timestamp=job.captured_at,
                              width=job.rgb.shape[1], height=job.rgb.shape[0],
                              method='screendc-region', roi=job.roi, pose=job.pose)
                with (self.cfg.outdir / 'index.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
            if diagnostic:
                continue
            event = self.tracker.result(job.event_id, job.frame_id, reading)
            self.publish(event)

    def sample(self, rgb, roi, visible, score, now, wall, pose=None):
        self.frame_id += 1
        self.ring.append((self.frame_id, now, wall, rgb, visible, score, roi, pose))
        while self.ring and now - self.ring[0][1] > 1:
            self.ring.popleft()
        if self.last_sample and now - self.last_sample > .12:
            self.write('sampling_gap', seconds=now - self.last_sample,
                       warning='此间可能存在未采到的显示边界')
            # An unsampled interval cannot serve as one of two adjacent blanks.
            self.tracker.blanks = 0
            if self.tracker.current and 'sampling_gap' not in self.tracker.current.uncertainties:
                self.tracker.current.uncertainties.append('sampling_gap')
        self.last_sample = now
        opened, closed = self.tracker.observe(visible, now)
        if opened:
            self.monitor.damage_hits += 1
            self.submitted = 0
            self.last_rgb = None
            self.publish(opened)
        if closed:
            self.publish(closed)
        self.write('visibility', frame_id=self.frame_id, timestamp=now,
                   total=visible, score=score,
                   event_id=self.tracker.current.event_id if self.tracker.current else None)
        event = self.tracker.current
        if not event or visible is not True:
            return
        if now - event.started > 10 and 'prolonged_display' not in event.uncertainties:
            event.uncertainties.append('prolonged_display')
            self.write('uncertain_boundary', event_id=event.event_id,
                       warning='长时间连续显示；可能是长动画，也可能漏采了相邻同值显示的空白')
        # First candidates at full capture rate; stable displays need only a
        # heartbeat, while changing text gets up to ten OCR candidates/second.
        changed = self.last_rgb is None or np.mean(np.abs(
            rgb.astype(np.int16) - self.last_rgb.astype(np.int16))) > 1.5
        interval = .1 if changed else 1.0
        if self.submitted >= 3 and now - self.last_submit < interval:
            return
        # Choose a clear, fresh frame from the ring; never reuse one frame to
        # obtain artificial multi-frame confirmation, or cross an event boundary.
        candidates = [item for item in self.ring if item[1] > self.last_submit
                      and item[1] >= event.started and item[4] is True
                      and now - item[1] <= .1]
        def clarity(item):
            x0, y0, x1, y1 = DIGITS_ROI
            gray = cv2.cvtColor(item[3][y0:y1, x0:x1], cv2.COLOR_RGB2GRAY)
            return float(cv2.Laplacian(gray, cv2.CV_32F).var())
        chosen = max(candidates, key=clarity) if candidates else self.ring[-1]
        frame_id, timestamp, captured_at, candidate_rgb, _, candidate_score, candidate_roi, candidate_pose = chosen
        job = OcrJob(event.event_id, frame_id, timestamp, captured_at, candidate_rgb, candidate_roi, candidate_score, candidate_pose)
        dropped = self.mailbox.put(job)
        if dropped:
            self.dropped += 1
            if dropped.event_id:
                self.tracker.events[dropped.event_id].dropped += 1
            self.write('queue_drop', event_id=dropped.event_id, frame_id=dropped.frame_id)
        self.last_submit, self.last_rgb = now, rgb
        self.submitted += 1

    def run(self, hotkey_vk=None, stop_vk=None):
        m, cfg = self.monitor, self.cfg
        path = cfg.outdir / f'session-{m.run_id}-events.jsonl'
        self.audit = path.open('a', encoding='utf-8')
        self.worker = threading.Thread(target=self._work, name='hbr-event-ocr', daemon=True)
        self.worker.start()
        self.write('config', version=2, active_fps=cfg.event_fps, action_gate=False,
                   blank_frames=2, confirm_frames=2, buffer_seconds=1,
                   queue_capacity=16, search_roi=SEARCH_ROI, total_roi=TOTAL_ROI, digits_roi=DIGITS_ROI,
                   warning='没有采到空白时无法可靠区分相邻同值事件；不根据数值变化拆分事件')
        m._say('事件识别已启用：开始行动按钮判定已禁用；可手动暂停 / 继续。')
        grabbed = failed = 0
        stopped_by_user = stopped_by_limit = False
        prev_key = False
        paused = False
        next_interval = self.started + cfg.interval if cfg.interval else None
        try:
            while True:
                now = time.monotonic()
                if (cfg.should_stop and cfg.should_stop()) or (stop_vk and win32.key_down(stop_vk)):
                    stopped_by_user = True
                    break
                if (cfg.duration and now - self.started >= cfg.duration) or (
                        cfg.max_frames and m.seq >= cfg.max_frames):
                    stopped_by_limit = True
                    break
                self.drain()
                if self.worker_error:
                    raise RuntimeError(f'OCR 工作线程失败: {self.worker_error}') from self.worker_error
                want_pause = bool(cfg.should_pause and cfg.should_pause())
                if want_pause != paused:
                    paused = want_pause
                    self.write('manual_pause', paused=paused, timestamp=now)
                    self.ring.clear()
                    self.tracker.blanks = 0
                    self.last_sample = 0
                    self.last_submit = 0
                    self.submitted = 0
                    if self.tracker.current and 'manual_pause' not in self.tracker.current.uncertainties:
                        self.tracker.current.uncertainties.append('manual_pause')
                    m._say('已暂停采集，已有候选正在收尾；累计保留。' if paused else '已继续采集，沿用本轮事件与累计。')
                if paused:
                    # Stop and OCR completion checks above remain responsive.
                    time.sleep(.05)
                    continue
                try:
                    info = self.grabber.refresh()
                    size, self.hwnd = info.client_size, info.hwnd
                    search_rgb, roi = self.capture(SEARCH_ROI, size)
                    rgb, visible, score, pose = self.visibility.prepare_total(search_rgb)
                    if visible is not True:
                        self.diagnostic(search_rgb, roi, score, pose, now)
                    self.sample(rgb, roi, visible, score, time.monotonic(), time.time(), pose)
                    preview_rgb = rgb
                    grabbed += 1
                    failed = 0
                except (OSError, ValueError, LookupError) as exc:
                    failed += 1
                    self.write('capture_failure', error=str(exc), timestamp=now)
                    if failed == 1:
                        m._say(f'区域采集暂不可用: {exc}')
                    if failed >= cfg.max_grab_failures:
                        raise
                    self.tracker.blanks = 0
                    time.sleep(.25)
                    continue
                key = bool(hotkey_vk and win32.key_down(hotkey_vk))
                if (key and not prev_key) or (next_interval is not None and now >= next_interval):
                    m._save(self.grabber.grab(), 'hotkey' if key else 'interval', 0)
                    if cfg.interval:
                        next_interval = now + cfg.interval
                prev_key = key
                if cfg.on_frame and now - self.last_preview >= .2:
                    # UI displays the actual inspected ROI; no full-window copy.
                    bgra = np.empty((*preview_rgb.shape[:2], 4), dtype=np.uint8)
                    bgra[:, :, :3], bgra[:, :, 3] = preview_rgb[:, :, ::-1], 255
                    frame = Frame(bgra.shape[1], bgra.shape[0], bgra.tobytes(),
                                  'screendc-region', 1.0)
                    cfg.on_frame(frame, dict(grabbed=grabbed, saved=m.seq,
                        damage_hits=m.damage_hits, elapsed=now - self.started,
                        method='区域采集', phase='正在识别',
                        queue_dropped=self.dropped, failed=failed, broken=False))
                    self.last_preview = now
                period = 1 / max(1, cfg.event_fps)
                remaining = period - (time.monotonic() - now)
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            stopped_by_user = True
        finally:
            event = self.tracker.close(time.monotonic(), 'monitor_stopped')
            if event:
                self.publish(event)
            self.mailbox.close()
            # Finish only the bounded backlog; late results still revise old IDs.
            while self.worker.is_alive():
                self.worker.join(.1)
                self.drain()
            self.drain()
            self.write('finished', processed=self.processed, queue_dropped=self.dropped,
                       worker_error=str(self.worker_error) if self.worker_error else None)
            self.audit.close()
        if self.worker_error:
            raise RuntimeError(f'OCR 工作线程失败: {self.worker_error}') from self.worker_error
        seconds = time.monotonic() - self.started
        m._say(f'事件 {m.damage_hits} 笔，处理候选帧 {self.processed} 张，队列丢帧 {self.dropped} 张。')
        return dict(saved=m.seq, by_trigger=m.counts, damage_hits=m.damage_hits,
                    grabbed=grabbed, failed=failed, seconds=round(seconds, 2),
                    fps_actual=round(grabbed / seconds, 2) if seconds else 0,
                    outdir=str(cfg.outdir), stopped_by_user=stopped_by_user,
                    stopped_by_limit=stopped_by_limit, queue_dropped=self.dropped)
