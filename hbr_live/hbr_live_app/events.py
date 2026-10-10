"""Accumulate executed hits by action identity, including overkill display values."""

class ActionLedger:
    def __init__(self):
        self.active = {}
        self.serial = 0

    def update(self, entries, enemies):
        events = []
        present = {entry['address'] for entry in entries}
        for key in list(self.active):
            if key not in present:
                old = self.active.pop(key)
                if old['hits']:
                    events.append(self.event(old, True))
        for entry in entries:
            key = entry['address']
            signature = (entry['skill'], entry['actor'], entry['hit_set'])
            old = self.active.get(key)
            if old is None or old['signature'] != signature:
                if old and old['hits']:
                    events.append(self.event(old, True))
                self.serial += 1
                old = self.active[key] = dict(signature=signature, id=self.serial,
                    actor=entry['actor_name'], skill=entry['skill_name'], hits={}, done=False)
            changed = False
            for hit in entry['hits']:
                if hit['finished'] and hit['target'] in enemies and hit['type'] == 1 and hit['damage'] > 0:
                    if old['hits'].get(hit['address']) != hit:
                        old['hits'][hit['address']] = dict(hit)
                        changed = True
            relevant = [h for h in entry['hits'] if h['target'] in enemies and h['type'] == 1]
            done = bool(relevant) and all(h['finished'] for h in relevant)
            if old['hits'] and (changed or done != old['done']):
                events.append(self.event(old, done))
            old['done'] = done
        return events

    @staticmethod
    def event(old, done):
        hits = list(old['hits'].values())
        return dict(event_id=old['id'], actor=old['actor'], skill=old['skill'], skill_label=old['signature'][0],
                    value=sum(h['damage'] for h in hits), hits=hits, settled=done)
