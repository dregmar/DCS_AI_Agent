"""
instructor/metrics/turn.py - observations for hover.turn (a 360 on the spot), from samples only.

Drift and height come from the stationary hover's own measures over the whole window (the reference is
what you held as the clock started). The turn itself is followed on the unwrapped heading: the sum of
the heading changes sample to sample (each within +-180), so 359 -> 001 counts as 2 degrees.

    turn.radius_p95   95th percentile of the distance from the start point (m)
    turn.height_rms   RMS height deviation from the start height (ft)
    turn.height_max   largest height deviation, either way (ft)
    turn.rate_sd      standard deviation of the turn rate (deg/s, over steps of at least RATE_STEP_S)
                      from the first TURN_START_DEG to 360
    turn.time         seconds from the first TURN_START_DEG to 360
    turn.overshoot    how far past 360 the heading went in the STOP_S after it (deg; 0 = stopped on it)

The turn observations are only made when the 360 was completed inside the window.
"""
import statistics
from typing import Dict, List, Optional, Tuple

from instructor import catalog
from instructor.metrics import hover
from instructor.models import Observation, Sample

RATE_STEP_S = 1.0


def unwrapped(samples: List[Sample]) -> List[Tuple[float, float]]:
    """(t, heading turned so far, signed: + right / clockwise) for the samples that have a heading."""
    out: List[Tuple[float, float]] = []
    prev, acc = None, 0.0
    for s in samples:
        if s.hdg is None:
            continue
        if prev is not None:
            acc += hover.angle_diff(s.hdg, prev)
        prev = s.hdg
        out.append((s.t, acc))
    return out


def progress(samples: List[Sample]) -> Dict[str, Optional[float]]:
    """Where the turn is: t_start (first TURN_START_DEG), t_360, direction (+1 right / -1 left)."""
    u = unwrapped(samples)
    t_start = t_360 = None
    sign = 0
    for t, a in u:
        if t_start is None and abs(a) >= catalog.TURN_START_DEG:
            t_start, sign = t, (1 if a > 0 else -1)
        if t_start is not None and sign * a >= 360.0:
            t_360 = t
            break
    return {"t_start": t_start, "t_360": t_360, "sign": sign, "turned": (sign * u[-1][1]) if u and sign else 0.0}


def observe(samples: List[Sample], t0: float, box: Dict[str, float], judged: Dict[str, bool],
            context: Optional[dict] = None) -> Dict[str, Observation]:
    out: Dict[str, Observation] = {}
    h = hover.observe(samples, t0, box, judged, context)
    for a, b in (("hover.radius_p95", "turn.radius_p95"), ("hover.height_rms", "turn.height_rms"),
                 ("hover.height_max", "turn.height_max")):
        if a in h:
            o = h[a]
            out[b] = Observation(b, o.value, o.units, o.kind, o.signals, o.evidence)

    p = progress(samples)
    if p["t_360"] is None:
        return out
    sign, t_s, t_e = p["sign"], p["t_start"], p["t_360"]
    word = "right" if sign > 0 else "left"
    u = unwrapped(samples)
    out["turn.time"] = Observation("turn.time", t_e - t_s, "s", "derived", ("hdg",),
                                   {"t_start": t_s, "t_360": t_e, "dir": word})

    seg = [(t, a) for t, a in u if t_s <= t <= t_e]
    rates, last = [], None
    for t, a in seg:
        if last is None:
            last = (t, a)
        elif t - last[0] >= RATE_STEP_S:
            rates.append(sign * (a - last[1]) / (t - last[0]))
            last = (t, a)
    if len(rates) >= 3:
        out["turn.rate_sd"] = Observation("turn.rate_sd", statistics.pstdev(rates), "deg/s", "derived", ("hdg",),
                                          {"n": len(rates), "mean": statistics.fmean(rates), "dir": word,
                                           "min": min(rates), "max": max(rates)})

    after = [sign * a - 360.0 for t, a in u if t_e <= t <= t_e + catalog.TURN_STOP_S]
    if after:
        out["turn.overshoot"] = Observation("turn.overshoot", max(0.0, max(after)), "deg", "derived", ("hdg",),
                                            {"t_360": t_e, "stop_s": catalog.TURN_STOP_S, "dir": word})
    return out
