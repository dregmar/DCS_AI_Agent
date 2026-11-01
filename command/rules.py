"""
command/rules.py - what Warlord and Darkside both judge by (Phase 2, P2-5 / P2-6).

    load_block(path)                     the hand-written "warlord" block of data/campaign_briefing.json
    zone_at(zones, lat, lon)             the first zone (no-strike / priority area) a point is in, or None
    friendlies_near(snap, lat, lon, r)   your side's ground / naval units within r metres (Export.lua's
                                         own-side list, tactical step T8 I-T3); None if Export doesn't send it

The "warlord" block (all optional; see data/examples/warlord_block.sample.json):

    "warlord": {
      "objectives": ["Destroy the SA-11 battery near Sochi", ...],
      "roe": "Weapons free on armour north of the river. No strikes inside towns.",
      "no_strike": [{"name": "Gudauta town", "lat": 43.10, "lon": 40.62, "radius_m": 2500}, ...],
      "priority_areas": [{"name": "Objective Bravo", "lat": 43.2, "lon": 40.9, "radius_m": 8000}, ...]
    }

Pure apart from reading the file once; a missing or broken block is an empty one, never an error.
"""
import json
import math
from typing import Any, Dict, List, Optional

DANGER_CLOSE_M = 1000.0


def _zone(z: Any) -> Optional[dict]:
    try:
        return {"name": str(z.get("name") or "a zone"), "lat": float(z["lat"]), "lon": float(z["lon"]),
                "radius_m": max(0.0, float(z.get("radius_m", 1000.0)))}
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def parse_block(data: Any) -> Dict[str, Any]:
    """{"objectives": [str], "roe": str, "no_strike": [zone], "priority_areas": [zone]} from the
    campaign briefing's data (the whole file's dict). Unreadable parts are left out."""
    w = data.get("warlord") if isinstance(data, dict) else None
    w = w if isinstance(w, dict) else {}
    obj = w.get("objectives")
    return {"objectives": [str(o) for o in obj if str(o).strip()] if isinstance(obj, list) else [],
            "roe": str(w.get("roe") or "").strip(),
            "no_strike": [z for z in map(_zone, w.get("no_strike") or []) if z],
            "priority_areas": [z for z in map(_zone, w.get("priority_areas") or []) if z]}


def load_block(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_block(json.load(f))
    except (OSError, ValueError) as e:
        print(f"[WARLORD] No warlord block read from {path}: {type(e).__name__}")
        return parse_block({})


def dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    n = (lat2 - lat1) * 111_320.0
    e = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(n, e)


def zone_at(zones: List[dict], lat: float, lon: float) -> Optional[dict]:
    for z in zones or []:
        if dist_m(z["lat"], z["lon"], lat, lon) <= z["radius_m"]:
            return z
    return None


def friendlies_near(snap: dict, lat: float, lon: float, radius: float = DANGER_CLOSE_M) -> Optional[List[dict]]:
    """Your side's ground and naval units within radius of the point, nearest first. None when
    Export.lua doesn't send its own-side list (an older Export.lua): then nobody can tell."""
    rows = snap.get("friendly_units")
    if rows is None:
        return None
    out = []
    for r in rows:
        try:
            typ, oid, rlat, rlon, _alt, cat = r
        except (TypeError, ValueError):
            continue
        if cat == "a":
            continue
        d = dist_m(lat, lon, float(rlat), float(rlon))
        if d <= radius:
            out.append({"type": str(typ), "id": oid, "d": d})
    return sorted(out, key=lambda x: x["d"])
