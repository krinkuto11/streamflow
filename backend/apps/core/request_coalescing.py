"""Coalesce concurrent reads without turning previous results into fresh data."""

import threading
from concurrent.futures import Future


class SingleFlight:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    def run(self, key, operation):
        with self._lock:
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result()
        try:
            result = operation()
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]
