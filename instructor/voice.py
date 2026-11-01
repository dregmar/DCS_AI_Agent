"""
instructor/voice.py - "Instructor, ..." phrases. Fast path only: the model never starts, stops or
scores an exercise (proposal section 9).

    Instructor, hover exercise                  select + brief (also "hover practice / drill")
    Instructor, hover turn / pedal turn / 360 turn exercise   the 360 on the spot (Phase 3)
    Instructor, approach exercise / approach to Alpha / landing to waypoint Bravo
                                                select the approach (a named waypoint, else the nearest FARP)
    Instructor, fuel exercise / bingo exercise  select the fuel exercise
    Instructor, begin / start [hover / approach / fuel]
    Instructor, stop / abort / cancel           stop the attempt (stored as aborted, never scored)
    Instructor, how did I do / results          the last result in more detail
    Instructor, status                          what's selected / running
    Instructor, easier / harder / level 3       the level of the next attempt
    Instructor, what should I practise          the suggested exercise
    Instructor, on / mode on                    what it can do; "off" / "mode off": stop and stand by
Anything else after the wake word gets "Instructor, say again ...", so it never reaches George.
"""
import re
from typing import Optional, Tuple

# Whisper sound-alikes for the wake word (the word is also added to the STT prompt)
WAKE = re.compile(r"^\s*(?:instructor|instructors|instructer|in structure|instruct her|constructor)"
                  r"[\s,.!?:;-]+(.*)$", re.I)
_STOP = re.compile(r"\b(?:stop|abort|cancel|end|terminate|knock it off|quit)\b")
_OFF = re.compile(r"\b(?:off|standby|stand by|dismissed)\b")
_ON = re.compile(r"^(?:mode on|on|instructor mode on|hello|ready to train|training)$")
_RESULT = re.compile(r"\bhow did i do\b|\bhow'd i do\b|\bresults?\b|\bscore\b|\bdebrief\b|\bgrade\b|\bfeedback\b")
_STATUS = re.compile(r"\bstatus\b|\bwhat'?s selected\b")
_SUGGEST = re.compile(r"\bwhat should i (?:practi[cs]e|work on|fly|do)\b|\bsuggest|\brecommend|\bnext exercise\b")
_EASIER = re.compile(r"\b(?:easier|make it easier|level down|lower (?:the )?level)\b")
_HARDER = re.compile(r"\b(?:harder|make it harder|level up|raise (?:the )?level)\b")
_LEVEL = re.compile(r"\blevel (one|two|three|four|five|[1-5])\b")
_BEGIN = re.compile(r"\b(?:begin|start|commence|ready|go)\b")
_HOVER = re.compile(r"\bhover(?:ing)?\b")
# "hover turn", "pedal turn", "360 turn", "turn exercise", "spot turn" (checked before the plain hover)
_TURN = re.compile(r"\b(?:pedal|spot|hover|three sixty|360)\s+turns?\b|\bturns?\s+(?:exercise|practice|drill)\b|"
                   r"\b(?:three sixty|360)s?\b")
_APPROACH = re.compile(r"\b(?:approach|landing|land)\b")
_FUEL = re.compile(r"\b(?:fuel|bingo)\b")
_BRIEF = re.compile(r"\b(?:brief|explain|what is|what's)\b")
_TARGET = re.compile(r"\b(?:to|on|at|for)\s+(?:the\s+)?(?:waypoint\s+)?([a-z0-9-]+)\b|\bwaypoint\s+([a-z0-9-]+)\b")
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def addressed(text: str) -> Optional[str]:
    """The words after "Instructor," or None when the instructor isn't addressed."""
    m = WAKE.match(text or "")
    return m.group(1).strip(" \t.,!?;:") if m else None


def exercise_in(t: str) -> Optional[str]:
    if _APPROACH.search(t):
        return "approach.pad"
    if _FUEL.search(t):
        return "fuel.recovery"
    if _TURN.search(t):
        return "hover.turn"
    if _HOVER.search(t):
        return "hover.stationary"
    return None


def names_target(rest: str) -> Optional[str]:
    """'approach to Alpha' / 'landing on waypoint Bravo' -> 'alpha' / 'bravo' (the word, lower case)."""
    m = _TARGET.search((rest or "").lower())
    if not m:
        return None
    word = m.group(1) or m.group(2)
    return None if word in ("the", "a", "an", "exercise", "practice", "drill", "farp", "pad", "helipad") else word


def parse(rest: str) -> Tuple[str, Optional[str], Optional[int]]:
    """(command, exercise id, level) for the words after the wake word. command: select / begin / stop /
    off / on / result / status / brief / easier / harder / level / suggest / unknown."""
    t = (rest or "").lower().strip(" .!?")
    ex = exercise_in(t)
    if _STOP.search(t):
        return "stop", None, None
    if _ON.match(t):
        return "on", None, None
    if _RESULT.search(t):
        return "result", None, None
    if _STATUS.search(t):
        return "status", None, None
    if _OFF.search(t):
        return "off", None, None
    if _SUGGEST.search(t):
        return "suggest", None, None
    if _EASIER.search(t):
        return "easier", None, None
    if _HARDER.search(t):
        return "harder", None, None
    m = _LEVEL.search(t)
    if m and not _BEGIN.search(t):
        return "level", None, int(_NUM.get(m.group(1), m.group(1)))
    if _BEGIN.search(t):
        return "begin", ex, None
    if ex:
        return ("brief" if _BRIEF.search(t) else "select"), ex, None
    return "unknown", None, None
