"""
huey/crew.py
The Huey's AI crew through the game's own crew commands (sent by huey/link.py to
george_huey_export.lua):
  * door gunners and co-pilot: ROE (HOLD -> RET.FIRE -> FREE FIRE, cycling) and burst
    (SHORT / LONG, toggling), closed loop: every press is confirmed on the Crew Status panel and
    George keeps pressing until the panel shows what you asked for;
  * the Crew Status panel itself (shown automatically while George needs to read it);
  * the game autopilot (on/off, ATTITUDE HOLD, LEVEL FLIGHT, ORBIT). The game shows no autopilot
    state anywhere George can read, so those are sent but not confirmed.
Blocking calls (up to a few seconds); run them on the Huey worker thread.
"""
import threading
import time
from typing import Callable, Dict, List, Tuple

from huey.link import (CMD_AP_ATTITUDE, CMD_AP_LEVEL, CMD_AP_ORBIT, CMD_AUTOPILOT, CMD_PANEL,
                       CMD_SEAT, VALUE_BURST, VALUE_ROE)
from huey.panel import SEAT_NAMES, SPOKEN_ROE

LINK_FRESH_SEC = 3.0
ROE_CYCLE = ("HOLD", "RETURN", "FREE")
AUTOPILOT = {"TOGGLE": (CMD_AUTOPILOT, "Autopilot switch pressed."),
             "ATTITUDE": (CMD_AP_ATTITUDE, "Autopilot, attitude hold."),
             "LEVEL": (CMD_AP_LEVEL, "Autopilot, level flight."),
             "ORBIT": (CMD_AP_ORBIT, "Autopilot, orbit.")}


