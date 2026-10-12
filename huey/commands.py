"""
huey/commands.py
What the UH-1H crew understands. match(text, checklist_active) turns a transcript into a
command dict, or None ("not sure": the LLM gets it). sanitize() validates an LLM result the
same way, so both paths produce the same, safe commands.

Command dict: {"action": ..., "speech": ..., plus per action:
    CREW_ROE / CREW_BURST  "seats": [left/right/copilot], "value": HOLD/RETURN/FREE or SHORT/LONG
    AUTOPILOT              "value": TOGGLE / ATTITUDE / LEVEL / ORBIT
    SWITCH                 "key", "value" (a state from huey.cockpit.SWITCHES)
    RADIO                  "key": uhf / vhf_am / vhf_fm, "mhz"
    CHECKLIST_START        "value": checklist name;  CHECKLIST_REPLY "value": CHECK/SET/SKIP/REPEAT/STOP
    PANEL                  "value": SHOW / HIDE
    FLARE, CREW_REPORT, STATUS, FUEL, ENGINE, CONTACTS, CAUTIONS, RADIO_REPORT, NONE}
"""
import re
from typing import Any, Dict, List, Optional

from huey.checklists import CHECKLISTS, REPLIES
from huey.cockpit import RADIOS, SWITCHES

SEATS = ("left", "right", "copilot")
ACTIONS = {"CREW_ROE", "CREW_BURST", "AUTOPILOT", "SWITCH", "RADIO", "CHECKLIST_START",
           "CHECKLIST_REPLY", "PANEL", "FLARE", "CREW_REPORT", "STATUS", "FUEL", "ENGINE",
           "CONTACTS", "CAUTIONS", "RADIO_REPORT", "NONE"}

_FILLER = re.compile(r"\b(?:george|chief|crew chief|please|okay|ok|now|just|uh|um|hey|alright|"
                     r"roger|copy|can you|could you|would you|go ahead and|i need you to|"
                     r"i want you to|let's|lets)\b")
