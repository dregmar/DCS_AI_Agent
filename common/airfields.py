"""
common/airfields.py - "nearest friendly field" / "nearest FARP" / "divert", for the crew and Overlord.

george_comms_hook.lua sends every airbase in the mission (airfields, FARPs and helipads, ships) with
its coalition, from the mission scripting state ("AIRBASES ..."), so this doesn't need Olympus.

    "nearest friendly airfield"  -> "Nearest friendly field, Kutaisi, 2 o'clock, steer 045, 23 miles, 9 minutes."
    "nearest FARP" / "nearest helipad"  -> only FARPs and helipads (and says so if there are none)
    "divert" / "where's the nearest base" / "Overlord, request vector to nearest friendly field"
Friendly = your coalition (Export.lua's CoalitionID). Neutral fields are named only when there's
no friendly one. Ships count as fields only when you ask for a ship / carrier / boat. Thread-safe.
"""
import math
import re
import threading
from typing import List, Optional

from common import nav

_ASK = re.compile(r"\b(?:nearest|closest|where(?:'s| is) the nearest|divert|diversion|alternate|vector to)\b")
_FIELD = re.compile(r"\b(?:airfields?|air ?fields?|airports?|air ?bases?|bases?|fields?|farps?|helipads?|"
                    r"pads?|landing (?:sites?|zones?)|places? to land|ships?|carriers?|boats?|divert|diversion)\b")
_FARP = re.compile(r"\b(?:farps?|helipads?|pads?)\b")
_SHIP = re.compile(r"\b(?:ships?|carriers?|boats?)\b")
COAL = {0: "neutral", 1: "red", 2: "blue"}


def wanted(text: str) -> bool:
    t = (text or "").lower()
    return bool(_ASK.search(t)) and bool(_FIELD.search(t)) or bool(re.search(r"\bdivert\b", t))


class Airfields:
    def __init__(self):
        self._lock = threading.Lock()
        self._bases: List[dict] = []          # {"name", "coal", "cat", "lat", "lon"}

    def on_airbases_message(self, text: str) -> None:
        bases = []
        for line in (text or "").split("\n")[1:]:
            p = line.split("\t")
            if len(p) < 5:
                continue
            try:
                bases.append({"name": p[0], "coal": int(p[1]), "cat": int(p[2]),
                              "lat": float(p[3]), "lon": float(p[4])})
            except ValueError:
                continue
        with self._lock:
            new = len(bases) != len(self._bases)
            self._bases = bases
        if new:
            print(f"[AIRFIELDS] {len(bases)} airbases known "
                  f"({sum(b['cat'] == 1 for b in bases)} FARPs / helipads, {sum(b['cat'] == 2 for b in bases)} ships).")

    def bases(self) -> List[dict]:
        with self._lock:
            return [dict(b) for b in self._bases]

    def nearest(self, snap: dict, farp: bool = False, ship: bool = False) -> Optional[tuple]:
        """(base, distance m, friendly?) or None."""
        me = snap.get("my_ll")
        if not me:
            return None
        mine = snap.get("coalition") or 2
        rows = [b for b in self.bases() if (b["cat"] == 2) == ship and (not farp or b["cat"] == 1)]

        def dist(b):
            dn = (b["lat"] - me[0]) * 111_320.0
            de = (b["lon"] - me[1]) * 111_320.0 * math.cos(math.radians(me[0]))
            return math.hypot(dn, de)
        for want in (mine, 0):                                  # friendly first, then neutral
            cands = [b for b in rows if b["coal"] == want]
            if cands:
                b = min(cands, key=dist)
                return b, dist(b), want == mine
        return None

    def report(self, snap: dict, base: dict, label: str) -> str:
        me = snap["my_ll"]
        dn = (base["lat"] - me[0]) * 111_320.0
        de = (base["lon"] - me[1]) * 111_320.0 * math.cos(math.radians(me[0]))
        rel = nav.relative((0.0, 0.0), dn, de, snap.get("hdg"))
        if rel["dist_m"] < 500:
            return f"{label}, {base['name']}, overhead."
        parts = [f"{label}, {base['name']}"]
        if rel["clock"]:
            parts.append(f"{rel['clock']} o'clock")
        parts.append(f"steer {rel['brg']:03.0f}")
        parts.append(nav.say_distance(rel["dist_m"]))
        ete = nav.ete_minutes(rel["dist_m"], nav.ground_speed_kt(snap.get("vel")))
        if ete is not None:
            parts.append(f"{ete:.0f} minutes" if ete >= 1.5 else "about a minute")
        return ", ".join(parts) + "."

    def handle(self, text: str, snap: dict) -> Optional[str]:
        if not wanted(text):
            return None
        t = (text or "").lower()
        if not self.bases():
            return "Unable, no airfield list yet. It comes from the comms hook."
        if not snap.get("my_ll"):
            return "Unable, no position."
        farp, ship = bool(_FARP.search(t)), bool(_SHIP.search(t))
        got = self.nearest(snap, farp=farp, ship=ship)
        if got is None and farp:
            got = self.nearest(snap)
            if got is None:
                return "No friendly fields known."
            base, _, friendly = got
            return "No FARPs known. " + self.report(snap, base, "Nearest friendly field" if friendly else
                                                     "Nearest neutral field")
        if got is None:
            return "No friendly fields known." if not ship else "No ships known."
        base, _, friendly = got
        kind = "FARP" if base["cat"] == 1 else "ship" if base["cat"] == 2 else "field"
        return self.report(snap, base, f"Nearest {'friendly' if friendly else 'neutral'} {kind}")


AIRFIELDS = Airfields()
