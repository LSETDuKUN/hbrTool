import sys
import tkinter as tk
import unittest
from unittest.mock import patch
from hbr_live_app.app import App


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(App, '_load_settings', return_value={}):
            self.app = App(self.root)

    def tearDown(self):
        self.app.closing = True
        self.root.update_idletasks()
        for after in self.root.tk.call('after', 'info'):
            self.root.after_cancel(after)
        self.root.destroy()

    def test_event_updates_existing_row_and_refilled_meter(self):
        enemy = dict(address=4, dp=100, hp=500, name='测试敌人')
        event = dict(event_id=1, actor='Saki', skill='凌空铁锤', value=50, settled=False,
                     hits=[dict(target=4, damage=50, funnel=False, critical=True)])
        self.app.model.update(dict(enemies=[enemy], events=[event]))
        self.app.render([event])
        event = dict(event, value=75, settled=True)
        enemy = dict(enemy, dp=300)
        self.app.model.update(dict(enemies=[enemy], events=[event]))
        self.app.render([event])
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertEqual(self.app.enemy_cards[4].state['dp'], 300)
        self.assertEqual(self.app.total_label.cget('text'), '75')
        self.assertIn('凌空铁锤', self.app.detail_text())

    def test_layout_switch_and_ocr_independence(self):
        for compact in [True, False, True]:
            self.app.compact.set(compact)
            self.app.layout()
            self.root.update_idletasks()
        self.assertFalse(any(k.startswith(('hbr_capture', 'hbr_recog')) for k in sys.modules))

    def test_three_enemy_cards_and_target_damage_detail(self):
        enemies = [dict(address=n, dp=n*100, hp=n*1000, name='测试敌人') for n in (4, 5, 6)]
        event = dict(event_id=1, actor='Saki', skill='群体技能', value=300, settled=True,
                     hits=[dict(target=n, damage=amount, funnel=False, critical=False)
                           for n, amount in [(4, 100), (6, 200)]])
        self.app.model.update(dict(enemies=enemies, events=[event]))
        self.app.render([event])
        self.assertEqual(len(self.app.enemy_cards), 3)
        self.assertEqual(self.app.enemy_cards[5].state['hp'], 5000)
        self.assertEqual(self.app.target_summary(event), '敌1、敌3')
        self.assertIn('敌1 100 / 敌3 200', self.app.detail_text())

    def test_pause_does_not_clear_model(self):
        self.app.connected = True
        self.app.model.run_id = 'keep'
        self.app.toggle_pause()
        self.assertTrue(self.app.paused.is_set())
        self.app.toggle_pause()
        self.assertFalse(self.app.paused.is_set())
        self.assertEqual(self.app.model.run_id, 'keep')