_NEG = re.compile(r"\b(?:not|dont|don't|do not|never|negative|disregard)\b")


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9.' ]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)           # keep decimal points only
    t = _FILLER.sub(" ", t)
    t = re.sub(r"^\s*(?:and|so|then|also)\s+", " ", t)
    t = re.sub(r"\b(?:the|a|my|your|our)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _r(action: str, speech: str = "", **kw) -> Dict[str, Any]:
    d = {"action": action, "speech": speech, "source": "fast"}
    d.update(kw)
    return d


# ------------------------------------------------------------------ crew
_ROE = [
    ("HOLD", re.compile(r"\b(?:hold(?:ing)? fire|cease (?:fire|firing)|check fire|weapons? hold|"
                        r"stop (?:shooting|firing)|hold your fire|ceasefire)\b")),
    ("RETURN", re.compile(r"\b(?:return fire|ret fire|weapons? tight|defensive fire|"
                          r"fire only if fired (?:up)?on|only return fire)\b")),
    ("FREE", re.compile(r"\b(?:weapons? free|free fire|fire at will|open fire|cleared hot|"
                        r"cleared to (?:engage|fire)|engage at will|light (?:em|them) up)\b")),
]
_BURST = re.compile(r"\b(short|long) bursts?\b")
_LEFT = re.compile(r"\bleft\b")
_RIGHT = re.compile(r"\bright\b")
_COPILOT = re.compile(r"\b(?:co ?pilot|operator)\b")
_ALL = re.compile(r"\b(?:all|everyone|everybody|crew)\b")
_BOTH = re.compile(r"\b(?:both|gunners|door gunners|guns|doors)\b")


def _seats(t: str, default: List[str]) -> List[str]:
    if _ALL.search(t):
        return list(SEATS)
    out = []
    if _LEFT.search(t):
        out.append("left")
    if _RIGHT.search(t):
        out.append("right")
    if _COPILOT.search(t):
        out.append("copilot")
    if not out and _BOTH.search(t):
        out = ["left", "right"]
    return out or list(default)


def _match_crew(t: str) -> Optional[Dict[str, Any]]:
    for value, rx in _ROE:
        if rx.search(t):
            if value != "HOLD" and _NEG.search(t):
                return None
            # "cease fire" with nobody named stops everyone, the co-pilot too
            default = list(SEATS) if value == "HOLD" else ["left", "right"]
            return _r("CREW_ROE", seats=_seats(t, default), value=value)
    m = _BURST.search(t)
    if m and not _NEG.search(t):
        return _r("CREW_BURST", seats=_seats(t, ["left", "right"]), value=m.group(1).upper())
    if re.search(r"\b(?:crew|gunners?|gunner) (?:status|report|check)\b|\bammo (?:check|status|state|count)\b"
                 r"|\bhow (?:are|is) (?:gunners?|crew)\b|\bgunners? ammo\b", t):
        return _r("CREW_REPORT")
    m = re.search(r"\b(show|hide|open|close|toggle)\b.*\b(?:crew|gunner|status) panel\b", t)
    if m:
        return _r("PANEL", value="HIDE" if m.group(1) in ("hide", "close") else "SHOW")
    return None


# ------------------------------------------------------------------ autopilot
def _match_autopilot(t: str) -> Optional[Dict[str, Any]]:
    if re.search(r"\battitude hold\b", t):
        return _r("AUTOPILOT", value="ATTITUDE")
    if re.search(r"\b(?:level flight|hold level|level off and hold)\b", t):
        return _r("AUTOPILOT", value="LEVEL")
    if re.search(r"\b(?:orbit|start (?:an )?orbit|circle here)\b", t) and "autopilot" in t or t in ("orbit", "start orbit"):
        return _r("AUTOPILOT", value="ORBIT")
    if re.search(r"\bauto ?pilot\b", t) and not _NEG.search(t):
        return _r("AUTOPILOT", value="TOGGLE")
    return None


# ------------------------------------------------------------------ switches
_ON = r"(?:on|up)"
_OFF = r"(?:off|out|down)"
_SWITCH_PHRASES = [
    # (regex, key, state)
    (r"\bmaster arm (?:on|armed|arm|hot)\b|^(?:arm|arm up|go hot|weapons hot)$", "master_arm", "ARMED"),
    (r"\bmaster arm safe\b|^(?:safe|safe it|safe weapons|weapons safe|go safe)$", "master_arm", "SAFE"),
    (r"\bmaster arm off\b|^(?:weapons cold|go cold)$", "master_arm", "OFF"),
    (r"^(?:select )?rockets$|\bselect rockets\b|\b2 ?75\b|\btwo seventy five\b", "arm_sel", "ROCKETS"),
    (r"^(?:select )?(?:guns|miniguns|mini guns)$|\bselect (?:guns|miniguns|mini guns)\b|\b7 ?62\b|"
     r"\bseven six two\b", "arm_sel", "GUNS"),
    (r"\b(?:grenades?|grenade launcher|forty mike mike|40 ?mm|40 mike mike)\b", "arm_sel", "GRENADES"),
    (r"\bgun selector (?:left|left guns?)\b|^left guns only$", "gun_sel", "LEFT"),
    (r"\bgun selector (?:right|right guns?)\b|^right guns only$", "gun_sel", "RIGHT"),
    (r"\bgun selector (?:both|all)\b|^(?:both|all) guns$", "gun_sel", "BOTH"),
    (r"\b(?:flares?|countermeasures|dispenser) (?:armed|arm|on|hot)\b|\barm (?:flares?|countermeasures|dispenser)\b",
     "cm_arm", "ARMED"),
    (r"\b(?:flares?|countermeasures|dispenser) (?:safe|off|cold)\b|\bsafe (?:flares?|countermeasures|dispenser)\b",
     "cm_arm", "SAFE"),
    (r"\blanding lights? " + _ON + r"\b", "ldg_light", "ON"),
    (r"\blanding lights? " + _OFF + r"\b", "ldg_light", "OFF"),
    (r"\bsearch ?lights? " + _ON + r"\b", "search_light", "ON"),
    (r"\bsearch ?lights? off\b", "search_light", "OFF"),
    (r"\bsearch ?lights? stow(?:ed)?\b|\bstow (?:search ?light)\b", "search_light", "STOW"),
    (r"\b(?:anti ?collision|beacon|strobe)s?(?: lights?)? on\b", "anticoll", "ON"),
    (r"\b(?:anti ?collision|beacon|strobe)s?(?: lights?)? off\b", "anticoll", "OFF"),
    (r"\b(?:nav|navigation|position) lights? (?:steady|on)\b", "nav_lts", "STEADY"),
    (r"\b(?:nav|navigation|position) lights? (?:flash|flashing)\b", "nav_lts", "FLASH"),
    (r"\b(?:nav|navigation|position) lights? off\b", "nav_lts", "OFF"),
    (r"\b(?:position|nav|navigation) lights? (?:dim)\b", "pos_lts", "DIM"),
    (r"\b(?:position|nav|navigation) lights? (?:bright)\b", "pos_lts", "BRIGHT"),
    (r"\bpitot heat(?:er)? on\b", "pitot", "ON"),
    (r"\bpitot heat(?:er)? off\b", "pitot", "OFF"),
    (r"\bbattery on\b", "battery", "ON"),
    (r"\bbattery off\b", "battery", "OFF"),
    (r"\b(?:main )?fuel (?:switch )?on\b", "main_fuel", "ON"),
    (r"\bmain fuel (?:switch )?off\b", "main_fuel", "OFF"),
    (r"\bforce trim on\b", "force_trim", "ON"),
    (r"\bforce trim off\b", "force_trim", "OFF"),
    (r"\b(?:hydraulics?|hyd control|hydraulic control) on\b", "hyd_cont", "ON"),
    (r"\b(?:hydraulics?|hyd control|hydraulic control) off\b", "hyd_cont", "OFF"),
    (r"\binverter (?:main|on)\b", "inverter", "MAIN"),
    (r"\binverter off\b", "inverter", "OFF"),
    (r"\b(?:main )?generator on\b", "main_gen", "ON"),
    (r"\bstarter generator (?:to )?standby\b|\bstandby generator\b", "starter_gen", "STBY GEN"),
    (r"\bstarter generator (?:to )?start\b", "starter_gen", "START"),
    (r"\blow rpm (?:audio|warning) on\b", "low_rpm_audio", "AUDIO"),
    (r"\blow rpm (?:audio|warning) off\b", "low_rpm_audio", "OFF"),
]
_SWITCH_RX = [(re.compile(rx), key, state) for rx, key, state in _SWITCH_PHRASES]
_PAIRS = re.compile(r"\brocket pairs? (\d|one|two|three|four|five|six|seven|zero)\b|"
                    r"\b(\d|one|two|three|four|five|six|seven|zero) (?:rocket )?pairs?\b")
_SMALL = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_FLARE = re.compile(r"^(?:flares?|pop (?:a )?flares?|dispense(?: flares?)?|drop (?:a )?flares?|"
                    r"flare flare|deploy flares?|kick (?:out )?flares?|flares now)$")


def _match_switch(t: str) -> Optional[Dict[str, Any]]:
    m = _PAIRS.search(t)
    if m:
        tok = m.group(1) or m.group(2)
        n = int(tok) if tok.isdigit() else _SMALL[tok]
        if 0 <= n <= 7:
            return _r("SWITCH", key="rocket_pair", value=str(n))
    if _FLARE.search(t):
        return _r("FLARE")
    if _NEG.search(t):
        return None
    for rx, key, state in _SWITCH_RX:
        if rx.search(t):
            return _r("SWITCH", key=key, value=state)
    return None


# ------------------------------------------------------------------ radios
_RADIO_WORD = re.compile(r"\b(uhf|uniform|vhf|victor|fm|fox ?mike)\b")
_DIGITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
           "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9", "tree": "3", "fife": "5"}


