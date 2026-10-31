"""
core/lanes.py - the urgent lane for safety orders (audit step 5, approved 30 Oct).

The voice thread only transcribes. Ordinary commands go to the command worker (main.py), which
runs them one at a time in the order they were spoken. A safety order ("cease fire", "hold fire",
"check fire", "weapons hold / tight", "stop lasing", "laser off", "abort") is run at once on the voice
thread, through the deterministic handlers only, and voids everything spoken before it that hasn't
been done yet:
  - a model call in progress for an older utterance: its answer is dropped (Superseded is raised
    inside ollama_chat.chat once it returns), whatever it was for (decision 3: everything);
  - commands still queued from before it: dropped when the worker reaches them (decision 2).
Drops are logged and shown on the kneeboard comms log, never spoken (decision 4).

One handler lock (HandlerLock) is held by whichever lane is handling, so two handlers never run side
by side. ollama_chat.chat releases it while it waits on Ollama: that wait is the only moment a safety
order can run in the middle of another command.

Superseded and NeedsModel derive from BaseException ON PURPOSE: the handlers' broad
`except Exception` blocks (which speak "Say again, system fault") must not catch them. Only the
command worker / the voice thread catch them (main.py).
"""
import re
import threading
from contextlib import contextmanager
from typing import Optional

# ------------------------------------------------------------------ which utterances are safety orders
_ADDRESSEE = (r"(?:george|crew|gunners?|door gunners?|co ?pilot|wingm[ae]n|flight|all|everyone|"
              r"two|three|four|2|3|4|outcast (?:\d+|one|two|three|four|five|six))")
_CEASE = r"(?:cease|c s|cs|sees|seas|seize|cees|ceas|ceased|sease) ?fire"   # Whisper's versions too
_SAFETY = (r"(?:" + _CEASE + r"|ceasefire|cease firing|hold (?:your )?fire|check fire|weapons? (?:hold|tight)|"
           r"stop lasing|cease lasing|laser off|abort)")
_SAFETY_RX = re.compile(r"^(?:(?:hey|okay|ok)\s+)?(?:" + _ADDRESSEE + r"[\s,]+)?" + _SAFETY
                        + r"(?:[\s,]+" + _SAFETY + r")*(?:[\s,]+(?:now|immediately))?$")
MAX_WORDS = 6


def is_safety_order(text: str) -> bool:
    """A whole, short safety order (with an optional addressee); never a question about one."""
    t = (text or "").lower()
    if "?" in t:
        return False
    t = re.sub(r"[^a-z0-9\s,]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,")
    if not t or len(t.split()) > MAX_WORDS:
        return False
    return _SAFETY_RX.match(t) is not None


# ------------------------------------------------------------------ control flow
class Superseded(BaseException):
    """A safety order was spoken after the utterance being handled: drop what it was doing."""


class NeedsModel(BaseException):
    """The urgent lane reached a model call: the order goes to the normal queue instead."""


class HandlerLock:
    """One lock for all command handling. released() gives it up while this thread waits on Ollama
    (a no-op on threads that don't hold it: warm-up, priming, kneeboard)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._local = threading.local()

    def holding(self) -> bool:
        return getattr(self._local, "held", False)

    @contextmanager
    def held(self):
        self._lock.acquire()
        self._local.held = True
        try:
            yield
        finally:
            self._local.held = False
            self._lock.release()

    @contextmanager
    def released(self):
        if not self.holding():
            yield
            return
        self._local.held = False
        self._lock.release()
        try:
            yield
        finally:
            self._lock.acquire()
            self._local.held = True


class Lanes:
    def __init__(self):
        self.handler = HandlerLock()
        self._lock = threading.Lock()
        self._last_safety: Optional[float] = None     # capture time (monotonic) of the last safety order
        self._local = threading.local()               # what this thread is handling

    # -- the last safety order
    def note_safety(self, captured: float) -> None:
        with self._lock:
            if self._last_safety is None or captured > self._last_safety:
                self._last_safety = captured

    def superseded(self, captured: Optional[float]) -> bool:
        """True when a safety order was spoken after this utterance."""
        if captured is None:
            return False
        with self._lock:
            return self._last_safety is not None and self._last_safety > captured

    # -- what this thread is doing
    @contextmanager
    def working(self, captured: Optional[float], urgent: bool = False):
        old = (getattr(self._local, "captured", None), getattr(self._local, "urgent", False))
        self._local.captured, self._local.urgent = captured, urgent
        try:
            yield
        finally:
            self._local.captured, self._local.urgent = old

    def urgent(self) -> bool:
        return getattr(self._local, "urgent", False)

    # -- called by ollama_chat.chat
    def before_model(self) -> None:
        if self.urgent():
            raise NeedsModel()

    def after_model(self) -> None:
        if self.superseded(getattr(self._local, "captured", None)):
            raise Superseded()

    @contextmanager
    def model_call(self):
        """Wraps a model call: never on the urgent lane; the handler lock is free while it waits;
        an answer for an utterance older than the last safety order is dropped."""
        self.before_model()
        try:
            with self.handler.released():
                yield
        except Exception:
            self.after_model()          # superseded: drop quietly instead of "system fault"
            raise
        self.after_model()


LANES = Lanes()
