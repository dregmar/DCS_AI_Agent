"""
instructor/scoring.py - samples -> quality -> observations -> criteria -> one Evaluation.

Pure functions: the same samples, exercise and meta always give the same Evaluation (no clock, no
randomness, no language model). The rules (proposal section 6):

  * A criterion is judged only if every signal it needs passes the quality gate (instructor/quality.py).
    Otherwise it is "not judged", its points leave the total and the reason is kept.
  * Linear deductions between the criterion's good and limit values; evidence for every deduction.
  * Only measured / derived observations deduct. (Phase 1 has no inferred ones.)
  * score = 100 x points kept / points judged.
  * outcome "scored" when >= 70 % of the points and every core criterion were judged, "partial" from
    40 %, else "not_scored" (no score).
  * confidence: high = all judged on good-rate data and >= 90 % of points judged; medium = degraded
    rate (today's 4 Hz) or 70-90 % judged; low = partial; none = not scored / aborted.
"""
from typing import Any, Callable, Dict, List, Optional

from instructor import quality
from instructor.metrics import approach as approach_metrics
from instructor.metrics import fuel as fuel_metrics
from instructor.metrics import hover as hover_metrics
from instructor.metrics import turn as turn_metrics
from instructor.models import (SCHEMA, Criterion, Deduction, Evaluation, Exercise, Observation, Sample,
                               SignalQuality)

SCORED_SHARE, PARTIAL_SHARE, HIGH_SHARE = 0.70, 0.40, 0.90
WEAKNESS_SHARE = 0.40           # a criterion that loses this share of its points tags a weakness

# area -> observe(samples, t0, box, judged signals, context)
OBSERVERS: Dict[str, Callable[..., Dict[str, Observation]]] = {
    "hover": hover_metrics.observe, "approach": approach_metrics.observe, "fuel": fuel_metrics.observe,
    "turn": turn_metrics.observe}
# signals a whole area looks at (beyond each criterion's own), for the quality table
AREA_SIGNALS = {"hover": ("pos", "agl_ft", "hdg"), "approach": ("ll", "vel", "vs_fpm", "agl_ft"),
                "fuel": ("ll", "fuel_lbs", "vel"), "turn": ("pos", "agl_ft", "hdg")}


