"""把 BattleReport 渲染成人类看的表格，或转成机器可读的 dict / 行。"""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, List

from .battle import BattleReport


# ---------------------------------------------------------------- 对齐工具

def display_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（CJK 算 2 列）。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    text = str(text)
    filler = " " * max(0, width - display_width(text))
    return filler + text if align == "right" else text + filler


def render_table(headers: List[str], rows: List[List[str]], aligns: List[str]) -> str:
    widths = []
    for i, head in enumerate(headers):
        cell_widths = [display_width(head)] + [
            display_width(row[i]) for row in rows
        ]
        widths.append(max(cell_widths))

    def line(cells: List[str]) -> str:
        return "  ".join(
            pad(cell, widths[i], aligns[i]) for i, cell in enumerate(cells)
        ).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


# ---------------------------------------------------------------- 数值格式

def fmt_amount(value: float) -> str:
    return f"{value:,.0f}"


def fmt_percent(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}%"


def fmt_ratio(part: float, whole: float) -> str:
    if whole <= 0:
        return "-"
    return f"{part / whole * 100:.1f}%"


def fmt_optional(value) -> str:
    if value is None:
        return "-"
    return f"{value:+.3f}"


# ---------------------------------------------------------------- 渲染


def render_text(report: BattleReport, detail: bool = True) -> str:
    lines: List[str] = []
    add = lines.append

    add("=" * 78)
    add(f"HBR 战斗推演结果  ——  {report.boss_name}")
    add("=" * 78)
    add("")
    add(f"Boss 初始 DP : {fmt_amount(report.boss_dp_max)}")
    add(f"Boss 初始 HP : {fmt_amount(report.boss_hp_max)}")
    add(
        "OD 规则      : {:.2f}%/hit   普通攻击 {:.2f}%   每级 {:.1f}%   上限 {} 级".format(
            report.od_config.hit_od_percent,
            report.od_config.normal_attack_od_percent,
            report.od_config.od_per_level,
            report.od_config.max_level,
        )
    )
    add("")

    # -------- 表 1：伤害与 Boss 状态
    headers = [
        "回合",
        "出手次数",
        "本回合伤害",
        "累计伤害",
        "Boss DP",
        "Boss HP",
        "剩余总量",
        "状态",
    ]
    aligns = ["right", "right", "right", "right", "right", "right", "right", "left"]
    rows: List[List[str]] = []
    for rec in report.records:
        rows.append(
            [
                str(rec.turn),
                str(len(rec.actions)),
                fmt_amount(rec.total_damage),
                fmt_amount(rec.cumulative_damage),
                fmt_amount(rec.boss_dp),
                fmt_amount(rec.boss_hp),
                fmt_amount(rec.boss_total_remaining),
                "破坏" if rec.broken else "正常",
            ]
        )
    add("【伤害 / Boss 状态】")
    add(render_table(headers, rows, aligns))
    add("")

    # -------- 表 2：超频条
    headers = [
        "回合",
        "本回合 OD",
        "OD 累计",
        "OD 等级",
        "OD 填充",
        "屏幕读数",
        "对填充偏差",
        "对累计偏差",
    ]
    aligns = ["right", "right", "right", "right", "right", "right", "right", "right"]
    rows = []
    for rec in report.records:
        rows.append(
            [
                str(rec.turn),
                fmt_percent(rec.od_gain),
                fmt_percent(rec.od_accumulated),
                f"{rec.od_available_levels}/{rec.od_earned_levels}",
                fmt_percent(rec.od_partial_percent),
                "-" if rec.observed_od_percent is None
                else fmt_percent(rec.observed_od_percent),
                fmt_optional(rec.delta_vs_partial),
                fmt_optional(rec.delta_vs_accumulated),
            ]
        )
    add("【超频条】  OD 等级 = 可发动/已攒够")
    add(render_table(headers, rows, aligns))
    add("")

    # -------- 明细
    if detail:
        add("【逐动作明细】")
        detail_rows: List[List[str]] = []
        for rec in report.records:
            for item in rec.actions:
                action = item.action
                if action.kind == "normal":
                    spec = f"普通攻击 (固定 {report.od_config.normal_attack_od_percent:.1f}%)"
                else:
                    spec = "{} hit + {} 珠, 链 {:.0%}".format(
                        action.hits, action.combo_orbs, action.chain_bonus
                    )
                damage_text = fmt_amount(action.total_damage)
                if len(action.damages) > 1:
                    damage_text += f" ({len(action.damages)} 段)"
                detail_rows.append(
                    [
                        str(rec.turn),
                        action.label,
                        spec,
                        damage_text,
                        fmt_percent(item.od_gain),
                        item.applied_to.upper(),
                        fmt_amount(item.dp_lost),
                        fmt_amount(item.hp_damage),
                        fmt_amount(item.wasted),
                        "破坏!" if item.broke else "",
                    ]
                )
        headers = [
            "回合",
            "动作",
            "参数",
            "伤害",
            "获得 OD",
            "打在",
            "扣 DP",
            "扣 HP",
            "白打",
            "备注",
        ]
        aligns = [
            "right", "left", "left", "right", "right",
            "left", "right", "right", "right", "left",
        ]
        add(render_table(headers, detail_rows, aligns))
        add("")

    # -------- 汇总
    add("【汇总】")
    add(f"回合数        : {len(report.records)}")
    add(f"累计伤害      : {fmt_amount(report.total_damage)}")
    add(f"OD 累计获得   : {fmt_percent(report.total_od_gain)}")
    add(
        "Boss 终态     : DP {} / {}  ({})   HP {} / {}  ({})   {}".format(
            fmt_amount(report.final_dp),
            fmt_amount(report.boss_dp_max),
            fmt_ratio(report.final_dp, report.boss_dp_max),
            fmt_amount(report.final_hp),
            fmt_amount(report.boss_hp_max),
            fmt_ratio(report.final_hp, report.boss_hp_max),
            "已破坏" if report.final_broken else "未破坏",
        )
    )

    calibration = _calibration_hint(report)
    if calibration:
        add("")
        add("【校准提示】")
        for line in calibration:
            add(line)

    add("")
    return "\n".join(lines)


