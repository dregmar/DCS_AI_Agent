"""
instructor/geometry.py - distances and bearings from lat/lon (flat earth: fine over a few km).

Waypoints and FARPs come as lat/lon, and bearings from lat/lon are true, like Export's heading. The
map-frame velocity's direction is grid north, which differs by the map's convergence (the 30 Oct probe
saw why that matters), so tracks here are taken from successive lat/lon positions instead.
"""
import math
from typing import Optional, Sequence, Tuple

M_PER_DEG = 111_320.0


def ne(ll: Tuple[float, float], ref: Tuple[float, float]) -> Tuple[float, float]:
    """(north, east) metres of ll from ref."""
    return ((ll[0] - ref[0]) * M_PER_DEG,
            (ll[1] - ref[1]) * M_PER_DEG * math.cos(math.radians(ref[0])))


def dist_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    n, e = ne(b, a)
    return math.hypot(n, e)


def bearing_deg(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """True bearing from a to b."""
    n, e = ne(b, a)
    return math.degrees(math.atan2(e, n)) % 360.0


def angle_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def compass8(bearing: float) -> str:
    return ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")[
        int(((bearing % 360.0) + 22.5) // 45.0) % 8]


def tracks(lls: Sequence[Optional[Tuple[float, float]]], times: Sequence[float], span_s: float = 1.0,
           min_m: float = 3.0) -> list:
    """True ground track at each sample from the position span_s earlier (None while too slow or unknown)."""
    out, k = [], -1            # k: the latest sample at least span_s before sample i
    for i, (p, t) in enumerate(zip(lls, times)):
        while k + 1 < i and times[k + 1] <= t - span_s:
            k += 1
        q = lls[k] if k >= 0 and times[k] <= t - span_s else None
        out.append(bearing_deg(q, p) if p is not None and q is not None and dist_m(q, p) >= min_m else None)
    return out
