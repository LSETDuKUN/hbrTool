"""Confirmed live readings, with revisions keyed by capture event."""

from dataclasses import dataclass, field


def enemy_count(value) -> int:
    text = str(value).strip()
    if not text.isdecimal() or not 1 <= int(text) <= 99:
        raise ValueError("怪物数量须为 1–99 的整数")
    return int(text)


@dataclass
class DamageLedger:
    enemies: int = 1
    events: dict = field(default_factory=dict)
    _sequence: int = 0

    def set_enemies(self, value):
        self.enemies = enemy_count(value)

    def add(self, event):
        identity = event.get("event_id")
        if identity is None:
            self._sequence += 1
            identity = ("legacy", self._sequence)
        key = (event.get("run"), identity)
        readings = [dict(r) for r in event["readings"]]
        old = self.events.get(key)
        # Later OCR failures must not replace a previously confirmed value.
        if old is not None:
            readings = [previous if self.amount(previous) is not None
                        and self.amount(current) is None else current
                        for previous, current in zip(old, readings)] if len(old) == len(readings) else old
        self.events[key] = readings

    def amount(self, reading):
        if reading.get("unresolved", 0) or reading.get("label") not in ("合计", "平均"):
            return None
        return reading["value"] * (self.enemies if reading["label"] == "平均" else 1)

    @property
    def readings(self):
        return [r for group in self.events.values() for r in group]

    @property
    def total(self):
        return sum(self.amount(r) or 0 for r in self.readings)

    @property
    def pending(self):
        return sum(self.amount(r) is None for r in self.readings)