def _calibration_hint(report: BattleReport) -> List[str]:
    """如果有屏幕读数，给出 od_per_level 的粗略反推建议。"""
    # 注意: 不能直接取 records[-1] —— 最后一个回合未必录了屏幕读数。
    observed_records = [
        rec for rec in report.records if rec.observed_od_percent is not None
    ]
    if not observed_records:
        return [
            "本场没有录入屏幕上的 OD 读数（observed_od_percent），无法反推 od_per_level。",
            "建议: 每回合记下屏幕上的 OD 槽位百分比，再重跑一次。",
        ]

    hints: List[str] = []
    last = observed_records[-1]
    last_accumulated = last.od_accumulated
    last_observed = last.observed_od_percent

    if len(observed_records) < len(report.records):
        hints.append(
            "提示: 只有第 {} 回合有屏幕读数，反推用的是最后一组有读数的数据。".format(
                ", ".join(str(r.turn) for r in observed_records)
            )
        )

    # 情况 1: 屏幕读数 == 累计获得量 -> 槽位直接显示总量，不需要每级常量来换算显示
    if abs(last_observed - last_accumulated) <= 0.5:
        hints.append(
            "屏幕读数 {:.3f}% 与推算的累计获得量 {:.3f}% 基本一致 —— "
            "说明槽位直接显示『累计获得量』，不按等级清零。".format(
                last_observed, last_accumulated
            )
        )

    # 情况 2: 屏幕读数 == 当前级填充量 -> 可以反推每级所需的百分比
    if last.od_earned_levels > 0:
        implied = (last_accumulated - last_observed) / last.od_earned_levels
        hints.append(
            "若槽位显示的是『当前级填充量』且每攒满一级就清零: "
            "用 {} 级反推，每级约 {:.2f}%（当前推算填充 {:.3f}%，屏幕 {:.3f}%）。".format(
                last.od_earned_levels,
                implied,
                last.od_partial_percent,
                last_observed,
            )
        )
    else:
        hints.append(
            "本场还没攒满任何一级 OD，无法反推每级所需百分比；"
            "需要一场至少攒满 1 级的记录。"
        )

    hints.append(
        "把确定下来的值填回输入 JSON 的 od.od_per_level，再跑一次对齐。"
    )
    return hints


