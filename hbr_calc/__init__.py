"""HBR 战斗数值推演。

三个阶段分得很清楚，方便后面接上真实读屏:
  1. battle.load_spec  —— 把用户录入的 Boss 数据 + 每回合动作解析成结构体
  2. battle.run_battle —— 推进 DP/HP 和超频条，产出逐回合记录
  3. report            —— 渲染成表格 / JSON / 每回合一行

本包不依赖任何第三方库，也不碰游戏进程，全部可离线单元测试。
"""

from .boss import Boss, DamageResult
from .od import (
    CHAIN_BONUS_MAX,
    CHAIN_BONUS_MIN,
    HIT_OD_PERCENT,
    NORMAL_ATTACK_EQUIVALENT_HITS,
    NORMAL_ATTACK_OD_PERCENT,
    OdConfig,
    OdGauge,
    OdRuleError,
    od_from_normal_attack,
    od_from_skill,
)
from .battle import (
    Action,
    ActionRecord,
    BattleReport,
    BattleSpec,
    TurnRecord,
    TurnSpec,
    load_spec,
    run_battle,
    run_source,
)
from .report import (
    display_width,
    pad,
    render_table,
    render_text,
    report_to_dict,
    report_to_rows,
)

__version__ = "0.1.0"

__all__ = [
    "Action",
    "ActionRecord",
    "BattleReport",
    "BattleSpec",
    "Boss",
    "CHAIN_BONUS_MAX",
    "CHAIN_BONUS_MIN",
    "DamageResult",
    "HIT_OD_PERCENT",
    "NORMAL_ATTACK_EQUIVALENT_HITS",
    "NORMAL_ATTACK_OD_PERCENT",
    "OdConfig",
    "OdGauge",
    "OdRuleError",
    "TurnRecord",
    "TurnSpec",
    "display_width",
    "load_spec",
    "od_from_normal_attack",
    "od_from_skill",
    "pad",
    "render_table",
    "render_text",
    "report_to_dict",
    "report_to_rows",
    "run_battle",
    "run_source",
]
