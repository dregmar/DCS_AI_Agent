"""
kneeboard/alerts.py - the kneeboard's alert panel (agreed 30 Oct 2026). Visual only: nothing here
speaks; the sources keep their spoken calls exactly as before.

    critical   Overlord's close-bandit call, bingo fuel, Apache EUFD warnings, the annunciator's
               priority air-defence warning
    warning    joker fuel, EUFD cautions, low altitude, a lost link (telemetry, DCS-BIOS, comms hook,
               Olympus, Ollama), enemy picture stale
    info       pop-up group, a link back up, training targets destroyed: clear after INFO_TTL_S

Critical and warning alerts stay until acknowledged ("kneeboard, acknowledge" / "acknowledge all")
or until their cause is gone (each alert may carry a resolved() check, run by tick() once a second
on the alert thread, outside the lock). Raising an alert that's already active updates it (count,
newest text) instead of adding another. Acknowledged and resolved alerts stay listed, greyed, for
HISTORY_S. Failed orders are not alerts: they stay on the Comms page.

Thread-safe: one lock, dictionary work only under it; resolved() checks run outside it.
"""
import itertools
import threading
import time
from typing import Callable, Dict, List, Optional

SEVERITIES = ("critical", "warning", "info")
INFO_TTL_S = 60.0
HISTORY_S = 600.0
MAX_ACTIVE = 40


class Alerts:
    def __init__(self, wall: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic):
        self.wall, self.clock = wall, clock
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._active: Dict[str, dict] = {}            # key -> alert
        self._done: List[dict] = []                   # acknowledged / resolved / expired, newest last
        self._version = 0

    # ------------------------------------------------------------ raising and clearing
    def raise_(self, key: str, severity: str, text: str, resolved: Optional[Callable[[], bool]] = None) -> None:
        if severity not in SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        now, wall = self.clock(), self.wall()
        with self._lock:
            a = self._active.get(key)
            if a is None:
                a = {"id": next(self._ids), "key": key, "count": 0, "t": wall}
                self._active[key] = a
                while len(self._active) > MAX_ACTIVE:              # never grows without bound
                    self._retire(next(iter(self._active)), "expired", wall)
            if a.get("severity") is None or SEVERITIES.index(severity) < SEVERITIES.index(a["severity"]):
                a["severity"] = severity                            # a repeat can only raise it
            a.update(text=text, last=wall, mono=now, resolved=resolved, count=a["count"] + 1)
            self._version += 1

    def clear(self, key: str, how: str = "resolved") -> bool:
        with self._lock:
            if key not in self._active:
                return False
            self._retire(key, how, self.wall())
            self._version += 1
            return True

    def _retire(self, key: str, how: str, wall: float) -> None:
        a = self._active.pop(key)
        a.update(state=how, ended=wall)
        self._done.append(a)

    def ack_newest(self) -> bool:
        """Acknowledges the newest active alert."""
        with self._lock:
            if not self._active:
                return False
            key = max(self._active.values(), key=lambda a: (a["last"], a["id"]))["key"]
            self._retire(key, "acknowledged", self.wall())
            self._version += 1
            return True

    def ack_all(self) -> int:
        with self._lock:
            keys = list(self._active)
            wall = self.wall()
            for k in keys:
                self._retire(k, "acknowledged", wall)
            if keys:
                self._version += 1
            return len(keys)

    def reset(self) -> None:
        """A new mission: the last one's alerts go to the history."""
        self.ack_all()

    # ------------------------------------------------------------ once a second
    def tick(self) -> None:
        """Clears alerts whose cause is gone and info alerts past INFO_TTL_S; trims the history."""
        now, wall = self.clock(), self.wall()
        with self._lock:
            checks = [(k, a["resolved"]) for k, a in self._active.items() if a.get("resolved") is not None]
            expired = [k for k, a in self._active.items()
                       if a["severity"] == "info" and now - a["mono"] > INFO_TTL_S]
        gone = []
        for k, fn in checks:                          # outside the lock: these read other registries
            try:
                if fn():
                    gone.append(k)
            except Exception as e:
                print(f"[ALERTS] check for '{k}' failed: {type(e).__name__}: {e}")
        with self._lock:
            changed = False
            for k in gone:
                if k in self._active:
                    self._retire(k, "resolved", wall)
                    changed = True
            for k in expired:
                if k in self._active:
                    self._retire(k, "expired", wall)
                    changed = True
            keep = [a for a in self._done if wall - a["ended"] <= HISTORY_S]
            if len(keep) != len(self._done):
                self._done = keep
                changed = True
            if changed:
                self._version += 1

    # ------------------------------------------------------------ for the page
    def version(self) -> int:
        with self._lock:
            return self._version

    def view(self) -> Dict:
        """Active alerts newest first, recent history, and the marker line. No times that tick, so the
        feed only changes when the alerts do."""
        with self._lock:
            active = sorted(self._active.values(), key=lambda a: (a["last"], a["id"]), reverse=True)
            done = list(reversed(self._done))
            out_active = [_row(a) for a in active]
            out_done = [dict(_row(a), state=a["state"], ended=a["ended"]) for a in done]
        top = None
        if out_active:
            worst = min(out_active, key=lambda a: (SEVERITIES.index(a["severity"]), -a["last"]))
            top = {"count": len(out_active), "severity": worst["severity"], "text": worst["text"]}
        return {"active": out_active, "recent": out_done, "top": top}

    def active_keys(self) -> List[str]:
        with self._lock:
            return list(self._active)


def _row(a: dict) -> dict:
    return {"id": a["id"], "key": a["key"], "severity": a["severity"], "text": a["text"], "t": a["t"],
            "last": a["last"], "count": a["count"]}


class LinkWatch:
    """Status-page links -> alerts: a link that was up and is now down is a warning ("Olympus lost");
    when it comes back the warning clears and an info alert says so. Links that were never up (a
    source you don't use) never alert."""

    NAMES = {"DCS telemetry": "DCS telemetry", "DCS-BIOS": "DCS-BIOS", "Comms hook": "Comms hook",
             "DCS Olympus": "Olympus", "Ollama": "Ollama", "Enemy picture": "Enemy picture"}

    def __init__(self, alerts: Alerts):
        self.alerts = alerts
        self._was: Dict[str, bool] = {}

    def update(self, links: List[dict]) -> None:
        for link in links:
            short = next((v for k, v in self.NAMES.items() if str(link.get("name", "")).startswith(k)), None)
            ok = link.get("ok")
            if short is None or ok is None:
                continue
            was = self._was.get(short)
            self._was[short] = bool(ok)
            key = f"link:{short}"
            if ok:
                if self.alerts.clear(key):
                    self.alerts.raise_(f"linkup:{short}", "info", f"{short} back")
                continue
            if was:                                   # up before, down now
                text = "Enemy picture stale" if short == "Enemy picture" else f"{short} lost"
                self.alerts.raise_(key, "warning", text)

    def reset(self) -> None:
        self._was.clear()


ALERTS = Alerts()
