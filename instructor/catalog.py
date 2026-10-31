"""
instructor/catalog.py - the exercises and their tolerances, as data.

Phase 1 has one exercise, hover.stationary, at five levels. Level 3 is the worked example in the
proposal (30 / 25 / 10 / 20 / 15 points); level 1 is for a first-time hover. Tolerances tighten with the level and the hover gets
longer. Change a number here = bump VERSION, so old results stay explained by their own criteria.

Height is judged against the height you held when the clock started (so the learned skid height
cancels out), position against the point you were over, heading against the heading you had.
"""
from typing import Dict, Optional

from instructor.models import Criterion, Exercise

HOVER_ID = "hover.stationary"
# 1: first flown 30 Oct (scores 7 and 15 at level 2: too steep a start)
# 2: a beginner level 1 added; old 1 -> 2, old 2 -> 3, old 3 -> 4, old 5 stays 5 (checkride standard)
HOVER_VERSION = 2
HELICOPTERS = ("UH-1H", "AH-64")

# level: duration, (good, limit) per criterion, and the box for "time inside the box"
HOVER_LEVELS: Dict[int, dict] = {
    1: dict(duration_s=30, radius=(6.0, 15.0), rms=(3.0, 8.0), excursion=(6.0, 12.0), heading=(10.0, 25.0),
            box=dict(radius_m=8.0, height_ft=5.0, heading_deg=15.0)),
    2: dict(duration_s=30, radius=(4.0, 8.0), rms=(1.5, 4.0), excursion=(4.0, 8.0), heading=(8.0, 20.0),
            box=dict(radius_m=5.0, height_ft=3.0, heading_deg=15.0)),
    3: dict(duration_s=45, radius=(2.0, 5.0), rms=(1.0, 3.0), excursion=(3.0, 6.0), heading=(5.0, 15.0),
            box=dict(radius_m=3.0, height_ft=2.0, heading_deg=10.0)),
    4: dict(duration_s=60, radius=(1.5, 4.0), rms=(0.8, 2.5), excursion=(2.5, 5.0), heading=(4.0, 12.0),
            box=dict(radius_m=2.5, height_ft=2.0, heading_deg=8.0)),
    5: dict(duration_s=90, radius=(0.75, 2.0), rms=(0.5, 1.5), excursion=(1.5, 3.0), heading=(2.0, 6.0),
            box=dict(radius_m=1.5, height_ft=1.0, heading_deg=4.0)),
}
LEVELS = tuple(sorted(HOVER_LEVELS))


def hover(level: int) -> Exercise:
    level = max(LEVELS[0], min(LEVELS[-1], int(level)))
    lv = HOVER_LEVELS[level]
    crit = (
        Criterion("hover.radius_p95", "Position drift", 30, "hover.radius_p95", *lv["radius"], "m",
                  ("pos",), "hover.drift", core=True),
        Criterion("hover.height_rms", "Height holding", 25, "hover.height_rms", *lv["rms"], "ft",
                  ("agl_ft",), "hover.height", core=True),
        Criterion("hover.height_max", "Largest height excursion", 10, "hover.height_max", *lv["excursion"], "ft",
                  ("agl_ft",), "hover.height"),
        Criterion("hover.heading_p95", "Heading holding", 20, "hover.heading_p95", *lv["heading"], "deg",
                  ("hdg",), "hover.heading"),
        # needs position; height and heading are added to the box when they pass the quality gate
        Criterion("hover.in_box", "Time inside the box", 15, "hover.in_box", 90.0, 60.0, "%",
                  ("pos",), "hover.drift", higher_is_better=True),
    )
    return Exercise(HOVER_ID, HOVER_VERSION, "hover", "hover", HELICOPTERS, level, float(lv["duration_s"]), crit,
                    dict(lv["box"]))


EXERCISES = {HOVER_ID: hover}


def get(exercise_id: str, level: int) -> Optional[Exercise]:
    make = EXERCISES.get(exercise_id)
    return make(level) if make else None


def brief(ex: Exercise) -> str:
    """One spoken line: what to do and how it's judged."""
    b = ex.box
    return (f"Instructor, {ex.name} exercise, level {ex.level}. Hold a steady hover for {ex.duration_s:.0f} seconds. "
            f"Box: {b['radius_m']:g} meters, {b['height_ft']:g} feet, {b['heading_deg']:g} degrees. "
            f"Say begin when ready.")
