"""
command/tasking.py - the tasking registry (Phase 2, P2-1).

One record per request: who asked, which role owns it, its kind (SEAD / CAS / CAP / ESCORT ...), the
flight (Olympus group and unit ids, once it appears), the target track keys, its state and why.

    requested -> approved | denied | deferred | failed
    denied    -> overridden                        ("Warlord, override", P2-5)
    deferred  -> approved | denied | failed
    approved / overridden -> en_route | failed | aborted
    en_route  -> on_task | holding | attacking | complete | aborted | failed | lost
    on_task   -> holding | attacking | complete | aborted | failed | lost     (CAP / escort on station)
    holding   -> cleared | attacking | on_task | complete | aborted | failed | lost   (CAS at the IP)
    cleared   -> attacking | holding | complete | aborted | failed | lost
    attacking -> attacking | holding | on_task | complete | aborted | failed | lost
    complete, aborted, failed, lost: final. denied is final unless overridden.

R5 (4 Nov): each record names its controller - the role that may send the flight in (attackUnit, weapons
free): Darkside for Overlord's CAS, Overlord for his SEAD / CAP / escort, you for your Outcasts. The
permission table reads it (command/permissions.py) instead of a built-in "CAS is Darkside's"; reassigning a
flight between roles later only changes this field.

Records are immutable (frozen dataclasses replaced on change), so a reader's copy never changes under
it. Thread-safe: one lock, never held while calling out. A mission load clears everything (reset()).
"""
import itertools
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

STATES = ("requested", "approved", "denied", "deferred", "overridden", "en_route", "on_task", "holding",
          "cleared", "attacking", "complete", "aborted", "failed", "lost")
FINAL = frozenset({"complete", "aborted", "failed", "lost"})
_NEXT = {
    "requested": {"approved", "denied", "deferred", "failed"},
    "denied": {"overridden"},
    "deferred": {"approved", "denied", "failed"},
    "approved": {"en_route", "failed", "aborted"},
    "overridden": {"en_route", "failed", "aborted"},
    "en_route": {"on_task", "holding", "attacking", "complete", "aborted", "failed", "lost"},
    "on_task": {"holding", "attacking", "complete", "aborted", "failed", "lost"},
    "holding": {"cleared", "attacking", "on_task", "complete", "aborted", "failed", "lost"},
    "cleared": {"attacking", "holding", "complete", "aborted", "failed", "lost"},
    "attacking": {"attacking", "holding", "on_task", "complete", "aborted", "failed", "lost"},
}
LIVE = frozenset({"approved", "overridden", "en_route", "on_task", "holding", "cleared", "attacking"})
MAX_KEPT = 200


@dataclass(frozen=True)
class Task:
    task_id: str                          # "K1", "K2" ... per mission
    kind: str                             # "SEAD" / "CAS" / "CAP" / "ESCORT" ...
    owner: str                            # the role that runs it (command.roles)
    requested_by: str                     # "pilot" or a role
    flight: str                           # "Hog flight"
    where: str = ""
    targets: Tuple[str, ...] = ()         # track keys "dcs:<id>"
    state: str = "requested"
    reason: str = ""
    group: Optional[str] = None           # Olympus group, once it appears
    units: FrozenSet[int] = frozenset()   # its unit object ids
    created_mono: float = 0.0
    updated_mono: float = 0.0
    history: Tuple[Tuple[str, str, float], ...] = ()   # (state, reason, mono)
    controller: str = ""                  # R5: the role that may send it in (default_controller)

    @property
    def live(self) -> bool:
        return self.state in LIVE


def default_controller(kind: str, owner: str) -> str:
    """R5: Overlord's CAS is Darkside's to clear hot; everything else is controlled by its owner."""
    if kind == "CAS" and owner == "OVERLORD":
        return "DARKSIDE"
    return owner


class TaskRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic, printer: Callable[[str], None] = print):
        self._clock, self._print = clock, printer
        self._lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._ids = itertools.count(1)

    def new(self, kind: str, owner: str, flight: str, requested_by: str = "pilot", where: str = "",
            targets=(), controller: Optional[str] = None) -> Task:
        now = self._clock()
        ctrl = controller or default_controller(kind, owner)
        with self._lock:
            tid = f"K{next(self._ids)}"
            t = Task(tid, kind, owner, requested_by, flight, where, tuple(k for k in targets if k),
                     created_mono=now, updated_mono=now, history=(("requested", "", now),), controller=ctrl)
            self._tasks[tid] = t
            if len(self._tasks) > MAX_KEPT:              # drop the oldest finished ones
                done = sorted((x for x in self._tasks.values() if not x.live), key=lambda x: x.updated_mono)
                for x in done[:len(self._tasks) - MAX_KEPT]:
                    del self._tasks[x.task_id]
        self._print(f"[TASKING] {tid} {kind} for {flight} ({owner}"
                    + (f", sent in by {ctrl}" if ctrl != owner else "") + ") requested")
        return t

    def set_state(self, task_id: Optional[str], state: str, reason: str = "") -> bool:
        """Moves a task on. False (and nothing changes) for an unknown task or a move the table doesn't
        allow (e.g. anything after a final state)."""
        if task_id is None or state not in STATES:
            return False
        now = self._clock()
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None or state not in _NEXT.get(t.state, ()):
                return False
            self._tasks[task_id] = replace(t, state=state, reason=reason, updated_mono=now,
                                           history=t.history + ((state, reason, now),))
        self._print(f"[TASKING] {task_id} {t.kind} {t.state} -> {state}" + (f": {reason}" if reason else ""))
        return True

    def attach(self, task_id: Optional[str], group: Optional[str] = None, units=()) -> bool:
        """The flight that carries the task (its Olympus group and unit ids, added to those known)."""
        with self._lock:
            t = self._tasks.get(task_id) if task_id else None
            if t is None:
                return False
            ids = frozenset(int(u) for u in units if u is not None)
            self._tasks[task_id] = replace(t, group=group or t.group, units=t.units | ids,
                                           updated_mono=self._clock())
        return True

    def get(self, task_id: Optional[str]) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id) if task_id else None

    def owner_of(self, unit_id, live_only: bool = True) -> Optional[Task]:
        """The task whose flight has this unit (the newest, if ever two). live_only=False also finds a
        finished one: its flight still flies home under its owner."""
        try:
            uid = int(unit_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            hits = [t for t in self._tasks.values() if uid in t.units and (t.live or not live_only)]
        return max(hits, key=lambda t: t.created_mono) if hits else None

    def active(self) -> List[Task]:
        with self._lock:
            return sorted((t for t in self._tasks.values() if t.live), key=lambda t: t.created_mono)

    def recent(self, n: int = 20) -> List[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.updated_mono)[-n:]

    def reset(self) -> None:
        """A new mission: unit ids start again, so the old tasks mean nothing."""
        with self._lock:
            self._tasks.clear()
            self._ids = itertools.count(1)


STATE_SAID = {"requested": "requested", "approved": "approved", "denied": "denied", "deferred": "deferred",
              "overridden": "overridden", "en_route": "en route", "on_task": "on station", "holding": "holding",
              "cleared": "cleared hot", "attacking": "attacking", "complete": "complete", "aborted": "aborted",
              "failed": "failed", "lost": "lost"}


def _ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s} s" if s < 90 else f"{s // 60} min"


def rows(registry: "TaskRegistry", now: float, finished: int = 3, held: Optional[Tuple[str, float]] = None) -> List[dict]:
    """The kneeboard's Taskings list (R2): live tasks oldest first, then the last few finished ones.
    held = (task_id, seconds left) of a Warlord denial waiting for an override."""
    live = registry.active()
    done = [t for t in registry.recent(50) if not t.live][-finished:]
    out = []
    for t in live + list(reversed(done)):
        state = STATE_SAID.get(t.state, t.state)
        if t.state == "holding" and t.reason == "at the IP":
            state = "holding at the IP"
        why = "" if not t.reason or t.reason in ("at the IP",) or t.state in ("en_route", "approved") else f", {t.reason}"
        left = ""
        if held is not None and held[0] == t.task_id:
            left = f", override for {int(held[1])} s"
        out.append({"id": t.task_id, "kind": t.kind, "flight": t.flight, "state": t.state, "live": t.live,
                    "text": f"{t.task_id} {t.kind}, {t.flight}: {state}{why}{left} ({_ago(now - t.updated_mono)})"})
    return out


TASKS = TaskRegistry()
