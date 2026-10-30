"""
kneeboard/comms_log.py - everything said on the radio and intercom, for the kneeboard (feature 1).

    LOG.add("pilot" | "george" | "overlord", text)   from the voice worker / the speech worker
    LOG.since(n) -> entries with id > n               for the page (SSE)

Thread-safe (a lock around a bounded deque); add() never blocks for long, so it's safe to call from
the speech and voice threads.
"""
import threading
import time
from collections import deque
from typing import List

MAX_ENTRIES = 300


class CommsLog:
    def __init__(self, maxlen: int = MAX_ENTRIES, clock=time.time):
        self._lock = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self._next = 1
        self.clock = clock

    def add(self, who: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._items.append({"id": self._next, "t": self.clock(), "who": who, "text": text})
            self._next += 1

    def since(self, last_id: int = 0) -> List[dict]:
        with self._lock:
            return [dict(e) for e in self._items if e["id"] > last_id]

    def last_id(self) -> int:
        with self._lock:
            return self._next - 1

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


LOG = CommsLog()