_SOUNDALIKE = {"for": "4", "to": "2", "too": "2", "won": "1", "ate": "8", "free": "3"}


def _freq_after(t: str) -> Optional[float]:
    """'251.5' / '251 decimal 5' / 'two five one point five' / '2 4 2.1' / '2 for 2.1' -> MHz.
    Joins the digits that follow the radio name (Whisper often splits them), up to the first
    other word."""
    out, started = "", False
    for tok in t.split():
        if tok in ("decimal", "point", "dot"):
            if not out or "." in out:
                break
            out += "."
        elif re.fullmatch(r"\d+(?:\.\d+)?", tok):
            if "." in tok and "." in out:
                break
            out += tok
        elif tok in _DIGITS:
            out += _DIGITS[tok]
        elif tok in _SOUNDALIKE and started:
            out += _SOUNDALIKE[tok]
        elif started:
            break
        else:
            continue                       # words before the number ("set", "to", "frequency")
        started = True
    m = re.fullmatch(r"(\d{2,3})(?:\.(\d{1,3}))?\.?", out)
    return float(m.group(1) + ("." + m.group(2) if m.group(2) else "")) if m else None


def _match_radio(t: str) -> Optional[Dict[str, Any]]:
    m = _RADIO_WORD.search(t)
    if not m:
        if re.search(r"\b(?:radio check|radios|frequencies|what are radios)\b", t):
            return _r("RADIO_REPORT")
        return None
    word = m.group(1)
    key = ("uhf" if word in ("uhf", "uniform") else "vhf_fm" if word.startswith(("fm", "fox"))
           else "vhf_am")
    mhz = _freq_after(t[m.end():])
    if mhz is None:
        return _r("RADIO_REPORT") if re.search(r"\b(?:check|frequency|what|status)\b", t) else None
    return _r("RADIO", key=key, mhz=mhz)


