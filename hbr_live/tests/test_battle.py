import copy
import unittest
from hbr_live_app.events import ActionLedger
from hbr_live_app.model import BattleModel


def entry(values, finished=0, address=1, hit_set=2):
    return dict(address=address, hit_set=hit_set, actor=3, actor_name='Saki',
        skill='STezukaSkill02', skill_name='凌空铁锤', hits=[dict(address=100+n,
        target=4, type=1, damage=value, finished=n < finished,
        funnel=n > 0, critical=True) for n, value in enumerate(values)])


def state(dp=2100000, hp=5000000, address=4):
    return dict(address=address, name='ExoWatcher Ω', dp=dp, hp=hp)


class BattleTests(unittest.TestCase):
    def test_recorded_break_and_kill_replay(self):
        import json
        from pathlib import Path
        frames = json.loads((Path(__file__).parent / 'fixtures/break_and_overkill.json').read_text(encoding='utf-8'))
        ledger, model = ActionLedger(), BattleModel()
        for frame in frames:
            model.update(dict(enemies=frame['enemies'], events=ledger.update(frame['entries'], {4})))
        self.assertEqual([e['value'] for e in model.actions.values()], [2786203, 7250520])
        self.assertEqual(model.enemies[4]['hp'], 0)
        self.assertEqual(model.enemies[4]['dp'], 0)

    def test_predicted_hits_never_count_until_executed(self):
        ledger = ActionLedger()
        self.assertEqual([], ledger.update([entry([100, 25])], {4}))
        update = ledger.update([entry([100, 25], 1)], {4})
        self.assertEqual(update[0]['value'], 100)
        self.assertFalse(update[0]['settled'])

    def test_long_display_and_progress_are_one_action(self):
        ledger, model = ActionLedger(), BattleModel()
        for count in [0, 1, 1, 1, 2, 2, 2]:
            model.update(dict(enemies=[state()], events=ledger.update([entry([100, 25], count)], {4})))
        self.assertEqual(model.total, 125)
        self.assertEqual(len(model.actions), 1)

    def test_adjacent_same_value_new_action(self):
        ledger = ActionLedger()
        one = ledger.update([entry([100], 1)], {4})[0]
        ledger.update([], {4})
        two = ledger.update([entry([100], 1)], {4})[0]
        self.assertNotEqual(one['event_id'], two['event_id'])

    def test_entry_reuse_new_hit_set_without_blank(self):
        ledger = ActionLedger()
        one = ledger.update([entry([100], 1)], {4})[0]
        updates = ledger.update([entry([100], 1, hit_set=9)], {4})
        self.assertNotEqual(one['event_id'], updates[-1]['event_id'])

    def test_real_break_and_overkill_keep_resources_separate(self):
        ledger, model = ActionLedger(), BattleModel()
        hits = [1435494, 392415, 437819, 520475]
        events = ledger.update([entry(hits, 4)], {4})
        model.update(dict(enemies=[state(0, 4041706)], events=events))
        self.assertEqual(model.total, 2786203)
        self.assertEqual(model.enemies[4]['hp'], 4041706)
        ledger.update([], {4})
        events = ledger.update([entry([7250520], 1, address=8)], {4})
        model.update(dict(enemies=[state(0, 0)], events=events))
        self.assertEqual(model.total, 10036723)
        self.assertEqual(model.enemies[4]['hp'], 0)

    def test_dp_refill_updates_meter_without_inventing_damage(self):
        model = BattleModel()
        for dp in [200, 70, 0, 300, 300, 1]:
            model.update(dict(enemies=[state(dp)], events=[]))
        enemy = model.enemies[4]
        self.assertEqual(enemy['dp'], 1)
        self.assertEqual(enemy['dp_reference'], 300)
        self.assertEqual(enemy['dp_rises'], 1)
        self.assertEqual(model.total, 0)

    def test_multiple_targets_exclude_self_effects(self):
        ledger = ActionLedger()
        action = entry([100, 200, 999], 3)
        action['hits'][1]['target'] = 5
        action['hits'][2]['target'] = 3
        event = ledger.update([action], {4, 5})[0]
        self.assertEqual(event['value'], 300)
        self.assertEqual({h['target'] for h in event['hits']}, {4, 5})

    def test_packets_are_snapshots_not_mutable_references(self):
        ledger = ActionLedger()
        first = ledger.update([entry([100, 25], 1)], {4})[0]
        saved = copy.deepcopy(first)
        ledger.update([entry([100, 25], 2)], {4})
        self.assertEqual(first, saved)


class ReadFailures(unittest.TestCase):
    def test_source_worker_saves_events_and_closes_handle(self):
        import json
        import tempfile
        import threading
        from pathlib import Path
        from unittest.mock import patch
        from hbr_live_app.source import run
        from hbr_live_app import runtime
        messages = []
        snapshots = [([state()], []), ([state()], []),
                     ([state(0, 0)], [entry([7250520], 1)]), ([state(0, 0)], [])]
        with tempfile.TemporaryDirectory() as directory:
            with patch('hbr_live_app.windows.game_pid', return_value=123), \
                 patch.object(runtime, 'open_process'), patch.object(runtime, 'h', 1), \
                 patch.object(runtime, 'close') as close, \
                 patch('hbr_live_app.source.locate', return_value=100), \
                 patch('hbr_live_app.source.snapshot', side_effect=snapshots), \
                 patch('hbr_live_app.names.Names.actor', side_effect=lambda label: label):
                run(threading.Event(), threading.Event(), messages.append, directory)
                close.assert_called_once_with(1)
            records = [json.loads(line) for line in next(Path(directory).glob('*.jsonl')).read_text(encoding='utf-8').splitlines()]
        self.assertFalse(any(kind == 'error' for kind, _ in messages))
        self.assertEqual(records[-1]['reason'], 'battle_end')
        model = BattleModel()
        for record in records:
            if 'enemies' in record:
                model.update(record)
        self.assertEqual(model.total, 7250520)
        self.assertEqual(model.enemies[4]['hp'], 0)

    def test_failed_resource_read_is_not_reported_as_zero(self):
        from unittest.mock import Mock, patch
        from hbr_live_app.source import snapshot
        reader = Mock()
        reader.q.return_value = 100
        reader.cls.return_value = 'BattleEnemyData'
        reader.read.return_value = b''
        with patch('hbr_live_app.source.members', return_value=[4]):
            with self.assertRaisesRegex(RuntimeError, '资源读取中断'):
                snapshot(reader, 1)
