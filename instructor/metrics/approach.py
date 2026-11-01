"""
instructor/metrics/approach.py - observations for approach.pad, from samples and the attempt's context.

context: target_ll (lat, lon), target_name, t_touchdown (model time; inferred from height / sink /
speed settling, so the evaluation's confidence is capped at medium), skid_ft (or None).

    approach.land_dist   distance from the point at touchdown (m)
    approach.sink_low    worst sink rate while below 50 ft above the skids, before touchdown (fpm)
    approach.vrs_s       seconds below 30 kt ground speed while sinking faster than 800 fpm (the crew
                         chief's ground-speed vortex-ring check: an approximation, labelled as such)
    approach.stability   standard deviation of vertical speed from 600 m to 100 m (fpm)
    approach.align_p95   95th percentile of |ground track - bearing to the point| from 600 m to 100 m,
                         above 10 kt (deg, true: both from lat/lon)
    approach.gate300     indicated airspeed as you pass 300 m from the point (kt; v3, was ground speed)
    approach.gate100     ground speed as you pass 100 m from the point (kt)
    approach.td_g        peak normal g from TD_G_BEFORE_S before first contact to TD_G_AFTER_S after it
                         (each 10 Hz packet carries the peak since the one before, read every frame)
    approach.td_sink     worst sink rate in the 2 s before FIRST CONTACT (the first sample at the skids, within
                         1.5 ft of the skid height, or 6 ft AGL without one): a hard arrival that bounces
                         and settles later is still judged on the arrival (31 Oct: 2803 fpm read as 383)
"""
import math
import statistics
from typing import Dict, List, Optional

from instructor import geometry
from instructor.metrics.hover import percentile
from instructor.models import Observation, Sample

KT_PER_MS = 1.94384
LOW_FT = 50.0
FINAL_FROM_M, FINAL_TO_M = 600.0, 100.0
VRS_GS_KT, VRS_SINK_FPM = 30.0, 800.0
TD_WINDOW_S = 2.0
TD_G_BEFORE_S, TD_G_AFTER_S = 0.5, 1.5


def gs_kt(s: Sample) -> Optional[float]:
    return math.hypot(s.vel[0], s.vel[1]) * KT_PER_MS if s.vel else None


def observe(samples: List[Sample], t0: float, box: Dict[str, float], judged: Dict[str, bool],
            context: Optional[dict] = None) -> Dict[str, Observation]:
    c = context or {}
    tgt = c.get("target_ll")
    t_td = c.get("t_touchdown")
    skid = c.get("skid_ft") or 0.0
    out: Dict[str, Observation] = {}
    if not tgt or not samples:
        return out
    tgt = tuple(tgt)
    air = [s for s in samples if t_td is None or s.t <= t_td]
    dist = [(geometry.dist_m(s.ll, tgt) if s.ll else None) for s in air]
    trk = geometry.tracks([s.ll for s in air], [s.t for s in air])

    if t_td is not None:
        at = [s for s in samples if s.t >= t_td and s.ll][:1] or [s for s in air if s.ll][-1:]
        if at:
            d = geometry.dist_m(at[0].ll, tgt)
            out["approach.land_dist"] = Observation(
                "approach.land_dist", d, "m", "derived", ("ll",),
                {"t": at[0].t, "dir": geometry.compass8(geometry.bearing_deg(tgt, at[0].ll)), "target": c.get("target_name", "")})
        contact = next((s.t for s in samples if s.t <= t_td and s.agl_ft is not None
                        and (s.agl_ft - skid <= 1.5 if c.get("skid_ft") is not None else s.agl_ft <= 6.0)), t_td)
        before = [s for s in samples if contact - TD_WINDOW_S <= s.t <= contact and s.vs_fpm is not None]
        if before:
            out["approach.td_sink"] = Observation(
                "approach.td_sink", max(0.0, max(-s.vs_fpm for s in before)), "fpm", "derived", ("vs_fpm",),
                {"n": len(before), "t_touchdown": t_td, "t_contact": contact})
        near = [(s.gmax if s.gmax is not None else s.g, s.t) for s in samples
                if contact - TD_G_BEFORE_S <= s.t <= contact + TD_G_AFTER_S and (s.gmax is not None or s.g is not None)]
        if near:
            gpk, tpk = max(near, key=lambda x: (x[0], -x[1]))
            out["approach.td_g"] = Observation("approach.td_g", gpk, "g", "measured", ("g",),
                                               {"t_peak": tpk, "t_contact": contact, "n": len(near)})

    low = [s for s in air if s.agl_ft is not None and s.vs_fpm is not None and s.agl_ft - skid < LOW_FT]
    if low:
        w = max(low, key=lambda s: (-s.vs_fpm, -s.t))
        out["approach.sink_low"] = Observation(
            "approach.sink_low", max(0.0, -w.vs_fpm), "fpm", "derived", ("vs_fpm", "agl_ft"),
            {"t_worst": w.t, "agl_ft": w.agl_ft - skid, "n": len(low)})

    vrs = 0.0
    for a, b in zip(air, air[1:]):
        g = gs_kt(a)
        if g is not None and a.vs_fpm is not None and g < VRS_GS_KT and -a.vs_fpm > VRS_SINK_FPM:
            vrs += min(b.t - a.t, 1.0)
    out["approach.vrs_s"] = Observation("approach.vrs_s", vrs, "s", "derived", ("vs_fpm", "vel"),
                                        {"gs_below_kt": VRS_GS_KT, "sink_above_fpm": VRS_SINK_FPM})

    final = [(s, d, tr) for s, d, tr in zip(air, dist, trk) if d is not None and FINAL_TO_M <= d <= FINAL_FROM_M]
    vs = [s.vs_fpm for s, _, _ in final if s.vs_fpm is not None]
    if len(vs) >= 4:
        out["approach.stability"] = Observation("approach.stability", statistics.pstdev(vs), "fpm", "derived",
                                                ("vs_fpm", "ll"), {"n": len(vs), "mean": statistics.fmean(vs)})
    off = [(abs(geometry.angle_diff(tr, geometry.bearing_deg(s.ll, tgt))), s.t) for s, d, tr in final
           if tr is not None and (gs_kt(s) or 0) >= 10]
    if len(off) >= 4:
        w, tw = max(off, key=lambda x: (x[0], -x[1]))
        out["approach.align_p95"] = Observation("approach.align_p95", percentile([o for o, _ in off], 95), "deg",
                                                "derived", ("ll", "vel"), {"n": len(off), "worst": w, "t_worst": tw})

    first = next(((s, d) for s, d in zip(air, dist) if d is not None and d <= 300.0 and s.ias_kt is not None), None)
    if first is not None:
        out["approach.gate300"] = Observation("approach.gate300", first[0].ias_kt, "kt", "measured", ("ll", "ias_kt"),
                                              {"t": first[0].t, "dist_m": first[1], "speed": "indicated airspeed"})
    first = next(((s, d) for s, d in zip(air, dist) if d is not None and d <= 100.0 and s.vel), None)
    if first is not None:
        out["approach.gate100"] = Observation("approach.gate100", gs_kt(first[0]), "kt", "derived", ("ll", "vel"),
                                              {"t": first[0].t, "dist_m": first[1], "speed": "ground speed"})
    return out
