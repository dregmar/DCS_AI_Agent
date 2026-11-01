"""
instructor/metrics/fuel.py - observations for fuel.recovery (bingo discipline).

context: home_ll, bingo_lbs, after_lbs (fuel you have after bingo to land back home), t_touchdown.
Bingo time = the first sample with fuel at or below bingo (fuel from core.fuel.current_lbs: DCS fuel,
updated every 5 s by the comms hook, so bingo is known to about 5 s).

    fuel.turn_delay   seconds from bingo until you were established for home: ground track within 30 deg
                      of the bearing home for 10 s above 20 kt (0 if you were already heading home)
    fuel.after_bingo  fuel used from bingo to touchdown, as a percentage of after_lbs
    fuel.direct       95th percentile |track - bearing home| on the way back (more than 500 m out, > 20 kt)
    fuel.home_dist    distance from where you started at touchdown (m)
"""
import math
from typing import Dict, List, Optional

from instructor import geometry
from instructor.metrics.hover import percentile
from instructor.models import Observation, Sample

KT_PER_MS = 1.94384
HOMEWARD_DEG, HOMEWARD_HOLD_S, HOMEWARD_KT = 30.0, 10.0, 20.0
RETURN_FROM_M = 500.0


def observe(samples: List[Sample], t0: float, box: Dict[str, float], judged: Dict[str, bool],
            context: Optional[dict] = None) -> Dict[str, Observation]:
    c = context or {}
    home, bingo, after = c.get("home_ll"), c.get("bingo_lbs"), c.get("after_lbs")
    t_td = c.get("t_touchdown")
    out: Dict[str, Observation] = {}
    if not home or bingo is None or not samples:
        return out
    home = tuple(home)
    air = [s for s in samples if t_td is None or s.t <= t_td]
    trk = geometry.tracks([s.ll for s in air], [s.t for s in air])
    homeward = []
    for s, tr in zip(air, trk):
        gs = math.hypot(s.vel[0], s.vel[1]) * KT_PER_MS if s.vel else 0.0
        ok = (tr is not None and s.ll is not None and gs >= HOMEWARD_KT
              and abs(geometry.angle_diff(tr, geometry.bearing_deg(s.ll, home))) <= HOMEWARD_DEG)
        homeward.append(ok)

    i_b = next((i for i, s in enumerate(air) if s.fuel_lbs is not None and s.fuel_lbs <= bingo), None)
    out_max = max((geometry.dist_m(home, s.ll) for s in air[: (i_b + 1) if i_b is not None else len(air)] if s.ll),
                  default=0.0)
    if i_b is not None:
        t_b = air[i_b].t
        # the start of each homeward run that lasted HOMEWARD_HOLD_S
        est = None
        run_start = None
        for i, (s, ok) in enumerate(zip(air, homeward)):
            if ok:
                run_start = s.t if run_start is None else run_start
                if s.t - run_start >= HOMEWARD_HOLD_S and s.t >= t_b:
                    est = run_start
                    break
            else:
                run_start = None
        delay = max(0.0, est - t_b) if est is not None else max(0.0, air[-1].t - t_b)
        out["fuel.turn_delay"] = Observation(
            "fuel.turn_delay", delay, "s", "derived", ("fuel_lbs", "ll"),
            {"t_bingo": t_b, "established": est is not None, "t_established": est, "out_max_m": out_max,
             "fuel_at_bingo": air[i_b].fuel_lbs})
        if t_td is not None and after:
            fuel_td = next((s.fuel_lbs for s in reversed(air) if s.fuel_lbs is not None), None)
            if fuel_td is not None:
                used = air[i_b].fuel_lbs - fuel_td
                out["fuel.after_bingo"] = Observation(
                    "fuel.after_bingo", 100.0 * used / after, "%", "derived", ("fuel_lbs",),
                    {"used_lbs": used, "after_lbs": after, "fuel_at_touchdown": fuel_td})
        if est is not None:
            back = [(abs(geometry.angle_diff(tr, geometry.bearing_deg(s.ll, home))), s.t)
                    for s, tr in zip(air, trk) if s.t >= est and tr is not None and s.ll
                    and geometry.dist_m(s.ll, home) > RETURN_FROM_M
                    and s.vel and math.hypot(s.vel[0], s.vel[1]) * KT_PER_MS >= HOMEWARD_KT]
            if len(back) >= 4:
                out["fuel.direct"] = Observation("fuel.direct", percentile([b for b, _ in back], 95), "deg", "derived",
                                                 ("ll",), {"n": len(back), "worst": max(b for b, _ in back)})
    if t_td is not None:
        at = next((s for s in reversed(air) if s.ll), None)
        if at is not None:
            out["fuel.home_dist"] = Observation("fuel.home_dist", geometry.dist_m(home, at.ll), "m", "derived", ("ll",),
                                                {"t": at.t, "out_max_m": out_max})
    return out
