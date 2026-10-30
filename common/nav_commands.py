"""
common/nav_commands.py
Navigation questions, for any aircraft (answers in common/nav.py):
    "next waypoint" · "where's the next waypoint"
    "waypoint 3" · "how far to waypoint 3"
    "where's home" · "bearing home" · "how far home"
    "fuel to home" · "fuel to waypoint 4" · "bingo?" · "can we make it home"   (needs a fuel reading)
"""
import re
from typing import Callable, Optional

from common import nav

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "zero": 0}
_N = r"(\d{1,2}|" + "|".join(_NUM) + r")"
_WP = r"(?:waypoint|way point|steer ?point|wp)"

_FUEL = re.compile(r"\bfuel (?:to|for|needed for|to get to) (?:home|base|" + _WP + r" " + _N + r")\b|"
                   r"\bbingo\b|\bcan we make (?:it )?(?:home|back)\b|\benough fuel\b")
_NEXT = re.compile(r"\b(?:next|active|current) " + _WP + r"\b")
_NUMBERED = re.compile(r"\b" + _WP + r" " + _N + r"\b")
_HOME = re.compile(r"\bwhere(?:'s| s| is) home\b|\bhome plate\b|\b(?:bearing|distance|steer|heading|vector|direction) (?:to )?home\b|"
                   r"\bhow far (?:is |to |back )?home\b|\bwhich way (?:is )?home\b|\btake (?:us|me) home\b")


# How Whisper hears "waypoint" (Huey flight 18 Oct 2026): "way of point two", "away point three",
# "way of 0.4" ("point" became "0."), "waypoint5", "waypoint tool".
_WP_HEARD = re.compile(r"\b(?:a ?way|way|weigh|why|wait)(?: of| a| the)? ?points?\b")
_WP_DOT = re.compile(r"\b(?:a ?way|way|weigh)(?: of| a)? 0?\.(\d{1,2})\b")
_NUM_HEARD = {"tool": "two", "to": "two", "too": "two", "for": "four", "fore": "four", "won": "one",
              "tree": "three", "ate": "eight", "sex": "six", "fife": "five", "niner": "nine"}


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = _WP_DOT.sub(r"waypoint \1", t)
    t = re.sub(r"\b(?:george|chief|please|okay|ok|the)\b", " ", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = _WP_HEARD.sub("waypoint", t)
    t = re.sub(r"\b(waypoint|wp)(\d)", r"\1 \2", t)
    t = re.sub(r"\b(waypoint) (" + "|".join(_NUM_HEARD) + r")\b", lambda m: f"{m.group(1)} {_NUM_HEARD[m.group(2)]}", t)
    return re.sub(r"\s+", " ", t).strip()


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM[tok]


def handle(text: str, snap: dict, fuel_lbs: Optional[float] = None, burn_lbs_hr: float = 555.0,
           reserve_lbs: float = 185.0) -> Optional[str]:
    """What to say, or None if this wasn't a navigation question."""
    t = _norm(text)
    m = _FUEL.search(t)
    if m:
        which = _num(m.group(1)) if m.group(1) else "home"
        return nav.fuel_to(snap, fuel_lbs, burn_lbs_hr, reserve_lbs, which)
    if _NEXT.search(t):
        return nav.waypoint_report(snap, "next")
    m = _NUMBERED.search(t)
    if m and not re.search(r"\b(?:point search|search)\b", t):     # Apache "point search T07"
        return nav.waypoint_report(snap, _num(m.group(1)))
    if _HOME.search(t):
        return nav.waypoint_report(snap, "home")
    return None
