"""
huey/crew_chief.py
Crew-chief callouts for the UH-1H, from the Huey link (gauges, caution lamps, Crew Status
panel) and Export.lua (height, vertical speed, speed, nearby enemy units). Limits are from the
DCS UH-1H Flight Manual: rotor 300-339 RPM (low warning 300-310), engine low-RPM warning
6100-6300, EGT 610 continuous / 625 30-minute / 760 maximum, torque 50 PSI, N1 101.5 %.

Each callout fires once when its condition starts and re-arms only after the value is back
inside a margin, so a needle sitting on a limit doesn't repeat. Runs only while the Huey
profile is active and the link is live.
"""
import threading
import time
from typing import Callable, Dict, Optional

from core.state import clock_position
from huey.panel import SEAT_NAMES

LINK_FRESH_SEC = 3.0
TELEMETRY_FRESH_SEC = 3.0
SETTLE_SEC = 5.0              # ignore lamps for this long after the link comes up
LAMP_TEST_COUNT = 6           # this many lamps lighting at once = lamp test, not a failure

LAMP_SAY = {
    "fire": "Fire light! Engine fire!",
    "twenty minute fuel": "Twenty minutes of fuel.",
    "engine chip detector": "Engine chip light.",
    "chip detector": "Chip detector light.",
    "transmission oil pressure": "Transmission oil pressure light!",
    "engine oil pressure": "Engine oil pressure light!",
    "hydraulic pressure": "Hydraulic pressure light, controls will be heavy.",
}
STATUS_LAMPS = {"armed", "safe", "master caution", "low RPM"}   # not failures; low RPM is a limit callout


