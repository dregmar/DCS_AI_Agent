"""
command/roles.py - the command roles (Phase 2) and your callsign (P2-2).

PILOT is anything not sent by a role: you, your wingmen and Outcasts.

Your callsign comes from the mission: george_comms_hook.lua sends "CALLSIGN Enfield11\\t<unit name>"
(Unit:getCallsign()) every 10 s. It's said as "Enfield 1-1". With nothing from the hook (old hook, no
unit yet, an empty callsign, older than CALLSIGN_MAX_AGE_S, or another unit's) the fallback is
config OPERATOR_CALLSIGN, default "NOMAD" (said "Nomad").
"""
import re
from typing import Optional, Tuple

PILOT = "PILOT"
WARLORD = "WARLORD"
OVERLORD = "OVERLORD"
DARKSIDE = "DARKSIDE"
ROLES = (PILOT, WARLORD, OVERLORD, DARKSIDE)

class Said(str):
    """A reply that is one transmission plus others in other voices, spoken around it in order:
    pre (before it) and then (after it), each a (text, speaker) pair. It's a str, so everything that
    passes replies along keeps working; main._speak_as speaks the extra ones."""
    def __new__(cls, text: str, pre=(), then=()):
        obj = str.__new__(cls, text)
        obj.pre, obj.then = tuple(pre), tuple(then)
        return obj


DEFAULT_CALLSIGN = "NOMAD"
CALLSIGN_MAX_AGE_S = 60.0        # the hook resends every 10 s


def parse_callsign(text: str) -> Optional[Tuple[str, str]]:
    """'CALLSIGN Enfield11\\tAerial-1-1' -> ('Enfield11', 'Aerial-1-1'); None if it isn't one."""
    if not isinstance(text, str) or not text.startswith("CALLSIGN "):
        return None
    body = text[len("CALLSIGN "):].strip("\r\n")
    raw, _, unit = body.partition("\t")
    raw = raw.strip()
    return (raw, unit.strip()) if raw else None


def _word(w: str) -> str:
    return w.capitalize() if w.isupper() or w.islower() else w     # "NOMAD" / "enfield" -> said as a word


def spoken(raw: str) -> str:
    """How a DCS callsign is said: 'Enfield11' -> 'Enfield 1-1', 'Colt12' -> 'Colt 1-2',
    'Uzi 3 1' / 'Uzi3-1' -> 'Uzi 3-1', 'Springfield101' -> 'Springfield 10-1', 'NOMAD' -> 'Nomad',
    '101' (eastern aircraft: a number) stays '101'."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return ""
    m = re.fullmatch(r"([A-Za-z][A-Za-z' ]*?)\s*(\d{1,2})\s*[- ]?\s*(\d)", s)
    if m:
        return f"{' '.join(_word(w) for w in m.group(1).split())} {int(m.group(2))}-{m.group(3)}"
    return " ".join(_word(w) if w.isalpha() else w for w in s.split())


def _your_flight_unit(snap: dict, unit: str) -> bool:
    """Is unit the player's own unit in the mission line-up? Export.lua's UnitName can be your pilot
    name ("RedArcher") while DCS's unit is "Rotary-2-1" (flight of 1 Nov): the hook's callsign is then
    still yours."""
    for u in snap.get("flight_units") or []:
        try:
            if u[1] == unit and (str(u[2]).lower() in ("player", "client") or u[3] is True):
                return True
        except (IndexError, TypeError):
            continue
    return False


def callsign(snap: dict, fallback: str = DEFAULT_CALLSIGN) -> Tuple[str, str]:
    """(what to call you, where it came from). Where: 'DCS' or why the fallback is used."""
    raw, unit, age = snap.get("callsign_raw"), snap.get("callsign_unit"), snap.get("callsign_age")
    fb = spoken(fallback or DEFAULT_CALLSIGN) or "Nomad"
    if not raw:
        return fb, "fallback: no callsign from the comms hook"
    if age is None or age > CALLSIGN_MAX_AGE_S:
        return fb, "fallback: the hook's callsign is old"
    mine = snap.get("my_unit")
    if unit and mine and unit != mine and not _your_flight_unit(snap, unit):
        return fb, f"fallback: the hook's callsign is for {unit}, not your unit"
    said = spoken(raw)
    return (said, "DCS") if said else (fb, "fallback: empty callsign")


def callsign_link(snap: dict, fallback: str = DEFAULT_CALLSIGN) -> dict:
    """The Status page row."""
    said, where = callsign(snap, fallback)
    raw = snap.get("callsign_raw")
    detail = f"{said} (from DCS: {raw})" if where == "DCS" else f"{said} ({where})"
    return {"name": "Pilot callsign", "ok": True if where == "DCS" else None, "detail": detail}
