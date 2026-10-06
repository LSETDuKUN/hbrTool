"""超频条 (Overdrive / OD) 计算规则。

已确认的规则（用户口述）:
  * 技能每 1 hit 提供 2.5% 超频条。
  * 连击珠 (连击珠 / combo orb) 每个额外提供 1 hit。
  * OD 链加成 (chain bonus) 取值 1% ~ 15%，即最高 1.15 倍获得率；
    0 表示当前没有链加成。
  * 普通攻击固定 7.5% 超频条，不吃链加成。

自洽性检查: 7.5 / 2.5 == 3，即"普通攻击 == 3 hit"。
这也解释了为什么普通攻击被单独规定成固定值 —— 它是 3 hit 但不吃任何加成。

尚未确认、已做成可配置的项（默认取最保守的读法）:
  * 普通攻击是否吃连击珠 -> OdConfig.normal_attack_gets_combo_orbs，默认 False。
  * 普通攻击是否吃链加成 -> OdConfig.normal_attack_gets_chain_bonus，默认 False。
  * 多少百分比 = 1 级 OD   -> OdConfig.od_per_level，默认 100.0（待用实测校准）。
  * OD 等级上限            -> OdConfig.max_level，默认 3。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 常量

HIT_OD_PERCENT = 2.5
NORMAL_ATTACK_OD_PERCENT = 7.5
NORMAL_ATTACK_EQUIVALENT_HITS = NORMAL_ATTACK_OD_PERCENT / HIT_OD_PERCENT  # == 3.0
CHAIN_BONUS_MIN = 0.01
CHAIN_BONUS_MAX = 0.15

_EPS = 1e-9


class OdRuleError(ValueError):
    """超频条输入违反了已知规则。"""


# ---------------------------------------------------------------- 校验


def _as_non_negative_int(value, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise OdRuleError(f"{name} 必须是整数，收到 {value!r}") from None
    if result < 0:
        raise OdRuleError(f"{name} 不能为负，收到 {result}")
    if abs(float(value) - result) > _EPS:
        raise OdRuleError(f"{name} 必须是整数，收到 {value!r}")
    return result


def validate_chain_bonus(rate) -> float:
    """OD 链加成必须是 0（无加成）或落在 1% ~ 15% 之间。"""
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        raise OdRuleError(f"chain_bonus 必须是数字，收到 {rate!r}") from None
    if abs(rate) < _EPS:
        return 0.0
    if rate < CHAIN_BONUS_MIN - _EPS or rate > CHAIN_BONUS_MAX + _EPS:
        raise OdRuleError(
            "OD 链加成必须是 0（无加成）或 {:.0%}~{:.0%} 之间，收到 {:.4f}".format(
                CHAIN_BONUS_MIN, CHAIN_BONUS_MAX, rate
            )
        )
    return rate


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class OdConfig:
    """超频条规则参数。改这里就能重校准，不用动计算逻辑。"""

    hit_od_percent: float = HIT_OD_PERCENT
    normal_attack_od_percent: float = NORMAL_ATTACK_OD_PERCENT
    normal_attack_gets_combo_orbs: bool = False
    normal_attack_gets_chain_bonus: bool = False
    od_per_level: float = 100.0
    max_level: int = 3

    def __post_init__(self):
        if self.hit_od_percent <= 0:
            raise OdRuleError("hit_od_percent 必须为正数")
        if self.normal_attack_od_percent < 0:
            raise OdRuleError("normal_attack_od_percent 不能为负")
        if self.od_per_level <= 0:
            raise OdRuleError("od_per_level 必须为正数")
        if self.max_level < 1:
            raise OdRuleError("max_level 必须 >= 1")

    @classmethod
    def from_dict(cls, data) -> "OdConfig":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise OdRuleError(f"od 配置里有无法识别的字段: {sorted(unknown)}")
        return cls(**{k: data[k] for k in known if k in data})


# ---------------------------------------------------------------- 获得量


def od_from_skill(
    hits: int,
    combo_orbs: int = 0,
    chain_bonus: float = 0.0,
    config: OdConfig | None = None,
) -> float:
    """技能获得的超频条百分比。

    (技能 hit 数 + 连击珠数量) x 每 hit 百分比 x (1 + 链加成)
    """
    cfg = config or OdConfig()
    hits = _as_non_negative_int(hits, "hits")
    combo_orbs = _as_non_negative_int(combo_orbs, "combo_orbs")
    bonus = validate_chain_bonus(chain_bonus)
    total_hits = hits + combo_orbs
    return total_hits * cfg.hit_od_percent * (1.0 + bonus)


def od_from_normal_attack(
    combo_orbs: int = 0,
    chain_bonus: float = 0.0,
    config: OdConfig | None = None,
) -> float:
    """普通攻击获得的超频条百分比，固定 7.5%，默认不吃连击珠也不吃链加成。"""
    cfg = config or OdConfig()
    combo_orbs = _as_non_negative_int(combo_orbs, "combo_orbs")
    bonus = validate_chain_bonus(chain_bonus)

    percent = cfg.normal_attack_od_percent
    if cfg.normal_attack_gets_combo_orbs and combo_orbs:
        base_hits = cfg.normal_attack_od_percent / cfg.hit_od_percent
        percent = (base_hits + combo_orbs) * cfg.hit_od_percent
    if cfg.normal_attack_gets_chain_bonus:
        percent *= (1.0 + bonus)
    return percent


# ---------------------------------------------------------------- 槽位状态


@dataclass
class OdGauge:
    """超频条状态：累计获得量 + 可发动的等级数。

    注意: 这里把"累计获得量"和"等级"当作两个独立的概念。
    od_per_level（默认 100%）是待校准值 —— 用实测的槽位百分比去反推它。
    """

    config: OdConfig = field(default_factory=OdConfig)
    accumulated: float = 0.0
    activated_levels: int = 0

    def gain(self, percent: float) -> float:
        """累加获得的百分比，返回累加后的总量。"""
        try:
            percent = float(percent)
        except (TypeError, ValueError):
            raise OdRuleError(f"获得的超频条必须是数字，收到 {percent!r}") from None
        if percent < -_EPS:
            raise OdRuleError(f"获得的超频条不能为负，收到 {percent}")
        self.accumulated += max(percent, 0.0)
        return self.accumulated

    @property
    def earned_levels(self) -> int:
        """按 od_per_level 换算出来的、已经攒够的等级数（不封顶）。"""
        return int(self.accumulated // self.config.od_per_level)

    @property
    def available_levels(self) -> int:
        """实际可发动的等级数，受 max_level 封顶。"""
        return min(self.earned_levels, self.config.max_level)

    @property
    def partial_percent(self) -> float:
        """距离下一级还差多少之外的那一截（即当前级的填充百分比）。"""
        return self.accumulated % self.config.od_per_level

    def activate(self, levels: int = 1) -> None:
        """发动 OD，消耗等级。"""
        levels = _as_non_negative_int(levels, "levels")
        if levels > self.available_levels:
            raise OdRuleError(
                f"只能发动 {self.available_levels} 级 OD，请求了 {levels} 级"
            )
        self.accumulated -= levels * self.config.od_per_level
        self.activated_levels += levels