class CrewChief:
    def __init__(self, speak: Callable[[str], None], snapshot: Callable[[], dict],
                 active: Callable[[], bool], contact_range_m: float = 1500.0,
                 contact_gap_sec: float = 10.0, interval: float = 0.5):
        self.speak, self.snapshot, self.active = speak, snapshot, active
        self.contact_range_m = contact_range_m
        self.contact_gap_sec = contact_gap_sec
        self.interval = interval
        self._armed: Dict[str, bool] = {}
        self._lamps = set()
        self._link_since = 0.0
        self._panel: Dict[str, dict] = {}
        self._called_contacts = set()
        self._last_contact_call = 0.0
        self._n1_prev: Optional[float] = None
        self._running = False
        self._now = 0.0
        self._spoken_at: Dict[str, float] = {}

    # ------------------------------------------------------------ helpers
    def _edge(self, key: str, cond: bool, rearm: bool, text: str, quiet_sec: float = 0.0) -> None:
        """Speak text when cond becomes true; re-arm when `rearm` is true. quiet_sec: never repeat
        sooner than this, for values that hover around a limit (torque in fast cruise)."""
        if cond and not self._armed.get(key):
            self._armed[key] = True
            if self._now - self._spoken_at.get(key, -1e9) >= quiet_sec:
                self._spoken_at[key] = self._now
                self.speak(text)
        elif rearm:
            self._armed[key] = False

    # ------------------------------------------------------------ the check
    def check(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._now = now
        snap = self.snapshot()
        h = snap.get("huey") or {}
        if snap.get("huey_age", float("inf")) > LINK_FRESH_SEC or not h:
            self._link_since = 0.0
            return
        if not self._link_since:
            self._link_since = now
            self._lamps = set(h.get("lamps") or [])
            self._panel = h.get("panel") or {}
        settled = now - self._link_since >= SETTLE_SEC

        tel_ok = snap.get("telemetry_age", float("inf")) <= TELEMETRY_FRESH_SEC
        agl, spd, vs = snap.get("agl_ft", -1), snap.get("speed_kts", 0), snap.get("vs_fpm", 0)
        flying = tel_ok and (agl > 15 or spd > 30)

        rotor, eng = h.get("rotor_rpm"), h.get("engine_rpm")
        egt, tq, n1 = h.get("egt_c"), h.get("torque_psi"), h.get("n1_pct")
        running = (eng or 0) > 6000 or (n1 or 0) > 60

        # --- engine start sequence (manual 9.1.2: rotor turning at 15 % N1, release at 40 %).
        # The rotor tach barely moves at 15 % (a false "no rotor" in the first test flight), so the
        # rotor is judged at 25 %.
        if n1 is not None and not running and self._n1_prev is not None:
            if self._n1_prev < 25 <= n1:
                self.speak("Rotor's turning." if (rotor or 0) > 0.5
                           else "No rotor turning. Abort the start!")
            if self._n1_prev < 40 <= n1:
                self.speak("Forty percent. Release the starter.")
        self._n1_prev = n1

        # --- limits in flight
        if rotor is not None:
            self._edge("rotor_low", flying and rotor < 300, rotor > 310, "Rotor RPM low!")
            self._edge("rotor_high", rotor > 339, rotor < 330, "Rotor overspeed!")
        if eng is not None:
            self._edge("eng_low", flying and eng < 6100, eng > 6300, "Engine RPM low.")
        if egt is not None:
            # 625 is a running limit; a start may briefly go higher (only 760 counts then)
            self._edge("egt_625", running and egt > 625, egt < 610, f"EGT {egt:.0f}.", quiet_sec=60)
            self._edge("egt_760", egt > 760, egt < 740, "EGT over limit!")
        if tq is not None:
            self._edge("torque", tq > 50, tq < 45, "Torque fifty, over limit.", quiet_sec=60)
        if n1 is not None:
            self._edge("n1", n1 > 101.5, n1 < 100.5, "N1 over limit.")

        # --- settling with power: slow, low and sinking fast (manual 9.5)
        self._edge("sink", flying and agl > 30 and spd < 30 and vs < -800,
                   vs > -400 or spd > 40, "Sink rate! You're settling.")

        # --- caution lamps (rising edge only)
        lamps = set(h.get("lamps") or [])
        new = lamps - self._lamps
        self._lamps = lamps
        # (not before the engine runs: the start checklist tests the fire and caution lights)
        if new and settled and len(new) < LAMP_TEST_COUNT and (running or flying):
            for name in sorted(new):
                if name not in STATUS_LAMPS:
                    self.speak(LAMP_SAY.get(name, f"Caution, {name}."))

        # --- gunners (only while the Crew Status panel is readable)
        panel = h.get("panel") or {}
        for seat, row in panel.items():
            if seat not in ("left", "right", "copilot"):
                continue
            prev = self._panel.get(seat) or {}
            name = SEAT_NAMES[seat].capitalize()
            if row.get("state") == "DEAD" and prev.get("state") != "DEAD" and prev:
                self.speak(f"{name} is hit!")
            a, pa = row.get("ammo"), prev.get("ammo")
            if a is not None and pa is not None:
                if a == 0 < pa:
                    self.speak(f"{name}, Winchester.")
                elif a <= 20 < pa:
                    self.speak(f"{name}, ammo low.")
        if panel:
            self._panel = panel

        # --- enemy contacts close in
        if tel_ok and self.contact_range_m:
            self._contacts(snap, now)

    def _contacts(self, snap: dict, now: float) -> None:
        if now - self._last_contact_call < self.contact_gap_sec:
            return
        for c in snap.get("contacts") or []:
            cid = c.get("id")
            if cid in self._called_contacts or c.get("dist_m") is None:
                continue
            if c["dist_m"] > self.contact_range_m:
                continue
            self._called_contacts.add(cid)
            self._last_contact_call = now
            clock = clock_position(c.get("brg"), snap.get("hdg"))
            where = f"{clock} o'clock, " if clock else ""
            dist = c["dist_m"]
            rng = f"{dist / 1000:.1f} kilometers" if dist >= 1000 else f"{round(dist, -1):.0f} meters"
            self.speak(f"Contact, {c.get('type', 'unknown')}, {where}{rng}.")
            return

    # ------------------------------------------------------------ thread
    def _loop(self):
        while self._running:
            time.sleep(self.interval)
            try:
                if self.active():
                    self.check()
                else:
                    self._link_since = 0.0
            except Exception as e:
                print(f"[CREW CHIEF ERROR] {type(e).__name__}: {e}")

    def start(self) -> "CrewChief":
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="huey-crew-chief").start()
        print("[CREW CHIEF] Huey callouts ready (active while you fly the UH-1H).")
        return self

    def stop(self):
        self._running = False
