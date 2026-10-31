"""
instructor/feedback.py - what the instructor says, from an Evaluation only (templates, no model).

Every number spoken is one the evaluation holds; nothing is invented or rephrased by an LLM.
"""
from typing import Any, Dict, List

CRIT_WORDS = {
    "hover.radius_p95": "drift",
    "hover.height_rms": "height holding",
    "hover.height_max": "height excursion",
    "hover.heading_p95": "heading holding",
    "hover.in_box": "time in the box",
}


def _fmt(v: float, units: str) -> str:
    if units == "m":
        return f"{v:.1f} meters"
    if units == "ft":
        return f"{v:.1f} feet" if v < 10 else f"{v:.0f} feet"
    if units == "deg":
        return f"{v:.0f} degrees"
    if units == "%":
        return f"{v:.0f} percent"
    return f"{v:g} {units}"


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
    if worst is not None and worst.get("lost", 0) > 0:
        tail = f" Biggest loss: {_what(worst)}."
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
    extra = [d for d in ev.deductions][1:3]
    if extra:
        line += " Also: " + "; ".join(_what(rows[d.criterion]) for d in extra) + "."
    if ev.confidence == "medium":
        slow = [q for q in ev.quality if q.status == "degraded"]
        if slow:
            line += f" Confidence is medium because telemetry is {slow[0].rate_hz:.0f} samples a second."
    return line