# ------------------------------------------------------------------ checklists
_CHECKLIST_NAMES = [
    ("before_takeoff", re.compile(r"\b(?:before|pre) ?take ?off\b|\btake ?off check")),
    ("before_start", re.compile(r"\b(?:before|pre) ?start(?:ing)?(?: engine)?\b|\bprestart\b")),
    ("runup", re.compile(r"\brun ?up\b")),
    ("shutdown", re.compile(r"\bshut ?down\b")),
    ("start", re.compile(r"\b(?:engine )?start(?:ing)?(?: engine)?\b")),
]
_CHECKLIST_WORD = re.compile(r"\bcheck ?lists?\b")
_REPLY = [
    ("STOP", re.compile(r"\b(?:stop|cancel|abort|end|quit) (?:the )?check ?list\b|^(?:stop|cancel|abort)$")),
    ("SET", re.compile(r"^(?:set it|you set it|do it|fix it|set that|go ahead set it|you do it|set)$")),
    ("SKIP", re.compile(r"^(?:skip(?: it| that| item)?|next item skip|pass)$")),
    ("REPEAT", re.compile(r"^(?:repeat|say again|again|what was that|repeat item)$")),
    ("CHECK", re.compile(r"^(?:check(?:ed)?|next|done|complete|set and checked|confirmed|good|"
                         r"on|off|yes|affirm(?:ative)?|in|tested|test(?:ed)? good|as required|"
                         r"check next|checked next)$")),
]


def _match_checklist(t: str, active: bool) -> Optional[Dict[str, Any]]:
    if _CHECKLIST_WORD.search(t) and not re.search(r"\b(?:stop|cancel|abort|end|quit)\b", t):
        for name, rx in _CHECKLIST_NAMES:
            if rx.search(t):
                return _r("CHECKLIST_START", value=name)
        return None
    if active:
        for word, rx in _REPLY:
            if rx.search(t):
                return _r("CHECKLIST_REPLY", value=word)
    return None


# ------------------------------------------------------------------ questions
_QUERIES = [
    ("STATUS", re.compile(r"^(?:status|sitrep|sit rep|situation report|status report)$")),
    ("FUEL", re.compile(r"\bfuel (?:state|check|status|remaining|quantity|left)\b|^fuel$|"
                        r"\bhow much fuel\b|\bwhat(?:'s| s| is)? fuel\b")),
    ("ENGINE", re.compile(r"\b(?:engine|gauges?|instruments?|engine instruments) (?:check|status|readout|report)\b|"
                          r"^(?:gauges|engine instruments|instruments)$|\bhow (?:is|s) engine\b")),
    ("CONTACTS", re.compile(r"\b(?:any )?(?:contacts?|threats?|enemies|bad guys)\b(?: around| near| out there)?$|"
                            r"\bwhat do you see\b")),
    ("CAUTIONS", re.compile(r"\b(?:cautions?|caution lights?|warning lights?|warnings?)\b(?: check| status)?$|"
                            r"\bany (?:cautions?|warnings?)\b")),
]


def match(text: str, checklist_active: bool = False) -> Optional[Dict[str, Any]]:
    t = _norm(text)
    if not t:
        return None
    for fn in (lambda: _match_checklist(t, checklist_active), lambda: _match_crew(t),
               lambda: _match_autopilot(t), lambda: _match_radio(t), lambda: _match_switch(t)):
        r = fn()
        if r:
            return r
    for action, rx in _QUERIES:
        if rx.search(t):
            return _r(action)
    return None