class HueyCrew:
    def __init__(self, link, source: Callable[[], Tuple[dict, float]], step_sec: float = 1.5,
                 panel_idle_hide_s: float = 8.0):
        """link: huey.link.CommandLink. source: state_store.huey_snapshot."""
        self.link, self.source = link, source
        self.step_sec = step_sec
        self.panel_idle_hide_s = panel_idle_hide_s
        self._opened_panel = False
        self._hide_timer = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ reading
    def live(self) -> bool:
        _, rx = self.source()
        return bool(rx) and time.monotonic() - rx <= LINK_FRESH_SEC

    def panel(self) -> Dict[str, dict]:
        data, _ = self.source()
        return data.get("panel") or {}

    def _fresh_panel(self, after: float) -> Dict[str, dict]:
        """Panel from a packet received after `after` (time.monotonic())."""
        data, rx = self.source()
        return (data.get("panel") or {}) if rx > after else {}

    def _wait(self, check: Callable[[Dict[str, dict]], bool], timeout: float) -> Dict[str, dict]:
        t0 = time.monotonic()
        end = t0 + timeout
        while time.monotonic() < end:
            p = self._fresh_panel(t0)
            if p and check(p):
                return p
            time.sleep(0.05)
        return {}

    # ------------------------------------------------------------ panel
    def _ensure_panel(self) -> bool:
        if self.panel():
            return True
        # Make sure with a fresh packet (the pilot may have just opened it) before toggling.
        if self._wait(lambda p: True, 0.5):
            return True
        self.link.send(CMD_PANEL)
        if self._wait(lambda p: True, self.step_sec + 0.5):
            self._opened_panel = True
            return True
        self.link.send(CMD_PANEL)            # put it back the way it was
        return False

    def _schedule_hide(self):
        if not self._opened_panel:
            return
        if self._hide_timer:
            self._hide_timer.cancel()
        self._hide_timer = threading.Timer(self.panel_idle_hide_s, self._auto_hide)
        self._hide_timer.daemon = True
        self._hide_timer.start()

    def _auto_hide(self):
        with self._lock:
            if self._opened_panel and self.panel():
                self.link.send(CMD_PANEL)
            self._opened_panel = False

    def show_panel(self, show: bool) -> Tuple[bool, str]:
        with self._lock:
            if not self.live():
                return False, "No crew link."
            if self._hide_timer:
                self._hide_timer.cancel()
            self._opened_panel = False
            if bool(self.panel()) == show:
                return True, "Crew panel up." if show else "Crew panel down."
            self.link.send(CMD_PANEL)
            if show:
                ok = bool(self._wait(lambda p: True, self.step_sec + 0.5))
                return ok, "Crew panel up." if ok else "Can't read the crew panel."
            return True, "Crew panel down."

    # ------------------------------------------------------------ ROE / burst
    @staticmethod
    def _unavailable(seat: str, row: dict) -> str:
        name = SEAT_NAMES[seat].capitalize()
        state = row.get("state")
        if state == "PLAYER":
            return "That's your seat."
        if state == "DEAD":
            return f"{name} is down."
        if state == "NET":
            return f"{name} is a player."
        return f"{name} not available."

    def _set(self, seat: str, field: str, want: str, value: float, max_presses: int) -> Tuple[bool, str]:
        name = SEAT_NAMES[seat]
        row = self.panel().get(seat)
        if row is None:
            return False, f"Can't see the {name}."
        if row.get("roe") is None:
            return False, self._unavailable(seat, row)
        if row.get(field) == want:
            return True, ""
        for _ in range(max_presses):
            before = row.get(field)
            self.link.send(CMD_SEAT[seat], value)
            p = self._wait(lambda q: q.get(seat, {}).get(field) != before, self.step_sec)
            if not p:
                return False, f"{name.capitalize()} not responding."
            row = p.get(seat, {})
            if row.get(field) == want:
                return True, ""
        return False, f"{name.capitalize()} stuck on {row.get(field)}."

    def set_roe(self, seats: List[str], want: str) -> Tuple[bool, str]:
        return self._set_many(seats, "roe", want, VALUE_ROE, len(ROE_CYCLE),
                              SPOKEN_ROE.get(want, want.lower()))

    def set_burst(self, seats: List[str], want: str) -> Tuple[bool, str]:
        return self._set_many(seats, "burst", want, VALUE_BURST, 2, f"{want.lower()} bursts")

    def _set_many(self, seats, field, want, value, max_presses, words) -> Tuple[bool, str]:
        with self._lock:
            if not self.live():
                return False, "No crew link. Check george_huey_export.lua."
            if not self._ensure_panel():
                return False, "Can't read the crew panel."
            try:
                done, problems = [], []
                for seat in seats:
                    ok, why = self._set(seat, field, want, value, max_presses)
                    (done if ok else problems).append(seat if ok else why)
            finally:
                self._schedule_hide()
            parts = []
            if done:
                who = ("Gunners" if set(done) == {"left", "right"} else
                       " and ".join(SEAT_NAMES[s] for s in done).capitalize())
                parts.append(f"{who}, {words}.")
            parts.extend(problems)
            return not problems, " ".join(parts)

    # ------------------------------------------------------------ report
    def report(self) -> Tuple[bool, str]:
        with self._lock:
            if not self.live():
                return False, "No crew link."
            if not self._ensure_panel():
                return False, "Can't read the crew panel."
            self._schedule_hide()
            return True, describe_crew(self.panel())

    # ------------------------------------------------------------ autopilot
    def autopilot(self, mode: str) -> Tuple[bool, str]:
        cmd, sentence = AUTOPILOT[mode]
        if not self.live():
            return False, "No crew link."
        self.link.send(cmd)
        return True, sentence


def describe_crew(panel: Dict[str, dict]) -> str:
    parts = []
    for seat in ("left", "right", "copilot"):
        row = panel.get(seat)
        if not row:
            continue
        name = SEAT_NAMES[seat].capitalize()
        if row.get("roe") is None:
            if row.get("state") == "DEAD":
                parts.append(f"{name} is down.")
            continue
        bits = [SPOKEN_ROE.get(row["roe"], "")]
        if row.get("ammo") is not None:
            bits.append(f"{row['ammo']} percent")
        if row.get("burst"):
            bits.append(f"{row['burst'].lower()} bursts")
        parts.append(f"{name}, " + ", ".join(b for b in bits if b) + ".")
    return " ".join(parts) or "No gunners aboard."