def mission_time(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def lost_points(c: Criterion, value: float) -> float:
    if c.higher_is_better:
        frac = (c.good - value) / (c.good - c.limit) if c.good != c.limit else float(value < c.good)
    else:
        frac = (value - c.good) / (c.limit - c.good) if c.limit != c.good else float(value > c.good)
    return round(c.points * max(0.0, min(1.0, frac)), 2)


def evidence(c: Criterion, o: Observation) -> str:
    ev, v = o.evidence, o.value
    lim = f"(good {c.good:g}, limit {c.limit:g} {c.units})"
    if c.id == "hover.radius_p95":
        return (f"95% of the time within {v:.1f} m of the start point {lim}; "
                f"furthest {ev['worst']:.1f} m at {mission_time(ev['t_worst'])}")
    if c.id == "hover.height_rms":
        return f"height {v:.1f} ft RMS from the {ev['ref_ft']:.0f} ft AGL you started at {lim}"
    if c.id == "hover.height_max":
        return f"{v:.0f} ft {ev['side']} at {mission_time(ev['t_worst'])} {lim}"
    if c.id == "hover.heading_p95":
        return (f"95% of the time within {v:.0f} deg of heading {ev['ref_deg']:03.0f} {lim}; "
                f"worst {ev['worst']:.0f} deg at {mission_time(ev['t_worst'])}")
    if c.id == "hover.in_box":
        axes = ", ".join({"pos": "position", "agl_ft": "height", "hdg": "heading"}.get(x, x) for x in ev["axes"])
        return f"{v:.0f}% of samples inside the box on {axes} (good {c.good:g}, limit {c.limit:g} %)"
    if c.id == "approach.land_dist":
        return f"touched down {v:.0f} m {ev['dir']} of {ev['target'] or 'the point'} {lim}"
    if c.id == "approach.sink_low":
        return f"sinking {v:.0f} fpm at {ev['agl_ft']:.0f} ft at {mission_time(ev['t_worst'])} {lim}"
    if c.id == "approach.vrs_s":
        return f"{v:.1f} s below 30 kt sinking faster than 800 fpm (vortex-ring zone, by ground speed) {lim}"
    if c.id == "approach.stability":
        return f"vertical speed varied {v:.0f} fpm (standard deviation) from 600 m to 100 m {lim}"
    if c.id == "approach.align_p95":
        return f"95% of the final within {v:.0f} deg of the line to the point; worst {ev['worst']:.0f} deg {lim}"
    if c.id in ("approach.gate300", "approach.gate100"):
        return f"{v:.0f} kt {ev.get('speed', 'ground speed')} at {ev['dist_m']:.0f} m from the point {lim}"
    if c.id == "approach.td_g":
        return f"peak {v:.2f} g at {mission_time(ev['t_peak'])}, around the first skid contact {lim}"
    if c.id == "turn.radius_p95":
        return (f"95% of the turn within {v:.1f} m of the start point {lim}; "
                f"furthest {ev['worst']:.1f} m at {mission_time(ev['t_worst'])}")
    if c.id == "turn.height_rms":
        return f"height {v:.1f} ft RMS from the {ev['ref_ft']:.0f} ft AGL you started at {lim}"
    if c.id == "turn.height_max":
        return f"{v:.0f} ft {ev['side']} at {mission_time(ev['t_worst'])} {lim}"
    if c.id == "turn.rate_sd":
        return f"turn rate varied {v:.1f} deg/s (standard deviation) about {ev['mean']:.1f} deg/s {ev['dir']} {lim}"
    if c.id == "turn.time":
        return f"360 degrees {ev['dir']} in {v:.0f} s {lim}"
    if c.id == "turn.overshoot":
        return f"turned {v:.0f} deg past the start heading before stopping {lim}"
    if c.id == "approach.td_sink":
        return f"sinking {v:.0f} fpm in the last 2 seconds before the skids touched {lim}"
    if c.id == "fuel.turn_delay":
        if not ev.get("established"):
            return f"never established for home after bingo ({v:.0f} s to the end) {lim}"
        return f"established for home {v:.0f} s after bingo at {mission_time(ev['t_bingo'])} {lim}"
    if c.id == "fuel.after_bingo":
        return (f"used {ev['used_lbs']:.0f} lb after bingo of the {ev['after_lbs']:.0f} lb you had ({v:.0f}%) "
                f"(good {c.good:g}, limit {c.limit:g} %)")
    if c.id == "fuel.direct":
        return f"95% of the way back within {v:.0f} deg of the line home {lim}"
    if c.id == "fuel.home_dist":
        return f"landed {v:.0f} m from where you started {lim}"
    return f"{c.observation} = {v:g} {c.units} {lim}"


def evaluate(ex: Exercise, samples: List[Sample], t0: float, t1: float, meta: Dict[str, Any],
             aborted: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> Evaluation:
    """meta: mission, pilot, aircraft, start_wall, end_wall (strings). aborted: why, or None.
    context: the attempt's setup (target, bingo, touchdown time ...), kept in the evaluation. When it
    says "inferred_window" (the end was inferred, e.g. touchdown), confidence is at most medium."""
    context = dict(context or {})
    samples = sorted((s for s in samples if t0 <= s.t <= t1), key=lambda s: s.t)
    total = float(sum(c.points for c in ex.criteria))
    base = dict(schema=SCHEMA, exercise_id=ex.id, exercise_version=ex.version, exercise_name=ex.name,
                level=ex.level, aircraft=str(meta.get("aircraft") or ""), mission=str(meta.get("mission") or ""),
                pilot=str(meta.get("pilot") or "default"), start_model=float(t0), end_model=float(t1),
                start_wall=str(meta.get("start_wall") or ""), end_wall=str(meta.get("end_wall") or ""),
                points_total=total)
    if aborted:
        return Evaluation(**base, outcome="aborted", score=None, points_judged=0.0,
                          criteria=tuple({"id": c.id, "label": c.label, "points": c.points, "judged": False,
                                          "reason": "aborted"} for c in ex.criteria),
                          deductions=(), observations=(), quality=(), confidence="none", weakness_tags=(),
                          note=aborted, context=context)

    signals = set(AREA_SIGNALS.get(ex.area, ())) | {s for c in ex.criteria for s in c.signals}
    q: Dict[str, SignalQuality] = quality.assess_all(samples, signals, t0, t1, ex.min_rate_hz, ex.good_rate_hz)
    judgeable = {s: qq.judgeable() for s, qq in q.items()}
    obs = OBSERVERS[ex.area](samples, t0, ex.box, judgeable, context) if samples else {}

    rows: List[Dict[str, Any]] = []
    deductions: List[Deduction] = []
    judged = kept = 0.0
    degraded = False
    tags = set()
    core_missing = False
    first_reason = ""
    for c in ex.criteria:
        bad = [q[s] for s in c.signals if not q[s].judgeable()]
        o = obs.get(c.observation)
        if bad or o is None or o.value is None or o.kind == "inferred":
            reason = (bad[0].reason if bad else "inferred only" if o is not None and o.kind == "inferred"
                      else "no data")
            rows.append({"id": c.id, "label": c.label, "points": c.points, "judged": False, "reason": reason})
            core_missing = core_missing or c.core
            first_reason = first_reason or reason
            continue
        # the box also used height / heading when they were judgeable: their quality counts too
        used = set(c.signals) | (set(o.signals) if c.id.endswith("in_box") else set())
        degraded = degraded or any(q[s].status == "degraded" for s in used if s in q)
        lost = lost_points(c, o.value)
        judged += c.points
        kept += c.points - lost
        rows.append({"id": c.id, "label": c.label, "points": c.points, "judged": True, "value": round(o.value, 3),
                     "units": c.units, "good": c.good, "limit": c.limit, "lost": lost})
        if lost > 0:
            deductions.append(Deduction(c.id, lost, o.id, evidence(c, o)))
            if lost >= WEAKNESS_SHARE * c.points:
                tags.add(c.tag)

    share = judged / total if total else 0.0
    if share >= SCORED_SHARE and not core_missing:
        outcome = "scored"
        confidence = "medium" if degraded or share < HIGH_SHARE or context.get("inferred_window") else "high"
    elif share >= PARTIAL_SHARE:
        outcome, confidence = "partial", "low"
    else:
        outcome, confidence = "not_scored", "none"
    score = round(100.0 * kept / judged, 1) if outcome in ("scored", "partial") and judged else None
    deductions.sort(key=lambda d: (-d.points, d.criterion))
    return Evaluation(**base, outcome=outcome, score=score, points_judged=judged, criteria=tuple(rows),
                      deductions=tuple(deductions), observations=tuple(obs[k] for k in sorted(obs)),
                      quality=tuple(q[k] for k in sorted(q)), confidence=confidence,
                      weakness_tags=tuple(sorted(tags)),
                      note="" if outcome != "not_scored" else first_reason, context=context)
