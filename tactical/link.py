"""
tactical/link.py - how the rest of the program asks the tactical picture about one unit (step T7).

    link.status(key)                 -> "alive" | "dead" | "unknown" | None
    link.position(key)               -> (lat, lon, alt_m or None) | None
    link.closure(key, me_ll, me_vel) -> metres per second (negative = closing) | None
    link.active()                    -> is a current picture available?

R4 (approved 2 Nov, built 4 Nov): the roles' area checks read the picture too. Each returns None when there
is no current picture (the caller then uses Export's raw lists as before), else a list nearest first of
{"type", "id", "lat", "lon", "d", "status", ...}:
    link.friendlies_near(lat, lon, r)     FRIENDLY ground / naval tracks (never aircraft or Olympus flights,
                                          whose category is unknown), any status but GONE: a friendly the
                                          picture can't vouch for (stale, lost) still counts at its last
                                          known position - never called clear
    link.threats_near(lat, lon, r)        the picture's threat list (hostile, not GONE or LOST), + "category"
                                          ("radar SAM", "AAA" ...) and "w" (weight)
    link.hostile_ground_near(lat, lon, r) HOSTILE ground tracks that are ACTIVE (JTAC targets: only units
                                          the picture can vouch for are offered), + "alt"

key = "dcs:<object id>" (tactical.models.dcs_key). None always means "the picture can't say": the picture
isn't running, isn't current (older than max_age), or doesn't hold that unit. Callers then use their own
rule exactly as before, so tests and old behaviour need no picture at all.

    "alive"    ACTIVE in the latest fresh scan
    "dead"     GONE: absent from 3 fresh scans while inside coverage (tactical/tracks.py)
    "unknown"  STALE, OUT_OF_COVERAGE or LOST: can't tell (never called dead)

main.py sets the provider once (set_provider(ServiceLink(TACTICAL))). Read-only: nothing here changes the
picture or acts. Thread-safe: the provider reference is swapped under a lock; the picture it reads is
immutable.
"""
import math
import threading
import time
from typing import Callable, Optional, Tuple

_lock = threading.Lock()
_provider = None


def set_provider(provider) -> None:
    global _provider
    with _lock:
        _provider = provider


def _get():
    with _lock:
        return _provider


def active() -> bool:
    p = _get()
    return p is not None and p.picture() is not None


def _track(key: Optional[str]):
    p = _get()
    if p is None or not key:
        return None
    pic = p.picture()
    if pic is None:
        return None
    return next((t for t in pic.tracks if t.key == key), None)


def status(key: Optional[str]) -> Optional[str]:
    t = _track(key)
    if t is None:
        return None
    v = t.status.value
    return "alive" if v == "active" else "dead" if v == "gone" else "unknown"


def position(key: Optional[str]) -> Optional[Tuple[float, float, Optional[float]]]:
    """Where the unit is now (or was last seen), from the picture."""
    t = _track(key)
    if t is None or t.position is None:
        return None
    return t.position.lat, t.position.lon, t.position.alt_m


def closure(key: Optional[str], me_ll, me_vel=None) -> Optional[float]:
    """Range rate from the unit's measured velocity (two scans) and yours: negative = closing. None when
    the picture has no velocity for it (one fix only, or after a gap)."""
    t = _track(key)
    if t is None or t.velocity is None or t.position is None or not me_ll:
        return None
    n = (t.position.lat - me_ll[0]) * 111_320.0
    e = (t.position.lon - me_ll[1]) * 111_320.0 * math.cos(math.radians((t.position.lat + me_ll[0]) / 2))
    d = math.hypot(n, e)
    if d < 1.0:
        return None
    vn, ve = t.velocity.north_mps, t.velocity.east_mps
    if me_vel:
        vn, ve = vn - float(me_vel[0]), ve - float(me_vel[1])
    return (n * vn + e * ve) / d


_AIR = frozenset({"air", "air_fixed", "air_rotary"})
_GROUND = frozenset({"ground", "surface", "structure"})
# 4 Nov flight: Overlord's Olympus flights are "unknown" category in the picture, so "not air" counted the
# Hog as friendly ground forces and Warlord denied CAS danger close. Friendlies are only what DCS reports as
# ground or naval (Export's own-side list), as the raw rule was.
_SURFACE = frozenset({"ground", "naval", "surface", "structure"})


def _picture():
    p = _get()
    return p.picture() if p is not None else None


def _dcs_id(key: str) -> Optional[int]:
    try:
        return int(str(key).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _row(t, d: float) -> dict:
    return {"type": t.type_name or t.display or "unknown", "id": _dcs_id(t.key), "lat": t.position.lat,
            "lon": t.position.lon, "d": d, "status": t.status.value, "track": t.track_id}


def _near(pic, lat: float, lon: float, radius: float, keep) -> list:
    from tactical.sources import dist_m
    out = []
    for t in pic.tracks:
        if t.position is None or not keep(t):
            continue
        d = dist_m((lat, lon), (t.position.lat, t.position.lon))
        if d <= radius:
            out.append(_row(t, d))
    return sorted(out, key=lambda r: r["d"])


def friendlies_near(lat: float, lon: float, radius: float) -> Optional[list]:
    pic = _picture()
    if pic is None:
        return None
    return _near(pic, lat, lon, radius, lambda t: t.affiliation.value == "friendly"
                 and t.category.value in _SURFACE and t.status.value != "gone")


def threats_near(lat: float, lon: float, radius: float) -> Optional[list]:
    pic = _picture()
    if pic is None:
        return None
    by_id = {a.track_id: a for a in pic.threats}
    out = _near(pic, lat, lon, radius, lambda t: t.track_id in by_id)
    for r in out:
        a = by_id[r["track"]]
        r["category"], r["w"] = a.category, a.weight
    return out


def hostile_ground_near(lat: float, lon: float, radius: float) -> Optional[list]:
    pic = _picture()
    if pic is None:
        return None
    out = _near(pic, lat, lon, radius, lambda t: t.affiliation.value == "hostile"
                and t.category.value in _GROUND and t.status.value == "active")
    alts = {t.track_id: (t.position.alt_m if t.position else None) for t in pic.tracks}
    for r in out:
        r["alt"] = alts.get(r["track"])
    return out


class ServiceLink:
    """The provider: a TacticalService's picture while its last update is younger than max_age."""
    def __init__(self, service, max_age: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.service, self.max_age, self.clock = service, max_age, clock

    def picture(self):
        last = getattr(self.service, "last_tick_mono", None)
        if last is None or self.clock() - last > self.max_age:
            return None
        return self.service.picture()
