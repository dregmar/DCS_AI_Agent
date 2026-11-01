"""
instructor/metrics/hover.py - observations for the stationary hover, from samples only.

Reference = what you held as the clock started: the median of the first REF_S seconds of position,
height and heading. Every observation is derived from measured Export.lua fields (pos in metres,
agl_ft in whole feet, hdg in degrees); none is inferred.

    hover.radius_p95    95th percentile of the horizontal distance from the reference point (m)
    hover.height_rms    root-mean-square height deviation from the reference height (ft)
    hover.height_max    largest height deviation, either way (ft)
    hover.heading_p95   95th percentile of the heading deviation, either way (deg)
    hover.in_box        share of samples inside the box on every axis that can be judged (%)
"""
import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

from instructor.models import Observation, Sample

REF_S = 1.0


def percentile(xs: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    s = sorted(xs)
    k = max(1, math.ceil(p / 100.0 * len(s)))
    return s[k - 1]


def angle_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _circular_median(hdgs: List[float]) -> float:
    """Median heading that survives 359/001: median of differences from the first."""
    base = hdgs[0]
    return (base + statistics.median(angle_diff(h, base) for h in hdgs)) % 360.0


def reference(samples: List[Sample], t0: float) -> Dict[str, Optional[float]]:
    first = [s for s in samples if s.t <= t0 + REF_S] or samples[:1]
    pos = [s.pos for s in first if s.pos]
    agl = [s.agl_ft for s in first if s.agl_ft is not None]
    hdg = [s.hdg for s in first if s.hdg is not None]
    return {"n": statistics.median(p[0] for p in pos) if pos else None,
            "e": statistics.median(p[1] for p in pos) if pos else None,
            "agl": statistics.median(agl) if agl else None,
            "hdg": _circular_median(hdg) if hdg else None}


def _worst(pairs: List[Tuple[float, float]]) -> Tuple[float, float]:
    """(value, t) of the largest value; earliest on a tie."""
    return max(pairs, key=lambda vt: (vt[0], -vt[1]))


END_S = 2.0            # the last END_S seconds give where you ended up (trend)
STEADY_SHARE = 0.6     # ended at least this share of the worst excursion away: a steady trend, not wandering


def _end_mean(samples: List[Sample], get) -> Optional[float]:
    if not samples:
        return None
    t_end = samples[-1].t
    vals = [get(s) for s in samples if s.t >= t_end - END_S and get(s) is not None]
    return statistics.fmean(vals) if vals else None


def observe(samples: List[Sample], t0: float, box: Dict[str, float], judged: Dict[str, bool],
            context: Optional[dict] = None) -> Dict[str, Observation]:
    """judged: which signals passed the quality gate (pos / agl_ft / hdg); the box only uses those.
    Evidence also carries the trend: where you ended up versus the worst moment (a steady drift south /
    climb, or wandering about the point), for the spoken feedback."""
    ref = reference(samples, t0)
    out: Dict[str, Observation] = {}

    radius = [(math.hypot(s.pos[0] - ref["n"], s.pos[1] - ref["e"]), s.t) for s in samples
              if s.pos and ref["n"] is not None]
    if radius:
        w, tw = _worst(radius)
        en = _end_mean(samples, lambda s: s.pos[0] - ref["n"] if s.pos else None)
        ee = _end_mean(samples, lambda s: s.pos[1] - ref["e"] if s.pos else None)
        net = math.hypot(en, ee) if en is not None and ee is not None else 0.0
        out["hover.radius_p95"] = Observation(
            "hover.radius_p95", percentile([r for r, _ in radius], 95), "m", "derived", ("pos",),
            {"n": len(radius), "worst": w, "t_worst": tw, "mean": statistics.fmean(r for r, _ in radius),
             "net_m": net, "net_dir": math.degrees(math.atan2(ee or 0.0, en or 0.0)) % 360.0,
             "steady": net >= STEADY_SHARE * w and net >= 1.0, "dur_s": samples[-1].t - samples[0].t})

    dh = [(s.agl_ft - ref["agl"], s.t) for s in samples if s.agl_ft is not None and ref["agl"] is not None]
    if dh:
        rms = math.sqrt(statistics.fmean(d * d for d, _ in dh))
        w, tw = _worst([(abs(d), t) for d, t in dh])
        side = next(("high" if d > 0 else "low") for d, t in dh if abs(d) == w and t == tw)
        net = _end_mean(samples, lambda s: s.agl_ft - ref["agl"] if s.agl_ft is not None else None) or 0.0
        trend = {"net_ft": net, "steady": abs(net) >= STEADY_SHARE * w and abs(net) >= 2.0,
                 "dur_s": samples[-1].t - samples[0].t}
        out["hover.height_rms"] = Observation(
            "hover.height_rms", rms, "ft", "derived", ("agl_ft",),
            dict({"n": len(dh), "ref_ft": ref["agl"], "mean_dev": statistics.fmean(d for d, _ in dh)}, **trend))
        out["hover.height_max"] = Observation(
            "hover.height_max", w, "ft", "derived", ("agl_ft",),
            dict({"n": len(dh), "t_worst": tw, "side": side}, **trend))

    dhdg = [(abs(angle_diff(s.hdg, ref["hdg"])), s.t) for s in samples if s.hdg is not None and ref["hdg"] is not None]
    if dhdg:
        w, tw = _worst(dhdg)
        out["hover.heading_p95"] = Observation(
            "hover.heading_p95", percentile([d for d, _ in dhdg], 95), "deg", "derived", ("hdg",),
            {"n": len(dhdg), "worst": w, "t_worst": tw, "ref_deg": ref["hdg"]})

    axes = [a for a in ("pos", "agl_ft", "hdg") if judged.get(a)]
    if axes and "pos" in axes:
        inside = total = 0
        for s in samples:
            ok = []
            if "pos" in axes:
                if not s.pos:
                    continue
                ok.append(math.hypot(s.pos[0] - ref["n"], s.pos[1] - ref["e"]) <= box["radius_m"])
            if "agl_ft" in axes:
                if s.agl_ft is None:
                    continue
                ok.append(abs(s.agl_ft - ref["agl"]) <= box["height_ft"])
            if "hdg" in axes:
                if s.hdg is None:
                    continue
                ok.append(abs(angle_diff(s.hdg, ref["hdg"])) <= box["heading_deg"])
            total += 1
            inside += all(ok)
        if total:
            out["hover.in_box"] = Observation("hover.in_box", 100.0 * inside / total, "%", "derived", tuple(axes),
                                              {"n": total, "inside": inside, "axes": list(axes)})
    return out
