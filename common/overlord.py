"""
common/overlord.py - Overlord, the C2 controller on the command radio.

Only utterances that START with "Overlord" come here; everything Overlord answers is spoken in the
Overlord voice (main.py). Flights are spawned and tasked through DCS Olympus (no radio-menu
fallback: with Olympus down he says so). Questions ("Overlord, what's our package priority?") are
answered from the briefing knowledge base (common/briefing.py) through the `ask` callable.

Requests ("requesting" is optional). Where: "15 kilometres north-east of me", "on waypoint 3" or
"on my position" is a point. For SEAD and CAS, a direction alone ("to my north") means the nearest
targets that way, and no place at all the nearest targets anywhere (SEAD within 75 km, CAS 25 km).
For CAP, a direction alone is 10 km that way and no place is over you.
    SEAD    "Overlord, requesting SEAD strike north of my current position"
            Weasel flight, 2 F-16C with HARMs, on the air-defence radars within 15 km of the point.
    CAS     "Overlord, requesting CAS on my target" / "... close air support 5 km east of me"
            Hog flight, 2 A-10C, on the enemy ground units within 3 km of the point (most dangerous
            first; the big radar SAMs are left to SEAD, and Overlord warns about them).
    CAP     "Overlord, requesting CAP over waypoint 4" / "... CAP north of me" / "... CAP" (= over you)
            Eagle flight, 2 F-15C, orbit the point and engage enemy aircraft within 40 km of it.
    Escort  "Overlord, requesting escort" / "... fighter cover"
            Shield flight, 2 F-16C, stay over you (move when you move 5 km) and engage enemy aircraft
            within 30 km of you.
    "Overlord, status" · "Overlord, cancel CAS" / "cancel all" (the flight goes home)

How a flight runs (one thread per mission): spawnAircrafts (airborne, `back_m` back from the point
on your side), wait until Export.lua shows the new Olympus-N group, weapons free, then:
  SEAD / CAS: attackUnit on the targets one at a time; "<type> destroyed", "area clear" (home).
  CAP / escort: setPath to the station (Olympus then orbits it), attackUnit on the nearest bandit
  that comes inside the radius, "splash one <type>", back to the station; home after 30 minutes.
A flight whose aircraft all disappear is "down". One flight of each kind at a time.

Export.lua lists only what's near you (radars 80 km, ground 40 km, aircraft 120 km, nearest N), so
a target outside that window is "unknown", never "destroyed".

Blocking work runs in the mission threads; handle() only parses, checks and sends the spawn (2 s
HTTP timeout), like the wingman orders.
"""
import math
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from common import geo
from common.debrief import DEBRIEF
from common.orders import ORDERS
from command import roles as _roles
from command.permissions import acting_as
from command import exchange
from command.roles import Said
from command.tasking import TASKS
from core.state import enemy_lists_stale, stale_reason
from intelligence.threat_engine import threat_weight

# ------------------------------------------------------------------ wake word
# Whisper's versions of "Overlord" (and a leading "hey"/"George"/"okay").
_WAKE = re.compile(r"^\s*(?:(?:hey|okay|ok|george|uh|um)[\s,.]+)*"
                   r"(?:over\s?lord|overlords|overload|overloaded|over\s?loud|over\s?the\s?lord|overlored|"
                   r"over\s?lard|overlaw|or\s?the\s?lord|or\s?a\s?lord|"
                   # heard 21 Oct: "or what I request in SEAD" (only with a request word after it)
                   r"or\s+what(?:\s+i)?(?=[\s,.]+(?:request|requesting|requested)\b))"
                   r"\b[\s,.!?]*(.*)$", re.I)


def addressed(text: str) -> Optional[str]:
    """The rest of the utterance when it starts with "Overlord", else None."""
    m = _WAKE.match(text or "")
    rest = m.group(1) if m else _sounds_like_overlord(text or "")
    if rest is None:
        return None
    return re.sub(r"\brequest in\b", "requesting", rest.strip(), flags=re.I)


def _sounds_like_overlord(text: str) -> Optional[str]:
    """Other spellings Whisper makes of "Overlord" as the first word (or two): "Overlaid,",
    "Over Lourd, ..." (24 Oct: the crew answered some Overlord requests). The rest, or None."""
    import difflib
    m = re.match(r"^\s*(?:(?:hey|okay|ok|george|uh|um)[\s,.]+)*([a-z']+)(?:[\s,.]+([a-z']+))?[\s,.!?]*(.*)$",
                 text, re.I)
    if not m:
        return None
    one, two, tail = m.group(1).lower(), (m.group(2) or "").lower(), m.group(3)
    ratio = lambda w: difflib.SequenceMatcher(None, w, "overlord").ratio()
    if len(one) >= 6 and one.startswith("o") and ratio(one) >= 0.75:
        return ((m.group(2) or "") + " " + tail).strip()
    if two and one in ("over", "ova", "olver", "oliver") and ratio(one + two) >= 0.8:
        return tail
    return None


# ------------------------------------------------------------------ flights
class Kind:
    def __init__(self, key, callsign, unit_type, loadout, alt_m, back_m, cruise_mps, desc, said):
        self.key, self.callsign, self.unit_type, self.loadout = key, callsign, unit_type, loadout
        self.alt_m, self.back_m, self.cruise_mps, self.desc, self.said = alt_m, back_m, cruise_mps, desc, said
        self.size = 2


SEAD_LOADOUT = "AIM-120C*2, AIM-9X*2, AGM-88C*4, ECM, TGP, HTS"
# Loadout names checked against the installed Olympus 2.0.5 unitPayloads.lua.
KINDS: Dict[str, Kind] = {
    "SEAD": Kind("SEAD", "Weasel", "F-16C_50", SEAD_LOADOUT, 6100, 45_000, 220.0,
                 "two Vipers with HARMs", "SEAD"),
    "CAS": Kind("CAS", "Hog", "A-10C_2", "AGM-65D*4, CBU-97*2, CBU-87*2, TGP, ECM, AIM-9*2", 3000, 30_000,
                160.0, "two A-10s", "CAS"),
    "CAP": Kind("CAP", "Eagle", "F-15C", "AIM-9*4,AIM-120*4,Fuel*3", 7600, 40_000, 230.0,
                "two Eagles", "CAP"),
    "ESCORT": Kind("ESCORT", "Shield", "F-16C_50", "AIM-120C*4, AIM-9X*2, FUEL*2, ECM", 6100, 35_000, 220.0,
                   "two Vipers", "escort"),
}
CALLSIGN = KINDS["SEAD"].callsign          # kept for older code / tests
SEAD_TYPE, SEAD_SIZE = KINDS["SEAD"].unit_type, 2

DEFAULT_DIST_M = 10_000            # "north of me" with no distance
SEARCH_RADIUS_M = 15_000           # SEAD: radars this close to the point
CAS_RADIUS_M = 3_000               # CAS: ground units this close to the point
SEAD_SEARCH_M = 75_000             # "SEAD to my north" / "SEAD": nearest radars within this of you
CAS_SEARCH_M = 25_000              # "CAS to my north" / "CAS": nearest ground units within this of you
MY_TARGET_FALLBACK_M = 10_000      # "CAS on my target" with no target known: nearest enemy this close
CAP_RADIUS_M = 40_000              # CAP: bandits this close to the station
ESCORT_RADIUS_M = 30_000           # escort: bandits this close to you
ESCORT_MOVE_M = 5_000              # escort: re-centre when you've moved this far
BREAK_OFF_M = 20_000               # a bandit this far outside the radius: back to station
COST_PER_AIRCRAFT = 10             # Olympus's default spawn cost when the database gives none
DEFER_WAIT_S = 300.0               # R7: a request Warlord defers waits this long (DEFER_WAIT_S in config)
DEFER_CHECK_S = 10.0               # ... and is rechecked this often
APPEAR_TIMEOUT_S = 30.0
MISSION_TIMEOUT_S = 25 * 60        # SEAD / CAS
CAS_MAX_TARGETS = 8                # CAS: the named target and the most dangerous others (1 Nov: 40 listed)
IP_M = 9_000                       # CAS under Darkside: the initial point, this far from the target area
IP_REACHED_M = 4_000               # ... reached within this
IP_TIMEOUT_S = 240.0               # ... or after this long (AI flights don't always fly to the point)
COVER_TIMEOUT_S = 30 * 60          # CAP / escort
POLL_S = 3.0

# What Export.lua lists: snapshot key -> (window around you, max entries)
WINDOWS = {"air_defence": (80_000, 25), "enemy_ground": (40_000, 40), "enemy_air": (120_000, 20)}

# ------------------------------------------------------------------ parsing
_SEAD = re.compile(r"\b(?:sead|s\s?e\s?a\s?d|seed|cede|seid|sid|see\s?add|c\s?e\s?a\s?d|wild\s?weasel|"
                   r"weasel|sam\s?suppression|suppress(?:ion)?\s+(?:of\s+)?(?:enemy\s+)?air\s?defen[cs]es?)\b")
_CAS = re.compile(r"\b(?:cas|c\s?a\s?s|cac|kass|cass|kaz|close\s?air\s?support|air\s?support|hogs?|a\s?10s?)\b")
_CAP = re.compile(r"\b(?:cap|c\s?a\s?p|caps|combat\s?air\s?patrol|air\s?patrol|barcap)\b")
_ESCORT = re.compile(r"\b(?:escorts?|escorting|fighter\s?cover|top\s?cover|air\s?cover|cover\s?me|"
                     r"fighter\s?escort)\b")
