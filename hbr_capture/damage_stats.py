"""Confirmed live readings, with revisions keyed by capture event."""

from dataclasses import dataclass, field


def pool_value(value, name="DP/HP") -> int:
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text.isdecimal():
        raise ValueError(f"{name} 请输入非负整数，可带千分位逗号")
    return int(text)


@dataclass(frozen=True)
class ResourceState:
    dp_max: int
    hp_max: int
    dp: int
    hp: int
    last_damage: int
    last_dp_loss: int
    last_hp_loss: int


def resource_state(dp_max: int, hp_max: int, total: int, last_damage: int = 0) -> ResourceState:
    """Widget's requested overflow rule: exhaust DP, then deduct excess from HP.

    Rebuild from the confirmed ledger so OCR revisions and monster-count changes
    never subtract the same event twice. This is independent of the hit simulator.
    """
    dp_max, hp_max = pool_value(dp_max, "DP"), pool_value(hp_max, "HP")
    total = max(0, int(total))
    last_damage = max(0, min(total, int(last_damage)))
    before = total - last_damage
    dp = max(0, dp_max - total)
    hp = max(0, hp_max - max(0, total - dp_max))
    dp_before = max(0, dp_max - before)
    hp_before = max(0, hp_max - max(0, before - dp_max))
    return ResourceState(dp_max, hp_max, dp, hp, last_damage,
                         dp_before - dp, hp_before - hp)


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
        if old is not None and not event.get("replace_group"):
            readings = [previous if self.amount(previous) is not None
                        and self.amount(current) is None else current
                        for previous, current in zip(old, readings)] if len(old) == len(readings) else old
        self.events[key] = readings

    def amount(self, reading):
        if (reading.get("confirmed") is False or reading.get("unresolved", 0)
                or reading.get("label") not in ("合计", "平均")):
            return None
        return reading["value"] * (self.enemies if reading["label"] == "平均" else 1)

    @property
    def readings(self):
        return [r for group in self.events.values() for r in group]

    @property
    def total(self):
        return sum(self.amount(r) or 0 for r in self.readings)

    @property
    def last_damage(self):
        for readings in reversed(list(self.events.values())):
            amounts = [self.amount(r) for r in readings]
            if any(amount is not None for amount in amounts):
                return sum(amount or 0 for amount in amounts)
        return 0

    @property
    def pending(self):
        return sum(self.amount(r) is None for r in self.readings)
