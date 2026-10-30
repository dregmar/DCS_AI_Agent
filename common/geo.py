"""
common/geo.py
DCS map coordinates (x = north, z = east, metres) <-> latitude / longitude, for DCS Olympus, which
places everything by lat/lon. Export.lua gives both for your own position (every second) and for
each contact; the mission route only comes as x / z. Over a few tens of km DCS's projection is
very nearly linear, so a least-squares fit of the latest pairs converts route points to within a
few metres.
"""
import math
import threading
from typing import List, Optional, Tuple

import numpy as np

MIN_SPREAD_M = 1000.0          # the known points must cover at least this much ground
MAX_SAMPLES = 400


class LocalFit:
    def __init__(self):
        self._lock = threading.Lock()
        self._pts: List[Tuple[float, float, float, float]] = []      # (x, z, lat, lon)
        self._coef = None

    def add(self, x: float, z: float, lat: float, lon: float) -> None:
        if not (math.isfinite(lat) and math.isfinite(lon)) or (lat == 0 and lon == 0):
            return
        with self._lock:
            for px, pz, _, _ in self._pts[-50:]:
                if abs(px - x) < 200 and abs(pz - z) < 200:
                    return                                  # nothing new here
            self._pts.append((x, z, lat, lon))
            if len(self._pts) > MAX_SAMPLES:
                self._pts = self._pts[-MAX_SAMPLES:]
            self._coef = None

    def add_snapshot(self, snap: dict) -> None:
        """Your position and every contact that carries both forms."""
        pos, ll = snap.get("pos"), snap.get("my_ll")
        if pos and ll:
            self.add(pos[0], pos[1], ll[0], ll[1])
        if pos:
            for c in snap.get("contacts") or []:
                if c.get("lat") and c.get("dist_m") is not None and c.get("brg") is not None:
                    b = math.radians(c["brg"])
                    self.add(pos[0] + c["dist_m"] * math.cos(b), pos[1] + c["dist_m"] * math.sin(b),
                             c["lat"], c["lon"])

    def _fit(self):
        if self._coef is not None:
            return self._coef
        if len(self._pts) < 3:
            return None
        a = np.array(self._pts)
        if np.ptp(a[:, 0]) < MIN_SPREAD_M and np.ptp(a[:, 1]) < MIN_SPREAD_M:
            return None
        m = np.column_stack([a[:, 0], a[:, 1], np.ones(len(a))])
        if np.linalg.matrix_rank(m) < 3:
            return None
        lat, *_ = np.linalg.lstsq(m, a[:, 2], rcond=None)
        lon, *_ = np.linalg.lstsq(m, a[:, 3], rcond=None)
        self._coef = (lat, lon)
        return self._coef

    def to_ll(self, x: float, z: float) -> Optional[Tuple[float, float]]:
        with self._lock:
            c = self._fit()
        if c is None:
            return None
        v = np.array([x, z, 1.0])
        return float(v @ c[0]), float(v @ c[1])


def offset_ll(lat: float, lon: float, north_m: float, east_m: float) -> Tuple[float, float]:
    """A point a few hundred metres away (spherical Earth: fine at this distance)."""
    return (lat + north_m / 111_320.0,
            lon + east_m / (111_320.0 * max(0.05, math.cos(math.radians(lat)))))


def body_to_ne(hdg_deg: float, forward_m: float, right_m: float) -> Tuple[float, float]:
    h = math.radians(hdg_deg or 0.0)
    return (forward_m * math.cos(h) - right_m * math.sin(h), forward_m * math.sin(h) + right_m * math.cos(h))


FIT = LocalFit()
