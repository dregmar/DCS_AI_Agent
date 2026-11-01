"""
instructor/adaptive.py - levels, recurring weaknesses and what to practise next (proposal section 8),
computed from the pilot's history every time (no hidden state; the same history gives the same answer).

Valid attempt = outcome "scored", confidence high or medium, the exercise's CURRENT version (a result
scored against older criteria doesn't move today's levels).

Levels (decision 7), per exercise:
    up one     median of the last 3 valid attempts at this level >= 85 and none below 70
    down one   median of those 3 < 55, or all 3 below 60
    hold       otherwise, and until 3 valid attempts have been flown at the current level
    override   "Instructor, easier / harder / level 3" sets the level for the next attempt; the rules carry
               on from there (each saved result records the level it was flown at)
Recurring weakness: a tag that lost >= 40 % of a criterion's points in at least 3 of the last 5 valid
attempts of any exercise.
Next exercise: the highest of 0.6 x (a recurring weakness it trains) + 0.25 x (days since practised,
capped at 14, / 14) + 0.15 x (100 - its median) / 100; ties by exercise id.
"""
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

UP_MEDIAN, UP_FLOOR = 85.0, 70.0
DOWN_MEDIAN, DOWN_ALL = 55.0, 60.0
SETTLE = 3
WEAK_SHARE, WEAK_COUNT, WEAK_WINDOW = 0.40, 3, 5
MIN_LEVEL, MAX_LEVEL = 1, 5

# which exercise trains which weakness tag
TRAINS = {
    "hover.drift": "hover.stationary", "hover.height": "hover.stationary", "hover.heading": "hover.stationary",
    "approach.position": "approach.pad", "approach.sink_rate": "approach.pad", "approach.stability": "approach.pad",
    "approach.alignment": "approach.pad", "approach.speed": "approach.pad", "approach.touchdown": "approach.pad",
    "fuel.decision": "fuel.recovery", "fuel.margin": "fuel.recovery",
    "turn.drift": "hover.turn", "turn.height": "hover.turn", "turn.rate": "hover.turn", "turn.stop": "hover.turn",
}
# suggested only after a scored attempt of the first at this score or better (proposal: the stationary
# hover first, then the turn)
PREREQ = {"hover.turn": ("hover.stationary", 70.0)}
TAG_WORDS = {
    "hover.drift": "hover drift", "hover.height": "hover height", "hover.heading": "hover heading",
    "approach.position": "landing position", "approach.sink_rate": "sink rate", "approach.stability": "approach stability",
    "approach.alignment": "approach alignment", "approach.speed": "approach speed", "approach.touchdown": "touchdown",
    "fuel.decision": "bingo decisions", "fuel.margin": "fuel margin",
    "turn.drift": "drift in the turn", "turn.height": "height in the turn", "turn.rate": "turn rate",
    "turn.stop": "stopping the turn",
}


def valid(rows: List[Dict[str, Any]], exercise_id: Optional[str] = None,
          versions: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("outcome") != "scored" or r.get("confidence") not in ("high", "medium"):
            continue
        if exercise_id is not None and r.get("exercise_id") != exercise_id:
            continue
        if versions is not None and r.get("exercise_version") != versions.get(r.get("exercise_id")):
            continue
        out.append(r)
    return out


def next_level(rows: List[Dict[str, Any]], default: int) -> Tuple[int, str]:
    """(level for the next attempt, why) from this exercise's valid attempts, oldest first."""
    if not rows:
        return _clamp(default), "first attempt"
    cur = int(rows[-1].get("level") or default)
    at = []
    for r in reversed(rows):                    # the trailing run at the current level
        if int(r.get("level") or 0) != cur:
            break
        at.append(float(r.get("score") or 0.0))
    at.reverse()
    if len(at) < SETTLE:
        return cur, f"{len(at)} of {SETTLE} attempts at level {cur}"
    last = at[-SETTLE:]
    med = statistics.median(last)
    if med >= UP_MEDIAN and min(last) >= UP_FLOOR and cur < MAX_LEVEL:
        return cur + 1, f"median {med:.0f} of the last {SETTLE} at level {cur}"
    if (med < DOWN_MEDIAN or all(x < DOWN_ALL for x in last)) and cur > MIN_LEVEL:
        return cur - 1, f"median {med:.0f} of the last {SETTLE} at level {cur}"
    return cur, f"median {med:.0f} at level {cur}"


def median_score(rows: List[Dict[str, Any]], n: int = 3) -> Optional[float]:
    s = [float(r.get("score") or 0.0) for r in rows[-n:]]
    return statistics.median(s) if s else None


def tags_lost(row: Dict[str, Any], tag_of: Dict[str, str]) -> Dict[str, float]:
    """tag -> the largest share of a criterion's points it lost in this attempt."""
    out: Dict[str, float] = {}
    for c in row.get("criteria") or []:
        if not c.get("judged") or not c.get("points"):
            continue
        tag = tag_of.get(c["id"])
        if tag:
            out[tag] = max(out.get(tag, 0.0), float(c.get("lost") or 0.0) / float(c["points"]))
    return out


def weaknesses(rows: List[Dict[str, Any]], tag_of: Dict[str, str]) -> Dict[str, int]:
    """Recurring tags -> in how many of the last WEAK_WINDOW valid attempts they lost >= 40 %."""
    counts: Dict[str, int] = {}
    for r in rows[-WEAK_WINDOW:]:
        for tag, share in tags_lost(r, tag_of).items():
            if share >= WEAK_SHARE:
                counts[tag] = counts.get(tag, 0) + 1
    return {t: n for t, n in sorted(counts.items()) if n >= WEAK_COUNT}


def suggest(all_valid: List[Dict[str, Any]], candidates: List[str], tag_of: Dict[str, str],
            now: Optional[float] = None) -> Tuple[Optional[str], str]:
    """(exercise id, why) to practise next, from the exercises available in this aircraft."""
    if not candidates:
        return None, "no exercises for this aircraft"
    now = time.time() if now is None else now
    weak = weaknesses(all_valid, tag_of)
    best = None
    for ex_id in sorted(candidates):
        pre = PREREQ.get(ex_id)
        if pre and pre[0] in candidates and not any(r.get("exercise_id") == pre[0] and (r.get("score") or 0) >= pre[1]
                                                    for r in all_valid):
            continue
        rows = [r for r in all_valid if r.get("exercise_id") == ex_id]
        w = max((n / WEAK_WINDOW for t, n in weak.items() if TRAINS.get(t) == ex_id), default=0.0)
        last = _when(rows[-1]) if rows else None
        days = 14.0 if last is None else min(14.0, max(0.0, (now - last) / 86400.0))
        med = median_score(rows)
        prio = 0.6 * w + 0.25 * days / 14.0 + 0.15 * (100.0 - (med if med is not None else 0.0)) / 100.0
        why = ([f"recurring {TAG_WORDS.get(t, t)}" for t in weak if TRAINS.get(t) == ex_id][:1]
               or (["not flown yet"] if not rows else [f"median {med:.0f}"]))[0]
        key = (-round(prio, 6), ex_id)
        if best is None or key < best[0]:
            best = (key, ex_id, why)
    return best[1], best[2]


def _when(row: Dict[str, Any]) -> Optional[float]:
    s = row.get("end_wall") or row.get("start_wall")
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _clamp(level: int) -> int:
    return max(MIN_LEVEL, min(MAX_LEVEL, int(level)))
