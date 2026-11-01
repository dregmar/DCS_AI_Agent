"""
instructor/quality.py - is there enough good data to judge a signal?

Per signal, over the exercise window [t0, t1] (model time):
    rate      distinct valid samples per second
    gap_max   longest stretch without a valid sample (the window's edges count)
    coverage  share of the window not lost in gaps longer than GAP_OK_S
Status:
    good          rate >= good_rate_hz, gap_max <= GAP_MAX_S, coverage >= COVERAGE_MIN
    degraded      the same but min_rate_hz <= rate < good_rate_hz: judged, confidence capped at medium
                  (today's Export.lua sends 4 Hz: every hover is "degraded" until item I5)
    insufficient  anything else: criteria on this signal are not judged (never a deduction, never an award)
"""
import math
from typing import Callable, Dict, Iterable, List, Optional

from instructor.models import Sample, SignalQuality

GAP_OK_S = 1.0          # gaps up to this are normal sample spacing
GAP_MAX_S = 1.0         # one gap longer than this and the signal isn't judged
COVERAGE_MIN = 0.9

GETTERS: Dict[str, Callable[[Sample], Optional[float]]] = {
    "pos": lambda s: s.pos,
    "vel": lambda s: s.vel,
    "agl_ft": lambda s: s.agl_ft,
    "vs_fpm": lambda s: s.vs_fpm,
    "hdg": lambda s: s.hdg,
    "ias_kt": lambda s: s.ias_kt,
    "pitch": lambda s: s.pitch,
    "roll": lambda s: s.roll,
    "g": lambda s: s.gmax if s.gmax is not None else s.g,
    "ll": lambda s: s.ll,
    "fuel_lbs": lambda s: s.fuel_lbs,
}


def assess(samples: List[Sample], signal: str, t0: float, t1: float,
           min_rate_hz: float, good_rate_hz: float) -> SignalQuality:
    get = GETTERS[signal]
    times = [s.t for s in samples if get(s) is not None and t0 <= s.t <= t1]
    dur = t1 - t0
    if dur <= 0 or len(times) < 2:
        return SignalQuality(signal, "insufficient", 0.0, dur, 0.0, f"no {_name(signal)} data")
    edges = [times[0] - t0] + [b - a for a, b in zip(times, times[1:])] + [t1 - times[-1]]
    gap_max = max(edges)
    lost = sum(max(0.0, g - GAP_OK_S) for g in edges)
    coverage = max(0.0, (dur - lost) / dur)
    span = times[-1] - times[0]
    rate = (len(times) - 1) / span if span > 0 else 0.0
    note = _resolution_note(samples, signal)
    if gap_max > GAP_MAX_S:
        return SignalQuality(signal, "insufficient", rate, gap_max, coverage,
                             f"{_name(signal)} dropped out for {gap_max:.1f} seconds")
    if coverage < COVERAGE_MIN:
        return SignalQuality(signal, "insufficient", rate, gap_max, coverage,
                             f"{_name(signal)} only covered {coverage:.0%} of the exercise")
    if rate < min_rate_hz:
        return SignalQuality(signal, "insufficient", rate, gap_max, coverage,
                             f"telemetry too slow, {rate:.1f} samples a second (need {min_rate_hz:g})")
    if rate < good_rate_hz:
        return SignalQuality(signal, "degraded", rate, gap_max, coverage,
                             f"{rate:.1f} samples a second (full confidence from {good_rate_hz:g})" + note)
    return SignalQuality(signal, "good", rate, gap_max, coverage, note.lstrip("; "))


def assess_all(samples: List[Sample], signals: Iterable[str], t0: float, t1: float,
               min_rate_hz: float, good_rate_hz: float) -> Dict[str, SignalQuality]:
    return {s: assess(samples, s, t0, t1, min_rate_hz, good_rate_hz) for s in sorted(set(signals))}


def _resolution_note(samples: List[Sample], signal: str) -> str:
    """Export.lua rounds AGL to whole feet (30 Oct probe): say so, it limits fine height scoring."""
    if signal != "agl_ft":
        return ""
    vals = [s.agl_ft for s in samples if s.agl_ft is not None]
    if vals and all(math.isfinite(v) and v == int(v) for v in vals):
        return "; height in whole feet"
    return ""


def _name(signal: str) -> str:
    return {"pos": "position", "agl_ft": "height", "hdg": "heading", "vel": "velocity",
            "vs_fpm": "vertical speed", "ias_kt": "airspeed", "ll": "position", "fuel_lbs": "fuel",
            "g": "g", "pitch": "pitch", "roll": "bank"}.get(signal, signal)
