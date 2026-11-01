"""
tactical/events.py - meaningful changes in the picture, as short lines for the comms log, the Alerts tab
and "what's changed?" (design sections 7, 9 and 10).

    log = EventLog()
    new = log.add(transitions, names)      # names: track_id -> display name (from the snapshot)
    log.recent(since_mono)                 # newest last

Which table transitions become events:
    first fresh scan of a mission       one summary line ("Picture: 23 units in the DCS scan."), not 23 "new"
    new track                           "New: SA-8 Osa (T0051)."
    GONE with evidence                  "T-72B (T0042) destroyed by George."
    GONE without evidence               "T-72B (T0042) no longer in the picture."  (never "destroyed": D3)
    LOST                                "Su-25T (T0007) out of the picture."
    back from GONE / LOST               "BMP-2 (T0012) back in the picture."
    picture stale / current again       one line for the whole picture, not one per track
    mission loaded                      "New mission: picture reset."
Not events (noise): going out of coverage, per-track STALE, coming back from those.

Text says only what the table knows; distances and directions are added by the readers (kneeboard,
queries) from the snapshot. Pure and single-owner; the log is bounded.
"""
from collections import deque
from typing import Deque, Iterable, List, Mapping, Optional

from tactical.models import PictureEvent, Status
from tactical.tracks import Transition

MAX_EVENTS = 200


class EventLog:
    def __init__(self, maxlen: int = MAX_EVENTS):
        self._events: Deque[PictureEvent] = deque(maxlen=maxlen)
        self._primed_generation: Optional[int] = None
        self.dropped = 0                                    # events pushed out by the bound

    def add(self, transitions: Iterable[Transition], names: Mapping[str, str]) -> List[PictureEvent]:
        transitions = list(transitions)
        out: List[PictureEvent] = []
        news = [t for t in transitions if t.kind == "status" and t.before is None]
        for t in transitions:
            if t.kind == "new_picture":
                self._primed_generation = None
                out.append(PictureEvent("new_picture", None, "New mission: picture reset.", t.mono, t.generation))
            elif t.kind == "picture_stale":
                out.append(PictureEvent("stale", None, f"Picture stale: {t.reason}.", t.mono, t.generation))
            elif t.kind == "picture_fresh":
                out.append(PictureEvent("fresh", None, "Picture current again.", t.mono, t.generation))
        news_ids = set()
        if news and self._primed_generation != news[0].generation:
            self._primed_generation = news[0].generation      # the first units of this mission
            if len(news) > 1:                                 # several at once: one summary line
                out.append(PictureEvent("picture", None, f"Picture: {len(news)} units in the DCS scan.",
                                        news[0].mono, news[0].generation))
                news_ids = {t.track_id for t in news}
        for t in transitions:
            if t.kind != "status" or t.track_id in news_ids:
                continue
            ev = self._status_event(t, names.get(t.track_id) or "unknown type")
            if ev is not None:
                out.append(ev)
        for ev in out:
            if len(self._events) == self._events.maxlen:
                self.dropped += 1
            self._events.append(ev)
        return out

    @staticmethod
    def _status_event(t: Transition, name: str) -> Optional[PictureEvent]:
        who = f"{name} ({t.track_id})"
        if t.before is None and t.after == Status.ACTIVE:
            return PictureEvent("new", t.track_id, f"New: {who}.", t.mono, t.generation)
        if t.after == Status.GONE:
            if t.destroyed:
                return PictureEvent("destroyed", t.track_id, f"{who} {t.reason}.", t.mono, t.generation)
            return PictureEvent("gone", t.track_id, f"{who} no longer in the picture.", t.mono, t.generation)
        if t.after == Status.LOST:
            return PictureEvent("lost", t.track_id, f"{who} out of the picture.", t.mono, t.generation)
        if t.after == Status.ACTIVE and t.before in (Status.GONE, Status.LOST):
            return PictureEvent("reappeared", t.track_id, f"{who} back in the picture.", t.mono, t.generation)
        return None

    def recent(self, since_mono: float = float("-inf")) -> List[PictureEvent]:
        return [e for e in self._events if e.mono >= since_mono]

    def __len__(self) -> int:
        return len(self._events)
