"""
tactical/link.py - how the rest of the program asks the tactical picture about one unit (step T7).

    link.status(key)                 -> "alive" | "dead" | "unknown" | None
    link.position(key)               -> (lat, lon, alt_m or None) | None
    link.closure(key, me_ll, me_vel) -> metres per second (negative = closing) | None
    link.active()                    -> is a current picture available?

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


class ServiceLink:
    """The provider: a TacticalService's picture while its last update is younger than max_age."""
    def __init__(self, service, max_age: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.service, self.max_age, self.clock = service, max_age, clock

    def picture(self):
        last = getattr(self.service, "last_tick_mono", None)
        if last is None or self.clock() - last > self.max_age:
            return None
        return self.service.picture()
