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


# ---------------------------------------------------------------- approach and landing on a point
# Ends on a named waypoint ("Instructor, approach to Alpha"), else the nearest helipad / FARP (decision,
# 30 Oct). The clock starts as you come inside START_M (having been beyond ARM_OUT_M) and stops at
# touchdown. At 300 m the speed is indicated airspeed (Phase 3: what you fly by, the ASI is reliable there);
# at 100 m it is ground speed (the ASI is meaningless near the hover). Bearings are true, from lat/lon.
# Touchdown g (Phase 3, I1): the peak normal g around first contact (Export.lua reads it every frame
# while an exercise runs and sends the peak with each 10 Hz packet).
APPROACH_ID = "approach.pad"
# 2 (31 Oct): touchdown judged at first contact, not after a bounce. 3 (Phase 3): IAS at 300 m, touchdown g
APPROACH_VERSION = 3
APPROACH_START_M, APPROACH_ARM_OUT_M = 600.0, 800.0
APPROACH_LEVELS: Dict[int, dict] = {   # (good, limit)
    1: dict(land=(15, 60), sink_low=(600, 1200), stab=(300, 700), align=(20, 45), g300=(60, 90), g100=(25, 45), td=(300, 700), tdg=(1.5, 2.5)),
    2: dict(land=(10, 40), sink_low=(500, 1000), stab=(250, 550), align=(15, 35), g300=(50, 75), g100=(20, 35), td=(250, 600), tdg=(1.4, 2.2)),
    3: dict(land=(6, 25), sink_low=(400, 800), stab=(200, 450), align=(10, 30), g300=(40, 60), g100=(15, 30), td=(200, 500), tdg=(1.3, 2.0)),
    4: dict(land=(4, 15), sink_low=(300, 600), stab=(150, 350), align=(8, 20), g300=(35, 50), g100=(12, 25), td=(150, 400), tdg=(1.25, 1.8)),
    5: dict(land=(2, 8), sink_low=(250, 500), stab=(100, 250), align=(5, 15), g300=(30, 45), g100=(10, 20), td=(100, 300), tdg=(1.2, 1.6)),
}


def approach(level: int) -> Exercise:
    level = max(LEVELS[0], min(LEVELS[-1], int(level)))
    lv = APPROACH_LEVELS[level]
    crit = (
        Criterion("approach.land_dist", "Landing position", 30, "approach.land_dist", *lv["land"], "m",
                  ("ll",), "approach.position", core=True),
        Criterion("approach.sink_low", "Sink rate below 50 feet", 15, "approach.sink_low", *lv["sink_low"], "fpm",
                  ("vs_fpm", "agl_ft"), "approach.sink_rate", core=True),
        Criterion("approach.vrs_s", "Time in the vortex-ring zone", 10, "approach.vrs_s", 0.5, 3.0, "s",
                  ("vs_fpm", "vel"), "approach.sink_rate"),
        Criterion("approach.stability", "Steady descent on final", 10, "approach.stability", *lv["stab"], "fpm",
                  ("vs_fpm", "ll"), "approach.stability"),
        Criterion("approach.align_p95", "Lined up on the point", 15, "approach.align_p95", *lv["align"], "deg",
                  ("ll", "vel"), "approach.alignment"),
        Criterion("approach.gate300", "Airspeed at 300 meters", 5, "approach.gate300", *lv["g300"], "kt",
                  ("ll", "ias_kt"), "approach.speed"),
        Criterion("approach.gate100", "Speed at 100 meters", 5, "approach.gate100", *lv["g100"], "kt",
                  ("ll", "vel"), "approach.speed"),
        Criterion("approach.td_sink", "Touchdown", 10, "approach.td_sink", *lv["td"], "fpm",
                  ("vs_fpm",), "approach.touchdown"),
        Criterion("approach.td_g", "Touchdown g", 10, "approach.td_g", *lv["tdg"], "g",
                  ("g",), "approach.touchdown"),
    )
    return Exercise(APPROACH_ID, APPROACH_VERSION, "approach", "approach", HELICOPTERS, level, 300.0, crit, {})


