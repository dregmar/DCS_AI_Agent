"""
command/wake.py - "Warlord, ..." and "Darkside, ..." as the first word(s) (Phase 2, P2-3).

    addressed(text) -> (role, rest) | None      role: command.roles.WARLORD / DARKSIDE

Like common/overlord.py addressed(): Whisper's spellings of the name, after an optional "hey" / "okay",
then the rest of the utterance. Overlord keeps his own wake word; none of these spellings is one of his.
"""
import difflib
import re
from typing import Optional, Tuple

from command.roles import DARKSIDE, WARLORD

_LEAD = r"^\s*(?:(?:hey|okay|ok|uh|um)[\s,.]+)*"
_NAMES = {
    WARLORD: re.compile(_LEAD + r"(?:war[\s-]?lords?|war[\s-]?lord's|warlords?|war[\s-]?load|war\s?lourd|"
                                r"walord|wore\s?lord|war\s?law)\b[\s,.!?]*(.*)$", re.I),
    DARKSIDE: re.compile(_LEAD + r"(?:dark[\s-]?sides?|dark[\s-]?side's|darksides?|dark[\s-]?sight|"
                                 r"dark[\s-]?site|dark[\s-]?sighed|doc\s?side|dart\s?side)\b[\s,.!?]*(.*)$", re.I),
}
_TARGET = {WARLORD: "warlord", DARKSIDE: "darkside"}


def addressed(text: str) -> Optional[Tuple[str, str]]:
    """(role, the rest) when the utterance starts with Warlord's or Darkside's name, else None."""
    t = text or ""
    for role, rx in _NAMES.items():
        m = rx.match(t)
        if m:
            return role, m.group(1).strip()
    m = re.match(_LEAD + r"([a-z']+)(?:[\s,.-]+([a-z']+))?[\s,.!?]*(.*)$", t, re.I)
    if not m:
        return None
    one, two, tail = m.group(1).lower(), (m.group(2) or "").lower(), m.group(3)
    for role, word in _TARGET.items():
        if len(one) >= 6 and one[0] == word[0] and difflib.SequenceMatcher(None, one, word).ratio() >= 0.8:
            return role, ((m.group(2) or "") + " " + tail).strip()
        if two and one[0] == word[0] and difflib.SequenceMatcher(None, one + two, word).ratio() >= 0.85:
            return role, tail.strip()
    return None
