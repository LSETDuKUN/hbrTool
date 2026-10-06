"""Boss 的 DP / HP 推演。

核心规则（依据用户确认的游戏机制）:

  * **一次命中只作用于一个池子。** 有 DP 时伤害全打 DP；哪怕 Boss 只剩 1 点 DP，
    你打 100000 也只是破盾，**一滴都进不了 HP**。绝对不存在"溢出部分进 HP"。

  * DP 归零后进入「破坏」状态，之后所有伤害按 break_rate 扣 HP。

  * **GAUGE BREAK 会锁盾** —— 也就是 DP 有下限，最少保留 1 点。
    锁盾期间无论打多少伤害都破不了盾，也就碰不到 HP。

  * break_rate（破坏率，游戏里显示成 `+115.9%` 那种）是破盾之后的**独立乘区**，
    作用在 HP 伤害上。

`wasted` 字段告诉你这一击有多少伤害是白打的 —— 锁盾期间会非常大，
这正是 GAUGE BREAK 状态下应该看到的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_EPS = 1e-9

#: GAUGE BREAK 锁盾时的 DP 下限
GAUGE_BREAK_DP_FLOOR = 1.0


@dataclass
class DamageResult:
    """一次伤害结算的结果。"""

    damage: float                 # 这一击的名义伤害
    applied_to: str               # "dp" 或 "hp" —— 这一击实际打在哪个池子上
    dp_lost: float                # DP 实际减少量
    hp_damage: float              # HP 实际减少量
    wasted: float                 # 白打的伤害（过杀 + 被锁盾挡掉）
    just_broke: bool
    dp_before: float
    hp_before: float
    dp_after: float
    hp_after: float

    @property
    def effective(self) -> float:
        """这一击真正起作用的量（扣掉的 DP + 扣掉的 HP）。"""
        return self.dp_lost + self.hp_damage


@dataclass
class Boss:
    name: str = "BOSS"
    dp_max: float = 0.0
    hp_max: float = 0.0
    break_rate: float = 1.0
    dp: Optional[float] = None
    hp: Optional[float] = None
    broken: bool = False
    #: DP 下限。0 = 正常；GAUGE_BREAK_DP_FLOOR = 锁盾中
    dp_floor: float = 0.0
    #: 锁盾是否生效（GAUGE BREAK 状态）
    gauge_break: bool = False

    def __post_init__(self):
        self.dp_max = float(self.dp_max)
        self.hp_max = float(self.hp_max)
        self.break_rate = float(self.break_rate)
        if self.dp_max < 0 or self.hp_max < 0:
            raise ValueError("dp_max / hp_max 不能为负")
        if self.break_rate < 0:
            raise ValueError("break_rate 不能为负")
        if self.dp is None:
            self.dp = self.dp_max
        if self.hp is None:
            self.hp = self.hp_max
        self.dp = float(self.dp)
        self.hp = float(self.hp)
        if self.gauge_break:
            self.dp_floor = GAUGE_BREAK_DP_FLOOR
        self.dp_floor = float(self.dp_floor)

    # ------------------------------------------------------------ 状态

    @property
    def dp_ratio(self) -> float:
        return self.dp / self.dp_max if self.dp_max > 0 else 0.0

    @property
    def hp_ratio(self) -> float:
        return self.hp / self.hp_max if self.hp_max > 0 else 0.0

    @property
    def total_remaining(self) -> float:
        return self.dp + self.hp

    @property
    def shielded(self) -> bool:
        """还有盾，所以任何伤害都进不了 HP。"""
        return self.dp > _EPS

    def set_break_rate(self, rate: float) -> None:
        rate = float(rate)
        if rate < 0:
            raise ValueError("break_rate 不能为负")
        self.break_rate = rate

    def set_gauge_break(self, active: bool) -> None:
        """开/关 GAUGE BREAK 锁盾。锁盾时 DP 最少保留 1。"""
        self.gauge_break = bool(active)
        self.dp_floor = GAUGE_BREAK_DP_FLOOR if active else 0.0

    # ------------------------------------------------------------ 结算

    def apply_damage(self, damage: float) -> DamageResult:
        """结算一次命中。

        注意参数是**一次命中**的伤害，不是一整个技能。多段技能要拆成多次调用 ——
        因为"单 hit 只作用于一个池子"，拆不拆会影响结果。
        """
        damage = float(damage)
        if damage < 0:
            raise ValueError(f"伤害不能为负，收到 {damage}")

        dp_before = self.dp
        hp_before = self.hp
        dp_lost = 0.0
        hp_damage = 0.0
        just_broke = False

        if self.shielded:
            # 有盾：这一击全部打在 DP 上，绝不溢出到 HP
            applied_to = "dp"
            target = max(self.dp_floor, self.dp - damage)
            if target <= _EPS and self.dp_floor <= _EPS:
                target = 0.0
            dp_lost = self.dp - target
            self.dp = target
            if self.dp <= _EPS and not self.broken:
                self.broken = True
                just_broke = True
        else:
            # 盾已破：打在 HP 上，吃破坏率乘区
            applied_to = "hp"
            hp_damage = damage * self.break_rate
            self.hp = max(0.0, self.hp - hp_damage)

        # 名义伤害 - 实际起作用的量 = 白打的
        wasted = max(0.0, damage - dp_lost - hp_damage)

        return DamageResult(
            damage=damage,
            applied_to=applied_to,
            dp_lost=dp_lost,
            hp_damage=hp_damage,
            wasted=wasted,
            just_broke=just_broke,
            dp_before=dp_before,
            hp_before=hp_before,
            dp_after=self.dp,
            hp_after=self.hp,
        )

    # ------------------------------------------------------------ 构造

    _KNOWN = {
        "name",
        "dp_max",
        "hp_max",
        "break_rate",
        "dp",
        "hp",
        "broken",
        "dp_floor",
        "gauge_break",
    }

    @classmethod
    def from_dict(cls, data) -> "Boss":
        if not data:
            raise ValueError("缺少 boss 配置")
        unknown = set(data) - cls._KNOWN
        if unknown:
            raise ValueError(f"boss 配置里有无法识别的字段: {sorted(unknown)}")
        if "dp_max" not in data or "hp_max" not in data:
            raise ValueError("boss 配置必须包含 dp_max 和 hp_max")
        return cls(**{k: data[k] for k in data})
