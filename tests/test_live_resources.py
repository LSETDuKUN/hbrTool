import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hbr_capture.damage_stats import DamageLedger, pool_value, resource_state


class TestResourceLedger(unittest.TestCase):
    def test_initial_full_and_shield_only_damage(self):
        state = resource_state(1000, 2000, 0)
        self.assertEqual((state.dp, state.hp, state.last_dp_loss, state.last_hp_loss), (1000, 2000, 0, 0))
        state = resource_state(1000, 2000, 300, 300)
        self.assertEqual((state.dp, state.hp, state.last_dp_loss, state.last_hp_loss), (700, 2000, 300, 0))

    def test_crossing_shield_splits_the_current_hit(self):
        # First 800 hits DP. The next 500 exhausts its 200 and takes 300 HP.
        state = resource_state(1000, 2000, 1300, 500)
        self.assertEqual((state.dp, state.hp, state.last_dp_loss, state.last_hp_loss), (0, 1700, 200, 300))

    def test_exact_break_then_hp_and_overkill(self):
        state = resource_state(1000, 2000, 1000, 1000)
        self.assertEqual((state.dp, state.hp, state.last_hp_loss), (0, 2000, 0))
        state = resource_state(1000, 2000, 1250, 250)
        self.assertEqual((state.last_dp_loss, state.last_hp_loss), (0, 250))
        state = resource_state(1000, 2000, 9000, 7750)
        self.assertEqual((state.dp, state.hp, state.last_hp_loss), (0, 0, 1750))

    def test_zero_capacity(self):
        state = resource_state(0, 500, 200, 200)
        self.assertEqual((state.dp, state.hp), (0, 300))
        state = resource_state(0, 0, 999, 999)
        self.assertEqual((state.dp, state.hp, state.last_hp_loss), (0, 0, 0))

    def test_ocr_revision_and_multiplier_rebuild_without_double_subtraction(self):
        ledger = DamageLedger(enemies=2)
        unknown = {"run": "one", "event_id": 1, "readings": [{"value": 300, "label": "未知"}]}
        ledger.add(unknown)
        self.assertEqual(resource_state(500, 1000, ledger.total).dp, 500)
        known = dict(unknown, readings=[{"value": 300, "label": "平均"}])
        ledger.add(known)
        ledger.add(known)
        self.assertEqual((ledger.total, ledger.last_damage), (600, 600))
        self.assertEqual(resource_state(500, 1000, ledger.total).hp, 900)
        ledger.set_enemies(1)
        state = resource_state(500, 1000, ledger.total, ledger.last_damage)
        self.assertEqual((state.dp, state.hp), (200, 1000))

    def test_new_ledger_refills_both_pools(self):
        ledger = DamageLedger()
        ledger.add({"readings": [{"value": 700, "label": "合计"}]})
        ledger = DamageLedger(enemies=ledger.enemies)
        state = resource_state(500, 1000, ledger.total, ledger.last_damage)
        self.assertEqual((state.dp, state.hp, state.last_damage), (500, 1000, 0))

    def test_pool_input_rejects_negative_fractional_and_non_numeric_values(self):
        self.assertEqual(pool_value("1,250,000"), 1250000)
        self.assertEqual(pool_value("0"), 0)
        for value in ("", "-1", "1.5", "nan", "1e6", "test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pool_value(value)


class TestStoppedWidget(unittest.TestCase):
    def test_monitor_reports_f10_as_user_stop(self):
        import tempfile
        from pathlib import Path
        from hbr_capture.monitor import Monitor, MonitorConfig
        info = SimpleNamespace(minimized=False, title_bar_height=0)
        grabber = SimpleNamespace(info=info, method="bitblt")
        with tempfile.TemporaryDirectory() as tmp:
            monitor = Monitor(MonitorConfig(outdir=Path(tmp), quiet=True,
                hotkey_name=None, stop_hotkey_name="F10"))
            with patch("hbr_capture.monitor.Grabber", return_value=grabber), \
                    patch.object(monitor, "_ensure_live_capture"), \
                    patch.object(monitor, "_write_session"), \
                    patch("hbr_capture.monitor.win32.key_down", return_value=True):
                summary = monitor.run()
            self.assertTrue(summary["stopped_by_user"])
            self.assertEqual(summary["grabbed"], 0)

    def test_stopped_watchdog_never_moves_or_searches_for_game(self):
        from hbr_capture.widget import Widget
        widget = Widget.__new__(Widget)
        widget._want_running = False
        widget._physical_bounds = Mock(side_effect=AssertionError("must not inspect position"))
        widget.reposition = Mock()
        with patch("hbr_capture.widget.Grabber") as grabber:
            self.assertFalse(widget._ensure_visible())
        grabber.assert_not_called()
        widget.reposition.assert_not_called()

    def test_cancel_while_waiting_for_game_really_stops(self):
        from hbr_capture.widget import Widget
        widget = Widget.__new__(Widget)
        widget._want_running, widget.thread = True, None
        widget.start, widget.stop = Mock(), Mock()
        widget.toggle()
        widget.stop.assert_called_once()
        widget.start.assert_not_called()

    def test_hotkey_completion_tells_ui_not_to_reconnect(self):
        import queue
        from hbr_capture.widget import Widget
        widget = Widget.__new__(Widget)
        widget.queue = queue.Queue()
        widget.monitor = SimpleNamespace(run=lambda: {"stopped_by_user": True})
        widget._run_monitor()
        self.assertEqual(widget.queue.get_nowait(), ("stopped",))
