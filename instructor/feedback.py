"""
instructor/feedback.py - what the instructor says, from an Evaluation only (templates, no model).

Every number spoken is one the evaluation holds; nothing is invented or rephrased by an LLM.
"""
from typing import Any, Dict, List, Optional

from instructor.geometry import compass8

CRIT_WORDS = {
    "hover.radius_p95": "drift",
    "hover.height_rms": "height holding",
    "hover.height_max": "height excursion",
    "hover.heading_p95": "heading holding",
    "hover.in_box": "time in the box",
    "approach.land_dist": "landing position",
    "approach.sink_low": "sink rate near the ground",
    "approach.vrs_s": "time in the vortex ring zone",
    "approach.stability": "descent steadiness",
    "approach.align_p95": "line-up",
    "approach.gate300": "airspeed at 300 meters",
    "approach.gate100": "speed at 100 meters",
    "approach.td_sink": "touchdown",
    "approach.td_g": "touchdown g",
    "turn.radius_p95": "drift",
    "turn.height_rms": "height holding",
    "turn.height_max": "height excursion",
    "turn.rate_sd": "turn rate steadiness",
    "turn.time": "time for the 360",
    "turn.overshoot": "overshoot",
    "fuel.turn_delay": "turning at bingo",
    "fuel.after_bingo": "fuel after bingo",
    "fuel.direct": "direct return",
    "fuel.home_dist": "landing back at the start",
}


# criteria whose limit is a safety limit: when one loses all its points it leads the spoken result
SAFETY = {"approach.sink_low", "approach.td_sink", "approach.vrs_s", "approach.td_g"}


def _fmt(v: float, units: str) -> str:
    if units == "m":
        return f"{v:.1f} meters" if v < 20 else f"{v:.0f} meters"
    if units == "ft":
        return f"{v:.1f} feet" if v < 10 else f"{v:.0f} feet"
    if units == "deg":
        return f"{v:.0f} degrees"
    if units == "%":
        return f"{v:.0f} percent"
    if units == "fpm":
        return f"{v:.0f} feet a minute"
    if units == "kt":
        return f"{v:.0f} knots"
    if units == "g":
        return f"{v:.1f} g"
    if units == "deg/s":
        return f"{v:.1f} degrees a second"
    if units == "s":
        return f"{v:.0f} seconds" if v >= 10 else f"{v:.1f} seconds"
    return f"{v:g} {units}"


def trend(ev, cid: str) -> Optional[str]:
    """'a steady drift south' / 'a steady climb of 8 feet' / 'wandering about the point', from the
    observation's own evidence (hover only)."""
    o = next((x for x in ev.observations if x.id == cid), None)
    if o is None:
        return None
    e = o.evidence
    if cid in ("hover.radius_p95", "turn.radius_p95") and "net_m" in e:
        if e.get("steady"):
            return f"a steady drift {compass8(e['net_dir'])}, {e['net_m']:.0f} meters by the end"
        return "wandering about the point"
    if cid in ("hover.height_rms", "hover.height_max", "turn.height_rms", "turn.height_max") and "net_ft" in e:
        if e.get("steady"):
            return (f"a steady {'climb' if e['net_ft'] > 0 else 'descent'} of {abs(e['net_ft']):.0f} feet "
                    f"over {e['dur_s']:.0f} seconds")
        return "up and down"
    return None


def _what(row: Dict[str, Any]) -> str:
    """'drift, 4.6 meters at the 95th percentile'"""
    word = CRIT_WORDS.get(row["id"], row["label"].lower())
    v, u = row["value"], row["units"]
    if row["id"].endswith("_p95"):
        return f"{word}, {_fmt(v, u)} at the 95th percentile"
    if row["id"].endswith("_rms"):
        return f"{word}, {_fmt(v, u)} typical deviation"        # RMS; Piper can't say "RMS"
    return f"{word}, {_fmt(v, u)}"


def _not_judged(ev) -> List[str]:
    return [CRIT_WORDS.get(r["id"], r["label"].lower()) for r in ev.criteria if not r.get("judged")]


def result_line(ev) -> str:
    """The line spoken when an attempt ends."""
    name = ev.exercise_name
    if ev.outcome == "aborted":
        return f"Instructor, {ev.exercise_name} stopped, not scored. {ev.note.capitalize()}." if ev.note else \
            f"Instructor, {ev.exercise_name} stopped, not scored."
    if ev.outcome == "not_scored":
        return f"Instructor, {ev.exercise_name} not scored: {ev.note or 'not enough data'}."
    judged = [r for r in ev.criteria if r.get("judged")]
    worst = max(judged, key=lambda r: (r.get("lost", 0), -judged.index(r)), default=None)
    head = (f"Instructor, {name}, {ev.score:.0f}, {ev.confidence} confidence."
            if ev.outcome == "scored" else
            f"Instructor, {name}, partial score {ev.score:.0f} from {100 * ev.points_judged / ev.points_total:.0f} "
            f"percent of the criteria, low confidence.")
    tail = ""
    unsafe = [r for r in judged if r["id"] in SAFETY and r.get("lost", 0) >= r["points"]]
    if unsafe:                      # a safety limit blown outright is said first, whatever lost most points
        tail = " Safety: " + "; ".join(_what(r) for r in unsafe) + "."
        if worst is not None and worst not in unsafe and worst.get("lost", 0) > 0:
            tail += f" Also: {_what(worst)}."
    elif worst is not None and worst.get("lost", 0) > 0:
        tr = trend(ev, worst["id"])
        tail = f" Biggest loss: {_what(worst)}" + (f", {tr}." if tr else ".")
    elif judged:
        tail = " Inside every tolerance."
    nj = _not_judged(ev)
    if nj:
        tail += f" Not judged: {', '.join(nj)}."
    return head + tail


def details(ev) -> str:
    """'How did I do?': the score line plus up to two more deductions, and why confidence isn't high."""
    line = result_line(ev)
    if ev.outcome not in ("scored", "partial"):
        return line
    rows = {r["id"]: r for r in ev.criteria}
    said = set(SAFETY) | {max((r for r in ev.criteria if r.get("judged")), key=lambda r: r.get("lost", 0))["id"]}
    extra = [d for d in ev.deductions if d.criterion not in said][:2]
    if extra:
        line += " Also: " + "; ".join(_what(rows[d.criterion]) for d in extra) + "."
    if ev.confidence == "medium":
        slow = [q for q in ev.quality if q.status == "degraded"]
        if slow:
            line += f" Confidence is medium because telemetry is {slow[0].rate_hz:.0f} samples a second."
        elif (ev.context or {}).get("inferred_window"):
            line += " Confidence is medium because the touchdown is worked out, not reported by DCS."
    return line