# ---------------------------------------------------------------- 机器可读


def report_to_dict(report: BattleReport) -> Dict[str, Any]:
    """完整结构化输出，供回放 / 校准脚本消费。"""
    return {
        "boss": {
            "name": report.boss_name,
            "dp_max": report.boss_dp_max,
            "hp_max": report.boss_hp_max,
        },
        "od_config": {
            "hit_od_percent": report.od_config.hit_od_percent,
            "normal_attack_od_percent": report.od_config.normal_attack_od_percent,
            "od_per_level": report.od_config.od_per_level,
            "max_level": report.od_config.max_level,
        },
        "summary": {
            "turns": len(report.records),
            "total_damage": report.total_damage,
            "total_od_gain": report.total_od_gain,
            "final_dp": report.final_dp,
            "final_hp": report.final_hp,
            "final_broken": report.final_broken,
        },
        "turns": [
            {
                "turn": rec.turn,
                "total_damage": rec.total_damage,
                "cumulative_damage": rec.cumulative_damage,
                "boss": {
                    "dp": rec.boss_dp,
                    "hp": rec.boss_hp,
                    "broken": rec.broken,
                },
                "od": {
                    "gain": rec.od_gain,
                    "accumulated": rec.od_accumulated,
                    "earned_levels": rec.od_earned_levels,
                    "available_levels": rec.od_available_levels,
                    "partial_percent": rec.od_partial_percent,
                    "observed_percent": rec.observed_od_percent,
                    "delta_vs_partial": rec.delta_vs_partial,
                    "delta_vs_accumulated": rec.delta_vs_accumulated,
                },
                "actions": [
                    {
                        "character": item.action.character,
                        "kind": item.action.kind,
                        "skill": item.action.skill,
                        "hits": item.action.hits,
                        "combo_orbs": item.action.combo_orbs,
                        "chain_bonus": item.action.chain_bonus,
                        "damages": list(item.action.damages),
                        "total_damage": item.action.total_damage,
                        "od_gain": item.od_gain,
                        "applied_to": item.applied_to,
                        "dp_lost": item.dp_lost,
                        "hp_damage": item.hp_damage,
                        "wasted": item.wasted,
                        "just_broke": item.broke,
                        "hits": [
                            {
                                "damage": r.damage,
                                "applied_to": r.applied_to,
                                "dp_lost": r.dp_lost,
                                "hp_damage": r.hp_damage,
                                "wasted": r.wasted,
                            }
                            for r in item.damage_results
                        ],
                    }
                    for item in rec.actions
                ],
            }
            for rec in report.records
        ],
    }


def report_to_rows(report: BattleReport) -> List[Dict[str, Any]]:
    """每回合一行，方便直接倒进 pandas / Excel。"""
    rows: List[Dict[str, Any]] = []
    for rec in report.records:
        rows.append(
            {
                "turn": rec.turn,
                "actions": len(rec.actions),
                "turn_damage": rec.total_damage,
                "cumulative_damage": rec.cumulative_damage,
                "boss_dp": rec.boss_dp,
                "boss_hp": rec.boss_hp,
                "broken": rec.broken,
                "od_gain": rec.od_gain,
                "od_accumulated": rec.od_accumulated,
                "od_levels": rec.od_available_levels,
                "od_partial_percent": rec.od_partial_percent,
                "observed_od_percent": rec.observed_od_percent,
            }
        )
    return rows