# 29 Oct: "Overlord, cancer escort" was taken as an escort request
_CANCEL = re.compile(r"\b(?:cancel\w*|cancer|abort|scrub|call off|knock it off|rtb|return to base|send .* home)\b")
_ALL = re.compile(r"\b(?:all|everything|everyone|every flight|all flights)\b")
_PICTURE = re.compile(r"\b(?:picture|bogey\s?dope|bogeys?|bandits?|air\s?threats?|anything\s+in\s+the\s+air|"
                      r"enemy\s+(?:air|aircraft|helicopters?))\b")
PICTURE_RANGE_M = 120_000          # everything Export.lua lists
GROUP_M = 5_000                    # aircraft this close together are one group
_STATUS = re.compile(r"\b(?:status|sitrep|how(?:'s| is) .* doing|report)\b")
_QUESTION = re.compile(r"^(?:what|what's|whats|where|where's|which|who|who's|when|why|how|is|are|do|does|"
                       r"did|tell me|remind me|brief me|read me|say again the|confirm|any)\b|\?\s*$")
_REQUEST = re.compile(r"\b(?:request|requesting|send|need|want|give me|get me|get us|i get|we get|"
                      r"launch|scramble|task|tasking)\b")
_MY_TARGET = re.compile(r"\b(?:my|our|the)\s+(?:current\s+)?target\b")

_DIRS = {"north": 0, "northeast": 45, "east": 90, "southeast": 135, "south": 180, "southwest": 225,
         "west": 270, "northwest": 315}
_NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
         "twenty five": 25, "thirty": 30, "forty": 40, "fifty": 50, "a": 1}
_DIST = re.compile(r"\b(\d+(?:\.\d+)?|" + "|".join(sorted(_NUMW, key=len, reverse=True)) + r")\s*"
                   r"(km|kilomet(?:er|re)s?|clicks?|klicks?|k\b|nautical miles?|nm|miles?|mi\b)")
_WP = re.compile(r"\b(?:waypoint|way point|wp|steerpoint)\s*(\d+|" + "|".join(_NUMW) + r")\b")
_ON_ME = re.compile(r"\b(?:on|at|near|around|over)\s+(?:my|our)\s+(?:current\s+)?(?:position|location|pos)\b|"
                    r"\b(?:on|near|around|over)\s+(?:me|us)\b")

# Radars a HARM can home on. IR systems (Strela, Igla, Avenger...) and unknown types are left out.
_RADAR_TYPES = re.compile(r"shilka|zsu-23|tunguska|2s6|pantsir|9a331|\btor\b|\bosa\b|9a33|sa-8|buk|9s18|"
                          r"9s470|9a310|sa-11|kub|1s91|2p25|sa-6|s-300|40b6|64h6|5p85|sa-10|snr|s-125|p-19|"
                          r"sa-2|sa-3|hawk|patriot|nasams|rapier|roland|gepard|hq-7|55g6|1l13|dog ear|ewr|"
                          r"radar|\bsr\b|\btr\b|\bstr\b", re.I)
_IR_ONLY = re.compile(r"strela|sa-13|sa-9|9k31|9a35|igla|stinger|avenger|chaparral|manpad|sa-18|sa-24", re.I)
# Medium / long-range radar SAMs: left out of CAS (A-10s would fly into them), warned about.
_BIG_SAM = re.compile(r"kub|1s91|2p25|sa-6|buk|9s18|9s470|9a310|sa-11|s-300|40b6|64h6|5p85|sa-10|snr|s-125|"
                      r"p-19|sa-2|sa-3|hawk|patriot|nasams|hq-7", re.I)

# Short spoken names for common DCS types.
_SAY = [(r"kub|1s91|2p25|sa-6", "SA-6"), (r"buk|9s18|9s470|9a310|sa-11", "SA-11"),
        (r"osa|9a33|sa-8", "SA-8"), (r"tor|9a331|sa-15", "SA-15"), (r"tunguska|2s6", "Tunguska"),
        (r"shilka|zsu-23", "Shilka"), (r"s-300|40b6|64h6|5p85|sa-10", "SA-10"),
        (r"snr|s-125|p-19|sa-3", "SA-3"), (r"sa-2", "SA-2"), (r"hawk", "Hawk"), (r"patriot", "Patriot"),
        (r"rapier", "Rapier"), (r"roland", "Roland"), (r"gepard", "Gepard"), (r"hq-7", "HQ-7"),
        (r"pantsir", "Pantsir"), (r"55g6|1l13|dog ear|ewr", "early warning radar"),
        (r"strela|sa-13", "SA-13"), (r"zu-23", "ZU-23"), (r"ural|kamaz|zil|gaz|truck", "truck")]


def _minutes(n: int) -> str:
    return "1 minute" if n == 1 else f"{n} minutes"


def _dist(metres: float, snap: dict, whole: bool = False) -> str:
    """'12 kilometres' (helicopters) / '7 miles' (jets): common/aircraft.py."""
    from common.aircraft import say_dist
    if whole:
        from common.aircraft import profile, M_PER_NM
        unit = M_PER_NM if profile(snap).imperial else 1000.0
        metres = max(1, round(metres / unit)) * unit
    return say_dist(metres, snap)


def _alt(metres: float, snap: dict) -> str:
    from common.aircraft import say_alt
    return say_alt(metres, snap)


def say_type(name: str) -> str:
    low = (name or "").lower()
    for rx, said in _SAY:
        if re.search(rx, low):
            return said
    return (name or "target").replace("_", " ")


def is_sead_target(name: str) -> bool:
    """A radar a HARM can home on: DCS's own attributes when it has described the type, else names."""
    from common.unit_types import TYPES
    arm = TYPES.is_arm_target(name)
    if arm is not None:
        return arm
    return bool(_RADAR_TYPES.search(name or "")) and not _IR_ONLY.search(name or "")


def is_big_sam(name: str) -> bool:
    from common.unit_types import TYPES
    big = TYPES.is_big_sam(name)
    return big if big is not None else bool(_BIG_SAM.search(name or ""))


def _num(tok: str) -> float:
    return float(tok) if re.match(r"^\d", tok) else float(_NUMW[tok])


def parse_distance(t: str) -> Optional[Tuple[float, str]]:
    """(metres, how it was said) or None."""
    m = _DIST.search(t)
    if not m:
        return None
    n, unit = _num(m.group(1)), m.group(2)
    if unit.startswith(("nautical", "nm")):
        return n * 1852.0, f"{n:g} nautical miles"
    if unit.startswith(("mile", "mi")):
        return n * 1609.34, f"{n:g} miles"
    return n * 1000.0, f"{n:g} kilometres"


def parse_direction(t: str) -> Optional[Tuple[float, str]]:
    """(bearing degrees, word) for "north", "north-east", "north east", "northeast"."""
    t = t.replace("-", " ")
    for word in sorted(_DIRS, key=len, reverse=True):
        spaced = word.replace("east", " east").replace("west", " west").strip() if len(word) > 5 else word
        if re.search(r"\b(?:" + word + "|" + spaced + r")\b", t):
            return float(_DIRS[word]), (spaced if spaced != word else word)
    return None


def parse_where(t: str, snap: dict, default_me: bool = False) -> Tuple[Optional[Tuple[float, float]], str]:
    """The point asked for: ((lat, lon), how to say it) or (None, why not).
    default_me: no place given means your position (CAP)."""
    me = snap.get("my_ll")
    from common.user_waypoints import USER_WAYPOINTS, named_in
    name = named_in(t)                          # "on waypoint Alpha", "over the objective"
    if name:
        ll = USER_WAYPOINTS.ll(name, snap)
        if ll is None:
            return None, (f"there's no waypoint {name}" if name != "Objective" else "no objective is set")
        return ll, ("the objective" if name == "Objective" else f"waypoint {name}")
    m = _WP.search(t)
    if m:
        n = int(_num(m.group(1)))
        for p in snap.get("route") or []:
            if len(p) >= 3 and int(p[0]) == n:
                ll = geo.FIT.to_ll(float(p[1]), float(p[2]))
                if ll is None:
                    return None, "I can't place your waypoints on the map yet"
                return ll, f"waypoint {n}"
        return None, f"there's no waypoint {n}"
    if not me:
        return None, "I don't have your position"
    d = parse_direction(t)
    if d is None:
        if default_me or _ON_ME.search(t) or re.search(r"\b(?:here|my position|my location)\b", t):
            return (me[0], me[1]), "your position"
        return None, "say where: a direction from you, or a waypoint"
    brg, word = d
    dist = parse_distance(t)
    from common.aircraft import say_dist
    metres, said = dist if dist else (DEFAULT_DIST_M, say_dist(DEFAULT_DIST_M, snap))
    n, e = metres * math.cos(math.radians(brg)), metres * math.sin(math.radians(brg))
    return geo.offset_ll(me[0], me[1], n, e), f"{said} {word} of you"


def is_question(t: str) -> bool:
    """A question for the briefing, not a request ("can I get CAS..." is a request)."""
    return bool(_QUESTION.search(t.strip())) and not _REQUEST.search(t)


