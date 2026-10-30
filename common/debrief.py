"""
common/debrief.py - what happened this mission, for "debrief" / "mission summary".

The program records what it knows for certain:
  * kills George called (engagement monitor), and missiles / gun bursts fired;
  * kills by the Outcasts (engage at will) and by Overlord's flights (SEAD, CAS, CAP, escort);
  * flights launched, and losses (an Outcast or an Overlord flight that's down);
  * fuel at the start of the mission and now.

"Debrief" / "mission summary" / "how did we do": George (or the Huey crew) speaks a short summary,
and the full list is written to logs/debrief_<date>_<time>.txt. A new mission (a different briefing
from george_comms_hook.lua) writes the old one's file first, then starts afresh, and so does closing
the program. Thread-safe: events come from the voice, monitor and mission threads.
"""
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from typing import Callable, List, Optional

try:
    import config
    LOG_DIR = os.path.join(config.BASE_DIR, "logs")
except Exception:
    LOG_DIR = "logs"

_ASK = re.compile(r"\b(?:debrief|de brief|mission summary|mission report|after action|how did we do|"
                  r"kill count|score ?card|battle damage)\b")


def wanted(text: str) -> bool:
    return bool(_ASK.search((text or "").lower()))


class Debrief:
    def __init__(self, log_dir: str = LOG_DIR):
        self._lock = threading.Lock()
        self.log_dir = log_dir
        self._reset_locked("")

    def _reset_locked(self, mission: str):
        self.mission = mission
        self.started = time.time()
        self.events: List[tuple] = []          # (time, kind, who, what)
        self.kills: "OrderedDict[str, List[str]]" = OrderedDict()
        self.losses: List[str] = []
        self.launched: List[str] = []
        self.shots = Counter()
        self.fuel_start: Optional[str] = None
        self.fuel_now: Optional[str] = None

    # ------------------------------------------------------------ recording
    def _event(self, kind: str, who: str, what: str = ""):
        self.events.append((time.time(), kind, who, what))

    def kill(self, who: str, what: str) -> None:
        with self._lock:
            self.kills.setdefault(who, []).append(what or "target")
            self._event("kill", who, what)

    def loss(self, who: str) -> None:
        with self._lock:
            if who not in self.losses:
                self.losses.append(who)
            self._event("loss", who)

    def launch(self, who: str, what: str = "") -> None:
        with self._lock:
            self.launched.append(who)
            self._event("launch", who, what)

    def shot(self, weapon: str, n: int = 1) -> None:
        with self._lock:
            self.shots[weapon] += n

    def fuel(self, said: str) -> None:
        """A fuel reading as spoken ('Fuel 2000 pounds.'): the first one is the start."""
        with self._lock:
            if self.fuel_start is None:
                self.fuel_start = said
            self.fuel_now = said

    # ------------------------------------------------------------ the summary
    def summary(self) -> str:
        with self._lock:
            parts = ["Mission summary."]
            if self.kills:
                total = sum(len(v) for v in self.kills.values())
                each = []
                for who, what in self.kills.items():
                    c = Counter(what)
                    top = ", ".join(f"{n} {k}" if n > 1 else k for k, n in c.most_common(2))
                    more = len(what) - sum(n for _, n in c.most_common(2))
                    each.append(f"{who} {len(what)}" + (f", {top}" if top else "") + (f" and {more} more" if more else ""))
                parts.append(f"{total} kill{'s' if total != 1 else ''}: " + "; ".join(each) + ".")
            else:
                parts.append("No kills recorded.")
            if self.losses:
                parts.append("Lost: " + ", ".join(self.losses) + ".")
            shots = []
            if self.shots.get("missile"):
                shots.append(f"{self.shots['missile']} missile{'s' if self.shots['missile'] != 1 else ''}")
            if self.shots.get("gun"):
                shots.append(f"{self.shots['gun']} gun burst{'s' if self.shots['gun'] != 1 else ''}")
            if shots:
                parts.append("Fired " + " and ".join(shots) + ".")
            if self.launched:
                parts.append(f"Overlord sent {len(self.launched)} flight{'s' if len(self.launched) != 1 else ''}.")
            if self.fuel_now:
                parts.append(self.fuel_now)
            return " ".join(parts)

    def write(self, reason: str = "") -> Optional[str]:
        """Writes logs/debrief_*.txt (only if anything happened). Returns the path."""
        with self._lock:
            if not (self.events or self.shots or self.fuel_now):
                return None
            events = list(self.events)
            started = self.started
            mission = self.mission
        text = self.summary()
        path = os.path.join(self.log_dir, time.strftime("debrief_%Y%m%d_%H%M%S.txt"))
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Mission: {mission or '?'}   started {time.strftime('%H:%M', time.localtime(started))}"
                        f"   {reason}\n\n{text}\n\n")
                for t, kind, who, what in events:
                    f.write(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {kind:6s} {who}"
                            + (f": {what}" if what else "") + "\n")
            print(f"[DEBRIEF] Saved {path}")
            return path
        except OSError as e:
            print(f"[DEBRIEF] Can't save: {e}")
            return None

    def new_mission(self, mission: str) -> None:
        """A different mission is loaded: save the last one, start again."""
        self.write("(next mission loaded)")
        with self._lock:
            self._reset_locked(mission)

    def handle(self, text: str) -> Optional[str]:
        if not wanted(text):
            return None
        said = self.summary()
        self.write("(asked for)")
        return said


DEBRIEF = Debrief()
