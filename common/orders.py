"""
common/orders.py - explicit outcomes of Olympus-backed orders (audit step 6).

Every order to a wingman, an Outcast or an Overlord flight gets an OrderResult:
    accepted   understood and started; the result comes later (a clone appearing, a flight spawning)
    submitted  the command(s) went out and Olympus / the radio menu took them
    completed  the asynchronous part is done (Outcast up, Overlord flight home / cancelled)
    failed     refused, unreachable, or it never happened (no clone, flight failed to launch / down)

Handlers keep returning what they said before (the spoken wording is unchanged); they record the
outcome here. Each result is logged as "[ORDER] ..." and passed to on_result (main.py: the kneeboard
comms log, as a line of kind "order").

capture(): the Olympus commands sent on this thread while it's open, as (name, ok, why), noted by
common/olympus.py Olympus.command. outcome() turns them (or, when a fake client sent nothing, the
spoken answer) into a status.

Thread-safe: one lock around a bounded deque; on_result is called outside it.
"""
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

STATUSES = ("accepted", "submitted", "completed", "failed")
_UNABLE = re.compile(r"^\s*(?:overlord,\s*)?(?:unable|negative)\b", re.I)


@dataclass(frozen=True)
class OrderResult:
    who: str                     # "Two", "Outcast 2", "Weasel flight", "Flight"
    order: str                   # "attack", "follow", "go independent", "SEAD", ...
    status: str                  # one of STATUSES
    reason: str = ""
    t: float = field(default_factory=time.time)

    def text(self) -> str:
        return f"{self.who}: {self.order}, {self.status}" + (f" ({self.reason})" if self.reason else "")


# ------------------------------------------------------------------ capture of Olympus commands
_local = threading.local()


@contextmanager
def capture():
    """Collects (name, ok, why) for every Olympus command sent on this thread inside the block."""
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    sent: List[Tuple[str, bool, str]] = []
    stack.append(sent)
    try:
        yield sent
    finally:
        stack.pop()


def note_command(name: str, ok: bool, why: str = "") -> None:
    """Called by Olympus.command with each result (cheap when nothing is capturing)."""
    for sent in getattr(_local, "stack", None) or ():
        sent.append((name, bool(ok), why or ""))


def outcome(sent: List[Tuple[str, bool, str]], said: Optional[str]) -> Optional[Tuple[str, str]]:
    """(status, reason) from the commands sent, else from the spoken answer; None = not an order
    that went anywhere (e.g. "Nobody's lasing.")."""
    if sent:
        bad = [s for s in sent if not s[1]]
        if bad:
            return "failed", f"{bad[0][0]}: {bad[0][2]}" if bad[0][2] else bad[0][0]
        return "submitted", ", ".join(dict.fromkeys(s[0] for s in sent))
    if said and _UNABLE.match(said):
        return "failed", said.strip().rstrip(".")
    return None


# ------------------------------------------------------------------ the registry
class OrderLog:
    def __init__(self, maxlen: int = 100, printer=print):
        self._lock = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self.printer = printer
        self.on_result: Optional[Callable[[OrderResult], None]] = None

    def record(self, who: str, order: str, status: str, reason: str = "") -> OrderResult:
        if status not in STATUSES:
            raise ValueError(f"unknown order status {status!r}")
        r = OrderResult(str(who), str(order), status, str(reason or ""))
        with self._lock:
            self._items.append(r)
        self.printer(f"[ORDER] {r.text()}")
        cb = self.on_result
        if cb is not None:
            try:
                cb(r)
            except Exception as e:
                self.printer(f"[ORDER] comms log: {type(e).__name__}: {e}")
        return r

    def record_outcome(self, who: str, order: str, sent, said: Optional[str]) -> Optional[OrderResult]:
        got = outcome(sent, said)
        return self.record(who, order, *got) if got else None

    def recent(self, n: int = 20) -> List[OrderResult]:
        with self._lock:
            return list(self._items)[-n:]


ORDERS = OrderLog()
