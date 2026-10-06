"""核心逻辑的单元测试。全部用手算过的数字对答案。

跑法（在 hbr/ 目录下）:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hbr_calc import (  # noqa: E402
    Boss,
    OdConfig,
    OdGauge,
    OdRuleError,
    load_spec,
    od_from_normal_attack,
    od_from_skill,
    render_text,
    run_battle,
)


class TestOdGain(unittest.TestCase):
    """2.5%/hit + 连击珠 + 链加成 的规则。"""

    def test_skill_with_orbs_and_full_chain_bonus(self):
        # (5 + 2) * 2.5 = 17.5 ; 17.5 * 1.15 = 20.125
        self.assertAlmostEqual(od_from_skill(5, 2, 0.15), 20.125, places=9)

    def test_skill_without_orbs_with_chain_bonus(self):
        # 3 * 2.5 = 7.5 ; 7.5 * 1.10 = 8.25
        self.assertAlmostEqual(od_from_skill(3, 0, 0.10), 8.25, places=9)

    def test_skill_no_bonus(self):
        # 4 * 2.5 = 10.0
        self.assertAlmostEqual(od_from_skill(4), 10.0, places=9)

    def test_combo_orbs_apply_per_hit(self):
        self.assertAlmostEqual(od_from_skill(1, 3), 10.0, places=9)

    def test_zero_hits_is_zero(self):
        self.assertAlmostEqual(od_from_skill(0, 0), 0.0, places=9)

    def test_normal_attack_is_flat_7_5(self):
        self.assertAlmostEqual(od_from_normal_attack(), 7.5, places=9)

    def test_normal_attack_ignores_chain_bonus_by_default(self):
        self.assertAlmostEqual(od_from_normal_attack(chain_bonus=0.15), 7.5, places=9)

    def test_normal_attack_ignores_combo_orbs_by_default(self):
        self.assertAlmostEqual(od_from_normal_attack(combo_orbs=5), 7.5, places=9)

    def test_normal_attack_equivalent_hits_matches_spec(self):
        # 7.5 / 2.5 == 3 —— 普通攻击就是 3 hit 但不吃加成
        self.assertAlmostEqual(7.5 / 2.5, 3.0, places=9)


class TestChainBonusValidation(unittest.TestCase):
    def test_zero_is_allowed_as_no_bonus(self):
        self.assertAlmostEqual(od_from_skill(1, 0, 0.0), 2.5, places=9)

    def test_lower_bound_is_allowed(self):
        self.assertAlmostEqual(od_from_skill(1, 0, 0.01), 2.525, places=9)

    def test_upper_bound_is_allowed(self):
        self.assertAlmostEqual(od_from_skill(1, 0, 0.15), 2.875, places=9)

    def test_between_zero_and_lower_bound_is_rejected(self):
        with self.assertRaises(OdRuleError):
            od_from_skill(1, 0, 0.005)

    def test_above_upper_bound_is_rejected(self):
        with self.assertRaises(OdRuleError):
            od_from_skill(1, 0, 0.20)

    def test_negative_is_rejected(self):
        with self.assertRaises(OdRuleError):
            od_from_skill(1, 0, -0.05)

    def test_fractional_hits_rejected(self):
        with self.assertRaises(OdRuleError):
            od_from_skill(2.5, 0)


class TestBoss(unittest.TestCase):
    """伤害模型：单 hit 只作用于一个池子，绝不溢出到 HP。"""

    def test_dp_absorbs_first(self):
        boss = Boss("B", dp_max=10000, hp_max=10000, break_rate=2.0)
        result = boss.apply_damage(3000)
        self.assertEqual(result.applied_to, "dp")
        self.assertAlmostEqual(result.dp_lost, 3000)
        self.assertAlmostEqual(result.hp_damage, 0.0)
        self.assertAlmostEqual(result.wasted, 0.0)
        self.assertAlmostEqual(boss.dp, 7000)
        self.assertAlmostEqual(boss.hp, 10000)
        self.assertFalse(boss.broken)

    def test_overkill_on_shield_never_reaches_hp(self):
        """核心规则：Boss 只剩 1 点 DP 时，打 100000 也只是破盾，一滴血都掉不了。"""
        boss = Boss("B", dp_max=10000, hp_max=10000, break_rate=2.0)
        boss.dp = 1.0
        result = boss.apply_damage(100000)

        self.assertEqual(result.applied_to, "dp")
        self.assertAlmostEqual(result.dp_lost, 1.0)      # 盾只掉了 1 点
        self.assertAlmostEqual(result.hp_damage, 0.0)    # HP 一点没动
        self.assertAlmostEqual(result.wasted, 99999.0)   # 其余全是白打
        self.assertAlmostEqual(boss.dp, 0.0)
        self.assertAlmostEqual(boss.hp, 10000)
        self.assertTrue(boss.broken)
        self.assertTrue(result.just_broke)

    def test_break_hit_does_not_spill_into_hp(self):
        boss = Boss("B", dp_max=10000, hp_max=10000, break_rate=2.0)
        boss.dp = 3500
        result = boss.apply_damage(4000)

        self.assertAlmostEqual(result.dp_lost, 3500)
        self.assertAlmostEqual(result.hp_damage, 0.0)    # 溢出 500 不进 HP
        self.assertAlmostEqual(result.wasted, 500.0)
        self.assertAlmostEqual(boss.dp, 0)
        self.assertAlmostEqual(boss.hp, 10000)           # HP 没变
        self.assertTrue(boss.broken)
        self.assertTrue(result.just_broke)

    def test_damage_after_break_scales_by_break_rate(self):
        boss = Boss("B", dp_max=10000, hp_max=10000, break_rate=2.0)
        boss.dp = 3500
        boss.apply_damage(4000)  # 破盾，HP 仍是 10000
        result = boss.apply_damage(1000)

        self.assertEqual(result.applied_to, "hp")
        self.assertAlmostEqual(result.hp_damage, 2000)   # 1000 * 2.0
        self.assertAlmostEqual(boss.hp, 8000)
        self.assertFalse(result.just_broke)

    def test_hp_never_goes_negative(self):
        boss = Boss("B", dp_max=0, hp_max=1000, break_rate=1.0)
        boss.apply_damage(999999)
        self.assertAlmostEqual(boss.hp, 0.0)

    def test_set_break_rate_rejects_negative(self):
        boss = Boss("B", dp_max=1, hp_max=1)
        with self.assertRaises(ValueError):
            boss.set_break_rate(-1)

    def test_exact_lethal_dp_hit_does_not_touch_hp(self):
        boss = Boss("B", dp_max=1000, hp_max=1000, break_rate=2.0)
        result = boss.apply_damage(1000)
        self.assertAlmostEqual(boss.dp, 0)
        self.assertAlmostEqual(result.hp_damage, 0.0)
        self.assertTrue(boss.broken)


class TestGaugeBreak(unittest.TestCase):
    """GAUGE BREAK 锁盾：DP 最少保留 1，因此永远破不了盾、碰不到 HP。"""

    def test_locked_shield_keeps_one_dp(self):
        boss = Boss("B", dp_max=10000, hp_max=10000)
        boss.set_gauge_break(True)
        boss.dp = 5000
        result = boss.apply_damage(999999)

        self.assertAlmostEqual(boss.dp, 1.0)        # 锁在 1
        self.assertAlmostEqual(result.dp_lost, 4999.0)
        self.assertAlmostEqual(result.hp_damage, 0.0)
        self.assertFalse(boss.broken)
        self.assertFalse(result.just_broke)

    def test_unlock_lets_it_break(self):
        boss = Boss("B", dp_max=10000, hp_max=10000)
        boss.set_gauge_break(True)
        boss.dp = 5000
        boss.apply_damage(999999)
        self.assertAlmostEqual(boss.dp, 1.0)

        boss.set_gauge_break(False)
        result = boss.apply_damage(999999)
        self.assertAlmostEqual(boss.dp, 0.0)
        self.assertTrue(boss.broken)
        self.assertTrue(result.just_broke)

    def test_gauge_break_constructor_flag(self):
        boss = Boss("B", dp_max=100, hp_max=100, gauge_break=True)
        self.assertTrue(boss.gauge_break)
        self.assertAlmostEqual(boss.dp_floor, 1.0)
        boss.apply_damage(1e9)
        self.assertAlmostEqual(boss.dp, 1.0)

    def test_broken_boss_with_lock_still_no_dp(self):
        """盾已经破了的情况下，锁盾不该把 DP 凭空抬回来。"""
        boss = Boss("B", dp_max=100, hp_max=100)
        boss.dp = 0.0
        boss.broken = True
        boss.set_gauge_break(True)
        result = boss.apply_damage(50)
        self.assertEqual(result.applied_to, "hp")
        self.assertAlmostEqual(result.hp_damage, 50.0)


class TestOdGauge(unittest.TestCase):
    def test_accumulates(self):
        gauge = OdGauge()
        gauge.gain(20.125)
        gauge.gain(8.25)
        self.assertAlmostEqual(gauge.accumulated, 28.375, places=9)

    def test_levels_and_partial_default_100_per_level(self):
        gauge = OdGauge()
        gauge.gain(35.875)
        self.assertEqual(gauge.earned_levels, 0)
        self.assertEqual(gauge.available_levels, 0)
        self.assertAlmostEqual(gauge.partial_percent, 35.875, places=9)

    def test_level_cap_applies_to_available_not_earned(self):
        gauge = OdGauge(config=OdConfig(od_per_level=20.0, max_level=3))
        gauge.gain(100.0)
        self.assertEqual(gauge.earned_levels, 5)
        self.assertEqual(gauge.available_levels, 3)
        self.assertAlmostEqual(gauge.partial_percent, 0.0, places=9)

    def test_activate_consumes_level(self):
        gauge = OdGauge(config=OdConfig(od_per_level=20.0, max_level=3))
        gauge.gain(50.0)  # 2 levels + 10 partial
        gauge.activate(1)
        self.assertEqual(gauge.activated_levels, 1)
        self.assertAlmostEqual(gauge.accumulated, 30.0, places=9)
        self.assertEqual(gauge.available_levels, 1)

    def test_activate_more_than_available_raises(self):
        gauge = OdGauge(config=OdConfig(od_per_level=20.0, max_level=3))
        gauge.gain(20.0)
        with self.assertRaises(OdRuleError):
            gauge.activate(2)

    def test_negative_gain_rejected(self):
        gauge = OdGauge()
        with self.assertRaises(OdRuleError):
            gauge.gain(-1.0)


class TestFullBattle(unittest.TestCase):
    """整场推演，用 Demo 里那组数据手算对答案。"""

    SPEC = {
        "boss": {
            "name": "示例Boss",
            "dp_max": 10000,
            "hp_max": 10000,
            "break_rate": 2.0,
        },
        "od": {"od_per_level": 100.0, "max_level": 3},
        "turns": [
            {
                "turn": 1,
                "observed_od_percent": 35.875,
                "actions": [
                    {
                        "character": "月城最中",
                        "kind": "skill",
                        "skill": "得意洋洋",
                        "hits": 5,
                        "combo_orbs": 2,
                        "chain_bonus": 0.15,
                        "damage": 3000,
                    },
                    {
                        "character": "佐月",
                        "kind": "skill",
                        "skill": "连击",
                        "hits": 3,
                        "combo_orbs": 0,
                        "chain_bonus": 0.10,
                        "damage": 2000,
                    },
                    {"character": "月城最中", "kind": "normal", "damage": 1500},
                ],
            },
            {
                "turn": 2,
                "observed_od_percent": 50.25,
                "actions": [
                    {
                        "character": "月城最中",
                        "kind": "skill",
                        "skill": "充能注入",
                        "hits": 4,
                        "combo_orbs": 1,
                        "chain_bonus": 0.15,
                        "damage": 4000,
                    }
                ],
            },
        ],
    }

    def setUp(self):
        self.report = run_battle(load_spec(self.SPEC))

    def test_turn_count(self):
        self.assertEqual(len(self.report.records), 2)

    def test_turn_1_damage_and_od(self):
        rec = self.report.records[0]
        self.assertAlmostEqual(rec.total_damage, 6500)
        self.assertAlmostEqual(rec.od_gain, 35.875, places=9)
        # 20.125 + 8.25 + 7.5
        self.assertAlmostEqual(rec.od_accumulated, 35.875, places=9)
        self.assertAlmostEqual(rec.boss_dp, 3500)
        self.assertAlmostEqual(rec.boss_hp, 10000)
        self.assertFalse(rec.broken)

    def test_turn_2_breaks_but_hp_untouched(self):
        """DP 3500 挨 4000 那一击：破盾，但一滴血都不掉（单 hit 不拆分）。"""
        rec = self.report.records[1]
        self.assertAlmostEqual(rec.total_damage, 4000)
        # (4+1) * 2.5 * 1.15 = 14.375
        self.assertAlmostEqual(rec.od_gain, 14.375, places=9)
        self.assertAlmostEqual(rec.od_accumulated, 50.25, places=9)
        self.assertAlmostEqual(rec.boss_dp, 0)
        self.assertAlmostEqual(rec.boss_hp, 10000)   # 不是 9000 —— 没有溢出
        self.assertTrue(rec.broken)

    def test_turn_2_wasted_is_the_overkill(self):
        rec = self.report.records[1]
        item = rec.actions[0]
        self.assertAlmostEqual(item.dp_lost, 3500)
        self.assertAlmostEqual(item.hp_damage, 0.0)
        self.assertAlmostEqual(item.wasted, 500)     # 4000 - 3500
        self.assertTrue(item.broke)

    def test_hit_is_applied_individually_not_summed(self):
        """盾 100，两段各 60（合计 120 > 100）。

        分开算：第一段 100->40，第二段 40->0 破盾，**溢出 20 白费，HP 一点没掉**。
        如果实现成"先加总再结算"，结果看起来会一样 —— 所以下面第三条是判据。
        """
        spec = {
            "boss": {"dp_max": 100, "hp_max": 1000, "break_rate": 1.0},
            "turns": [{"actions": [{"kind": "skill", "damage": [60, 60]}]}],
        }
        report = run_battle(load_spec(spec))
        item = report.records[0].actions[0]
        self.assertAlmostEqual(item.dp_lost, 100)
        self.assertAlmostEqual(item.hp_damage, 0.0)     # 没有溢出
        self.assertAlmostEqual(item.wasted, 20.0)
        self.assertTrue(item.broke)
        self.assertAlmostEqual(report.final_hp, 1000)

    def test_third_hit_after_break_goes_to_hp(self):
        """同样 100 盾，三段各 60。

        这条正是「逐 hit 结算」的判据：
          逐 hit  -> 前两段破盾，第三段整段进 HP -> HP = 940
          先加总  -> 180 一次打盾，破盾但无溢出   -> HP = 1000
        """
        spec = {
            "boss": {"dp_max": 100, "hp_max": 1000, "break_rate": 1.0},
            "turns": [{"actions": [{"kind": "skill", "damage": [60, 60, 60]}]}],
        }
        report = run_battle(load_spec(spec))
        item = report.records[0].actions[0]
        self.assertAlmostEqual(item.dp_lost, 100)
        self.assertAlmostEqual(item.hp_damage, 60.0)    # 只有第三段进 HP
        self.assertAlmostEqual(report.final_hp, 940)

    def test_gauge_break_blocks_break_entirely(self):
        spec = {
            "boss": {"dp_max": 100, "hp_max": 1000, "break_rate": 1.0},
            "turns": [
                {
                    "gauge_break": True,
                    "actions": [{"kind": "skill", "damage": [999999, 999999]}],
                }
            ],
        }
        report = run_battle(load_spec(spec))
        self.assertAlmostEqual(report.final_dp, 1.0)    # 锁在 1
        self.assertAlmostEqual(report.final_hp, 1000)   # HP 一点没动
        self.assertFalse(report.final_broken)

    def test_totals(self):
        self.assertAlmostEqual(self.report.total_damage, 10500)
        self.assertAlmostEqual(self.report.total_od_gain, 50.25, places=9)
        self.assertAlmostEqual(self.report.final_dp, 0)
        self.assertAlmostEqual(self.report.final_hp, 10000)
        self.assertTrue(self.report.final_broken)

    def test_observed_od_matches_when_model_is_consistent(self):
        for rec in self.report.records:
            self.assertIsNotNone(rec.observed_od_percent)
            self.assertAlmostEqual(rec.delta_vs_accumulated, 0.0, places=9)


class TestInputValidation(unittest.TestCase):
    def test_unknown_top_level_field_rejected(self):
        with self.assertRaises(ValueError):
            load_spec({"boss": {"dp_max": 1, "hp_max": 1}, "turns": [{"actions": []}], "nope": 1})

    def test_missing_turns_rejected(self):
        with self.assertRaises(ValueError):
            load_spec({"boss": {"dp_max": 1, "hp_max": 1}})

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            load_spec(
                {
                    "boss": {"dp_max": 1, "hp_max": 1},
                    "turns": [{"actions": [{"kind": "ultimate"}]}],
                }
            )

    def test_damage_accepts_list(self):
        spec = load_spec(
            {
                "boss": {"dp_max": 100, "hp_max": 100},
                "turns": [
                    {
                        "actions": [
                            {"kind": "normal", "damage": [10, 20, 30]}
                        ]
                    }
                ],
            }
        )
        self.assertAlmostEqual(spec.turns[0].actions[0].total_damage, 60)

    def test_boss_requires_dp_and_hp_max(self):
        with self.assertRaises(ValueError):
            load_spec({"boss": {"dp_max": 1}, "turns": [{"actions": []}]})


class TestCalibrationHintRegression(unittest.TestCase):
    """回归: 只有部分回合录了屏幕读数时，不能拿 records[-1] 直接相减。

    之前的写法会在这组数据上抛 TypeError: None - float。
    """

    SPEC = {
        "boss": {"dp_max": 1000, "hp_max": 1000},
        "turns": [
            {
                "turn": 1,
                "observed_od_percent": 10.0,
                "actions": [{"kind": "skill", "hits": 4, "damage": 100}],
            },
            {
                "turn": 2,
                # 故意不填 observed_od_percent
                "actions": [{"kind": "normal", "damage": 100}],
            },
        ],
    }

    def test_render_with_partial_observed_does_not_crash(self):
        report = run_battle(load_spec(self.SPEC))
        text = render_text(report)
        self.assertIn("校准提示", text)

    def test_names_which_turns_have_readings(self):
        report = run_battle(load_spec(self.SPEC))
        text = render_text(report)
        self.assertIn("只有第 1 回合有屏幕读数", text)

    def test_no_readings_at_all_still_renders(self):
        spec = {
            "boss": {"dp_max": 1000, "hp_max": 1000},
            "turns": [{"actions": [{"kind": "normal", "damage": 100}]}],
        }
        text = render_text(run_battle(load_spec(spec)))
        self.assertIn("无法反推", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
