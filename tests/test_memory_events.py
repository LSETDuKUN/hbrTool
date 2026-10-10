import unittest
from hbr_capture.memory_events import ActionLedger


class MemoryEventsTests(unittest.TestCase):
    def entry(self, done, value=100, address=1):
        return dict(address=address, skill='skill', actor=2, actor_name='Saki',
                    skill_name='test', hit_set=3, hits=[dict(finished=done,
                    target=4, type=1, damage=value)])

    def test_predictions_are_not_counted_and_revisions_replace(self):
        from hbr_capture.damage_stats import DamageLedger
        reducer, ledger = ActionLedger(), DamageLedger()
        self.assertEqual(reducer.update([self.entry(False)], {4}), [])
        for entry in [self.entry(True), self.entry(True), self.entry(True, 150)]:
            for event in reducer.update([entry], {4}):
                ledger.add(event)
        self.assertEqual(ledger.total, 150)
        self.assertEqual(len(ledger.events), 1)

    def test_same_damage_new_action_counts_again(self):
        reducer = ActionLedger()
        first = reducer.update([self.entry(True)], {4})[0]
        reducer.update([], {4})
        second = reducer.update([self.entry(True)], {4})[0]
        self.assertNotEqual(first['event_id'], second['event_id'])

    def test_self_effect_and_unexecuted_hits_excluded(self):
        reducer = ActionLedger()
        self.assertEqual(reducer.update([self.entry(True)], {5}), [])

    def test_overkill_is_preserved_in_display_damage(self):
        event = ActionLedger().update([self.entry(True, 7250520)], {4})[0]
        self.assertEqual(event['readings'][0]['value'], 7250520)
