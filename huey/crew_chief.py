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
from huey.talkdown import TalkDown
from common import nav
from intelligence.threat_engine import threat_weight

LINK_FRESH_SEC = 3.0
TELEMETRY_FRESH_SEC = 3.0
SETTLE_SEC = 5.0              # ignore lamps for this long after the link comes up
LAMP_TEST_COUNT = 6           # this many lamps lighting at once = lamp test, not a failure
FLYING_STABLE_SEC = 3.0       # limits need this long "flying": the first packets after a mission
                              # loads can look like flight ("Rotor RPM low!" while cold on the ground)
BINGO_MIN_HOME_M = 1500.0     # no bingo call when home is right here
# Contacts: never more than CONTACT_BURST calls a minute. Past that, one "multiple contacts" and
# quiet, except air defence (always called). Trucks and buildings aren't called one by one.
CONTACT_BURST = 3
CONTACT_WINDOW_SEC = 60.0
CONTACT_ALWAYS_WEIGHT = 90    # AAA, MANPADS, SAM (intelligence/threat_engine.py weights)
CONTACT_MIN_WEIGHT = 30       # below: soft vehicles, buildings

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
                 contact_gap_sec: float = 10.0, interval: float = 0.25, talkdown: bool = True,
                 fuel_burn_lbs_hr: float = 555.0, reserve_lbs: float = 185.0):
        self.speak, self.snapshot, self.active = speak, snapshot, active
        self.talkdown = TalkDown(speak) if talkdown else None
        self.talkdown_on: Callable[[], bool] = lambda: True     # HUEY_TALKDOWN, a live setting (main.py)
        self.fuel_burn, self.reserve = fuel_burn_lbs_hr, reserve_lbs
        self._bingo_said = False
        self.contact_range_m = contact_range_m
        self.contact_gap_sec = contact_gap_sec
        self.interval = interval
        self._armed: Dict[str, bool] = {}
        self._lamps = set()
        self._link_since = 0.0
        self._panel: Dict[str, dict] = {}
        self._called_contacts = set()
        self._last_contact_call = 0.0
        self._contact_calls: list = []           # times of recent contact calls
        self._flying_since: Optional[float] = None
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
        flying_now = tel_ok and (agl > 15 or spd > 30)
        if not flying_now:
            self._flying_since = None
        elif self._flying_since is None:
            self._flying_since = now
        flying = (flying_now and settled and self._flying_since is not None
                  and now - self._flying_since >= FLYING_STABLE_SEC)

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
            # (only with the engine governed: EGT peaks above 625 for a while after every start)
            governed = (eng or 0) > 6000
            self._edge("egt_625", governed and egt > 625, egt < 610, f"EGT {egt:.0f}.", quiet_sec=60)
            self._edge("egt_760", egt > 760, egt < 740, "EGT over limit!")
        if tq is not None:
            # in flight only: the needle swings past 50 for a moment on the ground during run-up
            self._edge("torque", flying and tq > 50, tq < 45, "Torque fifty, over limit.", quiet_sec=60)
        if n1 is not None:
            self._edge("n1", n1 > 101.5, n1 < 100.5, "N1 over limit.")

        # --- settling with power: slow, low and sinking fast (manual 9.5)
        self._edge("sink", flying and agl > 30 and spd < 30 and vs < -800,
                   vs > -400 or spd > 40, "Sink rate! You're settling.")

        # --- talking you down / onto a load
        if self.talkdown and tel_ok and self.talkdown_on():
            self.talkdown.check(snap, now)

        # --- bingo: flying home from here would land you under the reserve
        fuel = h.get("fuel_lbs")
        if tel_ok and flying and not self._bingo_said and fuel and fuel > 0:
            hp, _ = nav.home_point(snap)
            home = nav.relative(snap["pos"], hp[1], hp[2], snap.get("hdg")) if hp and snap.get("pos") else None
            if home and home["dist_m"] >= BINGO_MIN_HOME_M and nav.bingo(snap, fuel, self.fuel_burn, self.reserve):
                self._bingo_said = True
                self.speak(f"Bingo fuel. Home is {nav.say_distance(home['dist_m'])}, steer {home['brg']:03.0f}.")

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
        new = [c for c in snap.get("contacts") or []
               if c.get("id") not in self._called_contacts and c.get("dist_m") is not None
               and c["dist_m"] <= self.contact_range_m]
        if not new:
            return
        for c in new:
            c["_w"] = threat_weight(str(c.get("type") or ""))
        danger = [c for c in new if c["_w"] >= CONTACT_ALWAYS_WEIGHT]
        if danger:                                  # air defence: always, nearest first
            if now - self._last_contact_call >= 3.0:
                self._call_contact(min(danger, key=lambda c: c["dist_m"]), snap, now)
            return
        if now - self._last_contact_call < self.contact_gap_sec:
            return
        worth = [c for c in new if c["_w"] >= CONTACT_MIN_WEIGHT]
        self._contact_calls = [t for t in self._contact_calls if now - t < CONTACT_WINDOW_SEC]
        if len(self._contact_calls) >= CONTACT_BURST:
            if len(new) >= 2:                       # busy area: one summary, then quiet a while
                for c in new:
                    self._called_contacts.add(c.get("id"))
                clocks = sorted({clock_position(c.get("brg"), snap.get("hdg")) for c in new} - {None})
                where = f", {clocks[0]} to {clocks[-1]} o'clock" if len(clocks) > 1 else (
                    f", {clocks[0]} o'clock" if clocks else "")
                self._last_contact_call = now
                self._contact_calls = [now] * CONTACT_BURST     # stay in summary mode for a minute
                self.speak(f"Multiple contacts, {len(new)} more{where}.")
            return
        if worth:
            self._call_contact(min(worth, key=lambda c: c["dist_m"]), snap, now)
        else:
            for c in new:                           # trucks / buildings: counted, not called
                self._called_contacts.add(c.get("id"))

    def _call_contact(self, c: dict, snap: dict, now: float) -> None:
        self._called_contacts.add(c.get("id"))
        self._last_contact_call = now
        self._contact_calls.append(now)
        clock = clock_position(c.get("brg"), snap.get("hdg"))
        where = f"{clock} o'clock, " if clock else ""
        dist = c["dist_m"]
        rng = f"{dist / 1000:.1f} kilometers" if dist >= 1000 else f"{round(dist, -1):.0f} meters"
        name = str(c.get("type") or "unknown").replace("_", " ")
        self.speak(f"Contact, {name}, {where}{rng}.")

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
