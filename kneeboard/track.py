"""
kneeboard/track.py - where you've flown since the mission started, for the kneeboard map (agreed
30 Oct 2026).

A recorder thread samples your position from the state store once a second (the telemetry loop is
untouched) and keeps a point only after you've moved MIN_STEP_M (helicopters 50 m, jets 300 m) or
turned TURN_DEG since the last kept point, so straight legs cost almost nothing and turns stay round.
The whole mission is kept: past MAX_POINTS the oldest half is thinned (every other point) rather
than dropped. A jump of more than JUMP_M between two samples (a respawn, a new slot) starts a new
segment instead of drawing a line across the map. A mission load or restart discards everything.

On / off: config KNEEBOARD_TRACK sets the default; "kneeboard, track on / off" shows or hides it for
this session only. Recording carries on while it's hidden.

Thread-safe: one lock; view() returns copies and a version, so the feed only changes when points
are added (or the switch / a reset changes it).
"""
import math
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

MIN_STEP_M = {"helo": 50.0, "jet": 300.0}
TURN_DEG = 10.0
JUMP_M = 5000.0               # between two samples ~1 s apart: faster than anything flies
MAX_POINTS = 2000
SAMPLE_S = 1.0
TELEMETRY_FRESH_S = 5.0


def _dist_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    n = (b[0] - a[0]) * 111_320.0
    e = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(n, e)


def _turn(a: float, b: float) -> float:
    return abs((b - a + 180.0) % 360.0 - 180.0)


class Track:
    def __init__(self, snapshot: Callable[[], dict], kind: Optional[Callable[[dict], str]] = None,
                 visible: bool = True, max_points: int = MAX_POINTS):
        self.snapshot = snapshot
        self.kind = kind or _kind
        self.max_points = max_points
        self._lock = threading.Lock()
        self._segs: List[List[Tuple[float, float]]] = []
        self._last: Optional[Tuple[float, float, float]] = None     # lat, lon, heading of the last kept point
        self._prev: Optional[Tuple[float, float]] = None            # the last sample (kept or not)
        self._visible = bool(visible)
        self._version = 0
        self._running = False

    # ------------------------------------------------------------ recording
    def sample(self, snap: Optional[dict] = None) -> bool:
        """One look. True when a point was added."""
        snap = self.snapshot() if snap is None else snap
        ll = snap.get("my_ll")
        if not ll or (snap.get("telemetry_age") or 0.0) > TELEMETRY_FRESH_S:
            return False
        here = (float(ll[0]), float(ll[1]))
        hdg = float(snap.get("hdg") or 0.0)
        step = MIN_STEP_M.get(self.kind(snap), MIN_STEP_M["helo"])
        with self._lock:
            prev, self._prev = self._prev, here
            if prev is not None and _dist_m(prev, here) > JUMP_M:      # respawn / new slot: new line
                self._segs.append([here])
                self._last = (here[0], here[1], hdg)
                self._version += 1
                return True
            if self._last is None or not self._segs:
                self._segs.append([here])
                self._last = (here[0], here[1], hdg)
                self._version += 1
                return True
            d = _dist_m(self._last[:2], here)
            if d < step and not (_turn(self._last[2], hdg) >= TURN_DEG and d >= step / 5):
                return False
            self._segs[-1].append(here)
            self._last = (here[0], here[1], hdg)
            if sum(len(s) for s in self._segs) > self.max_points:
                self._thin()
            self._version += 1
            return True

    def _thin(self) -> None:
        """Every other point of the oldest half goes; each segment keeps its ends."""
        total = sum(len(s) for s in self._segs)
        budget, out = total // 2, []
        for seg in self._segs:
            if budget <= 0 or len(seg) <= 2:
                out.append(seg)
                budget -= len(seg)
                continue
            n = min(len(seg), budget)
            head = seg[:n]
            kept = head[0:1] + head[1:-1][1::2] + head[-1:] if n > 2 else head
            out.append(kept + seg[n:])
            budget -= n
        self._segs = out

    def reset(self) -> None:
        """A new mission (or a restart): everything before it goes."""
        with self._lock:
            self._segs, self._last, self._prev = [], None, None
            self._version += 1

    # ------------------------------------------------------------ the switch
    @property
    def visible(self) -> bool:
        with self._lock:
            return self._visible

    @visible.setter
    def visible(self, on: bool) -> None:
        with self._lock:
            if self._visible != bool(on):
                self._visible = bool(on)
                self._version += 1

    # ------------------------------------------------------------ for the page
    def points(self) -> int:
        with self._lock:
            return sum(len(s) for s in self._segs)

    def view(self) -> Dict:
        with self._lock:
            return {"on": self._visible, "v": self._version,
                    "segs": [[[round(p[0], 5), round(p[1], 5)] for p in s] for s in self._segs] if self._visible else []}

    # ------------------------------------------------------------ thread
    def start(self) -> "Track":
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="kb-track").start()
        return self

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            time.sleep(SAMPLE_S)
            try:
                self.sample()
            except Exception as e:
                print(f"[TRACK] {type(e).__name__}: {e}")


def _kind(snap: dict) -> str:
    from common.aircraft import profile
    return "jet" if profile(snap).kind == "jet" else "helo"