# ---------------------------------------------------------------- hover turn (Phase 3)
# From a steady hover, a 360 degree turn on the spot, either way, then stop on the heading you started
# on. Judged: drift and height through the turn, how steady the turn rate was, how long the 360 took
# (from the first 10 degrees) and the overshoot in the STOP_S after it. Same box sizes as the hover.
TURN_ID = "hover.turn"
TURN_VERSION = 1
TURN_START_DEG, TURN_STOP_S = 10.0, 4.0
TURN_LEVELS: Dict[int, dict] = {   # (good, limit); rate_sd = standard deviation of the turn rate, deg/s
    1: dict(radius=(6.0, 15.0), rms=(3.0, 8.0), excursion=(6.0, 12.0), rate_sd=(8.0, 16.0), time=(60, 120), over=(15, 40)),
    2: dict(radius=(4.0, 8.0), rms=(1.5, 4.0), excursion=(4.0, 8.0), rate_sd=(6.0, 12.0), time=(50, 100), over=(12, 30)),
    3: dict(radius=(3.0, 6.0), rms=(1.2, 3.5), excursion=(3.0, 6.0), rate_sd=(4.0, 9.0), time=(40, 80), over=(8, 20)),
    4: dict(radius=(2.0, 5.0), rms=(1.0, 3.0), excursion=(2.5, 5.0), rate_sd=(3.0, 7.0), time=(30, 60), over=(5, 15)),
    5: dict(radius=(1.5, 3.0), rms=(0.6, 2.0), excursion=(1.5, 3.0), rate_sd=(2.0, 5.0), time=(25, 45), over=(3, 10)),
}


def turn(level: int) -> Exercise:
    level = max(LEVELS[0], min(LEVELS[-1], int(level)))
    lv = TURN_LEVELS[level]
    crit = (
        Criterion("turn.radius_p95", "Position drift", 30, "turn.radius_p95", *lv["radius"], "m",
                  ("pos",), "turn.drift", core=True),
        Criterion("turn.height_rms", "Height holding", 20, "turn.height_rms", *lv["rms"], "ft",
                  ("agl_ft",), "turn.height", core=True),
        Criterion("turn.height_max", "Largest height excursion", 10, "turn.height_max", *lv["excursion"], "ft",
                  ("agl_ft",), "turn.height"),
        Criterion("turn.rate_sd", "Steady turn rate", 15, "turn.rate_sd", *lv["rate_sd"], "deg/s",
                  ("hdg",), "turn.rate", core=True),
        Criterion("turn.time", "Time for the 360", 10, "turn.time", *lv["time"], "s",
                  ("hdg",), "turn.rate"),
        Criterion("turn.overshoot", "Stopping on the heading", 15, "turn.overshoot", *lv["over"], "deg",
                  ("hdg",), "turn.stop"),
    )
    # duration: the longest a turn may take before it is stopped (the time criterion is lost long before)
    return Exercise(TURN_ID, TURN_VERSION, "turn", "hover turn", HELICOPTERS, level, float(lv["time"][1]) + 30.0, crit,
                    dict(HOVER_LEVELS[level]["box"]))


# ---------------------------------------------------------------- fuel: bingo discipline
# Said on the ground: home = here, bingo = fuel now - ALLOWANCE, and after bingo you have AFTER_SHARE x
# ALLOWANCE to land back here. Scored: how soon you turned for home after bingo, the fuel you used after
# bingo, how directly you came back, and how close to home you landed. The instructor doesn't call bingo.
FUEL_ID = "fuel.recovery"
FUEL_VERSION = 1
FUEL_ALLOWANCE_LBS = 80.0          # config.INSTRUCTOR_FUEL_ALLOWANCE_LBS overrides
FUEL_AFTER_SHARE = 1.25
FUEL_MIN_OUT_M = 2000.0
FUEL_MAX_S = 45 * 60.0
FUEL_LEVELS: Dict[int, dict] = {   # (good, limit); after = fuel used after bingo, % of what you had
    1: dict(turn=(60, 180), after=(100, 140), direct=(25, 60), home=(150, 800)),
    2: dict(turn=(45, 150), after=(95, 130), direct=(20, 50), home=(100, 500)),
    3: dict(turn=(30, 120), after=(90, 120), direct=(15, 40), home=(60, 300)),
    4: dict(turn=(20, 90), after=(85, 110), direct=(10, 30), home=(40, 200)),
    5: dict(turn=(10, 60), after=(80, 100), direct=(8, 25), home=(25, 100)),
}


