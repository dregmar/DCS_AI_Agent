"""
common/jtac.py - Overlord as JTAC (feature 9): a target talk-on ready for the CDU / UFC.

    "Overlord, request target"            the most dangerous enemy ground unit near you (big radar SAMs
    "... request target north of me"      are left out and called as threats instead), in a direction,
    "... request target, armour"          or of a kind
    "Overlord, next target"               another one
    "Overlord, say again target"          repeats it
    "Overlord, mark target (red smoke)"   smoke on it through Olympus
    "Overlord, BDA"                       destroyed / still active / can't assess

    -> "Overlord, target ZSU-23-4 Shilka. Grid 40 Romeo, Delta Quebec, 1 2 3 4 5, 6 7 8 9 0.
        Elevation 1,240 feet. 12 miles north east of you. Threats: SA-8, 2 miles north of the target."

Coordinates per aircraft (common/aircraft.py target_coords): MGRS, 10 digits for the A-10C's CDU; lat/long
in degrees and decimal minutes (3 decimals) for the Hornet's UFC. Elevation is the target's own
altitude from Export.lua (a ground unit sits on the ground). The current target is also "my target"
for "Outcast 2, attack / lase my target" and "Overlord, CAS on my target" (main.py).
When the target is destroyed Overlord says so on his own ("good effect").
Thread-safe: orders come on the voice thread, tick() on the profile-watch thread.
"""
import re
import threading
from typing import Callable, List, Optional

from common import aircraft
from common.overlord import (dist_brg, compass, say_type, ground_near, radars_near, is_big_sam,
                             parse_direction, target_state)
from common.position import to_mgrs, NATO
from core.state import stale_reason

FT_PER_M = 3.28084
THREAT_RADIUS_M = 10_000            # air defence this close to the target is called as a threat
DIR_CONE_DEG = 35.0

_REQUEST = re.compile(r"\b(?:request(?:ing)?|give me|need|say|send)\s+(?:a\s+|the\s+|me\s+)?(?:new\s+)?"
                      r"(?:target|tasking|task)\b|\b(?:any|got)\s+(?:a\s+)?targets?\b|^\s*target\s*\??$", re.I)
_NEXT = re.compile(r"\b(?:next|another|new|different)\s+target\b", re.I)
_REPEAT = re.compile(r"\b(?:say again|repeat)\b.*\b(?:target|grid|coordinates|position)\b|"
                     r"\b(?:say|repeat)\s+(?:the\s+)?(?:grid|coordinates)\b", re.I)
# 28 Oct: "Overlord, smoke on my target" went to the router, which made it a SEAD request
_MARK = re.compile(r"\bmark\b.*\btarget\b|\bmark (?:it|with smoke)\b|"
                   r"\bsmoke (?:on )?(?:the |my |our |that )?target\b|\b(?:request(?:ing)?|need) (?:\w+ )?smoke\b", re.I)
_BDA = re.compile(r"\bbda\b|\bb d a\b|\bbattle damage\b|\bdamage assessment\b|\bis (?:it|the target) (?:dead|destroyed)\b",
                  re.I)
_COLORS = ("red", "green", "white", "orange", "blue")


def _say_digits(s: str) -> str:
    return " ".join(s)


def say_grid(lat: float, lon: float, digits: int) -> str:
    gzd, sq, e, n = to_mgrs(lat, lon, digits)
    return f"Grid {gzd[:-1]} {NATO[gzd[-1]]}, {NATO[sq[0]]} {NATO[sq[1]]}, {_say_digits(e)}, {_say_digits(n)}."


def ddm(v: float, pos: str, neg: str, decimals: int = 3) -> str:
    d = abs(v)
    deg = int(d)
    mins = round((d - deg) * 60, decimals)
    if mins >= 60:
        deg, mins = deg + 1, 0.0
    return f"{pos if v >= 0 else neg} {deg} degrees {mins:0{3 + decimals}.{decimals}f}"


def say_ddm(lat: float, lon: float) -> str:
    """'North 25 degrees 36.452, East 56 degrees 48.123.' (Hornet UFC precise entry)."""
    return f"{ddm(lat, 'North', 'South')}, {ddm(lon, 'East', 'West')}."


def text_coords(lat: float, lon: float) -> str:
    """For the log / kneeboard: both forms."""
    gzd, sq, e, n = to_mgrs(lat, lon, 5)
    return f"{gzd} {sq} {e} {n} | {ddm(lat, 'N', 'S')}' {ddm(lon, 'E', 'W')}'"


