"""
common/nav.py
Navigation help for any aircraft, from Export.lua's own position, ground velocity and mission
route (LoGetRoute). DCS world coordinates: x = north, z = east, metres.

    "next waypoint"        -> "Waypoint 3, 2 o'clock, steer 045, 4.1 miles, 3 minutes."
    "waypoint 2" / "home"  -> same for that waypoint (home = the first route point)
    "fuel to home"         -> needs a fuel reading (Huey); see fuel_to()
"""
import math
from typing import Optional, Sequence, Tuple

M_PER_NM = 1852.0
MS_PER_KT = 0.514444


def body_velocity(vel, hdg_deg) -> Optional[Tuple[float, float]]:
    """World ground velocity (north, east, up) -> (forward, right) in m/s for this heading."""
    if not vel or hdg_deg is None or hdg_deg < 0:
        return None
    h = math.radians(hdg_deg)
    vn, ve = vel[0], vel[1]
    return vn * math.cos(h) + ve * math.sin(h), -vn * math.sin(h) + ve * math.cos(h)


def ground_speed_kt(vel) -> Optional[float]:
    if not vel:
        return None
    return math.hypot(vel[0], vel[1]) / MS_PER_KT


def relative(pos, north: float, east: float, hdg_deg) -> Optional[dict]:
    """Where a point is from us: distance, true bearing, clock, and (forward, right) metres."""
    if not pos:
        return None
    dn, de = north - pos[0], east - pos[1]
    dist = math.hypot(dn, de)
    brg = math.degrees(math.atan2(de, dn)) % 360
    out = {"dist_m": dist, "brg": brg, "clock": None, "fwd": None, "right": None}
    if hdg_deg is not None and hdg_deg >= 0:
        rel = (brg - hdg_deg) % 360
        h = int(round(rel / 30.0)) % 12
        out["clock"] = 12 if h == 0 else h
        r = math.radians(hdg_deg)
        out["fwd"] = dn * math.cos(r) + de * math.sin(r)
        out["right"] = -dn * math.sin(r) + de * math.cos(r)
    return out


def say_distance(m: float) -> str:
    nm = m / M_PER_NM
    if nm < 0.5:
        return f"{round(m, -1):.0f} meters"
    return f"{nm:.1f} miles" if nm < 10 else f"{nm:.0f} miles"


def find_waypoint(route: Sequence, number: Optional[int]):
    """route: [(n, north, east)]. number None = home (first point)."""
    if not route:
        return None
    if number is None:
        return route[0]
    for p in route:
        if int(p[0]) == number:
            return p
    return None


def home_point(snap: dict):
    """The first route point, or where this flight started when there's no route."""
    route = snap.get("route") or []
    if route:
        return route[0], "Home"
    sp = snap.get("start_pos")
    if sp:
        return (0, sp[0], sp[1]), "Start point"
    return None, "Home"


def ete_minutes(dist_m: float, gs_kt: Optional[float]) -> Optional[float]:
    if not gs_kt or gs_kt < 15:
        return None
    return dist_m / (gs_kt * MS_PER_KT) / 60.0


def waypoint_report(snap: dict, which) -> str:
    """which: 'next', 'home' or a waypoint number."""
    if snap.get("telemetry_age", float("inf")) > 3.0 or not snap.get("pos"):
        return "No position data."
    route = snap.get("route") or []
    if which == "home":
        p, name = home_point(snap)
        if p is None:
            return "No route and no start point."
    elif not route:
        return "No route loaded. Home is where we started."
    elif which == "next":
        number = snap.get("wp_next", -1)
        p = find_waypoint(route, number) if number is not None and number >= 0 else None
        if p is None:
            return "No active waypoint."
        name = f"Waypoint {int(p[0])}"
    else:
        p = find_waypoint(route, int(which))
        if p is None:
            return f"No waypoint {which}."
        name = f"Waypoint {int(p[0])}"
    r = relative(snap["pos"], p[1], p[2], snap.get("hdg"))
    parts = [name]
    if r["dist_m"] < 300:
        return f"{name}, overhead."
    if r["clock"]:
        parts.append(f"{r['clock']} o'clock")
    parts.append(f"steer {r['brg']:03.0f}")
    parts.append(say_distance(r["dist_m"]))
    ete = ete_minutes(r["dist_m"], ground_speed_kt(snap.get("vel")))
    if ete is not None:
        parts.append(f"{ete:.0f} minutes" if ete >= 1.5 else "about a minute")
    return ", ".join(parts) + "."


def fuel_to(snap: dict, fuel_lbs: Optional[float], burn_lbs_hr: float, reserve_lbs: float,
            which="home", cruise_kt: float = 90.0) -> str:
    """'Home is 12 miles, about 110 pounds at 90 knots. You'll land with 640.'"""
    if fuel_lbs is None:
        return "No fuel reading."
    if not snap.get("pos"):
        return "No position data."
    if which == "home":
        p, home_name = home_point(snap)
        if p is None:
            return "No route and no start point."
    else:
        p, home_name = find_waypoint(snap.get("route") or [], int(which)), ""
        if p is None:
            return f"No waypoint {which}."
    r = relative(snap["pos"], p[1], p[2], snap.get("hdg"))
    gs = ground_speed_kt(snap.get("vel"))
    speed = gs if gs and gs > 40 else cruise_kt
    need = r["dist_m"] / (speed * MS_PER_KT) / 3600.0 * burn_lbs_hr
    left = fuel_lbs - need
    name = home_name if which == "home" else f"Waypoint {int(p[0])}"
    out = (f"{name} is {say_distance(r['dist_m'])}, about {need:.0f} pounds at {speed:.0f} knots. "
           f"You'd land with {max(left, 0):.0f}.")
    if left < reserve_lbs:
        out += " That's below reserve."
    return out


def bingo(snap: dict, fuel_lbs: Optional[float], burn_lbs_hr: float, reserve_lbs: float,
          cruise_kt: float = 90.0) -> Optional[bool]:
    """True when the fuel left after flying home would be under the reserve (None = unknown)."""
    if fuel_lbs is None or not snap.get("pos"):
        return None
    p, _ = home_point(snap)
    if p is None:
        return None
    r = relative(snap["pos"], p[1], p[2], snap.get("hdg"))
    need = r["dist_m"] / (cruise_kt * MS_PER_KT) / 3600.0 * burn_lbs_hr
    return fuel_lbs - need < reserve_lbs
