"""
common/targets.py
Which enemy unit an order means, from Export.lua's contact list (object id, type, range, bearing,
lat/lon): "my target", "the ZSU at two o'clock", "the tank", or a set of them for the flight.

"My target" is George's priority target in the Apache (matched to a contact by type and range),
else the contact closest to your nose (within 30 degrees), nearest first.
"""
import re
from typing import List, Optional

from core.state import clock_position
from intelligence.threat_engine import threat_weight

NOSE_DEG = 30.0
MAX_RANGE_M = 8000.0

# spoken word -> pattern on DCS type names
KINDS = [
    (r"\b(?:zsu|shilka|zu ?23|triple a|aaa|flak|guns?|anti ?aircraft)\b",
     r"zsu|shilka|zu-23|2a38|tunguska|s-60|vulcan|gepard|aaa"),
    (r"\b(?:sams?|missile|sa ?\d+|osa|strela|tor|buk|igla|stinger|manpads?)\b",
     r"sa-\d|osa|strela|tor|buk|kub|igla|stinger|manpad|9a3|hawk|patriot|avenger|sam\b"),
    (r"\b(?:tanks?|armou?r|mbts?)\b", r"t-55|t-62|t-64|t-72|t-80|t-90|abrams|leopard|m1a|challenger|merkava|tank"),
    (r"\b(?:apcs?|ifvs?|bmps?|btrs?|brdms?|bradley|m113|mtlb|mt lb)\b",
     r"bmp|btr|brdm|bradley|m113|mt-?lb|marder|warrior|lav|stryker|ifv|apc"),
    (r"\b(?:trucks?|vehicles?|ural|kamaz|zil|gaz|jeeps?|technicals?)\b",
     r"ural|kamaz|zil|gaz|truck|hmmwv|humvee|uaz|tigr|land ?rover|technical"),
    (r"\b(?:infantry|soldiers?|troops|rpg|men)\b", r"soldier|infantry|rpg|paratrooper|insurgent"),
    (r"\b(?:artillery|grad|mlrs|howitzer|mortar)\b", r"grad|bm-21|mlrs|msta|gvozdika|akatsiya|howitzer|mortar|2s\d"),
]
_CLOCK = re.compile(r"\b(?:at |on )?(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
                    r" ?(?:o ?clock|oclock)\b")
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def _clock_of(c: dict, hdg) -> Optional[int]:
    return clock_position(c.get("brg"), hdg)


def _off_nose(c: dict, hdg) -> float:
    if hdg is None or c.get("brg") is None:
        return 180.0
    return abs((c["brg"] - hdg + 180) % 360 - 180)


def _usable(snap: dict) -> List[dict]:
    return [c for c in snap.get("contacts") or []
            if c.get("id") is not None and c.get("dist_m") is not None and c["dist_m"] <= MAX_RANGE_M
            and (c.get("lat") or c.get("lon"))]


def described(text: str) -> bool:
    """Does the order name a kind of target or a clock position?"""
    t = text.lower()
    return bool(_CLOCK.search(t)) or any(re.search(spoken, t) for spoken, _ in KINDS)


def my_target(snap: dict, george_top: Optional[dict] = None) -> Optional[dict]:
    cands = _usable(snap)
    if not cands:
        return None
    hdg = snap.get("hdg")
    if george_top:                              # Apache: George's priority target
        typ = str(george_top.get("type") or "").lower()
        words = [w for w in re.split(r"[^a-z0-9]+", typ) if len(w) >= 2]
        km = george_top.get("dist_km")

        def score(c):
            name = str(c.get("type") or "").lower()
            s = sum(1 for w in words if w in name)
            if km is not None:
                s -= abs(c["dist_m"] / 1000.0 - km) / max(1.0, km)
            return s
        best = max(cands, key=score)
        if score(best) > 0:
            return best
    ahead = [c for c in cands if _off_nose(c, hdg) <= NOSE_DEG]
    if not ahead:
        return None
    return min(ahead, key=lambda c: (_off_nose(c, hdg) > 10, c["dist_m"]))