# ------------------------------------------------------------------ LLM
def llm_prompt(state_line: str, checklist: Optional[str]) -> str:
    switches = "; ".join(f"{k}: {'/'.join(v[1])}" for k, v in SWITCHES.items() if v[0])
    return f"""
You are the crew of a UH-1H Huey in DCS: the crew chief, the co-pilot and the two door gunners.
The pilot talks to you on the intercom. Turn what he says into ONE command.

Current state: {state_line}
Checklist running: {checklist or "none"}

Actions:
- "CREW_ROE": gunner rules of engagement. "seats": any of ["left","right","copilot"]
  (no one named: ["left","right"]; for HOLD with no one named: all three).
  "value": HOLD (hold fire / cease fire), RETURN (return fire only), FREE (free fire / weapons free).
- "CREW_BURST": gunner burst length. "seats" as above, "value": SHORT or LONG.
- "CREW_REPORT": gunners' ROE, ammo and burst.
- "PANEL": crew status panel, "value": SHOW or HIDE.
- "AUTOPILOT": "value": TOGGLE (on/off), ATTITUDE (attitude hold), LEVEL (level flight), ORBIT.
- "SWITCH": set a cockpit switch. "key" and "value" from: {switches}
- "FLARE": dispense a flare.
- "RADIO": tune a radio. "key": uhf, vhf_am or vhf_fm; "mhz": number.
- "RADIO_REPORT": read the radio frequencies.
- "CHECKLIST_START": "value" one of {', '.join(CHECKLISTS)}.
- "CHECKLIST_REPLY": answer to the running checklist, "value": {', '.join(REPLIES)}.
- "STATUS", "FUEL", "ENGINE" (engine instruments), "CONTACTS" (enemies nearby), "CAUTIONS".
- "NONE": chatter, or anything you can't do.

Rules: never invent switch names or values. Questions are never switch commands.
"Hold fire", "cease fire" always mean CREW_ROE HOLD. Keep speech under 12 words.

Respond STRICTLY in JSON:
{{"action": "<ACTION>", "seats": [..] or null, "key": "<key or null>", "value": "<value or null>",
  "mhz": <number or null>, "speech": "<short reply>"}}
"""


def sanitize(res: Dict[str, Any]) -> Dict[str, Any]:
    """LLM result -> a command dict that is safe to execute (unknown things become NONE)."""
    action = str(res.get("action") or "NONE").strip().upper()
    speech = str(res.get("speech") or "Copy.").strip()[:160]
    none = {"action": "NONE", "speech": speech}
    if action not in ACTIONS:
        print(f"[HUEY LLM] Rejected unknown action: {action!r}")
        return {"action": "NONE", "speech": "Say again."}
    value = str(res.get("value") or "").strip().upper()
    out = {"action": action, "speech": speech, "source": "llm"}
    if action in ("CREW_ROE", "CREW_BURST"):
        seats = [s for s in (res.get("seats") or []) if s in SEATS] or (
            list(SEATS) if value == "HOLD" else ["left", "right"])
        ok = value in (("HOLD", "RETURN", "FREE") if action == "CREW_ROE" else ("SHORT", "LONG"))
        return dict(out, seats=seats, value=value) if ok else {"action": "NONE", "speech": "Say again."}
    if action == "AUTOPILOT":
        return dict(out, value=value) if value in ("TOGGLE", "ATTITUDE", "LEVEL", "ORBIT") else none
    if action == "PANEL":
        return dict(out, value=value) if value in ("SHOW", "HIDE") else none
    if action == "SWITCH":
        key = str(res.get("key") or "").strip().lower()
        if key not in SWITCHES or SWITCHES[key][0] is None or value not in SWITCHES[key][1]:
            return {"action": "NONE", "speech": "Unable, which switch?"}
        return dict(out, key=key, value=value)
    if action == "RADIO":
        key = str(res.get("key") or "").strip().lower()
        try:
            mhz = float(res.get("mhz"))
        except (TypeError, ValueError):
            mhz = None
        if key not in RADIOS or mhz is None:
            return {"action": "NONE", "speech": "Say again the frequency."}
        return dict(out, key=key, mhz=mhz)
    if action == "CHECKLIST_START":
        value = value.lower()
        return dict(out, value=value) if value in CHECKLISTS else {"action": "NONE", "speech": "Which checklist?"}
    if action == "CHECKLIST_REPLY":
        return dict(out, value=value) if value in REPLIES else none
    return out
