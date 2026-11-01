"""
tactical/clock.py - when the world lists were true, and which mission they belong to (design section 3).

Two clocks, never mixed except here:
    DCS model time     when DCS saw something (Export.lua's "t" and "scan_t"); restarts at 0 per mission
    time.monotonic()   when this program received it

Export.lua rescans the world once a model second and every packet (4 Hz) carries the last scan's model
time. state_store.snapshot() gives that scan's model time ("scan_t") and its age ("enemy_lists_age",
model lag + time since the packet). So the scan was true at  seen_mono = now - enemy_lists_age.

Generations: a mission load (the comms hook's "LOADED", via mission_loaded()) or model time going
backwards starts a new picture. DCS object ids restart per mission, so track keys are only unique
within a generation. Both signs come with every mission load, in either order, so two within MERGE_S
seconds count as one.

Old Export.lua (no "t" / "scan_t"): the lists' ages are unknown; ages fall back to the packet's
receive time and are flagged (age_known False).

Used only by the tactical service thread (single owner): no lock.
"""
import math
from dataclasses import dataclass
from typing import Any, Optional

# Model time must go back by more than this to count as a restart (float noise, paused frames).
RESTART_BACKSTEP_S = 0.5
# The comms hook's "LOADED" and Export.lua's model time restarting both mark the same mission load and
# arrive within a fraction of a second, in either order: two signs this close are one new picture.
MERGE_S = 10.0

NEW, SAME, OLDER, NO_SCAN, AGE_UNKNOWN = "new", "same", "older", "no_scan", "age_unknown"


@dataclass(frozen=True)
class ScanTime:
    kind: str                     # NEW / SAME / OLDER / NO_SCAN / AGE_UNKNOWN
    seen_model: Optional[float]   # the scan's model time (None: unknown)
    seen_mono: Optional[float]    # when the scan was true, in time.monotonic() (None: no data at all)
    age_s: Optional[float]        # how old the lists are now (None: unknown)
    age_known: bool
    generation: int
    restarted: bool               # this snapshot started a new generation


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


class PictureClock:
    def __init__(self):
        self.generation = 0
        self._last_t: Optional[float] = None
        self._last_scan: Optional[float] = None
        self._gen_mono: Optional[float] = None       # when the current generation began (None: not known)
        self.merged = 0                               # second signs of a load already counted

    def _new_generation(self, now_mono: Optional[float]) -> bool:
        """Start a new generation, unless one began less than MERGE_S ago (the same mission load)."""
        if now_mono is not None and self._gen_mono is not None and 0.0 <= now_mono - self._gen_mono < MERGE_S:
            self.merged += 1
            return False
        self.generation += 1
        self._gen_mono = now_mono
        return True

    def mission_loaded(self, now_mono: Optional[float] = None) -> int:
        """A mission was loaded or restarted (comms hook "LOADED"): a new picture, unless model time already
        restarted within MERGE_S (pass now_mono to merge them). Returns the generation."""
        if self._new_generation(now_mono):
            self._last_t = None
            self._last_scan = None
        return self.generation

    def scan_time(self, snap: dict, now_mono: float) -> ScanTime:
        """Classify the snapshot's world lists. now_mono: time.monotonic() taken right after the snapshot."""
        restarted = False
        t = _num(snap.get("t_model"))
        if t is not None:
            if self._last_t is not None and t < self._last_t - RESTART_BACKSTEP_S:
                restarted = self._new_generation(now_mono)
                self._last_scan = None                    # model time restarted: scan times start again
            self._last_t = t
        gen = self.generation
        age = snap.get("enemy_lists_age")
        if age is None:                                   # old Export.lua: no scan times
            tel = _num(snap.get("telemetry_age"))
            seen = (now_mono - tel) if tel is not None else None
            return ScanTime(AGE_UNKNOWN, None, seen, None, False, gen, restarted)
        age = _num(age) if not (isinstance(age, float) and math.isinf(age)) else None
        scan_t = _num(snap.get("scan_t"))
        if age is None or (scan_t is not None and scan_t < 0):
            return ScanTime(NO_SCAN, None, None, None, True, gen, restarted)
        seen_mono = now_mono - max(0.0, age)
        if scan_t is None:                                # age known, scan time not passed on: treat as new
            return ScanTime(NEW, None, seen_mono, age, True, gen, restarted)
        if self._last_scan is None or scan_t > self._last_scan:
            kind = NEW
            self._last_scan = scan_t
        elif scan_t == self._last_scan:
            kind = SAME
        else:
            kind = OLDER
        return ScanTime(kind, scan_t, seen_mono, age, True, gen, restarted)
