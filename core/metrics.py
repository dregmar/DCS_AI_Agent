"""
core/metrics.py - latency per utterance and event counts (audit step 1). Measures only: nothing here
changes what the program does.

One record per utterance, from the end of PTT capture to the first word played:
    queue     capture end -> the voice thread picks it up
    stt       Whisper
    shared    radio menu / wingmen / Overlord / navigation handlers (handle_shared)
    model     time inside Ollama calls made for this utterance (sum)
    handler   the whole of handle_command
    speech_wait  queued speech -> playback starts (first transmission for this utterance)
    synth     Piper synthesis of that transmission
    to_audio  capture end -> that transmission starts playing (the end-to-end figure)

The voice thread binds the utterance's id (bind / bound); speak_radio picks it up from the calling
thread, so a reply queued while handling an utterance is credited to it. Speech queued from other
threads (George's driver, mission threads, callouts) carries no id and isn't counted here.

One "[LATENCY]" line is printed per utterance: when its first transmission starts, or when the
handler ends if nothing was queued, or when its last queued transmission is dropped.

Thread-safe: one lock; nothing is done under it but dictionary updates.
"""
import itertools
import statistics
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from typing import Dict, Optional

RECENT = 20                         # utterances kept for the Status page figures
_STAGES = ("queue", "stt", "shared", "model", "handler", "speech_wait", "synth", "to_audio")


class Metrics:
    def __init__(self, clock=time.monotonic, printer=print):
        self.clock, self.printer = clock, printer
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._open: Dict[int, dict] = {}          # utterances not yet reported
        self._recent = deque(maxlen=RECENT)        # reported records
        self._counts = Counter()
        self._local = threading.local()

    # ------------------------------------------------------------ binding
    def current(self) -> Optional[int]:
        return getattr(self._local, "uid", None)

    @contextmanager
    def bound(self, uid: Optional[int]):
        """Credits speech queued on this thread to utterance uid (None: nothing)."""
        old = self.current()
        self._local.uid = uid
        try:
            yield uid
        finally:
            self._local.uid = old

    # ------------------------------------------------------------ the voice thread
    def start(self, captured: Optional[float], picked: Optional[float] = None) -> int:
        """A new utterance: captured = end of capture (time.monotonic), picked = taken off the queue."""
        uid = next(self._ids)
        picked = self.clock() if picked is None else picked
        rec = {"id": uid, "captured": captured, "text": "", "pending": 0, "handled": False, "played": False,
               "queue": (picked - captured) if captured is not None else None}
        with self._lock:
            self._open[uid] = rec
            while len(self._open) > 50:                     # never grows without bound
                self._open.pop(next(iter(self._open)))
        return uid

    def stage(self, uid: Optional[int], name: str, secs: float, add: bool = False) -> None:
        if uid is None:
            return
        with self._lock:
            rec = self._open.get(uid)
            if rec is None:
                return
            rec[name] = (rec.get(name) or 0.0) + secs if add else secs

    def text(self, uid: Optional[int], text: str) -> None:
        with self._lock:
            if uid in self._open:
                self._open[uid]["text"] = text[:60]

    def model_time(self, secs: float) -> None:
        """An Ollama call on this thread (summed into the bound utterance)."""
        self.stage(self.current(), "model", secs, add=True)

    def handled(self, uid: Optional[int], secs: float) -> None:
        """handle_command is done. Reports now if no speech is waiting for this utterance."""
        if uid is None:
            return
        with self._lock:
            rec = self._open.get(uid)
            if rec is None:
                return
            rec["handler"], rec["handled"] = secs, True
            played, done = rec.get("played"), rec["pending"] == 0
        if played:
            self._report(uid, "")
        elif done:
            self._report(uid, "no reply spoken")

    def forget(self, uid: Optional[int]) -> None:
        """The utterance stopped before handling (no speech, filtered): not a latency sample."""
        with self._lock:
            self._open.pop(uid, None)

    # ------------------------------------------------------------ the speech thread
    def queued(self, uid: Optional[int]) -> None:
        """Speech queued for uid, or work handed to another thread that may speak (release() it)."""
        if uid is None:
            return
        with self._lock:
            if uid in self._open:
                self._open[uid]["pending"] += 1

    def release(self, uid: Optional[int]) -> None:
        """Another thread finished work it was handed for uid (see queued(): a hold)."""
        if uid is None:
            return
        with self._lock:
            rec = self._open.get(uid)
            if rec is None:
                return
            rec["pending"] = max(0, rec["pending"] - 1)
            played, done = rec["played"], rec["pending"] == 0 and rec["handled"]
        if done and not played:
            self._report(uid, "no reply spoken")

    def dropped(self, uid: Optional[int]) -> None:
        """A queued transmission for uid was dropped unspoken (too old)."""
        if uid is None:
            return
        with self._lock:
            rec = self._open.get(uid)
            if rec is None:
                return
            rec["pending"] = max(0, rec["pending"] - 1)
            done = rec["pending"] == 0 and rec["handled"]
        if done:
            self._report(uid, "reply dropped")

    def playing(self, uid: Optional[int], enqueued: float, synth_secs: float, started: float) -> None:
        """The first transmission for uid starts playing (later ones are ignored)."""
        if uid is None:
            return
        with self._lock:
            rec = self._open.get(uid)
            if rec is None or rec["played"]:
                return
            rec["speech_wait"] = started - enqueued - synth_secs
            rec["synth"] = synth_secs
            if rec.get("captured") is not None:
                rec["to_audio"] = started - rec["captured"]
            rec["played"] = True
            report = rec["handled"]          # else handled() reports, with the handler time
        if report:
            self._report(uid, "")

    # ------------------------------------------------------------ counts
    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    # ------------------------------------------------------------ reporting
    def _report(self, uid: int, note: str) -> None:
        with self._lock:
            rec = self._open.pop(uid, None)
            if rec is None:
                return
            rec["note"] = note
            self._recent.append(rec)
        parts = [f"{k} {rec[k]:.2f}" for k in _STAGES if rec.get(k) is not None]
        self.printer(f"[LATENCY] #{uid} '{rec['text']}': " + ", ".join(parts) + (f" ({note})" if note else ""))

    def summary(self) -> Dict:
        """For the Status page: medians and maxima over the recent utterances, and the counts."""
        with self._lock:
            recent = list(self._recent)
            counts = dict(self._counts)
        out = {"n": len(recent), "counts": counts}
        for k in _STAGES:
            vals = [r[k] for r in recent if r.get(k) is not None]
            if vals:
                out[k] = {"median": statistics.median(vals), "max": max(vals), "n": len(vals)}
        if recent:
            last = recent[-1]
            out["last"] = {k: last.get(k) for k in _STAGES + ("text", "note")}
        return out


METRICS = Metrics()
