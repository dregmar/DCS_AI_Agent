"""
common/user_waypoints.py - waypoints you name in flight, and the objective.

    "Mark our current position as waypoint Alpha"     · "mark here as Bravo"
    "Set grid 38T LM 7218 4018 as waypoint Bravo"     · "set grid Lima Mike 721 401 as Charlie"
        (zone and square can be left out: they're taken from where you are)
    "The objective is waypoint 3"                     · "set the objective at grid 7218 4018"
    "Where is Alpha?" · "how far to Bravo?" · "bearing to the objective" · "steer to Charlie"
        -> "Alpha, 11 o'clock, steer 353, 8.4 miles, 5 minutes."
    "List waypoints" · "delete waypoint Alpha" · "clear all waypoints"

Names are the NATO alphabet (Alpha ... Zulu) and "objective". They're kept in
data/user_waypoints.json until you clear them (DCS names every single-player mission "tempMission",
so there's no telling missions apart). Overlord ("CAS on waypoint Alpha", "SEAD on the objective")
and the Outcasts ("Outcast 2, go to waypoint Bravo") use them too. Thread-safe.
"""
import json
import math
import os
import re
import threading
from typing import Optional, Tuple

from common import geo, nav, position

try:
    import config
    STORE_FILE = os.path.join(config.BASE_DIR, "data", "user_waypoints.json")
except Exception:
    STORE_FILE = None

_NATO = ("alpha alfa bravo charlie delta echo foxtrot golf hotel india juliet juliett kilo lima mike november "
         "oscar papa quebec romeo sierra tango uniform victor whiskey xray x-ray yankee zulu").split()
NAME = r"(" + "|".join(sorted(_NATO, key=len, reverse=True)) + r"|objective)"
_WPW = r"(?:(?:the\s+)?(?:waypoint|way point|point|mark)\s+)?"

_MARK = re.compile(r"\b(?:mark|save|store|set|record|remember|drop)\b.*?\b(?:position|location|here|this spot|"
                   r"this point)\b.*?\bas\s+(?:the\s+)?" + _WPW + NAME + r"\b|"
                   r"\b(?:mark|set|save|drop)\s+(?:the\s+)?" + _WPW + NAME + r"\s+(?:here|at\s+(?:our|my|the)\s+"
                   r"(?:current\s+)?(?:position|location))\b")
_GRID_AS = re.compile(r"\bgrid\b(.*?)\bas\s+(?:the\s+)?" + _WPW + NAME + r"\b")
_NAME_GRID = re.compile(r"\b(?:set|make|put|mark)?\s*(?:the\s+)?" + _WPW + NAME + r"\s+(?:at|to|is|on)?\s*(?:grid\b)(.*)$")
_OBJ_WP = re.compile(r"\bobjective\s+(?:is\s+)?(?:at\s+)?(?:waypoint|way point|wp)\s+(\d+|[a-z]+)\b|"
                     r"\bset\s+(?:waypoint|way point|wp)\s+(\d+|[a-z]+)\s+as\s+(?:the\s+)?objective\b")
_NAV = re.compile(r"\b(?:where(?:'s| s| is)|how far|distance|bearing|steer|heading|direction|vector|take me|"
                  r"navigate|time|eta|range)\b.*?\b" + _WPW + NAME + r"\b")
_DELETE = re.compile(r"\b(?:delete|remove|cancel|clear|erase)\s+(?:the\s+)?" + _WPW + NAME + r"\b")
_CLEAR = re.compile(r"\b(?:clear|delete|erase|remove)\s+(?:all\s+)?(?:the\s+|our\s+)?(?:named\s+|marked\s+)?"
                    r"(?:way ?points|points|marks)\b")
_LIST = re.compile(r"\b(?:list|what are)\s+(?:the\s+|our\s+)?(?:named\s+|marked\s+)?(?:way ?points|points|marks)\b|"
                   r"\bwhat (?:way ?points|points) (?:do we have|are (?:set|marked))\b")
_NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "to": 2, "too": 2, "for": 4, "tree": 3}


def canon(name: str) -> str:
    n = name.lower().replace("-", "")
    return {"alfa": "Alpha", "juliett": "Juliet", "xray": "X-ray"}.get(n, n.capitalize())


def named_in(text: str) -> Optional[str]:
    """'on waypoint Alpha' / 'over the objective' -> 'Alpha' / 'Objective', else None."""
    m = re.search(r"\b(?:on|at|over|near|to|around)\s+(?:the\s+)?" + _WPW + NAME + r"\b", (text or "").lower())
    return canon(m.group(1)) if m else None


