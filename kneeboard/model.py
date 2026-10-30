"""
kneeboard/model.py - what the kneeboard map shows, built from a state snapshot. Pure functions (no
threads, no I/O), so they're cheap to call 5 times a second and easy to test.

    build_map(snap, ctx) -> dict (JSON-ready)
    ring_m(type_name)    -> approximate engagement radius in metres for air defence, or None

ctx (main.py) supplies what isn't in the snapshot: Outcasts, Overlord's flights, named waypoints,
the JTAC target, and lat/long for DCS x/z route points.

Threat rings are APPROXIMATE (typical maximum engagement range by system), drawn so you can see where
not to go; they're labelled "approx" on the page. TODO: Olympus's unit database, if it lists ranges.
"""
import re
from typing import Callable, Dict, List, Optional

FT_PER_M = 3.28084
MAX_GROUND = 120
MAX_AIR = 40

# (pattern on the DCS type name, radius km) - first match wins. Typical max engagement ranges.
_RINGS = [
    (r"s-300|sa-10|5p85|40b6|64h6", 75), (r"buk|sa-11|9a310|9s18|9s470", 35), (r"s-125|sa-3|snr", 25),
    (r"s-75|sa-2|sam sa-2", 40), (r"kub|sa-6|2p25|1s91", 25), (r"patriot", 70), (r"hawk", 40),
    (r"nasams", 25), (r"tor|sa-15|9a331", 12), (r"osa|sa-8|9a33", 10), (r"tunguska|2s6|sa-19", 8),
    (r"roland", 8), (r"rapier", 7), (r"hq-7", 12), (r"pantsir", 18),
    (r"strela-10|sa-13|9a35", 5), (r"strela-1|sa-9|9p31", 4), (r"avenger|stinger|igla|manpad|sa-18|sa-24", 5),
    (r"chaparral", 6), (r"s-60|57mm", 6), (r"gepard", 4), (r"zsu-23|shilka", 2.5), (r"zsu-57", 4),
    (r"zu-23|vulcan|m163", 2), (r"flak|bofors|ks-19", 5),
]


def ring_m(type_name: str, is_air_defence: Optional[Callable[[str], bool]] = None) -> Optional[float]:
    t = (type_name or "").lower()
    for rx, km in _RINGS:
        if re.search(rx, t):
            return km * 1000.0
    if is_air_defence is not None and is_air_defence(type_name):
        return 5000.0                              # air defence DCS knows about, system not in the list
    return None


def _ll(lat, lon) -> Optional[List[float]]:
    try:
        return [round(float(lat), 6), round(float(lon), 6)]
    except (TypeError, ValueError):
        return None


def build_map(snap: dict, ctx) -> Dict:
    me_ll = snap.get("my_ll")
    me = None
    if me_ll:
        me = {"lat": round(me_ll[0], 6), "lon": round(me_ll[1], 6), "hdg": round(snap.get("hdg") or 0.0, 1),
              "alt_ft": round(snap.get("alt_ft") or 0.0), "kts": round(snap.get("speed_kts") or 0.0),
              "ac": snap.get("aircraft") or ""}
    route = []
    for p in snap.get("route") or []:
        try:
            ll = ctx.xz_to_ll(float(p[1]), float(p[2]))
        except Exception:
            ll = None
        if ll:
            route.append({"n": int(p[0]), "lat": round(ll[0], 6), "lon": round(ll[1], 6)})

    seen, ground = set(), []
    is_ad = getattr(ctx, "is_air_defence", None)
    rows = [(r, True) for r in snap.get("air_defence") or []] + [(r, False) for r in snap.get("enemy_ground") or []]
    for r, from_ad in rows:
        try:
            name, oid, lat, lon = str(r[0]), int(r[1]), float(r[2]), float(r[3])
        except (TypeError, ValueError, IndexError):
            continue
        if oid in seen:
            continue
        seen.add(oid)
        ring = ring_m(name, is_ad)
        ground.append({"id": oid, "type": name, "lat": round(lat, 6), "lon": round(lon, 6),
                       "ad": bool(from_ad or ring), "ring_m": ring})
    if me_ll:
        ground.sort(key=lambda g: (g["lat"] - me_ll[0]) ** 2 + (g["lon"] - me_ll[1]) ** 2)
    air = []
    for a in (snap.get("enemy_air") or [])[:MAX_AIR]:
        try:
            air.append({"id": int(a[1]), "type": str(a[0]), "lat": round(float(a[2]), 6),
                        "lon": round(float(a[3]), 6), "alt_ft": round(float(a[4]) * FT_PER_M)})
        except (TypeError, ValueError, IndexError):
            continue
    return {
        "units": "nm" if ctx.imperial(snap) else "km",
        "me": me,
        "route": route,
        "wp_next": snap.get("wp_next"),
        "named": ctx.named_waypoints(snap),
        "outcasts": ctx.outcasts(snap),
        "flights": ctx.flights(snap),
        "ground": ground[:MAX_GROUND],
        "air": air,
        "jtac": ctx.jtac(snap),
    }