# ------------------------------------------------------------------ geometry
def dist_brg(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
    """Metres and true bearing from a to b (flat Earth: fine within a few hundred km)."""
    n = (b[0] - a[0]) * 111_320.0
    e = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(n, e), math.degrees(math.atan2(e, n)) % 360


def compass(brg: float) -> str:
    return ["north", "north east", "east", "south east", "south", "south west", "west",
            "north west"][int(((brg % 360) + 22.5) // 45) % 8]


def radars_near(snap: dict, point: Tuple[float, float], radius: float = SEARCH_RADIUS_M) -> List[dict]:
    out, seen = [], set()
    # Export's air-defence list goes by names; the ground list adds types only DCS knows are radars
    rows = [(a, "air_defence") for a in snap.get("air_defence") or []] + \
           [(g, "enemy_ground") for g in snap.get("enemy_ground") or []]
    for a, src in rows:
        name, oid, lat, lon = a[0], int(a[1]), float(a[2]), float(a[3])
        if oid < 0 or oid in seen or not is_sead_target(name):
            continue
        seen.add(oid)
        d, _ = dist_brg(point, (lat, lon))
        if d <= radius:
            out.append({"type": name, "id": oid, "lat": lat, "lon": lon, "d": d,
                        "w": threat_weight(name), "src": src})
    out.sort(key=lambda r: (-r["w"], r["d"]))
    return out


def ground_near(snap: dict, point: Tuple[float, float], radius: float = CAS_RADIUS_M) -> List[dict]:
    """Enemy ground units for CAS (big radar SAMs left out), most dangerous first."""
    out, seen = [], set()
    for g in snap.get("enemy_ground") or []:
        name, oid, lat, lon = g[0], int(g[1]), float(g[2]), float(g[3])
        if oid < 0 or oid in seen or is_big_sam(name):
            continue
        d, _ = dist_brg(point, (lat, lon))
        if d <= radius:
            seen.add(oid)
            out.append({"type": name, "id": oid, "lat": lat, "lon": lon, "d": d,
                        "w": threat_weight(name), "src": "enemy_ground"})
    out.sort(key=lambda r: (-r["w"], r["d"]))
    return out


_STRUCTURE_NAMES = re.compile(r"outpost|bunker|road|house|tower|depot|warehouse|barrack|fortif|sandbag|checkpoint", re.I)


def is_structure(name: str) -> bool:
    """A fortification / building / static object (DCS's attributes; by name until DCS describes it)."""
    from common.unit_types import TYPES
    a = TYPES.attrs(name)
    if a:
        return bool(a & {"Fortifications", "Buildings", "Static"})
    return bool(_STRUCTURE_NAMES.search(name or ""))


def cas_targets(targets: List[dict], keep_first: bool = False) -> List[dict]:
    """The CAS list: structures dropped while there are vehicles to hit (a named first target stays),
    at most CAS_MAX_TARGETS, most dangerous first as ground_near sorted them."""
    head, rest = (targets[:1], targets[1:]) if keep_first else ([], targets)
    live = [t for t in rest if not is_structure(t["type"])]
    if live or head:
        rest = live
    return (head + rest)[:CAS_MAX_TARGETS]


def cas_ip(snap: dict, point: Tuple[float, float], toward: Optional[Tuple[float, float]] = None,
           ip_m: Optional[float] = None) -> Tuple[float, float]:
    """The CAS initial point: IP_M from the target area, on the side away from known air defence (else
    towards `toward`: where the flight came from, or you). Hog flights and the 9-line (5 Nov) share it."""
    lat0, lon0 = point
    ip_m = IP_M if ip_m is None else ip_m
    threats = radars_near(snap, point, SEARCH_RADIUS_M)
    if threats:
        n = sum(t["lat"] - lat0 for t in threats)
        e = sum((t["lon"] - lon0) * math.cos(math.radians(lat0)) for t in threats)
        brg = (math.degrees(math.atan2(e, n)) + 180.0) % 360 if (n or e) else 0.0
    elif toward is not None:
        _, brg = dist_brg(point, toward)
    else:
        brg = 180.0
    return geo.offset_ll(lat0, lon0, ip_m * math.cos(math.radians(brg)), ip_m * math.sin(math.radians(brg)))


def big_sams_near(snap: dict, point: Tuple[float, float], radius: float = SEARCH_RADIUS_M) -> List[dict]:
    """Big radar SAMs near the point. R4: from the tactical picture's threat list while it's current (a stale
    SAM still counts; a destroyed one doesn't), else Export's lists. Warlord's denial and Overlord's caution
    both come from here, so they agree."""
    from tactical import link
    pic = link.threats_near(point[0], point[1], radius)
    if pic is not None:
        out = [dict(r, src="air_defence") for r in pic if is_big_sam(r["type"])]
        out.sort(key=lambda r: (-r["w"], r["d"]))
        return out
    return [r for r in radars_near(snap, point, radius) if is_big_sam(r["type"])]


def bandits_near(snap: dict, point: Tuple[float, float], radius: float) -> List[dict]:
    out = []
    for a in snap.get("enemy_air") or []:
        name, oid, lat, lon = a[0], int(a[1]), float(a[2]), float(a[3])
        if oid < 0 or not str(name or "").strip() or str(name).upper() == "UNKNOWN":
            continue                                    # nameless objects aren't aircraft (23 Oct)
        d, _ = dist_brg(point, (lat, lon))
        if d <= radius:
            out.append({"type": name, "id": oid, "lat": lat, "lon": lon, "d": d, "src": "enemy_air",
                        "alt": float(a[4]) if len(a) > 4 else 0.0})
    out.sort(key=lambda r: r["d"])
    return out


def target_state(snap: dict, tgt: dict) -> str:
    """'alive', 'dead' or 'unknown' (outside what Export.lua lists, or the lists are stale).
    With a current tactical picture (step T7) its track decides: dead only after 3 fresh scans without
    the unit inside coverage, never on one missing row. Without one, the old rule below."""
    from tactical import link
    from tactical.models import dcs_key
    st = link.status(dcs_key(tgt.get("id")))
    if st is not None:
        return st
    if enemy_lists_stale(snap):                  # a frozen picture can't say what's dead
        return "unknown"
    key = tgt.get("src", "air_defence")
    rows = snap.get(key) or []
    for r in rows:
        if int(r[1]) == tgt["id"]:
            return "alive"
    me = snap.get("my_ll")
    window, cap = WINDOWS.get(key, (0, 0))
    if not me:
        return "unknown"
    d, _ = dist_brg(me, (tgt["lat"], tgt["lon"]))
    if d > window * 0.95:
        return "unknown"
    if len(rows) >= cap:                         # list full: farther units are cut off
        far = max(dist_brg(me, (float(r[2]), float(r[3])))[0] for r in rows)
        if d >= far:
            return "unknown"
    return "dead"


def _say_list(items: List[str]) -> str:
    counts: Dict[str, int] = {}
    for i in items:
        counts[i] = counts.get(i, 0) + 1
    said = [f"{n} {k}s" if n > 1 else k for k, n in counts.items()]
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]


# ------------------------------------------------------------------ missions
class Mission:
    def __init__(self, kind: Kind, point, where: str, targets: List[dict], spawn_ll, known_groups):
        self.kind, self.point, self.where = kind, point, where
        self.targets = targets
        self.spawn_ll = spawn_ll
        self.known_groups = set(known_groups)
        self.station = point                 # CAP / escort: where they orbit
        self.ip = None                       # CAS under Darkside: the initial point (P2-6)
        self.group: Optional[str] = None
        self.lead_id: Optional[int] = None
        self.current: Optional[dict] = None
        self.killed: List[str] = []
        self.state = "launching"            # launching > engaging > home | lost | failed | cancelled
        self.started = time.monotonic()
        self.cancel = threading.Event()
        self.task_id: Optional[str] = None   # its record in the tasking registry (command/tasking.py)

    @property
    def cs(self) -> str:
        return self.kind.callsign


class Overlord:
    """Owns the Olympus client (shared with the wingman executor) and the running missions.

    my_target(snap) -> contact dict or None: "my target" for CAS (George's priority target).
    ask(question) -> what to say: briefing questions (common/briefing.py)."""

    def __init__(self, oly, snapshot: Callable[[], dict], speak: Callable[[str], None],
                 my_target: Optional[Callable[[dict], Optional[dict]]] = None,
                 ask: Optional[Callable[[str], str]] = None, tasks=None, warlord=None, darkside=None):
        """warlord (common/warlord.py): asked before every spawn; None = no review (as before Phase 2).
        darkside (common/darkside.py): controls CAS flights (weapons hold, IP, cleared hot); None = CAS
        attacks on its own, as before."""
        self.oly, self.snapshot, self.speak = oly, snapshot, speak
        self.my_target, self.ask = my_target, ask
        self.tasks = tasks if tasks is not None else TASKS   # Phase 2: every flight has a tasking record
        self.warlord, self.darkside = warlord, darkside
        self.callsign: Callable[[], str] = lambda: "Nomad"     # main.py: the mission's callsign (P2-2)
        self._lock = threading.Lock()
        self._missions: List[Mission] = []
        # R7: requests Warlord deferred, one per kind: kind -> {"task_id", "text", "since", "reason"}
        self._waiting: Dict[str, dict] = {}
        self._req = threading.local()                    # the request being handled (its words, a recheck)
        self._waiter: Optional[threading.Thread] = None
        self.defer_wait_s, self.defer_check_s = DEFER_WAIT_S, DEFER_CHECK_S
        self.clock: Callable[[], float] = time.monotonic
        # speaks an unprompted line in a role's voice (main.py); default: Overlord's speak
        self.say_role: Callable[[str, str], None] = lambda text, who: self.speak(text)
        if warlord is not None:
            warlord.waiting_lines = self.waiting_lines   # "Warlord, status" lists them (R2's hook)

    # -------------------------------------------------------------- entry
    def handle(self, text: str) -> Optional[str]:
        """What Overlord says now, or None if the utterance isn't addressed to Overlord."""
        rest = addressed(text)
        if rest is None:
            return None
        t = re.sub(r"[^a-z0-9.'? -]+", " ", rest.lower()).strip()
        if not t:
            return "Overlord, go ahead."
        named = self._kinds_named(t)
        if _CANCEL.search(t):
            return self.cancel(named, bool(_ALL.search(t)))
        from common import position, airfields
        if airfields.wanted(t) and not named:           # "Overlord, vector to nearest friendly field"
            said = airfields.AIRFIELDS.handle(t, self.snapshot())
            return f"Overlord, {said[0].lower() + said[1:]}"
        if position.wanted(t) and not named:            # "Overlord, request our grid" / "say bullseye"
            said = position.handle(t, self.snapshot(), lambda: position.olympus_bullseye(self.oly))
            return f"Overlord, {said[0].lower() + said[1:] if said.startswith('Unable') else said}"
        if _PICTURE.search(t) and not named:            # "Overlord, what's the picture?" (AWACS-style)
            why = stale_reason(self.snapshot())
            return f"Overlord, unable, {why}. Say again in a moment." if why else self.picture()
        if _STATUS.search(t) and not _DIST.search(t) and parse_direction(t) is None:
            return self.status()
        if is_question(t) and self.ask is not None:
            return self.ask(t)
        t = t.rstrip("?")
        if named:                                       # no new tasking from a frozen picture
            why = stale_reason(self.snapshot())
            if why:
                return f"Overlord, unable, {why}. Say again in a moment."
        self._req.text = t                              # R7: kept with a deferred request, to ask again
        if "SEAD" in named:
            return self.request_sead(t)
        if "CAS" in named:
            return self.request_cas(t)
        if "CAP" in named:
            return self.request_cap(t)
        if "ESCORT" in named:
            return self.request_escort(t)
        return "Overlord, say again your request. I can send SEAD, CAS, CAP or an escort."

    @staticmethod
    def _kinds_named(t: str) -> List[str]:
        named = [k for k, rx in (("SEAD", _SEAD), ("CAS", _CAS), ("CAP", _CAP), ("ESCORT", _ESCORT))
                 if rx.search(t)]
        # by callsign (29 Oct: "Overlord, cancel Shield flight" got "say which")
        for k, rx in (("CAP", r"\beagles?\b"), ("ESCORT", r"\bshields?\b")):
            if re.search(rx, t) and k not in named:
                named.append(k)
        if "CAP" in named and "ESCORT" in named:     # "CAP escort flight over point alpha" is a CAP
            named.remove("ESCORT")
        return named

    def _busy(self, key: str) -> Optional[str]:
        with self._lock:
            if any(m.kind.key == key and m.state in ("launching", "engaging") for m in self._missions):
                k = KINDS[key]
                return f"Overlord, {k.callsign} flight is already tasked. Say cancel {k.said} first."
        return None

    # -------------------------------------------------------------- requests
    @staticmethod
    def _area(t: str, snap: dict, near: Callable, radius: float, search_m: float, what: str):
        """Where the targets are: (point, how to say it, targets) or (None, what to say, []).
        A waypoint, "on my position" or a direction WITH a distance is a point. A direction alone
        ("to my north") or nothing at all means the nearest ones that way (or anywhere) within
        search_m of you (21 Oct: "CAS to my north" missed targets 5 km north of the 10 km point)."""
        d = parse_direction(t)
        from common.user_waypoints import named_in
        if _WP.search(t) or _ON_ME.search(t) or named_in(t) or (d is not None and parse_distance(t) is not None):
            point, where = parse_where(t, snap)
            if point is None:
                return None, f"Overlord, unable, {where}.", []
            found = near(snap, point, radius)
            if not found:
                return None, (f"Overlord, negative. No {what} known within {_dist(radius, snap)} "
                              f"of {where}. No flight sent."), []
            return point, where, found
        me = snap.get("my_ll")
        if not me:
            return None, "Overlord, unable, I don't have your position.", []
        cands = near(snap, (me[0], me[1]), search_m)
        if d is not None:
            cands = [c for c in cands
                     if abs((dist_brg((me[0], me[1]), (c["lat"], c["lon"]))[1] - d[0] + 180) % 360 - 180) <= 35]
        if not cands:
            way = f" {d[1]} of you" if d else " of you"
            return None, (f"Overlord, negative. No {what} known within {_dist(search_m, snap)}"
                          f"{way}. No flight sent."), []
        nearest = min(cands, key=lambda c: dist_brg((me[0], me[1]), (c["lat"], c["lon"]))[0])
        point = (nearest["lat"], nearest["lon"])
        dist, brg = dist_brg((me[0], me[1]), point)
        return point, f"{_dist(dist, snap, whole=True)} {compass(brg)} of you", near(snap, point, radius)

    def request_sead(self, t: str) -> str:
        snap = self.snapshot()
        busy = self._busy("SEAD")
        if busy:
            return busy
        point, where, radars = self._area(t, snap, radars_near, SEARCH_RADIUS_M, SEAD_SEARCH_M,
                                          "air-defence radars")
        if point is None:
            return where
        k = KINDS["SEAD"]
        names = _say_list([say_type(r["type"]) for r in radars])
        return self._launch(k, snap, point, where, radars,
                            f"Overlord copies. {k.callsign} flight, {k.desc}, tasked SEAD {where}. "
                            f"Targets: {names}. Time on target {_minutes(self._eta(k))}.",
                            f"Overlord, {k.callsign} flight tasked SEAD {where}, {_minutes(self._eta(k))}.")

    def request_cas(self, t: str) -> str:
        snap = self.snapshot()
        busy = self._busy("CAS")
        if busy:
            return busy
        first = None
        if _MY_TARGET.search(t):
            first = self.my_target(snap) if self.my_target else None
            if first and first.get("lat") is not None:
                point, where = (float(first["lat"]), float(first["lon"])), "your target"
                targets = ground_near(snap, point)
            else:                    # no target George or the nose can name: the nearest enemy
                first = None
                point, where, targets = self._area("", snap, ground_near, CAS_RADIUS_M, MY_TARGET_FALLBACK_M,
                                                   "enemy ground units")
                if point is None:
                    return where
                where = "the nearest enemy, " + where
        else:
            point, where, targets = self._area(t, snap, ground_near, CAS_RADIUS_M, CAS_SEARCH_M,
                                               "enemy ground units")
            if point is None:
                return where
        if first is not None:
            tgt = {"type": first.get("type") or "target", "id": int(first["id"]), "lat": float(first["lat"]),
                   "lon": float(first["lon"]), "d": 0.0, "w": 999, "src": "enemy_ground"}
            targets = [tgt] + [x for x in targets if x["id"] != tgt["id"]]
        targets = cas_targets(targets, keep_first=first is not None)
        if not targets:
            return (f"Overlord, negative. No enemy ground units known within "
                    f"{_dist(CAS_RADIUS_M, snap)} of {where}. No flight sent.")
        sams = big_sams_near(snap, point)
        k = KINDS["CAS"]
        kinds = _say_list([say_type(x["type"]) for x in targets[:3]])
        more = f" and {len(targets) - 3} more" if len(targets) > 3 else ""
        warn = (f" Caution, {_say_list([say_type(s['type']) for s in sams])} within "
                f"{_dist(SEARCH_RADIUS_M, snap)}. Recommend SEAD." if sams else "")
        dark = " Weapons hold until Darkside clears them." if self.darkside is not None else ""
        return self._launch(k, snap, point, where, targets,
                            f"Overlord copies. {k.callsign} flight, {k.desc}, tasked CAS {where}. "
                            f"Targets: {kinds}{more}. Time on target {_minutes(self._eta(k))}.{warn}{dark}",
                            f"Overlord, {k.callsign} flight tasked CAS {where}, {_minutes(self._eta(k))}.{warn}")

    def request_cap(self, t: str) -> str:
        snap = self.snapshot()
        busy = self._busy("CAP")
        if busy:
            return busy
        point, where = parse_where(t, snap, default_me=True)
        if point is None:
            return f"Overlord, unable, {where}."
        k = KINDS["CAP"]
        where_said = "over " + where if where in ("your position",) or where.startswith("waypoint") else where
        return self._launch(k, snap, point, where, [],
                            f"Overlord copies. {k.callsign} flight, {k.desc}, CAP {where_said}. "
                            f"On station in {_minutes(self._eta(k))}.",
                            f"Overlord, {k.callsign} flight, CAP {where_said}, on station in {_minutes(self._eta(k))}.")

    def request_escort(self, t: str) -> str:
        snap = self.snapshot()
        busy = self._busy("ESCORT")
        if busy:
            return busy
        me = snap.get("my_ll")
        if not me:
            return "Overlord, unable, I don't have your position."
        k = KINDS["ESCORT"]
        return self._launch(k, snap, (me[0], me[1]), "you", [],
                            f"Overlord copies. {k.callsign} flight, {k.desc}, escorting you. "
                            f"Joining in {_minutes(self._eta(k))}.",
                            f"Overlord, {k.callsign} flight joining you in {_minutes(self._eta(k))}.")

    def _eta(self, k: Kind) -> int:
        return max(1, round((self._back_m(k, self.snapshot()) / k.cruise_mps) / 60))

    @staticmethod
    def _back_m(k: Kind, snap: dict) -> float:
        """How far back the flight spawns. A jet's escort starts closer, or it never catches you."""
        from common.aircraft import profile
        p = profile(snap)
        return p.escort_back_m if k.key == "ESCORT" and p.escort_back_m else k.back_m

    # -------------------------------------------------------------- spawning
    def _tasking_text(self, k: Kind, ok_text: str, short_text: str) -> str:
        """Overlord's tasking line in the exchange's form (command/exchange.py, P2-7). Without Warlord
        (command roles off) it's today's line, unchanged."""
        if self.warlord is None:
            return ok_text
        if exchange.short():
            return short_text or ok_text
        body = re.sub(r"^Overlord copies\.\s*", "", ok_text)
        dark = " Contact Darkside." if k.key == "CAS" and self.darkside is not None else ""
        return f"{self.callsign()}, Overlord, {body}{dark}"

    def _ack(self, k: Kind, where: str) -> str:
        """Full form: Overlord's first answer, before he asks Warlord."""
        what = k.said if k.key == "ESCORT" else f"{k.said} {where}"
        return f"{self.callsign()}, Overlord, copy {what}. Checking with Warlord."

    def _launch(self, k: Kind, snap: dict, point, where: str, targets: List[dict], ok_text: str = "",
                short_text: str = "") -> str:
        """Asks Warlord, then spawns the flight and starts its thread. Returns what to say: ok_text
        when it launched (with Warlord's note after it, if he has one), else why not. A Warlord denial is
        held for "Warlord, override" (common/warlord.py) and said in his voice after Overlord's line."""
        who = f"{k.callsign} flight"
        retry = getattr(self._req, "retry", None)        # R7: a deferred request asked again
        if retry is not None:
            retry["result"] = "waiting"                  # unless something below decides
        if not self.oly.connect():
            if retry is not None:
                retry["reason"] = "Olympus isn't running"
                return ""
            ORDERS.record(who, k.key, "failed", "Olympus isn't running")
            return "Overlord, unable, Olympus isn't running."
        cost = k.size * COST_PER_AIRCRAFT
        why = self._budget_problem(cost)
        if why:
            if retry is not None:
                retry["reason"] = why
                return ""
            ORDERS.record(who, k.key, "failed", why)
            return f"Overlord, unable, {why}."
        from tactical.models import dcs_key
        # R5: who sends it in - Darkside for CAS when he's on the net (else Overlord, who then runs it himself)
        ctrl = _roles.DARKSIDE if k.key == "CAS" and self.darkside is not None else (
            _roles.OVERLORD if k.key == "CAS" else None)
        task = self.tasks.get(retry["task_id"]) if retry is not None else None
        if task is None:
            task = self.tasks.new(k.key, _roles.OVERLORD, who, where=where,
                                  targets=[dcs_key(x.get("id")) for x in targets], controller=ctrl)
        note = ()
        final = self._tasking_text(k, ok_text, short_text)
        full = self.warlord is not None and not exchange.short()
        if self.warlord is None:
            self.tasks.set_state(task.task_id, "approved", "no Warlord")
        else:
            d = self.warlord.review(k.key, point, targets, snap)
            if d.verdict == "deny":
                self.tasks.set_state(task.task_id, "denied", d.reason)
                ORDERS.record(who, k.key, "failed", f"Warlord denied: {d.reason}")
                self.warlord.hold(task.task_id, k.key, d,
                                  lambda: self._spawn(k, self.snapshot(), point, where, targets, task.task_id)
                                  or final)
                if retry is not None:
                    retry["result"] = "denied"
                    return Said("", then=[(self.warlord.deny_text(k.key, d), "warlord")])
                return Said(self._ack(k, where) if full else "",
                            then=[(self.warlord.deny_text(k.key, d), "warlord")])
            if d.verdict == "defer":
                if retry is not None:                    # still not: keep waiting, nothing said
                    retry["reason"] = d.reason
                    return ""
                self.tasks.set_state(task.task_id, "deferred", d.reason)
                ORDERS.record(who, k.key, "submitted", f"Warlord deferred: {d.reason}; waiting")
                self._wait(k.key, task.task_id, getattr(self._req, "text", "") or k.key.lower(), d.reason)
                return Said(self._ack(k, where) if full else "",
                            then=[(self.warlord.defer_text(k.key, d, self.defer_wait_s), "warlord")])
            self.tasks.set_state(task.task_id, "approved", "Warlord approved")
            if d.advice:
                note = ((self.warlord.advice_text(k.key, d), "warlord"),)
        err = self._spawn(k, snap, point, where, targets, task.task_id)
        if retry is not None:
            retry["result"] = "failed" if err else "approved"
            return err or (Said(final, then=list(note)) if note else final)
        if err:
            return err
        if full:                         # the whole exchange: ack, Warlord's approval, the tasking, his note
            return Said(self._ack(k, where), then=[(self.warlord.approve_text(k.key), "warlord"),
                                                   (final, "overlord"), *note])
        return Said(final, then=note) if note else final

    def _spawn(self, k: Kind, snap: dict, point, where: str, targets: List[dict], task_id: str) -> Optional[str]:
        """Spawns an approved (or overridden) task's flight and starts its thread. Why not, else None."""
        who = f"{k.callsign} flight"
        cost = k.size * COST_PER_AIRCRAFT
        me = snap.get("my_ll") or point
        d_me, brg_me = dist_brg(point, me)
        if d_me < 1000:                      # on your own position: come in from behind you
            brg_me = ((snap.get("hdg") or 0.0) + 180.0) % 360
        back = self._back_m(k, snap)
        n, e = back * math.cos(math.radians(brg_me)), back * math.sin(math.radians(brg_me))
        spawn = geo.offset_ll(point[0], point[1], n, e)
        heading = math.radians((brg_me + 180.0) % 360)      # towards the point; Olympus wants radians
        units = []
        for i in range(k.size):
            ln, le = geo.body_to_ne(math.degrees(heading), -150.0 * i, 150.0 * i)
            lat, lon = geo.offset_ll(spawn[0], spawn[1], ln, le)
            units.append({"unitType": k.unit_type, "location": {"lat": lat, "lng": lon},
                          "altitude": k.alt_m, "heading": heading, "loadout": k.loadout,
                          "liveryID": "", "skill": "High"})
        known = {u[0] for u in snap.get("olympus_units") or []}
        task = self.tasks.get(task_id)
        with acting_as(_roles.OVERLORD, task.task_id):
            ok, why = self.oly.command("spawnAircrafts", {
                "coalition": "blue", "country": "", "airbaseName": "", "immediate": True,
                "spawnPoints": cost, "units": units})
        if not ok:
            self.tasks.set_state(task.task_id, "failed", f"spawnAircrafts: {why}")
            ORDERS.record(who, k.key, "failed", f"spawnAircrafts: {why}")
            return f"Overlord, unable, {why}."
        ORDERS.record(who, k.key, "accepted", f"spawning {k.size} {k.unit_type}")
        self.tasks.set_state(task.task_id, "en_route", f"spawning {k.size} {k.unit_type}")
        m = Mission(k, point, where, targets, spawn, known)
        m.task_id = task.task_id
        DEBRIEF.launch(f"{k.callsign} flight", k.key)
        with self._lock:
            self._missions.append(m)
        threading.Thread(target=self._run, args=(m,), daemon=True,
                         name=f"overlord-{k.key.lower()}").start()
        return None

    def _budget_problem(self, cost: int) -> str:
        """Why Olympus would refuse the spawn (it refuses silently), or ''."""
        try:
            m = self.oly.get("/olympus/mission")
            opts = ((m or {}).get("mission") or {}).get("commandModeOptions") or {}
        except Exception:
            return ""
        mode = opts.get("commandMode")
        if mode == "Observer":
            return "Olympus logged us in as observer, check the password"
        if mode == "Red commander":
            return "Olympus logged us in as red commander"
        if opts.get("restrictSpawns"):
            have = (opts.get("spawnPoints") or {}).get("blue")
            if isinstance(have, (int, float)) and have < cost:
                return f"not enough Olympus spawn points, {int(have)} left, {cost} needed"
        return ""

    # -------------------------------------------------------------- the mission thread
    def _flight(self, snap: dict, m: Mission) -> List[tuple]:
        """The flight's units, number 1 first (by unit name, Olympus-N-1, -2)."""
        return sorted((u for u in snap.get("olympus_units") or [] if m.group and u[0] == m.group),
                      key=lambda u: str(u[1]))

    def _find_new_group(self, snap: dict, m: Mission) -> Optional[str]:
        best = None
        mine = self.oly.claimed() if hasattr(self.oly, "claimed") else set()
        for u in snap.get("olympus_units") or []:
            if u[0] in m.known_groups or u[0] in mine:
                continue
            d, _ = dist_brg(m.spawn_ll, (float(u[3]), float(u[4])))
            if d < 15_000 and (best is None or d < best[0]):
                best = (d, u[0])
        return best[1] if best else None

    def _task(self, m: Mission, state: str, reason: str = "") -> None:
        self.tasks.set_state(m.task_id, state, reason)

    def _run(self, m: Mission) -> None:
        with acting_as(_roles.OVERLORD, m.task_id):     # every command this flight's thread sends
            try:
                if not self._join(m):
                    return
                if m.kind.key == "CAS" and self.darkside is not None:
                    self._cas_loop(m)                # Darkside's terminal control (P2-6)
                elif m.kind.key in ("SEAD", "CAS"):
                    self._strike_loop(m)
                else:
                    self._cover_loop(m)
            except Exception as e:                   # never kill the program over a mission
                print(f"[OVERLORD] {m.kind.key} mission error: {type(e).__name__}: {e}")
                m.state = "failed"
                self._task(m, "failed", f"error: {type(e).__name__}")

    def _join(self, m: Mission) -> bool:
        """Waits for the spawned group; True when it's ready to task."""
        deadline = time.monotonic() + APPEAR_TIMEOUT_S
        while time.monotonic() < deadline:                 # also when cancelled: it still has to go home
            m.group = self._find_new_group(self.snapshot(), m)
            if m.group:
                break
            time.sleep(1.0)
        if not m.group:
            self._task(m, "aborted" if m.cancel.is_set() else "failed", "no group appeared")
            if not m.cancel.is_set():
                m.state = "failed"
                ORDERS.record(f"{m.cs} flight", m.kind.key, "failed", "failed to launch: no group appeared")
                self.speak(f"Overlord, {m.cs} flight failed to launch. Check Olympus.")
            return False
        flight = self._flight(self.snapshot(), m)
        m.lead_id = int(flight[0][2])
        self.tasks.attach(m.task_id, m.group, [int(u[2]) for u in flight])
        if hasattr(self.oly, "claim"):
            self.oly.claim(m.group)
        print(f"[OVERLORD] {m.kind.key} flight is {m.group}, lead id {m.lead_id}")
        if m.cancel.is_set():
            self._task(m, "aborted", "cancelled")
            self._send_home(m)
            return False
        hold = m.kind.key == "CAS" and self.darkside is not None   # CAS arrives weapons hold (P2-6)
        ok, why = self.oly.command("setROE", {"ID": m.lead_id, "ROE": 4 if hold else 0})   # hold / weapons free
        if m.cancel.is_set():                            # cancelled while it was being tasked
            self._task(m, "aborted", "cancelled")
            self._send_home(m)
            return False
        ORDERS.record(f"{m.cs} flight", m.kind.key, "submitted",
                      f"{m.group} on task" + ("" if ok else f"; setROE: {why}"))
        m.state = "engaging"
        return True

    # -------------------------------------------------------------- R7: deferred requests wait
    def _wait(self, kind: str, task_id: str, text: str, reason: str) -> None:
        """Keeps a deferred request (one per kind: a newer one replaces it) and starts the recheck thread."""
        with self._lock:
            old = self._waiting.get(kind)
            self._waiting[kind] = {"task_id": task_id, "text": text, "since": self.clock(), "reason": reason}
            start = self._waiter is None or not self._waiter.is_alive()
            if start:
                self._waiter = threading.Thread(target=self._wait_loop, daemon=True, name="overlord-deferred")
        if old is not None and old["task_id"] != task_id:
            self.tasks.set_state(old["task_id"], "failed", "replaced by a newer request")
        if start:
            self._waiter.start()

    def waiting_lines(self) -> List[str]:
        """For "Warlord, status": "CAS request waiting: 2 flights already up, 4 minutes left"."""
        now = self.clock()
        with self._lock:
            items = list(self._waiting.items())
        out = []
        for kind, w in items:
            left = max(0, int(self.defer_wait_s - (now - w["since"])))
            when = f"{left // 60} minute{'s' if left // 60 != 1 else ''}" if left >= 60 else f"{left} seconds"
            out.append(f"{KINDS[kind].said} request waiting: {w['reason']}, {when} left")
        return out

    def _wait_loop(self) -> None:
        while True:
            time.sleep(self.defer_check_s)
            with self._lock:
                items = list(self._waiting.items())
            if not items:
                return
            for kind, w in items:
                try:
                    self._recheck(kind, w)
                except Exception as e:                   # never let one request stop the others
                    print(f"[OVERLORD] Deferred {kind} recheck failed: {e}")

    def _drop(self, kind: str, w: dict) -> bool:
        with self._lock:
            if self._waiting.get(kind) is not w:
                return False
            del self._waiting[kind]
            return True

    def _recheck(self, kind: str, w: dict) -> None:
        k = KINDS[kind]
        who = f"{k.callsign} flight"
        if self.clock() - w["since"] > self.defer_wait_s:
            if self._drop(kind, w):
                self.tasks.set_state(w["task_id"], "failed", f"deferred: {w['reason']}; no approval in time")
                ORDERS.record(who, kind, "failed", f"Warlord deferred: {w['reason']}; the wait ran out")
                if self.warlord is not None:
                    self.say_role(self.warlord.expired_text(kind, w["reason"]), "warlord")
            return
        if stale_reason(self.snapshot()):                # a frozen picture: ask again next time
            return
        if self._busy(kind):                             # one of this kind is already up
            if self._drop(kind, w):
                self.tasks.set_state(w["task_id"], "failed", f"a {k.said} flight is already up")
                self.say_role(f"Overlord, {k.said} request dropped, a {k.said} flight is already up.", "overlord")
            return
        fn = {"SEAD": self.request_sead, "CAS": self.request_cas, "CAP": self.request_cap,
              "ESCORT": self.request_escort}[kind]
        self._req.retry, self._req.text = dict(w, result=None), w["text"]
        try:
            said = fn(w["text"])
            res = self._req.retry["result"]
            w["reason"] = self._req.retry.get("reason", w["reason"])
        finally:
            self._req.retry = None
        if res == "waiting":
            return
        if not self._drop(kind, w):                      # cancelled meanwhile
            return
        if res is None:                                  # the request didn't get as far as Warlord
            self.tasks.set_state(w["task_id"], "failed", "the target is no longer in the picture")
            ORDERS.record(who, kind, "failed", "deferred request: the target is no longer in the picture")
            self.say_role(f"Overlord, {k.said} cancelled, the target is no longer in the picture.", "overlord")
            return
        if res == "approved" and self.warlord is not None:
            self.say_role(self.warlord.now_approved_text(kind), "warlord")
        self._say_said(said)

    def _say_said(self, said) -> None:
        if said is None:                                 # (an empty Said can still carry Warlord's line)
            return
        if str(said):
            self.say_role(str(said), "overlord")
        for text, who in getattr(said, "then", None) or []:
            if text:
                self.say_role(text, who)

    def _cancel_waiting(self, named: List[str], everything: bool, flights_up: bool) -> List[str]:
        """R7: "Overlord, cancel CAS" drops a waiting CAS request too. Returns the kinds dropped."""
        with self._lock:
            kinds = list(self._waiting)
        if named:
            drop = [x for x in kinds if x in named]
        elif everything or (not flights_up and len(kinds) == 1):
            drop = kinds
        else:
            drop = []
        out = []
        for kind in drop:
            with self._lock:
                w = self._waiting.pop(kind, None)
            if w is not None:
                self.tasks.set_state(w["task_id"], "failed", "cancelled while waiting for Warlord")
                ORDERS.record(f"{KINDS[kind].callsign} flight", kind, "failed", "cancelled while deferred")
                out.append(kind)
        return out

    def reset(self) -> None:
        """A new mission: stop following the last one's flights, without calling them lost."""
        with self._lock:
            self._waiting.clear()                        # R7: the old mission's deferred requests
            old, self._missions = list(self._missions), []
        for m in old:
            m.state = "cancelled"
            m.cancel.set()
        self.tasks.reset()                               # unit ids start again

    def _check_flight(self, snap: dict, m: Mission) -> bool:
        """False when the flight is gone. Follows a new lead when the first one is lost."""
        if m.cancel.is_set():
            return False
        flight = self._flight(snap, m)
        if not flight:
            m.state = "lost"
            self._task(m, "lost", "flight down")
            ORDERS.record(f"{m.cs} flight", m.kind.key, "failed", "flight down")
            DEBRIEF.loss(f"{m.cs} flight")
            self.speak(f"Overlord, {m.cs} flight is down.")
            return False
        # Export.lua lists units in no fixed order: only a lead that's gone is replaced
        if all(int(u[2]) != m.lead_id for u in flight):
            m.lead_id = int(flight[0][2])
            self.tasks.attach(m.task_id, m.group, [int(u[2]) for u in flight])
            m.current = None
            print(f"[OVERLORD] {m.cs} lead lost, {flight[0][1]} leads (id {m.lead_id})")
        return True

    def _strike_loop(self, m: Mission) -> None:
        """SEAD / CAS: the targets one at a time."""
        while not m.cancel.is_set():
            snap = self.snapshot()
            if not self._check_flight(snap, m):
                return
            if m.current and target_state(snap, m.current) == "dead":
                m.killed.append(say_type(m.current["type"]))
                DEBRIEF.kill(f"{m.cs} flight", say_type(m.current["type"]))
                left = [r for r in m.targets if r is not m.current and target_state(snap, r) != "dead"]
                self.speak(f"Overlord, {m.cs} reports {say_type(m.current['type'])} destroyed."
                           + (f" {len(left)} to go." if left else ""))
                m.current = None
            if m.current is None:
                m.targets = [r for r in m.targets if target_state(snap, r) != "dead"]
                if not m.targets:
                    m.state = "home"
                    self._task(m, "complete", "area clear")
                    ORDERS.record(f"{m.cs} flight", m.kind.key, "completed", "area clear, returning to base")
                    self.speak(f"Overlord, {m.cs} reports the area clear. Returning to base.")
                    self._send_home(m)
                    return
                m.current = m.targets[0]
                ok, why = self.oly.command("attackUnit", {"ID": m.lead_id, "targetID": m.current["id"]})
                if ok:
                    self._task(m, "attacking", say_type(m.current["type"]))
                zone = re.search(r"inside the (.+?) no-strike zone", why or "") if not ok else None
                if zone:                                 # R1: skip it, the others are still fair game
                    skipped, m.current = m.current, None
                    m.targets = [r for r in m.targets if r is not skipped]
                    self.speak(f"Overlord, {m.cs} skipping the {say_type(skipped['type'])} "
                               f"in the {zone.group(1)} no-strike zone.")
                    continue
                if not ok:
                    m.state = "failed"
                    self._task(m, "failed", f"attackUnit: {why}")
                    ORDERS.record(f"{m.cs} flight", m.kind.key, "failed", f"attackUnit: {why}")
                    self.speak(f"Overlord, {m.cs} can't be tasked, {why}.")
                    return
            if time.monotonic() - m.started > MISSION_TIMEOUT_S:
                m.state = "home"
                self._task(m, "complete", "out of time on station")
                ORDERS.record(f"{m.cs} flight", m.kind.key, "completed", "out of time on station, returning to base")
                self.speak(f"Overlord, {m.cs} is out of time on station. Returning to base.")
                self._send_home(m)
                return
            time.sleep(POLL_S)

    def _ip(self, m: Mission, snap: dict):
        return cas_ip(snap, m.point, m.spawn_ll)

    def _lead_ll(self, snap: dict, m: Mission):
        for u in self._flight(snap, m):
            if int(u[2]) == m.lead_id:
                return float(u[3]), float(u[4])
        return None

    def _to_ip(self, m: Mission) -> None:
        self.oly.command("setPath", {"ID": m.lead_id, "path": [{"lat": m.ip[0], "lng": m.ip[1]}]})

    def _cas_loop(self, m: Mission) -> None:
        """CAS under Darkside (P2-6): weapons hold to the IP, check in, cleared hot only by Darkside,
        target by target; any abort brings it back to the IP."""
        d = self.darkside
        m.ip = self._ip(m, self.snapshot())
        self._to_ip(m)
        d.register(m)
        at_ip, hot, joined = False, False, time.monotonic()
        try:
            while not m.cancel.is_set():
                snap = self.snapshot()
                if not self._check_flight(snap, m):
                    return
                if m.current and target_state(snap, m.current) == "dead":
                    what = say_type(m.current["type"])
                    m.killed.append(what)
                    DEBRIEF.kill(f"{m.cs} flight", what)
                    left = [r for r in m.targets if r is not m.current and target_state(snap, r) != "dead"]
                    d.good_effect(m, what, len(left), m.current.get("id"))
                    m.current = None
                    if hot and left:                     # the next one, if Darkside still clears it
                        ok, why = d.clearance(m, left[0], snap)
                        if ok:
                            m.current = left[0]
                            if not d.attack(m, m.current):
                                hot = False
                        else:
                            d.hold(m, why)
                            d.withheld(m, why)
                if m.current is None:
                    m.targets = [r for r in m.targets if target_state(snap, r) != "dead"]
                    if not m.targets:
                        m.state = "home"
                        self._task(m, "complete", "area clear")
                        ORDERS.record(f"{m.cs} flight", m.kind.key, "completed", "area clear, returning to base")
                        d.area_clear(m)
                        self._send_home(m)
                        return
                    m.current = m.targets[0]
                state = (self.tasks.get(m.task_id).state if self.tasks.get(m.task_id) else "")
                if hot and state == "holding":           # Darkside aborted or withheld: back to the IP
                    hot, at_ip = False, False
                    self._to_ip(m)
                if not hot:
                    if not at_ip:
                        ll = self._lead_ll(snap, m)
                        if ((ll and dist_brg(ll, m.ip)[0] <= IP_REACHED_M)
                                or time.monotonic() - joined > IP_TIMEOUT_S):
                            at_ip = True
                            if state != "holding":
                                self._task(m, "holding", "at the IP")
                            d.check_in(m)
                    if at_ip:
                        ok, why = d.clearance(m, m.current, snap)
                        if ok:
                            hot = d.clear(m, m.current)
                        else:
                            d.withheld(m, why)
                if time.monotonic() - m.started > MISSION_TIMEOUT_S:
                    m.state = "home"
                    self._task(m, "complete", "out of time on station")
                    ORDERS.record(f"{m.cs} flight", m.kind.key, "completed", "out of time on station, returning to base")
                    self.speak(f"Overlord, {m.cs} is out of time on station. Returning to base.")
                    self._send_home(m)
                    return
                time.sleep(POLL_S)
        finally:
            d.unregister(m)

    def _goto_station(self, m: Mission) -> None:
        if self._escort_follows(m):                      # jets: formation on you, not an orbit
            from common.aircraft import profile
            snap = self.snapshot()
            x, y, z = profile(snap).escort_offset
            self.oly.command("followUnit", {"ID": m.lead_id, "targetID": snap.get("self_id"),
                                            "offsetX": x, "offsetY": y, "offsetZ": z})
            return
        self.oly.command("setPath", {"ID": m.lead_id, "path": [{"lat": m.station[0], "lng": m.station[1]}]})

    def _escort_follows(self, m: Mission) -> bool:
        from common.aircraft import profile
        snap = self.snapshot()
        return (m.kind.key == "ESCORT" and profile(snap).escort_follow
                and (snap.get("self_id") if snap.get("self_id") is not None else -1) >= 0)

    def _cover_loop(self, m: Mission) -> None:
        """CAP / escort: hold the station, engage bandits inside the radius."""
        escort = m.kind.key == "ESCORT"
        radius = ESCORT_RADIUS_M if escort else CAP_RADIUS_M
        if escort and self.snapshot().get("my_ll"):
            m.station = tuple(self.snapshot()["my_ll"][:2])
        self._goto_station(m)
        self._task(m, "on_task", "on station")
        while not m.cancel.is_set():
            snap = self.snapshot()
            if not self._check_flight(snap, m):
                return
            me = snap.get("my_ll")
            stale = enemy_lists_stale(snap)              # no commit / break-off from a frozen picture
            centre = (me[0], me[1]) if escort and me else m.station
            if (escort and me and m.current is None and dist_brg(m.station, centre)[0] > ESCORT_MOVE_M
                    and not self._escort_follows(m)):
                m.station = centre
                self._goto_station(m)
            if m.current:
                st = target_state(snap, m.current)
                now = next((b for b in bandits_near(snap, centre, 10 ** 7) if b["id"] == m.current["id"]), None)
                if st == "dead":
                    m.killed.append(say_type(m.current["type"]))
                    DEBRIEF.kill(f"{m.cs} flight", say_type(m.current["type"]))
                    self.speak(f"Overlord, {m.cs} reports splash one {say_type(m.current['type'])}.")
                    m.current = None
                    self._goto_station(m)
                    self._task(m, "on_task", "back on station")
                elif not stale and now is not None and now["d"] > radius + BREAK_OFF_M:
                    self.speak(f"Overlord, {m.cs} breaking off, back on station.")
                    m.current = None
                    self._goto_station(m)
                    self._task(m, "on_task", "broke off")
            if m.current is None and not stale:
                bandits = bandits_near(snap, centre, radius)
                if bandits:
                    b = bandits[0]
                    ok, why = self.oly.command("attackUnit", {"ID": m.lead_id, "targetID": b["id"]})
                    if ok:
                        m.current = b
                        self._task(m, "attacking", say_type(b["type"]))
                        where = ""
                        if me:
                            d, brg = dist_brg((me[0], me[1]), (b["lat"], b["lon"]))
                            where = f", {_dist(d, snap, whole=True)} {compass(brg)} of you"
                        self.speak(f"Overlord, {m.cs} committing on {say_type(b['type'])}{where}.")
            if time.monotonic() - m.started > COVER_TIMEOUT_S:
                m.state = "home"
                self._task(m, "complete", "bingo fuel")
                self.speak(f"Overlord, {m.cs} is bingo fuel. Returning to base.")
                self._send_home(m)
                return
            time.sleep(POLL_S)

    def _send_home(self, m: Mission) -> None:
        if m.lead_id is not None:
            self.oly.command("setPath", {"ID": m.lead_id,
                                         "path": [{"lat": m.spawn_ll[0], "lng": m.spawn_ll[1]}]})

    # -------------------------------------------------------------- picture
    def picture(self) -> str:
        """Enemy aircraft around you, grouped, nearest first (up to three groups)."""
        snap = self.snapshot()
        me = snap.get("my_ll")
        if not me:
            return "Overlord, unable, I don't have your position."
        groups: List[List[dict]] = []
        for b in bandits_near(snap, (me[0], me[1]), PICTURE_RANGE_M):
            for g in groups:
                if dist_brg((g[0]["lat"], g[0]["lon"]), (b["lat"], b["lon"]))[0] < GROUP_M:
                    g.append(b)
                    break
            else:
                groups.append([b])
        if not groups:
            return "Overlord, picture clean."
        parts = []
        for g in groups[:3]:
            lead = g[0]
            d, brg = dist_brg((me[0], me[1]), (lead["lat"], lead["lon"]))
            names = [say_type(b["type"]) for b in g]
            kinds = names[0] if len(set(names)) == 1 else _say_list(names)
            size = "single" if len(g) == 1 else f"{len(g)} ship"
            parts.append(f"{size} {kinds}, {_dist(d, snap, whole=True)} {compass(brg)} of you, "
                         f"{_alt(lead.get('alt', 0.0), snap)}")
        more = f" {len(groups) - 3} more groups beyond." if len(groups) > 3 else ""
        n = len(groups)
        return f"Overlord, picture, {n} group{'s' if n > 1 else ''}. " + ". ".join(parts) + "." + more

    # -------------------------------------------------------------- status / cancel
    def active(self) -> List[Mission]:
        """Flights launching or engaging (for the kneeboard map)."""
        return self._active()

    def _active(self) -> List[Mission]:
        with self._lock:
            # cancel flag too: a cancel landing while _join tasks the flight could be overwritten
            # by its state = "engaging" (seen 29 Oct as an intermittent test_cancel_which failure)
            return [m for m in self._missions if m.state in ("launching", "engaging") and not m.cancel.is_set()]

    def _status_of(self, m: Mission) -> str:
        task = m.kind.said + (" " + m.where if m.kind.key != "ESCORT" else "")
        if m.state == "launching":
            return f"{m.cs} flight is launching, tasked {task}."
        done = f" {len(m.killed)} destroyed so far." if m.killed else ""
        if m.kind.key in ("SEAD", "CAS"):
            tgt = f" Engaging the {say_type(m.current['type'])}." if m.current else ""
        else:
            tgt = f" Engaging a {say_type(m.current['type'])}." if m.current else " No bandits."
        t = self.tasks.get(m.task_id)                    # R2: the tasking record says why it's holding
        if t is not None and t.state == "holding":
            why = "" if t.reason in ("", "at the IP") else f", {t.reason}"
            return f"{m.cs} flight holding at the IP{why}, tasked {task}.{done}"
        if t is not None and t.state == "cleared":
            tgt = tgt or " Cleared hot."
        return f"{m.cs} flight on task, {task}.{tgt}{done}"

    def status(self) -> str:
        act = self._active()
        if not act:
            return "Overlord, no flights tasked."
        return "Overlord, " + " ".join(self._status_of(m) for m in act)

    def cancel(self, named: List[str], everything: bool = False) -> str:
        act = self._active()
        dropped = self._cancel_waiting(named, everything, bool(act))         # R7
        waited = (f"{_say_list([KINDS[x].said for x in dropped])} request cancelled." if dropped else "")
        if named:
            act = [m for m in act if m.kind.key in named]
        if not act:
            if waited:
                return f"Overlord copies. {waited[0].upper() + waited[1:]}"
            if named:
                return f"Overlord, no {KINDS[named[0]].said} flight tasked."
            return "Overlord, no flights tasked."
        elif len(act) > 1 and not everything:
            return ("Overlord, say which: " + _say_list([m.kind.said for m in act]).replace(" and ", " or ")
                    + ", or cancel all.")
        for m in act:
            m.cancel.set()
            m.state = "cancelled"
            self._task(m, "aborted", "cancelled")
            ORDERS.record(f"{m.cs} flight", m.kind.key, "completed", "cancelled, returning to base")
            with acting_as(_roles.OVERLORD, m.task_id):   # on the reply thread, not the flight's
                if m.kind.key == "CAS" and self.darkside is not None and m.lead_id is not None:
                    self.oly.command("setROE", {"ID": m.lead_id, "ROE": 4})   # anyone may stop an attack
                self._send_home(m)
        names = _say_list([f"{m.cs} flight" for m in act])
        return f"Overlord copies. {names}, mission cancelled, returning to base." + (f" {waited[0].upper() + waited[1:]}"
                                                                                     if waited else "")


# ------------------------------------------------------------------ pop-up warnings
class AirWarning:
    """Overlord calls enemy aircraft on his own when they come within WARN_M of you ("pop-up
    group"), and again if one closes inside CLOSE_M. Each aircraft is called once per stage; one
    that's been gone (or outside 1.5 x WARN_M) for FORGET_S can be called again. Runs on its own
    daemon thread (main.py), from Export.lua's enemy aircraft list: works without Olympus."""
    WARN_M, CLOSE_M, FORGET_S, POLL_S = 40_000, 15_000, 300.0, 4.0

    def __init__(self, snapshot: Callable[[], dict], speak: Callable[[str], None],
                 active: Callable[[], bool] = lambda: True):
        self.snapshot, self.speak, self.active = snapshot, speak, active
        self._warned: Dict[int, float] = {}            # aircraft id -> last time it was inside the area
        self._closed: Dict[int, float] = {}
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True, name="overlord-air-warning")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.POLL_S):
            try:
                self.check()
            except Exception as e:
                print(f"[OVERLORD] air warning: {type(e).__name__}: {e}")

    CLOSING_MPS = 5.0

    @staticmethod
    def _closing(b: dict, me, snap: dict) -> str:
        """", closing" / ", opening" only when the tactical picture measured it (two scans, step T7);
        nothing when it can't say. Without a picture: the old wording (", closing")."""
        from tactical import link
        from tactical.models import dcs_key
        if not link.active():
            return ", closing"
        vel = snap.get("vel")
        rr = link.closure(dcs_key(b["id"]), me, vel[:2] if vel else None)
        if rr is None:
            return ""
        if rr <= -AirWarning.CLOSING_MPS:
            return ", closing"
        if rr >= AirWarning.CLOSING_MPS:
            return ", opening"
        return ""

    def check(self, now: Optional[float] = None) -> List[str]:
        """One look. Returns what was said (also for tests)."""
        now = time.monotonic() if now is None else now
        snap = self.snapshot()
        me = snap.get("my_ll")
        if not me or snap.get("telemetry_age", 0.0) > 5.0 or not self.active():
            return []
        me = (me[0], me[1])
        from common.aircraft import profile             # jets: 50 / 20 nm, helicopters: 40 / 15 km
        prof = profile(snap)
        warn_m, close_m = prof.air_warn_m or self.WARN_M, prof.air_close_m or self.CLOSE_M
        near = bandits_near(snap, me, warn_m * 1.5)
        for b in near:                                  # still around: keep their "called" marks fresh
            if b["id"] in self._warned:
                self._warned[b["id"]] = now
            if b["id"] in self._closed and b["d"] <= close_m * 1.5:
                self._closed[b["id"]] = now
        for table in (self._warned, self._closed):
            for oid in [o for o, t in table.items() if now - t > self.FORGET_S]:
                del table[oid]
        said = []
        new = [b for b in near if b["d"] <= warn_m and b["id"] not in self._warned]
        groups: List[List[dict]] = []
        for b in new:
            for g in groups:
                if dist_brg((g[0]["lat"], g[0]["lon"]), (b["lat"], b["lon"]))[0] < GROUP_M:
                    g.append(b)
                    break
            else:
                groups.append([b])
        for g in groups:
            lead = g[0]
            names = [say_type(b["type"]) for b in g]
            kinds = names[0] if len(set(names)) == 1 else _say_list(names)
            size = "single" if len(g) == 1 else f"{len(g)} ship"
            d, brg = dist_brg(me, (lead["lat"], lead["lon"]))
            said.append(f"Overlord, pop-up group, {size} {kinds}, {_dist(d, snap, whole=True)} "
                        f"{compass(brg)} of you, {_alt(lead.get('alt', 0.0), snap)}.")
            for b in g:
                self._warned[b["id"]] = now
                if b["d"] <= close_m:
                    self._closed[b["id"]] = now          # already close: one call is enough
        for b in near:
            if b["d"] <= close_m and b["id"] not in self._closed:
                d, brg = dist_brg(me, (b["lat"], b["lon"]))
                said.append(f"Overlord, bandit, {say_type(b['type'])}, {_dist(d, snap, whole=True)} "
                            f"{compass(brg)} of you{self._closing(b, me, snap)}.")
                self._closed[b["id"]] = now
                self._warned[b["id"]] = now
        for s in said:
            print(f"[OVERLORD]: '{s}'")
            self.speak(s)
        return said
