"""Pure reducer for observed (not predicted) IL2CPP battle hits."""


class ActionLedger:
    def __init__(self):
        self.active = {}
        self.serial = 0

    def update(self, entries, enemies):
        present = {e['address'] for e in entries}
        self.active = {k: v for k, v in self.active.items() if k in present}
        events = []
        for entry in entries:
            key = entry['address']
            signature = (entry['skill'], entry['actor'], entry['hit_set'])
            old = self.active.get(key)
            if old is None or old['signature'] != signature:
                self.serial += 1
                old = self.active[key] = dict(signature=signature, id=self.serial, value=None)
            hits = [h for h in entry['hits'] if h['finished'] and h['target'] in enemies
                    and h['type'] == 1 and h['damage'] > 0]
            value = sum(h['damage'] for h in hits)
            if hits and (old['value'] is None or value > old['value']):
                old['value'] = value
                events.append({'event_id': old['id'], 'readings': [{
                    'value': value, 'label': '合计', 'actor': entry['actor_name'],
                    'skill': entry['skill_name'], 'hits': hits,
                    'source': 'memory'}]})
        return events
