"""
common/fuel_watch.py - joker and bingo fuel for jets (common/aircraft.py: fuel_calls).

    bingo = fuel to fly home at cruise (distance / cruise speed x burn) + the landing reserve
    joker = bingo + the profile's margin

Home is the friendly airfield or ship you started from (looked up again each time, so a carrier's
current position is used), else the first route point, else the nearest friendly field. Overlord
calls each once per sortie ("Overlord, joker fuel, 6,200 pounds. Home plate Kutaisi, 85 miles,
steer 210."); taking on fuel (tanker, rearm) re-arms them. "Say bingo" / "what's my joker" answers
on request. All numbers per aircraft in common/aircraft.py (config.AIRCRAFT_OVERRIDES).
Runs on its own daemon thread; the speak function must not block.
"""
import math
import re
import threading
import time
from typing import Callable, Optional

from common import aircraft, geo, nav
from core.fuel import current_lbs

M_PER_NM = 1852.0
START_MATCH_M = 6000.0              # a field / ship this close to where you were first seen is home
REARM_LBS = 800.0                   # this much more fuel than at the call: refuelled, calls again
# Any mention of bingo / joker, and "fuel to home" / "can we make it back" (28 Oct: "See you bingo"
# went to the route-based answer, whose home was the carrier's start point 2.8 miles away).
_ASK = re.compile(r"\b(?:bingo|joker)\b|\bfuel (?:to|for|needed for|to get to) (?:home|base|mother|the boat|the ship)\b|"
                  r"\bcan we make (?:it )?(?:home|back)\b|\benough fuel\b", re.I)


def _dist_brg(a, b):
    n = (b[0] - a[0]) * 111_320.0
    e = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(n, e), math.degrees(math.atan2(e, n)) % 360


def _lbs(v: float) -> str:
    return f"{int(round(v, -2)):,}"


class FuelWatch:
    POLL_S = 10.0

    def __init__(self, snapshot: Callable[[], dict], speak: Callable[[str], None], airfields=None):
        self.snapshot, self.speak = snapshot, speak
        if airfields is None:
            from common.airfields import AIRFIELDS
            airfields = AIRFIELDS
        self.airfields = airfields
        self._lock = threading.Lock()
        self._home = None                   # name of the base / ship you started at
        self._start_ll = None
        self._called = {}                   # "joker" / "bingo" -> fuel (lbs) when called
        self._aircraft = None
        self._stop = threading.Event()

    def reset(self) -> None:
        """A new mission: where you start now is home again."""
        with self._lock:
            self._start_ll, self._home, self._called = None, None, {}

    # ------------------------------------------------------------ home
    def _note_start(self, snap: dict) -> None:
        """First fix of each aircraft: remember where you started and which base that was."""
        me = snap.get("my_ll")
        if not me:
            return
        with self._lock:
            if self._aircraft != snap.get("aircraft"):
                self._aircraft, self._start_ll, self._home, self._called = snap.get("aircraft"), None, None, {}
            if self._start_ll is not None:
                return
            self._start_ll = (me[0], me[1])
        mine = snap.get("coalition") or 2
        best = None
        for b in self.airfields.bases():
            if b["coal"] != mine:
                continue
            d, _ = _dist_brg(self._start_ll, (b["lat"], b["lon"]))
            if d <= START_MATCH_M and (best is None or d < best[1]):
                best = (b["name"], d)
        if best:
            with self._lock:
                self._home = best[0]
            print(f"[FUEL] Home plate: {best[0]}")

    def home(self, snap: dict):
        """(name, (lat, lon)) or None."""
        with self._lock:
            name = self._home
        if name:
            b = next((x for x in self.airfields.bases() if x["name"] == name), None)
            if b:
                return name, (b["lat"], b["lon"])
        p, label = nav.home_point(snap)
        if p is not None:
            ll = geo.FIT.to_ll(float(p[1]), float(p[2]))
            if ll:
                return "home", ll
        got = self.airfields.nearest(snap)
        if got and got[2]:
            return got[0]["name"], (got[0]["lat"], got[0]["lon"])
        return None

    # ------------------------------------------------------------ numbers
    def numbers(self, snap: dict) -> Optional[dict]:
        prof = aircraft.profile(snap)
        fuel = current_lbs(snap)
        me = snap.get("my_ll")
        home = self.home(snap) if me else None
        if fuel is None or not home or not prof.burn_lbs_hr or not prof.cruise_kts:
            return None
        d, brg = _dist_brg((me[0], me[1]), home[1])
        need = d / M_PER_NM / prof.cruise_kts * prof.burn_lbs_hr
        bingo = need + prof.reserve_lbs
        return {"fuel": fuel, "bingo": bingo, "joker": bingo + prof.joker_margin_lbs,
                "home": home[0], "dist_m": d, "brg": brg}

    def _where(self, n: dict) -> str:
        return (f"Home plate {n['home'] if n['home'] != 'home' else ''}".rstrip() + ", "
                f"{max(1, round(n['dist_m'] / M_PER_NM))} miles, steer {n['brg']:03.0f}.")

    # ------------------------------------------------------------ calls
    def check(self) -> Optional[str]:
        """One look; what was said (or None)."""
        snap = self.snapshot()
        if not aircraft.profile(snap).fuel_calls or snap.get("telemetry_age", 0.0) > 5.0:
            return None
        self._note_start(snap)
        if (snap.get("speed_kts") or 0.0) < 80:              # on the ground / deck: no calls
            return None
        n = self.numbers(snap)
        if n is None:
            return None
        with self._lock:
            for k, v in list(self._called.items()):        # refuelled: call again later
                if n["fuel"] > v + REARM_LBS:
                    del self._called[k]
            stage = ("bingo" if n["fuel"] <= n["bingo"] and "bingo" not in self._called else
                     "joker" if n["fuel"] <= n["joker"] and "joker" not in self._called and "bingo" not in self._called
                     else None)
            if stage:
                self._called[stage] = n["fuel"]
                if stage == "bingo":
                    self._called["joker"] = n["fuel"]
        if not stage:
            return None
        said = (f"Overlord, {stage} fuel, {_lbs(n['fuel'])} pounds. {self._where(n)}"
                + (" Recommend RTB." if stage == "bingo" else ""))
        print(f"[FUEL] {said}")
        self.speak(said)
        return said

    def wanted(self, text: str) -> bool:
        return bool(_ASK.search(text or ""))

    def answer(self, snap: dict) -> str:
        n = self.numbers(snap)
        if n is None:
            return "Unable, no fuel reading or no home plate."
        return (f"Fuel {_lbs(n['fuel'])}. Joker {_lbs(n['joker'])}, bingo {_lbs(n['bingo'])}. {self._where(n)}")

    # ------------------------------------------------------------ thread
    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True, name="fuel-watch")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.POLL_S):
            try:
                self.check()
            except Exception as e:
                print(f"[FUEL] watch: {type(e).__name__}: {e}")
