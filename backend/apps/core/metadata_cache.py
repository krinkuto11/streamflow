"""Bounded monotonic TTL cache for connector catalogs, never event readiness."""

import threading
import time
from collections import OrderedDict
from copy import deepcopy

from apps.core.request_coalescing import SingleFlight


class MetadataCache:
    def __init__(self, ttl_seconds=300, max_entries=32, clock=time.monotonic):
        self.ttl_seconds = max(0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.clock = clock
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._generation = 0
        self._reads = SingleFlight()
        self.hits = 0
        self.misses = 0

    def clear(self):
        with self._lock:
            self._generation += 1
            self._entries.clear()

    def get(self, key, loader):
        with self._lock:
            generation = self._generation
            entry = self._entries.get(key)
            if entry and entry[0] > self.clock():
                self.hits += 1
                self._entries.move_to_end(key)
                return deepcopy(entry[1])
            self.misses += 1

        def load():
            value = loader()
            with self._lock:
                if generation == self._generation:
                    self._entries[key] = (self.clock() + self.ttl_seconds, deepcopy(value))
                    self._entries.move_to_end(key)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
            return value

        return deepcopy(self._reads.run((generation, key), load))
