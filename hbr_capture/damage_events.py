"""Pure event accounting and a bounded, event-aware OCR mailbox.

No game capture or GUI dependency: recorded/synthetic timelines use the exact
same state machines as live capture. Values never create event boundaries.
"""
from collections import deque
from dataclasses import dataclass, field
import threading


@dataclass
class DamageEvent:
    event_id: int
    started: float
    ended: float | None = None
    reason: str = ''
    observations: dict = field(default_factory=dict)
    revision: int = 0
    dropped: int = 0
    uncertainties: list = field(default_factory=list)

    def reading(self):
        candidate = None
        streak = 0
        confirmed = False
        partial = None
        for _, reading in sorted(self.observations.items()):
            if reading is None or reading.get('unresolved', 0):
                # A failed OCR is neither a new event nor a numeric contradiction.
                streak = 0
                if reading and (partial is None or reading.get('confidence', 0) > partial.get('confidence', 0)):
                    partial = reading
                continue
            if candidate and reading['value'] == candidate['value']:
                streak += 1
            else:
                streak = 1
                confirmed = False
            candidate = reading
            if streak >= 2:
                confirmed = True
        result = dict(candidate or partial or dict(value=0, text='—', confidence=0, unresolved=1))
        result.update(label='合计', confirmed=confirmed, event_id=self.event_id)
        return result

    def result(self, run):
        return dict(run=run, event_id=self.event_id, revision=self.revision,
                    event_tracking=True, started=self.started, ended=self.ended,
                    reason=self.reason, dropped=self.dropped,
                    uncertainties=list(self.uncertainties),
                    readings=[self.reading()])


class EventTracker:
    def __init__(self, blank_frames=2):
        self.blank_frames = blank_frames
        self.events = {}
        self.current = None
        self.blanks = 0

    def observe(self, visible, timestamp):
        opened = closed = None
        if visible is True:
            self.blanks = 0
            if self.current is None:
                opened = self.current = DamageEvent(len(self.events) + 1, timestamp)
                self.events[opened.event_id] = opened
        elif visible is False:
            self.blanks += 1
            if self.blanks >= self.blank_frames:
                closed = self.close(timestamp, 'visual_disappearance')
        else:
            self.blanks = 0
        return opened, closed

    def close(self, timestamp, reason):
        event, self.current = self.current, None
        self.blanks = 0
        if event:
            event.ended, event.reason = timestamp, reason
            event.revision += 1
        return event

    def result(self, event_id, frame_id, reading):
        event = self.events[event_id]
        # Reprocessing one frame must not satisfy the two-frame confirmation.
        event.observations[frame_id] = reading
        event.revision += 1
        return event


class ActionGate:
    def __init__(self):
        # Starting mid-animation must work, even before seeing an action button.
        self.idle = False
        self.present = self.absent = 0

    def observe(self, visible):
        before = self.idle
        if visible is True:
            self.present += 1
            self.absent = 0
            if self.present >= 3:
                self.idle = True
        elif visible is False:
            self.absent += 1
            self.present = 0
            if self.absent >= 2:
                self.idle = False
        else:
            self.present = self.absent = 0
        return before != self.idle


class OcrMailbox:
    """Preserve two frames per event when possible; replace redundant backlog.

    Jobs expose event_id. Every drop is returned to the producer for auditing.
    A finite queue cannot guarantee zero loss under unlimited overload.
    """
    def __init__(self, capacity=16, per_event=4):
        self.capacity, self.per_event = capacity, per_event
        self.jobs = deque()
        self.condition = threading.Condition()
        self.closed = False

    def put(self, job):
        with self.condition:
            if self.closed:
                raise RuntimeError('OCR mailbox closed')
            dropped = None
            same = [i for i, item in enumerate(self.jobs) if item.event_id == job.event_id]
            if len(same) >= self.per_event:
                index = same[-2]  # keep the first two and the newest candidates
            elif len(self.jobs) >= self.capacity:
                counts = {}
                for item in self.jobs:
                    counts[item.event_id] = counts.get(item.event_id, 0) + 1
                index = next((i for i, item in enumerate(self.jobs)
                              if counts[item.event_id] > 2), None)
                if index is None:
                    return job  # keep evidence for older events; report rejected frame
            else:
                index = None
            if index is not None:
                dropped = self.jobs[index]
                del self.jobs[index]
            self.jobs.append(job)
            self.condition.notify()
            return dropped

    def get(self):
        with self.condition:
            self.condition.wait_for(lambda: self.jobs or self.closed)
            return self.jobs.popleft() if self.jobs else None

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
