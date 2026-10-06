"""逐回合推演引擎：统计每回合伤害 + 推进 Boss DP/HP + 推进超频条。

输入是一个 JSON（见 examples/），包含:
  boss     —— 用户录入的 Boss 初始数据（dp_max / hp_max / break_rate ...）
  od       —— 超频条规则参数（可选）
  turns    —— 每回合的动作列表；每个动作带着它的 hit 数、连击珠、链加成、以及
              从伤害帧读到的伤害数字

输出 BattleReport，可直接喂给 report.py 渲染成表格，或 dump 成 JSON 供回放校准。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Union

from .boss import Boss, DamageResult
from .od import OdConfig, OdGauge, od_from_normal_attack, od_from_skill

VALID_KINDS = ("skill", "normal")


# ---------------------------------------------------------------- 输入模型


@dataclass
class Action:
    character: str = ""
    kind: str = "skill"
    skill: str = ""
    hits: int = 0
    combo_orbs: int = 0
    chain_bonus: float = 0.0
    damages: List[float] = field(default_factory=list)

    @property
    def total_damage(self) -> float:
        return float(sum(self.damages))

    @property
    def label(self) -> str:
        name = self.skill if self.kind == "skill" and self.skill else "普通攻击"
        return f"{self.character}·{name}" if self.character else name


@dataclass
class TurnSpec:
    turn: int
    actions: List[Action]
    break_rate: Optional[float] = None
    observed_od_percent: Optional[float] = None
    #: 这一回合 GAUGE BREAK 是否生效（生效时锁盾，DP 最少保留 1）
    gauge_break: Optional[bool] = None


@dataclass
class BattleSpec:
    boss: Boss
    turns: List[TurnSpec]
    od_config: OdConfig = field(default_factory=OdConfig)
    od_initial_accumulated: float = 0.0


# ---------------------------------------------------------------- 输出模型


@dataclass
class ActionRecord:
    action: Action
    od_gain: float
    damage_results: List[DamageResult] = field(default_factory=list)

    @property
    def dp_lost(self) -> float:
        return float(sum(r.dp_lost for r in self.damage_results))

    @property
    def hp_damage(self) -> float:
        return float(sum(r.hp_damage for r in self.damage_results))

    @property
    def wasted(self) -> float:
        return float(sum(r.wasted for r in self.damage_results))

    @property
    def broke(self) -> bool:
        return any(r.just_broke for r in self.damage_results)

    @property
    def applied_to(self) -> str:
        """这一整组命中打在哪。多段可能一部分打盾一部分打血，报 mixed。"""
        kinds = {r.applied_to for r in self.damage_results}
        if not kinds:
            return "-"
        if len(kinds) > 1:
            return "mixed"
        return kinds.pop()


@dataclass
class TurnRecord:
    turn: int
    actions: List[ActionRecord]
    total_damage: float
    cumulative_damage: float
    od_gain: float
    od_accumulated: float
    od_earned_levels: int
    od_available_levels: int
    od_partial_percent: float
    observed_od_percent: Optional[float]
    delta_vs_partial: Optional[float]
    delta_vs_accumulated: Optional[float]
    boss_dp: float
    boss_hp: float
    broken: bool

    @property
    def boss_total_remaining(self) -> float:
        return self.boss_dp + self.boss_hp


@dataclass
class BattleReport:
    boss_name: str
    boss_dp_max: float
    boss_hp_max: float
    od_config: OdConfig
    records: List[TurnRecord]

    @property
    def total_damage(self) -> float:
        return float(sum(r.total_damage for r in self.records))

    @property
    def total_od_gain(self) -> float:
        return float(sum(r.od_gain for r in self.records))

    @property
    def final_dp(self) -> float:
        return self.records[-1].boss_dp if self.records else self.boss_dp_max

    @property
    def final_hp(self) -> float:
        return self.records[-1].boss_hp if self.records else self.boss_hp_max

    @property
    def final_broken(self) -> bool:
        return self.records[-1].broken if self.records else False


# ---------------------------------------------------------------- 解析


def _require_mapping(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{what} 必须是一个对象，收到 {type(value).__name__}")
    return value


def _reject_unknown(data: dict, known: set, what: str) -> None:
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{what} 里有无法识别的字段: {sorted(unknown)}")


def _as_float(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} 必须是数字，收到 {value!r}") from None


def _as_optional_float(value: Any, what: str) -> Optional[float]:
    if value is None:
        return None
    return _as_float(value, what)


_ACTION_KEYS = {
    "character",
    "kind",
    "skill",
    "hits",
    "combo_orbs",
    "chain_bonus",
    "damage",
}


def parse_action(data: Any, index: int) -> Action:
    data = _require_mapping(data, f"第 {index + 1} 个动作")
    _reject_unknown(data, _ACTION_KEYS, f"第 {index + 1} 个动作")

    kind = str(data.get("kind", "skill")).strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(
            f"第 {index + 1} 个动作的 kind 必须是 {VALID_KINDS} 之一，收到 {kind!r}"
        )

    raw_damage = data.get("damage", 0.0)
    if isinstance(raw_damage, (list, tuple)):
        damages = [_as_float(v, f"第 {index + 1} 个动作的 damage 项") for v in raw_damage]
    else:
        damages = [_as_float(raw_damage, f"第 {index + 1} 个动作的 damage")]

    hits = data.get("hits", 0)
    combo_orbs = data.get("combo_orbs", 0)
    for name, value in (("hits", hits), ("combo_orbs", combo_orbs)):
        if int(value) != float(value):
            raise ValueError(f"第 {index + 1} 个动作的 {name} 必须是整数，收到 {value!r}")

    return Action(
        character=str(data.get("character", f"#{index + 1}")),
        kind=kind,
        skill=str(data.get("skill", "")),
        hits=int(hits),
        combo_orbs=int(combo_orbs),
        chain_bonus=_as_float(data.get("chain_bonus", 0.0), "chain_bonus"),
        damages=damages,
    )


_TURN_KEYS = {"turn", "actions", "break_rate", "observed_od_percent", "gauge_break"}


def parse_turn(data: Any, index: int) -> TurnSpec:
    data = _require_mapping(data, f"第 {index + 1} 个回合")
    _reject_unknown(data, _TURN_KEYS, f"第 {index + 1} 个回合")

    raw_actions = data.get("actions", [])
    if not isinstance(raw_actions, (list, tuple)):
        raise ValueError(f"第 {index + 1} 个回合的 actions 必须是数组")

    raw_gauge = data.get("gauge_break")
    return TurnSpec(
        turn=int(data.get("turn", index + 1)),
        actions=[parse_action(a, i) for i, a in enumerate(raw_actions)],
        break_rate=_as_optional_float(data.get("break_rate"), "break_rate"),
        observed_od_percent=_as_optional_float(
            data.get("observed_od_percent"), "observed_od_percent"
        ),
        gauge_break=None if raw_gauge is None else bool(raw_gauge),
    )


_SPEC_KEYS = {"boss", "od", "turns", "od_initial_accumulated"}


def parse_spec(data: Any) -> BattleSpec:
    data = _require_mapping(data, "输入")
    _reject_unknown(data, _SPEC_KEYS, "输入")

    raw_turns = data.get("turns")
    if not isinstance(raw_turns, (list, tuple)) or not raw_turns:
        raise ValueError("输入必须包含非空的 turns 数组")

    return BattleSpec(
        boss=Boss.from_dict(data.get("boss")),
        turns=[parse_turn(t, i) for i, t in enumerate(raw_turns)],
        od_config=OdConfig.from_dict(data.get("od")),
        od_initial_accumulated=_as_float(
            data.get("od_initial_accumulated", 0.0), "od_initial_accumulated"
        ),
    )


def load_spec(source: Union[str, Path, dict]) -> BattleSpec:
    """从 JSON 文件路径或已解析的 dict 载入战斗输入。"""
    if isinstance(source, dict):
        return parse_spec(source)
    text = Path(source).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} 不是合法 JSON: {exc}") from None
    return parse_spec(data)


# ---------------------------------------------------------------- 推演


def run_battle(spec: BattleSpec) -> BattleReport:
    """跑完整场推演，返回逐回合记录。"""
    boss = spec.boss
    gauge = OdGauge(config=spec.od_config, accumulated=spec.od_initial_accumulated)

    records: List[TurnRecord] = []
    cumulative_damage = 0.0

    for turn_spec in spec.turns:
        if turn_spec.break_rate is not None:
            boss.set_break_rate(turn_spec.break_rate)
        if turn_spec.gauge_break is not None:
            boss.set_gauge_break(turn_spec.gauge_break)

        action_records: List[ActionRecord] = []
        turn_damage = 0.0
        turn_od = 0.0

        for action in turn_spec.actions:
            if action.kind == "normal":
                gained = od_from_normal_attack(
                    combo_orbs=action.combo_orbs,
                    chain_bonus=action.chain_bonus,
                    config=gauge.config,
                )
            else:
                gained = od_from_skill(
                    hits=action.hits,
                    combo_orbs=action.combo_orbs,
                    chain_bonus=action.chain_bonus,
                    config=gauge.config,
                )

            gauge.gain(gained)
            turn_od += gained

            # 关键: 每个 hit 单独结算。因为「单 hit 只作用于一个池子」，
            # 把多段伤害加总后一次性结算会算出完全不同的结果 ——
            # 比如 DP 只剩 100，两段各 60：分开算是"第一段破盾、第二段打血"，
            # 加总算成 120 则会全打在盾上、一点血都掉不了。
            results = [boss.apply_damage(hit) for hit in action.damages]
            turn_damage += action.total_damage

            action_records.append(
                ActionRecord(action=action, od_gain=gained, damage_results=results)
            )

        cumulative_damage += turn_damage

        observed = turn_spec.observed_od_percent
        delta_partial = None if observed is None else observed - gauge.partial_percent
        delta_accumulated = None if observed is None else observed - gauge.accumulated

        records.append(
            TurnRecord(
                turn=turn_spec.turn,
                actions=action_records,
                total_damage=turn_damage,
                cumulative_damage=cumulative_damage,
                od_gain=turn_od,
                od_accumulated=gauge.accumulated,
                od_earned_levels=gauge.earned_levels,
                od_available_levels=gauge.available_levels,
                od_partial_percent=gauge.partial_percent,
                observed_od_percent=observed,
                delta_vs_partial=delta_partial,
                delta_vs_accumulated=delta_accumulated,
                boss_dp=boss.dp,
                boss_hp=boss.hp,
                broken=boss.broken,
            )
        )

    return BattleReport(
        boss_name=boss.name,
        boss_dp_max=boss.dp_max,
        boss_hp_max=boss.hp_max,
        od_config=spec.od_config,
        records=records,
    )


def run_source(source: Union[str, Path, dict]) -> BattleReport:
    return run_battle(load_spec(source))