def fuel(level: int) -> Exercise:
    level = max(LEVELS[0], min(LEVELS[-1], int(level)))
    lv = FUEL_LEVELS[level]
    crit = (
        Criterion("fuel.turn_delay", "Turning for home at bingo", 35, "fuel.turn_delay", *lv["turn"], "s",
                  ("fuel_lbs", "ll"), "fuel.decision", core=True),
        Criterion("fuel.after_bingo", "Fuel used after bingo", 35, "fuel.after_bingo", *lv["after"], "%",
                  ("fuel_lbs",), "fuel.margin", core=True),
        Criterion("fuel.direct", "Direct return", 15, "fuel.direct", *lv["direct"], "deg",
                  ("ll",), "fuel.decision"),
        Criterion("fuel.home_dist", "Landing back at the start", 15, "fuel.home_dist", *lv["home"], "m",
                  ("ll",), "fuel.margin"),
    )
    return Exercise(FUEL_ID, FUEL_VERSION, "fuel", "fuel", HELICOPTERS, level, FUEL_MAX_S, crit, {})


EXERCISES = {HOVER_ID: hover, TURN_ID: turn, APPROACH_ID: approach, FUEL_ID: fuel}
NAMES = {HOVER_ID: "hover", TURN_ID: "hover turn", APPROACH_ID: "approach", FUEL_ID: "fuel"}


def get(exercise_id: str, level: int) -> Optional[Exercise]:
    make = EXERCISES.get(exercise_id)
    return make(level) if make else None


def brief(ex: Exercise, context: Optional[dict] = None) -> str:
    """One spoken line: what to do and how it's judged."""
    c = context or {}
    if ex.id == APPROACH_ID:
        return (f"Instructor, approach exercise, level {ex.level}, to {c.get('target_name', 'the point')}. "
                f"Fly out beyond {APPROACH_ARM_OUT_M:.0f} meters, then approach and land on it. "
                f"The clock starts at {APPROACH_START_M:.0f} meters. Say begin when ready.")
    if ex.id == FUEL_ID:
        return (f"Instructor, fuel exercise, level {ex.level}. Say begin on the ground. I'll give you a bingo; "
                f"fly out at least {FUEL_MIN_OUT_M / 1000:.0f} kilometers, turn for home at bingo, and land back where you started.")
    if ex.id == TURN_ID:
        return (f"Instructor, hover turn exercise, level {ex.level}. From a steady hover, turn 360 degrees on the spot, "
                f"left or right, and stop on the heading you started on. Hold your position and height. "
                f"Say begin when ready.")
    b = ex.box
    return (f"Instructor, {ex.name} exercise, level {ex.level}. Hold a steady hover for {ex.duration_s:.0f} seconds. "
            f"Box: {b['radius_m']:g} meters, {b['height_ft']:g} feet, {b['heading_deg']:g} degrees. "
            f"Say begin when ready.")


def brief_lines(ex: Exercise, context: Optional[dict] = None) -> list:
    """The kneeboard Briefing section: (title, text) rows."""
    rows = [("What to do", brief(ex, context).replace("Instructor, ", "", 1))]
    for c in ex.criteria:
        better = "at least" if c.higher_is_better else "at most"
        rows.append((f"{c.label} ({c.points:g} points)",
                     f"Full points {better} {c.good:g} {c.units}; none past {c.limit:g} {c.units}."))
    return rows