class Jtac:
    def __init__(self, snapshot: Callable[[], dict], oly=None, speak: Optional[Callable[[str], None]] = None):
        """oly: common.olympus.Olympus (marking), or None. speak: Overlord's unprompted voice."""
        self.snapshot, self.oly, self.speak = snapshot, oly, speak
        self._lock = threading.Lock()
        self.current: Optional[dict] = None
        self._given: List[int] = []                 # ids already given this sortie ("next target")
        self._reported_dead = None

    # ------------------------------------------------------------ what was said
    def wanted(self, rest: str) -> bool:
        t = rest or ""
        return bool(_REQUEST.search(t) or _NEXT.search(t) or _BDA.search(t) or _MARK.search(t)
                    or (_REPEAT.search(t) and self.current is not None))

    def handle(self, rest: str) -> Optional[str]:
        """What Overlord says (the text after "Overlord,"), or None if it isn't a JTAC request."""
        t = rest or ""
        if _BDA.search(t):
            return self.bda()
        if _MARK.search(t):
            return self.mark(t)
        if _REPEAT.search(t) and self.current is not None:
            return self.describe(self.current, self.snapshot())
        if _NEXT.search(t):
            return self.request(t, skip_given=True)
        if _REQUEST.search(t):
            return self.request(t)
        return None

    # ------------------------------------------------------------ choosing
    def candidates(self, snap: dict, text: str = "") -> List[dict]:
        me = snap.get("my_ll")
        if not me:
            return []
        prof = aircraft.profile(snap)
        radius = 37_000 if prof.kind == "jet" else 12_000
        cands = ground_near(snap, (me[0], me[1]), radius)       # big radar SAMs are left out
        alts = {int(g[1]): float(g[4]) for g in snap.get("enemy_ground") or [] if len(g) >= 5}
        for c in cands:
            c["alt"] = alts.get(c["id"])
        d = parse_direction(text.lower())
        if d is not None:
            cands = [c for c in cands
                     if abs((dist_brg((me[0], me[1]), (c["lat"], c["lon"]))[1] - d[0] + 180) % 360 - 180) <= DIR_CONE_DEG]
        from common import targets
        kind = targets.wanted_kind(text)
        if kind is not None:
            from common.unit_types import TYPES
            cands = [c for c in cands if re.search(kind[1], c["type"].lower()) or TYPES.matches_spoken(c["type"], text.lower())]
        return cands                                             # most dangerous first, then nearest

    def request(self, text: str = "", skip_given: bool = False) -> str:
        snap = self.snapshot()
        if not snap.get("my_ll"):
            return "Overlord, unable, I don't have your position."
        why = stale_reason(snap)                 # no target from a frozen picture (audit step 3)
        if why:
            return f"Overlord, unable, {why}. Say again in a moment."
        cands = self.candidates(snap, text)
        with self._lock:
            given = set(self._given) | ({self.current["id"]} if skip_given and self.current else set())
        if skip_given:
            cands = [c for c in cands if c["id"] not in given]
        if not cands:
            return "Overlord, negative, no " + ("more " if skip_given else "") + "targets for you."
        tgt = dict(cands[0], src="enemy_ground")
        with self._lock:
            self.current = tgt
            if tgt["id"] not in self._given:
                self._given.append(tgt["id"])
            self._reported_dead = None
        print(f"[JTAC] Target {tgt['type']} (id {tgt['id']}): {text_coords(tgt['lat'], tgt['lon'])}"
              + (f", elevation {tgt['alt'] * FT_PER_M:.0f} ft" if tgt.get("alt") is not None else ""))
        return self.describe(tgt, snap)

    @staticmethod
    def _live(tgt: dict) -> dict:
        """The target where it is now: the tactical picture follows a moving unit (step T7); without a
        picture, the position copied when it was assigned."""
        from tactical import link
        from tactical.models import dcs_key
        pos = link.position(dcs_key(tgt.get("id")))
        if pos is None:
            return tgt
        return dict(tgt, lat=pos[0], lon=pos[1], alt=pos[2] if pos[2] is not None else tgt.get("alt"))

    def describe(self, tgt: dict, snap: dict) -> str:
        tgt = self._live(tgt)
        prof = aircraft.profile(snap)
        parts = [f"Overlord, target {say_type(tgt['type'])}."]
        if prof.target_coords == "ddm":
            parts.append(say_ddm(tgt["lat"], tgt["lon"]))
        else:
            parts.append(say_grid(tgt["lat"], tgt["lon"], prof.target_grid_digits))
        if tgt.get("alt") is not None:
            parts.append(f"Elevation {int(round(tgt['alt'] * FT_PER_M, -1)):,} feet.")
        me = snap.get("my_ll")
        if me:
            d, brg = dist_brg((me[0], me[1]), (tgt["lat"], tgt["lon"]))
            parts.append(f"{aircraft.say_dist(d, snap).capitalize()} {compass(brg)} of you.")
        threats = self.threats(tgt, snap)
        if threats:
            parts.append("Threats: " + "; ".join(threats) + ".")
        return " ".join(parts)

    def threats(self, tgt: dict, snap: dict) -> List[str]:
        """Air defence near the target (SAMs, AAA), nearest first, up to two."""
        from common.unit_types import TYPES
        here = (tgt["lat"], tgt["lon"])
        seen, out = set(), []
        rows = radars_near(snap, here, THREAT_RADIUS_M) + [
            g for g in ground_near(snap, here, THREAT_RADIUS_M) if TYPES.is_air_defence(g["type"]) or g["w"] >= 90]
        for g in snap.get("enemy_ground") or []:             # big SAMs ground_near leaves out
            if is_big_sam(str(g[0])) and int(g[1]) not in {r["id"] for r in rows}:
                d, _ = dist_brg(here, (float(g[2]), float(g[3])))
                if d <= THREAT_RADIUS_M:
                    rows.append({"type": g[0], "id": int(g[1]), "lat": float(g[2]), "lon": float(g[3]), "d": d})
        for r in sorted(rows, key=lambda r: dist_brg(here, (r["lat"], r["lon"]))[0]):
            if r["id"] == tgt["id"] or r["id"] in seen:
                continue
            seen.add(r["id"])
            d, brg = dist_brg(here, (r["lat"], r["lon"]))
            where = "at the target" if d < 300 else f"{aircraft.say_dist(d, snap)} {compass(brg)} of the target"
            out.append(f"{say_type(r['type'])}, {where}")
            if len(out) == 2:
                break
        return out

    # ------------------------------------------------------------ mark, BDA
    def mark(self, text: str) -> str:
        with self._lock:
            tgt = self.current
        if tgt is None:
            return "Overlord, no target to mark. Request a target first."
        if self.oly is None or not getattr(self.oly, "connect", lambda: False)():
            return "Overlord, unable to mark, Olympus isn't running."
        tgt = self._live(tgt)                        # smoke where it is now, not where it was assigned
        color = next((c for c in _COLORS if re.search(r"\b" + c + r"\b", text.lower())), "red")
        ok, why = self.oly.command("smoke", {"color": color, "location": {"lat": tgt["lat"], "lng": tgt["lon"]}})
        if not ok:
            return f"Overlord, unable to mark, {why}."
        return f"Overlord, {color} smoke on the target."

    def bda(self) -> str:
        snap = self.snapshot()
        with self._lock:
            tgt = self.current
        if tgt is None:
            return "Overlord, no target assigned."
        why = stale_reason(snap)
        if why:
            return f"Overlord, unable to assess, {why}."
        st = target_state(snap, tgt)
        if st == "dead":
            with self._lock:
                self._reported_dead = tgt["id"]
            return f"Overlord, BDA, {say_type(tgt['type'])} destroyed. Good effect. Request next target when ready."
        if st == "alive":
            return f"Overlord, BDA, {say_type(tgt['type'])} still active."
        return "Overlord, unable to assess, the target's out of my picture."

    def active(self) -> bool:
        with self._lock:
            return self.current is not None

    def my_target(self) -> Optional[dict]:
        """The current target while it's alive (for "attack / lase / CAS on my target")."""
        with self._lock:
            tgt = self.current
        if tgt is None or target_state(self.snapshot(), tgt) == "dead":
            return None
        return dict(self._live(tgt), jtac=True)

    def tick(self) -> Optional[str]:
        """Calls the target destroyed once, on his own."""
        with self._lock:
            tgt, said = self.current, self._reported_dead
        if tgt is None or said == tgt["id"] or self.speak is None:
            return None
        if target_state(self.snapshot(), tgt) != "dead":
            return None
        with self._lock:
            self._reported_dead = tgt["id"]
        text = f"Overlord, good effect, {say_type(tgt['type'])} destroyed."
        self.speak(text)
        return text

    def reset(self) -> None:
        with self._lock:
            self.current, self._given, self._reported_dead = None, [], None
