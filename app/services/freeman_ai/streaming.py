"""Bounded process-local owner drafts. Final messages/jobs remain durable in PostgreSQL.

A different HTTP process or process restart has no draft: clients fall back to the
existing persisted conversation. This channel never schedules/retries work.
"""
import time
from .security import chat_text

class Drafts:
    def __init__(self, capacity=32, ttl=90):
        self.items = {}
        self.capacity, self.ttl = capacity, ttl

    def prune(self):
        clock = time.monotonic()
        for key, item in list(self.items.items()):
            if clock - item['at'] > self.ttl:
                del self.items[key]

    def publish(self, job_id, lease, message_id, text, phase='writing'):
        self.prune()
        key = (job_id, lease)
        if key not in self.items and len(self.items) >= self.capacity:
            del self.items[min(self.items, key=lambda k: self.items[k]['at'])]
        self.items[key] = dict(at=time.monotonic(), message_id=message_id,
                               text=chat_text(text)[:3500], phase=phase)

    def read(self, job_id, lease):
        self.prune()
        item = self.items.get((job_id, lease))
        return {k:v for k,v in item.items() if k != 'at'} if item else None

    def discard(self, job_id, lease):
        self.items.pop((job_id, lease), None)

drafts = Drafts()
