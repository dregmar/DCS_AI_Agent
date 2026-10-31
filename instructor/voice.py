"""
instructor/voice.py - "Instructor, ..." phrases. Fast path only: the model never starts, stops or
scores an exercise (proposal section 9).

    Instructor, hover exercise          select + brief (one line; also "hover practice / drill / hover")
    Instructor, begin / start           arm: the clock starts once you're in a steady hover
    Instructor, begin hover exercise    both at once
    Instructor, stop / abort / cancel   stop the attempt (stored as aborted, never scored)
    Instructor, how did I do / results  the last result in more detail
    Instructor, status                  what's selected / running
    Instructor, off                     stop and go idle
Anything else after the wake word gets "Instructor, say again ...", so it never reaches George.
"""
import re
from typing import Optional, Tuple

# Whisper sound-alikes for the wake word (the word is also added to the STT prompt)
WAKE = re.compile(r"^\s*(?:instructor|instructors|instructer|in structure|instruct her|constructor)"
                  r"[\s,.!?:;-]+(.*)$", re.I)
_STOP = re.compile(r"\b(?:stop|abort|cancel|end|terminate|knock it off|quit)\b")
_OFF = re.compile(r"\b(?:off|standby|stand by|dismissed)\b")
_RESULT = re.compile(r"\bhow did i do\b|\bhow'd i do\b|\bresults?\b|\bscore\b|\bdebrief\b|\bgrade\b|\bfeedback\b")
_STATUS = re.compile(r"\bstatus\b|\bwhat'?s selected\b")
_BEGIN = re.compile(r"\b(?:begin|start|commence|ready|go)\b")
_HOVER = re.compile(r"\bhover(?:ing)?\b")
_BRIEF = re.compile(r"\b(?:brief|explain|what is|what's)\b")


def addressed(text: str) -> Optional[str]:
    """The words after "Instructor," or None when the instructor isn't addressed."""
    m = WAKE.match(text or "")
    return m.group(1).strip(" \t.,!?;:") if m else None


def parse(rest: str) -> Tuple[str, Optional[str]]:
    """(command, exercise) for the words after the wake word. command: select / begin / stop / off /
    result / status / brief / unknown."""
    t = (rest or "").lower().strip(" .!?")
    ex = "hover.stationary" if _HOVER.search(t) else None
    if _STOP.search(t):
        return "stop", None
    if _RESULT.search(t):
        return "result", None
    if _STATUS.search(t):
        return "status", None
    if _OFF.search(t):
        return "off", None
    if _BEGIN.search(t):
        return "begin", ex
    if ex:
        return ("brief" if _BRIEF.search(t) else "select"), ex
    return "unknown", None
