"""
command/permissions.py - the one permission table every Olympus command passes (Phase 2, P2-1).

Each role module sends its commands inside acting_as(role, task_id): a thread-local, so a mission's own
thread and the reply thread each carry their own. A command sent outside any role is the pilot's
(your wingmen, Outcasts, marking today) and is always allowed, as before.

    command                         PILOT   OVERLORD                     DARKSIDE                   WARLORD
    spawnAircrafts                  yes     his task, once approved      no                         no
    attackUnit / setROE free        yes     his SEAD / CAP / ESCORT      Overlord's CAS, cleared    no
    setROE hold / return fire       yes     yes, any flight              yes, any flight            yes (anyone can stop)
    smoke / fireLaser / fireInfrared yes    no                           yes                        no
    followUnit, setPath, landing,   yes     his own flights (also after  no                         no
      altitude, speed, reaction             the task ends: going home)
    deleteUnit                      yes     his own flights              no                         no
    anything else                   yes     no                           no                         no

check() only answers; Guard (the Olympus client's guard hook, common/olympus.py) applies it. With
enforce=False (PERMISSIONS_ENFORCE, False until P2-6) a refusal is only logged as "would refuse" and
the command goes out as today. The pilot's own weapons and George's fire are never Olympus commands
and never pass through here.
"""
import contextlib
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from command.roles import DARKSIDE, OVERLORD, PILOT, WARLORD
from command.tasking import TASKS, TaskRegistry

ROE_FREE_MAX = 2                 # DCS ROE 0 weapons free, 1 / 2 free / priority; 3 return fire, 4 hold
OVERLORD_ATTACKS = frozenset({"SEAD", "CAP", "ESCORT"})
MOVE = frozenset({"followUnit", "setPath", "landAtPoint", "landAt", "setAltitudeType", "setSpeed",
                  "changeAltitude", "setAltitude", "setReactionToThreat", "changeSpeed", "setSpeedType"})
MARK = frozenset({"smoke", "fireLaser", "fireInfrared"})

_local = threading.local()


@contextlib.contextmanager
def acting_as(role: str, task_id: Optional[str] = None):
    """Commands sent inside this block are sent by role (for task_id). Nests; per thread."""
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    stack.append((role, task_id))
    try:
        yield
    finally:
        stack.pop()


def current() -> Tuple[str, Optional[str]]:
    stack = getattr(_local, "stack", None)
    return stack[-1] if stack else (PILOT, None)


def _roe(params: Dict[str, Any]) -> Optional[int]:
    try:
        return int(params.get("ROE"))
    except (TypeError, ValueError):
        return None


def check(name: str, params: Dict[str, Any], role: str, task_id: Optional[str] = None,
          tasks: TaskRegistry = TASKS) -> Tuple[bool, str]:
    """(allowed, why not). Pure apart from reading the registry."""
    if role == PILOT:
        return True, ""
    params = params if isinstance(params, dict) else {}
    unit_task = tasks.owner_of(params.get("ID"), live_only=False) if "ID" in params else None
    roe = _roe(params) if name == "setROE" else None
    if name == "setROE" and roe is not None and roe > ROE_FREE_MAX:
        return True, ""                                      # holding fire: anyone may stop an attack
    who = role.capitalize()
    if role == WARLORD:
        return False, "Warlord approves and stops; he doesn't task flights"
    if name == "spawnAircrafts":
        if role != OVERLORD:
            return False, f"only Overlord spawns flights, not {who}"
        t = tasks.get(task_id)
        if t is None or t.owner != OVERLORD:
            return False, "Overlord spawn with no tasking record"
        if t.state not in ("approved", "overridden"):
            return False, f"tasking {t.task_id} is {t.state}, not approved"
        return True, ""
    if name in MARK:
        return (True, "") if role == DARKSIDE else (False, f"marking is Darkside's, not {who}'s")
    if name in ("attackUnit", "setROE"):                    # setROE here = weapons free
        if unit_task is None or not unit_task.live:
            return False, f"unit {params.get('ID')} isn't on a live tasking"
        if unit_task.kind == "CAS" and unit_task.owner == OVERLORD:
            if role != DARKSIDE:
                return False, f"{unit_task.flight} is CAS: Darkside clears it hot, not {who}"
            if unit_task.state not in ("cleared", "attacking"):
                return False, f"{unit_task.flight} isn't cleared hot ({unit_task.state})"
            return True, ""
        if role == OVERLORD and unit_task.owner == OVERLORD and unit_task.kind in OVERLORD_ATTACKS:
            return True, ""
        return False, f"{unit_task.flight} ({unit_task.kind}) isn't {who}'s to send in"
    if name in MOVE or name == "deleteUnit":
        if role == OVERLORD and unit_task is not None and unit_task.owner == OVERLORD:
            return True, ""
        return False, f"{name}: not one of {who}'s flights"
    return False, f"{name} isn't in {who}'s permissions"


class Guard:
    """The Olympus client's guard(name, params) -> (ok, why). Logs every refusal (once per role,
    command and reason on the console; all of them in recent()). enforce=False lets them through."""
    def __init__(self, tasks: TaskRegistry = TASKS, enforce: bool = False, printer: Callable[[str], None] = print,
                 maxlen: int = 50):
        self.tasks, self.enforce, self._print = tasks, enforce, printer
        self._lock = threading.Lock()
        self._refused: Deque[Tuple[str, str, str]] = deque(maxlen=maxlen)
        self._said = set()
        self.count = 0

    def __call__(self, name: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        role, task_id = current()
        ok, why = check(name, params, role, task_id, self.tasks)
        if ok:
            return True, ""
        key = (role, name, why)
        with self._lock:
            self.count += 1
            self._refused.append(key)
            first = key not in self._said
            self._said.add(key)
        if first:
            self._print(f"[PERMISSIONS] {'refused' if self.enforce else 'would refuse'} {name} "
                        f"from {role}: {why}")
        return (False, why) if self.enforce else (True, "")

    def recent(self):
        with self._lock:
            return list(self._refused)

    def link(self) -> dict:
        """The Status page row."""
        with self._lock:
            n, last = self.count, (self._refused[-1] if self._refused else None)
        mode = "enforced" if self.enforce else "watching (P2-1: logs only)"
        detail = f"{mode}; {n} {'refused' if self.enforce else 'would refuse'}"
        if last:
            detail += f", last: {last[1]} from {last[0]}: {last[2]}"
        return {"name": "Command roles (permissions)", "ok": True, "detail": detail}
