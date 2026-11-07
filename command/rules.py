"""
command/rules.py - what Warlord and Darkside both judge by (Phase 2, P2-5 / P2-6).

    load_block(path)                     the hand-written "warlord" block of data/campaign_briefing.json
    zone_at(zones, lat, lon)             the first zone (no-strike / priority area) a point is in, or None
    friendlies_near(snap, lat, lon, r)   your side's ground / naval units within r metres (Export.lua's
                                         own-side list, tactical step T8 I-T3); None if Export doesn't send it.
                                         R4: from the tactical picture's FRIENDLY tracks while it's current
                                         (stale / lost friendlies count at their last known position)

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
DANGER_CLOSE_AGL_M = 300.0          # R6: your aircraft this low near the target holds a CAS run
CAUTION_M = 5000.0                  # R6: MANPADS / AAA this close to a CAS target: Warlord's caution


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


def position_of(snap: dict, target_id) -> Optional[tuple]:
    """(lat, lon) of a DCS object from the tactical picture, else from Export's lists; None if unknown."""
    try:
        oid = int(target_id)
    except (TypeError, ValueError):
        return None
    try:
        from tactical import link
        from tactical.models import dcs_key
        p = link.position(dcs_key(oid))
        if p is not None:
            return float(p[0]), float(p[1])
    except Exception:
        pass
    for key, i_id, i_lat in (("enemy_ground", 1, 2), ("air_defence", 1, 2), ("enemy_air", 1, 2),
                             ("olympus_enemy", 2, 3), ("neutral_units", 1, 2)):
        for r in snap.get(key) or []:
            try:
                if int(r[i_id]) == oid:
                    return float(r[i_lat]), float(r[i_lat + 1])
            except (IndexError, TypeError, ValueError):
                continue
    for c in snap.get("contacts") or []:
        if isinstance(c, dict) and c.get("id") == oid and c.get("lat") is not None:
            return float(c["lat"]), float(c["lon"])
    return None


def no_strike_zone_of(snap: dict, zones: List[dict], target_id) -> Optional[str]:
    """The name of the no-strike zone a target is in, or None (unknown position = None)."""
    pos = position_of(snap, target_id)
    if pos is None or not zones:
        return None
    z = zone_at(zones, pos[0], pos[1])
    return z["name"] if z else None


def alt_of(snap: dict, target_id) -> Optional[float]:
    """The target's altitude (m MSL) from the picture, else Export's lists; None if unknown."""
    try:
        oid = int(target_id)
    except (TypeError, ValueError):
        return None
    try:
        from tactical import link
        from tactical.models import dcs_key
        pos = link.position(dcs_key(oid))
        if pos is not None and pos[2] is not None:
            return float(pos[2])
    except Exception:
        pass
    for key in ("enemy_ground", "air_defence"):
        for r in snap.get(key) or []:
            try:
                if int(r[1]) == oid and len(r) >= 5:
                    return float(r[4])
            except (TypeError, ValueError, IndexError):
                continue
    return None


def own_low_near(snap: dict, lat: float, lon: float, radius: float = DANGER_CLOSE_M,
                 agl_max: float = DANGER_CLOSE_AGL_M, outcasts: Optional[Dict[int, str]] = None,
                 target_alt: Optional[float] = None) -> List[dict]:
    """R6: you, your wingmen and your Outcasts within radius of the point (horizontally) and below agl_max
    above the ground, nearest first: [{"who": "you" | "your wingman" | "Outcast 2", "d"}]. A jet passing
    high overhead isn't counted. Heights nobody reports count as low (never called clear):
      you          Export's AGL
      wingmen      no height in Export's list: your own AGL (they fly with you), else low
      Outcasts     their altitude above the target's (the ground there), else low"""
    out = []
    agl_ft = snap.get("agl_ft")
    my_agl = float(agl_ft) * 0.3048 if isinstance(agl_ft, (int, float)) else None
    me = snap.get("my_ll")
    if me and (my_agl is None or my_agl <= agl_max):
        d = dist_m(lat, lon, float(me[0]), float(me[1]))
        if d <= radius:
            out.append({"who": "you", "d": d})
    self_id = snap.get("self_id")
    for w in snap.get("wing") or []:
        try:
            wid, wlat, wlon = int(w[1]), float(w[2]), float(w[3])
        except (TypeError, ValueError, IndexError):
            continue
        if wid < 0 or wid == self_id or (my_agl is not None and my_agl > agl_max):
            continue
        d = dist_m(lat, lon, wlat, wlon)
        if d <= radius:
            out.append({"who": "your wingman", "d": d})
    for r in snap.get("olympus_units") or []:
        try:
            uid, ulat, ulon, ualt = int(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError, IndexError):
            continue
        name = (outcasts or {}).get(uid)
        if name is None or (target_alt is not None and ualt - target_alt > agl_max):
            continue
        d = dist_m(lat, lon, ulat, ulon)
        if d <= radius:
            out.append({"who": name, "d": d})
    return sorted(out, key=lambda x: x["d"])


def friendlies_near(snap: dict, lat: float, lon: float, radius: float = DANGER_CLOSE_M) -> Optional[List[dict]]:
    """Your side's ground and naval units within radius of the point, nearest first. None when
    Export.lua doesn't send its own-side list (an older Export.lua): then nobody can tell."""
    rows = snap.get("friendly_units")
    if rows is None:
        return None
    from tactical import link
    pic = link.friendlies_near(lat, lon, radius)
    if pic is not None:                                      # R4: the picture decides while it's current
        return [{"type": r["type"], "id": r["id"], "d": r["d"], "status": r["status"], "lat": r["lat"],
                 "lon": r["lon"]} for r in pic]
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
            out.append({"type": str(typ), "id": oid, "d": d, "lat": float(rlat), "lon": float(rlon)})
    return sorted(out, key=lambda x: x["d"])