class UserWaypoints:
    def __init__(self, store_file: Optional[str] = STORE_FILE):
        self._lock = threading.Lock()
        self.store_file = store_file
        self._pts = {}               # name -> {"ll": [lat, lon], "grid": str} or {"wp": n}
        if store_file:
            try:
                with open(store_file, encoding="utf-8") as f:
                    self._pts = dict(json.load(f).get("points") or {})
            except Exception:
                pass

    def _save(self):
        if not self.store_file:
            return
        try:
            os.makedirs(os.path.dirname(self.store_file), exist_ok=True)
            with open(self.store_file, "w", encoding="utf-8") as f:
                json.dump({"points": self._pts}, f, indent=1)
        except OSError as e:
            print(f"[WAYPOINTS] Can't save: {e}")

    def set(self, name: str, rec: dict) -> None:
        with self._lock:
            self._pts[name] = rec
            self._save()

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            r = self._pts.get(name)
            return dict(r) if r else None

    def names(self):
        with self._lock:
            return list(self._pts)

    def delete(self, name: str) -> bool:
        with self._lock:
            ok = self._pts.pop(name, None) is not None
            self._save()
            return ok

    def clear(self) -> int:
        with self._lock:
            n = len(self._pts)
            self._pts = {}
            self._save()
            return n

    def ll(self, name: str, snap: dict) -> Optional[Tuple[float, float]]:
        """The named point's lat/lon (a route waypoint is placed with geo.FIT)."""
        r = self.get(name)
        if not r:
            return None
        if "ll" in r:
            return float(r["ll"][0]), float(r["ll"][1])
        p = nav.find_waypoint(snap.get("route") or [], int(r["wp"]))
        return geo.FIT.to_ll(float(p[1]), float(p[2])) if p else None

    # ------------------------------------------------------------ reports
    def report(self, name: str, snap: dict) -> str:
        r = self.get(name)
        if not r:
            return f"No waypoint {name}." if name != "Objective" else "No objective set. Say objective is waypoint 3, or give a grid."
        label = name if name != "Objective" else "Objective"
        if "wp" in r:                                          # a route waypoint: map coordinates
            p = nav.find_waypoint(snap.get("route") or [], int(r["wp"]))
            if p is None or not snap.get("pos"):
                return f"{label} is waypoint {r['wp']}, but I can't place it."
            rel = nav.relative(snap["pos"], p[1], p[2], snap.get("hdg"))
            label = f"{label}, waypoint {r['wp']}"
        else:
            me = snap.get("my_ll")
            if not me:
                return "No position data."
            dn = (r["ll"][0] - me[0]) * 111_320.0
            de = (r["ll"][1] - me[1]) * 111_320.0 * math.cos(math.radians(me[0]))
            rel = nav.relative((0.0, 0.0), dn, de, snap.get("hdg"))
        if rel["dist_m"] < 300:
            return f"{label}, overhead."
        parts = [label]
        if rel["clock"]:
            parts.append(f"{rel['clock']} o'clock")
        parts.append(f"steer {rel['brg']:03.0f}")
        parts.append(nav.say_distance(rel["dist_m"]))
        ete = nav.ete_minutes(rel["dist_m"], nav.ground_speed_kt(snap.get("vel")))
        if ete is not None:
            parts.append(f"{ete:.0f} minutes" if ete >= 1.5 else "about a minute")
        return ", ".join(parts) + "."

    # ------------------------------------------------------------ voice
    def handle(self, text: str, snap: dict) -> Optional[str]:
        """What to say, or None if this isn't about named waypoints."""
        t = re.sub(r"[^a-z0-9' -]+", " ", (text or "").lower())
        t = re.sub(r"\s+", " ", t).strip()
        me = snap.get("my_ll")
        m = _OBJ_WP.search(t)
        if m:
            tok = m.group(1) or m.group(2)
            n = int(tok) if tok.isdigit() else _NUMW.get(tok)
            if n is None:
                return "Say again the waypoint number."
            if nav.find_waypoint(snap.get("route") or [], n) is None:
                return f"No waypoint {n} in the route."
            self.set("Objective", {"wp": n})
            return f"Objective is waypoint {n}. " + self.report("Objective", snap)
        m = _GRID_AS.search(t)
        grid_text, name = (m.group(1), m.group(2)) if m else (None, None)
        if m is None:
            m2 = _NAME_GRID.search(t)
            if m2:
                grid_text, name = m2.group(2), m2.group(1)
        if grid_text is not None:
            name = canon(name)
            got = position.parse_grid(grid_text, me)
            if got is None:
                return f"Say again the grid for {name}."
            lat, lon, as_heard = got
            self.set(name, {"ll": [lat, lon], "grid": as_heard})
            label = "Objective" if name == "Objective" else f"Waypoint {name}"
            gzd, sq, e, n = as_heard.split()
            said = f"{label} set, grid {gzd[:-1]} {position.NATO[gzd[-1]]}, {position.NATO[sq[0]]} " \
                   f"{position.NATO[sq[1]]}, {' '.join(e)}, {' '.join(n)}."
            rep = self.report(name, snap)
            return said + (" " + rep.split(", ", 1)[1] if ", " in rep else "")
        m = _MARK.search(t)
        if m:
            name = canon(m.group(1) or m.group(2))
            if not me or snap.get("telemetry_age", 0.0) > 10.0:
                return "Unable, no position."
            grid = position.mgrs_text(me[0], me[1], 5)
            self.set(name, {"ll": [me[0], me[1]], "grid": grid})
            return f"Waypoint {name} marked. " + position.say_mgrs(me[0], me[1])
        if _CLEAR.search(t):
            n = self.clear()
            return f"{n} waypoint{'s' if n != 1 else ''} cleared." if n else "No named waypoints."
        m = _DELETE.search(t)
        if m and not re.search(r"\bas\b", t):
            name = canon(m.group(1))
            return f"Waypoint {name} deleted." if self.delete(name) else f"No waypoint {name}."
        if _LIST.search(t):
            names = self.names()
            if not names:
                return "No named waypoints."
            return "Waypoints: " + ", ".join(names) + "."
        m = _NAV.search(t)
        if m:
            return self.report(canon(m.group(1)), snap)
        return None


USER_WAYPOINTS = UserWaypoints()