def nearest(snap: dict) -> Optional[dict]:
    """The nearest enemy contact within MAX_RANGE_M, or None."""
    cands = _usable(snap)
    return min(cands, key=lambda c: c["dist_m"]) if cands else None


def by_description(snap: dict, text: str) -> Optional[dict]:
    """'the ZSU at two o'clock', 'the tank', 'the truck on the left'... nearest that fits."""
    t = text.lower()
    cands = _usable(snap)
    hdg = snap.get("hdg")
    from common.unit_types import TYPES
    for spoken, pattern in KINDS:
        if re.search(spoken, t):                 # by name, or by what DCS says the type is
            cands = [c for c in cands if re.search(pattern, str(c.get("type") or "").lower())
                     or TYPES.matches_spoken(str(c.get("type") or ""), t)]
            break
    m = _CLOCK.search(t)
    if m:
        want = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        def near(c):
            k = _clock_of(c, hdg)
            return k is not None and min((k - want) % 12, (want - k) % 12) <= 1
        cands = [c for c in cands if near(c)]
    return min(cands, key=lambda c: c["dist_m"]) if cands else None


def has_clock(text: str) -> bool:
    """'... at two o'clock': a clock position, which only makes sense among your own contacts."""
    return bool(_CLOCK.search(text.lower()))


WIDE_RANGE_M = 15000.0
DIR_CONE_DEG = 35.0


def wanted_kind(text: str) -> Optional[tuple]:
    """The (spoken, pattern) entry of KINDS the order names, or None."""
    t = text.lower()
    return next(((sp, pat) for sp, pat in KINDS if re.search(sp, t)), None)


def by_description_wide(snap: dict, text: str, radius_m: float = WIDE_RANGE_M,
                        origin: Optional[tuple] = None) -> Optional[dict]:
    """'the tanks', 'armour to the north-west': the nearest enemy ground unit of that kind among
    all the ones DCS lists (not only your own contacts, 24 Oct: "engage the tanks" got "No target"
    with tanks 8 km away), within radius_m of origin (default: you), in the direction said."""
    kind = wanted_kind(text)
    me = snap.get("my_ll")
    if kind is None or not me:
        return None
    origin = origin or me
    from common.overlord import dist_brg, parse_direction
    from common.unit_types import TYPES
    want = parse_direction(text.lower())
    best = None
    for g in snap.get("enemy_ground") or []:
        try:
            name, oid, lat, lon = str(g[0]), int(g[1]), float(g[2]), float(g[3])
        except (TypeError, ValueError, IndexError):
            continue
        if oid < 0 or not (re.search(kind[1], name.lower()) or TYPES.matches_spoken(name, text.lower())):
            continue
        d, brg = dist_brg(origin, (lat, lon))
        if d > radius_m:
            continue
        if want is not None and abs((brg - want[0] + 180) % 360 - 180) > DIR_CONE_DEG:
            continue
        dm, bm = dist_brg(me, (lat, lon))
        if best is None or d < best["_d"]:
            best = {"id": oid, "type": name, "lat": lat, "lon": lon, "dist_m": dm, "brg": bm, "_d": d,
                    "wide": True}
    if best:
        best.pop("_d")
    return best


def for_flight(snap: dict, count: int, text: str = "") -> List[dict]:
    """Up to `count` different targets for the flight: what was described, else the most
    dangerous ones within range, nearest first among equals."""
    cands = _usable(snap)
    t = text.lower()
    from common.unit_types import TYPES
    for spoken, pattern in KINDS:
        if re.search(spoken, t):                 # by name, or by what DCS says the type is
            cands = [c for c in cands if re.search(pattern, str(c.get("type") or "").lower())
                     or TYPES.matches_spoken(str(c.get("type") or ""), t)]
            break
    cands.sort(key=lambda c: (-threat_weight(str(c.get("type") or "")), c["dist_m"]))
    return cands[:count]


def say(c: dict, hdg=None) -> str:
    clock = _clock_of(c, hdg)
    return str(c.get("type") or "target").replace("_", " ") + (f", {clock} o'clock" if clock else "")
