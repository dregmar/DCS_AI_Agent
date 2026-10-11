"""
Deterministic fast path for simple, unambiguous pilot commands.

match_fast(text, snapshot) returns an LLM-shaped dict
    {"action": ..., "target_index": ..., "setting": ..., "value": ..., "speech": ..., "source": "fast"}
or None, meaning "not sure - hand it to the LLM". When in doubt this module ALWAYS
returns None, so a wrong match is much rarer than a missed one. The result is still run
through sanitize_result() by the caller, exactly like an LLM result.

George CPG actions are the names in core/george_interface.py (ACTIONS + GEORGE_SET etc.).
"Select missiles / gun / rockets" now sets GEORGE's assigned weapon (GEORGE_SET weapon).
The pilot's own WAS switch (DCS-BIOS) is only used for "WAS off".

Always deferred to the LLM path:
  * Master arm / arm / disarm / safe   (readback is still being verified)
  * engage / attack / kill / launch ... (anything about firing except the exact phrases below)
  * negations, questions, multi-command sentences ("... and ...", "... then ..."), long chatter
Handled BEFORE those guards, because they must be instant:
  * "cease fire" / "hold fire" / "stop tracking"        -> CEASE
  * "cancel target list"                                -> LIST_EXIT
  * "fire" / "cleared to fire" / "cleared hot" (whole utterance only) -> CONSENT_TO_FIRE
  * "weapons free" / "weapons hold"                     -> GEORGE_SET roe
"""
import re
from typing import Any, Dict, List, Optional

from intelligence.threat_engine import evaluate_tactical_environment

MAX_WORDS = 9   # after filler removal; longer than this is conversational -> LLM

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"

# Hard negation: never act on these, not even the "instant" commands.
_HARD_NEG = re.compile(r"\b(?:not|dont|don't|do not|never|negative|disregard)\b")
# More than one instruction in the sentence -> LLM.
_CONJ = re.compile(r"\b(?:and|then|also|after|before)\b")

_DEFER = re.compile(
    r"\b(?:dis)?arm\w*|\bmaster\b|\bsafe\b|\bhot\b|\bcold\b"                # arming state
    r"|\b(?:fire|firing|shoot|engage|attack|kill|destroy|launch|release|splash)\b"
    r"|\b(?:cancel|abort|stop|wait|hold)\b"
    r"|" + _CONJ.pattern
)

_FILLER = re.compile(
    r"\b(?:george|please|copy|roger|okay|ok|now|just|uh|um|hey|alright|right|"
    r"can you|could you|would you|will you|go ahead and|i need you to|i want you to|"
    r"let's|lets)\b"
)
_QUESTION_START = re.compile(
    r"^(?:what|which|how|why|when|where|who|do|does|did|are|is|am|have|has|should|can|could)\b")

_SELECT_VERB = r"(?:select|switch|use|go to|give me|load|pick|set|change|choose)"
# "action" / "my" / "WAS" mean YOUR weapon (Weapons Action Switch via DCS-BIOS), not George's.
_PILOT_WEAPON = re.compile(r"\b(?:action|actioned|was)\b")

# Whisper hears LOBL / LOAL in many ways; STT_PROMPT biases it, these catch the rest.
_LOBL = r"(?:lobl|lobel|lowball|low ball|low bull|lock on before launch)"
_LOAL = r"(?:loal|low all|lowell|loyal|low al|lo al|lock on after launch)"


def _result(action: str, speech: str, idx: Optional[int] = None,
            setting: Optional[str] = None, value: Optional[str] = None,
            targets: Optional[List[int]] = None) -> Dict[str, Any]:
    return {"action": action, "target_index": idx, "setting": setting, "value": value,
            "targets": targets, "speech": speech, "source": "fast"}


# (action, regex, speech, setting, value). Must run BEFORE the defer guard: their wording
# contains "fire" / "cancel" / "stop" / "hold".
_PRIORITY = [
    ("SET_ARMED",
     re.compile(r"\bmaster arm (?:on|arm|armed|hot)\b|^(?:arm|arm up|go hot)$"),
     "Master arm on.", None, None),
    ("SET_DISARMED",
     re.compile(r"\bmaster arm (?:off|safe|cold)\b|^(?:safe|go safe|go cold|safe it|disarm|disarm weapons)$"),
     "Master arm safe.", None, None),
    ("CEASE",
     re.compile(r"\b(?:cease fire|hold fire|hold your fire|check fire|stop firing|stop tracking|stand down|"
                r"(?:c s|cs|c's|sees|seas|seize|cees|ceas|ceased|sease|cease) fire)\b"),
     "Ceasing.", None, None),
    ("LIST_EXIT",
     re.compile(r"\b(?:cancel|clear|dismiss|exit)\b.*\b(?:target list|list|targets)\b"),
     "List cancelled.", None, None),
    ("CONSENT_TO_FIRE",   # whole utterance only, so "fire at the ZSU" etc. go to the LLM
     re.compile(r"^(?:fire(?: fire)?|cleared to fire|cleared hot|you are cleared hot|"
                r"consent to fire|fire (?:missile|hellfire|gun|it)|send it|shoot|shoot it)$"),
     "Firing.", None, None),
    ("GEORGE_SET",
     re.compile(r"\b(?:weapons free|free fire|cleared weapons free)\b"),
     "Weapons free.", "roe", "FREE"),
    ("GEORGE_SET",
     re.compile(r"\bweapons (?:hold|tight)\b"),
     "Weapons hold.", "roe", "HOLD"),
]

# (action, regex, speech). Evaluated against the normalised text.
_PATTERNS = [
    ("STATUS",
     re.compile(r"\b(?:status|sitrep|sit rep|systems check|report in|give me a report|how are we)\b"),
     "Stand by."),
    ("CYCLE_WEAPON",
     re.compile(r"\b(?:cycle|next|switch|change)\b.*\bweapons?\b(?!.*\b(?:type|types|warhead|warheads)\b)"
                r"|\bweapons? cycle\b"),
     "Cycling weapon."),
    ("TOGGLE_ROE",
     re.compile(r"\b(?:toggle|change|switch|cycle)\b.*\b(?:roe|r o e|rules of engagement)\b"),
     "Toggling R O E."),

    # --- search family. Up-short = slave TADS to helmet LOS and search; Down-long = search again.
    ("TADS_SEARCH",
     re.compile(r"\b(?:search again|scan again|new search|re ?search|re ?scan|search here)\b"),
     "Searching again."),
    ("PHS_SEARCH",
     re.compile(r"\b(?:search|scan)\b(?!.*\b(?:list|area|point)\b)|\bslave[ds]?\b(?!.*\bpoint\b)"),
     "Searching."),

    # --- list navigation
    ("LIST_DOWN",
     re.compile(r"^(?:scroll down|down|next one|next target|next|go down)$"),
     "Next."),
    ("LIST_UP",
     re.compile(r"^(?:scroll up|up|previous|previous one|previous target|go up|back up)$"),
     "Previous."),
    ("TRACK_TARGET",
     re.compile(r"^(?:track|track it|track that|track that one|track target|select it|select that|"
                r"lock it|lock that|lock that one|that one)$"),
     "Tracking."),
    ("NEXT_TARGET",
     re.compile(r"\bskip\b.*\btarget\b|^skip(?: it)?$"),
     "Skipping."),

    # --- TADS / sensor
    ("TOGGLE_LASER",
     re.compile(r"\b(?:laser on|laser off|lase|lase it|start lasing|stop lasing|toggle laser|designate)\b"),
     "Laser."),
    ("ZOOM_IN",
     re.compile(r"\bzoom in\b|\bincrease (?:zoom|magnification)\b"),
     "Zooming in."),
    ("ZOOM_OUT",
     re.compile(r"\bzoom out\b|\bdecrease (?:zoom|magnification)\b"),
     "Zooming out."),
    ("TOGGLE_SENSOR",
     re.compile(r"\b(?:flir|dtv|tv|thermal|day tv)\b|\b(?:switch|toggle|change)\b.*\bsensor\b"),
     "Switching sensor."),
    ("TOGGLE_FOV",
     re.compile(r"\b(?:toggle|change|switch)\b.*\b(?:fov|field of view)\b|\b(?:narrow|zoom) fov\b"),
     "Field of view."),
    ("STORE_TARGET",
     re.compile(r"\b(?:store|mark|save)\b.*\b(?:target|it|that|point)\b"),
     "Storing target."),
    ("TOGGLE_THREAT_WARN",
     re.compile(r"\b(?:toggle|turn)\b.*\bthreat warn(?:ing|ings)?\b"),
     "Threat warnings."),

    # --- area / point search workflow
    ("OPEN_SEARCH_LIST",
     re.compile(r"\b(?:search list|area search list|open area search|area searches)\b"),
     "Search list."),
    ("OPEN_POINT_LIST",
     re.compile(r"\b(?:point list|open points)\b"),
     "Point list."),
    ("START_AREA_SEARCH",
     re.compile(r"\b(?:start|begin|execute|run)\b.*\barea search\b"),
     "Starting area search."),
    ("POINT_SEARCH",
     re.compile(r"\bpoint search\b|\bsearch (?:the )?point\b"),
     "Point search."),

    # --- wheel visibility
    ("WHEEL_SHOW",
     re.compile(r"\b(?:show|open|bring up|pull up|display)\b.*\b(?:wheel|menu|interface)\b"),
     "Wheel up."),
    ("WHEEL_HIDE",
     re.compile(r"\b(?:hide|close|dismiss|remove)\b.*\b(?:wheel|menu|interface)\b"),
     "Wheel hidden."),
    ("SHOW_HIDE",
     re.compile(r"\btoggle\b.*\b(?:wheel|menu|interface)\b|^(?:wheel|menu)$"),
     "Wheel toggled."),
]

# --- George's assigned weapon (Left-short cycle) ---
_WEAPON_TARGETS = [
    ("MSL", re.compile(r"\b(?:hellfires?|helfires?|missiles?|msl)\b"), "Missiles."),
    ("GUN", re.compile(r"\b(?:gun|cannon|chain ?gun|30 ?mm|thirty millimeter)\b"), "Gun."),
    ("RKT", re.compile(r"\b(?:rockets?|hydras?|rkt)\b"), "Rockets."),
]
_SELECT_WEAPON = re.compile(r"\b" + _SELECT_VERB + r"\b")
_NO_WEAPON = re.compile(r"\b(?:no weapon|weapons? off|unassign weapon)\b")
_PILOT_WEAPON_OFF = re.compile(r"\b(?:de ?action|un ?action|action off|was off|weapon action switch off)\b")
_PILOT_ACTIONS = {"MSL": ("SELECT_HELLFIRE", "Your missiles."), "GUN": ("SELECT_GUN", "Your gun."),
                  "RKT": ("SELECT_ROCKETS", "Your rockets.")}

# --- George's weapon settings: values ---
_BURST = re.compile(r"\bburst\b")
_QTY = re.compile(r"\b(?:quantity|qty|salvo|ripple|pairs?)\b")
_TRAJ = re.compile(r"\b(?:trajectory|trajectories|traj|profile)\b")
_TRAJ_VALUES = [("HI", re.compile(r"\b(?:high|hi)\b")), ("LO", re.compile(r"\b(?:low|lo)\b")),
                ("DIR", re.compile(r"\b(?:direct|dir)\b"))]
_METHOD_LOBL = re.compile(r"\b" + _LOBL + r"\b")
_METHOD_LOAL = re.compile(r"\b" + _LOAL + r"\b")
_TYPE_WORD = re.compile(r"\b(?:type|types|warhead|warheads|seeker|subtype)\b")
_MSL_TYPES = [("RF", re.compile(r"\b(?:rf|radar|longbow|l model)\b")),
              ("SAL2", re.compile(r"\b(?:sal|sal ?2|laser hellfire|laser missile|k model)\b"))]
_RKT_TYPES = [("HE", re.compile(r"\b(?:he|high explosive|m151|m229)\b")),
              ("ILL", re.compile(r"\b(?:ill|illum|illumination|m257)\b")),
              ("SMK", re.compile(r"\b(?:smk|smoke|m274)\b"))]
_CYCLE_WORD = re.compile(r"\b(?:cycle|next|change|switch|toggle)\b")

# Spoken numbers -> setting values. Longest phrases first.
_VALUE_PHRASES = [
    ("one hundred", "100"), ("a hundred", "100"), ("twenty four", "24"), ("hundred", "100"),
    ("fifty", "50"), ("twenty", "20"), ("twelve", "12"), ("ten", "10"), ("eight", "8"),
    ("four", "4"), ("two", "2"), ("one", "1"), ("all", "ALL"), ("everything", "ALL"),
]


def _spoken_value(t: str) -> Optional[str]:
    m = re.search(r"\b(\d{1,3})\b", t)
    if m:
        return m.group(1)
    for phrase, val in _VALUE_PHRASES:
        if re.search(r"\b" + phrase + r"\b", t):
            return val
    return None


_DEFER_HIT = {"defer": True}


def _match_setting(t: str) -> Optional[Dict[str, Any]]:
    """George's weapon settings with or without a value. Returns a result, None (no match)
    or _DEFER_HIT (it's a setting, but ambiguous -> LLM)."""
    if _BURST.search(t):
        v = _spoken_value(t)
        if v is None:
            return _result("GEORGE_SET", "Next burst.", setting="gun_burst", value="NEXT")
        if v in ("10", "20", "50", "100", "ALL"):
            return _result("GEORGE_SET", f"Burst {v.lower()}.", setting="gun_burst", value=v)
        return _DEFER_HIT
    if _QTY.search(t) and re.search(r"\b(?:rockets?|quantity|qty|salvo|ripple)\b", t):
        v = _spoken_value(t)
        if v is None:
            return _result("GEORGE_SET", "Next quantity.", setting="rocket_qty", value="NEXT")
        if v in ("1", "2", "4", "8", "12", "24", "ALL"):
            return _result("GEORGE_SET", f"Rockets {v.lower()}.", setting="rocket_qty", value=v)
        return _DEFER_HIT
    lobl, loal = bool(_METHOD_LOBL.search(t)), bool(_METHOD_LOAL.search(t))
    if lobl and loal:
        return _DEFER_HIT
    if lobl or loal:
        v = "LOBL" if lobl else "LOAL"
        return _result("GEORGE_SET", f"{v}.", setting="missile_method", value=v)
    if _TRAJ.search(t):
        vals = [v for v, p in _TRAJ_VALUES if p.search(t)]
        if len(vals) > 1:
            return _DEFER_HIT
        if vals:
            spoken = {"HI": "high", "LO": "low", "DIR": "direct"}[vals[0]]
            return _result("GEORGE_SET", f"Trajectory {spoken}.", setting="missile_traj", value=vals[0])
        return _result("GEORGE_SET", "Next trajectory.", setting="missile_traj", value="NEXT")
    if _TYPE_WORD.search(t) or any(p.search(t) for _, p in _MSL_TYPES + _RKT_TYPES):
        msl = re.search(r"\b(?:hellfires?|missiles?|msl)\b", t) or any(p.search(t) for _, p in _MSL_TYPES)
        rkt = re.search(r"\b(?:rockets?|hydras?|rkt)\b", t) or any(p.search(t) for _, p in _RKT_TYPES)
        if bool(msl) == bool(rkt):
            return _DEFER_HIT if (msl or _TYPE_WORD.search(t)) else None
        table, setting = (_MSL_TYPES, "missile_type") if msl else (_RKT_TYPES, "rocket_type")
        vals = [v for v, p in table if p.search(t)]
        if len(vals) > 1:
            return _DEFER_HIT
        if vals:
            return _result("GEORGE_SET", f"{vals[0]}.", setting=setting, value=vals[0])
        if _TYPE_WORD.search(t) and _CYCLE_WORD.search(t):
            return _result("GEORGE_SET", "Next type.", setting=setting, value="NEXT")
        return _DEFER_HIT
    return None


def _match_weapon(t: str, pilot: bool = False) -> Optional[Dict[str, Any]]:
    """'select missiles' / 'gun' / 'no weapon' -> George's assigned weapon.
    'action missiles' / 'my gun' / 'de-action' (pilot=True) -> YOUR Weapons Action Switch."""
    if _PILOT_WEAPON_OFF.search(t):
        return _result("SELECT_WAS_OFF", "Your weapon off.")
    if pilot or _PILOT_WEAPON.search(t):
        hits = [v for v, p, _ in _WEAPON_TARGETS if p.search(t)]
        if len(hits) != 1:
            return None
        action, speech = _PILOT_ACTIONS[hits[0]]
        return _result(action, speech)
    if _NO_WEAPON.search(t):
        return _result("GEORGE_SET", "No weapon.", setting="weapon", value="NO_WPN")
    hits = [(v, s) for v, p, s in _WEAPON_TARGETS if p.search(t)]
    if len(hits) != 1:
        return None
    v, speech = hits[0]
    bare = re.fullmatch(r"(?:hellfires?|helfires?|missiles?|gun|cannon|rockets?)", t)
    if _SELECT_WEAPON.search(t) or bare:
        return _result("GEORGE_SET", speech, setting="weapon", value=v)
    return None


# Target selection is handled separately because it carries an index.
_SELECT_TARGET_VERB = re.compile(r"\b(?:lock|select|designate|target|track|slew to|go to|switch to|pick|choose)\b")
_TARGET_BY_NUMBER = re.compile(r"\b(?:target|contact|number|no)\s*(?:number\s*)?" + _NUM + r"\b")
_TARGET_BY_ORDINAL = re.compile(
    r"\b(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s*(?:one|target|contact)?\b")
_WEAPON_WORDS = re.compile(
    r"\b(?:weapons?|hell ?fires?|helfires?|missiles?|msl|rockets?|hydras?|rkt|gun|cannon|chain ?gun|warheads?)\b")
_TARGET_PRIORITY = re.compile(r"\b(?:priority|highest|top)\b(?:\s+(?:threat|target|contact))?")


def _normalise(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9#' ]+", " ", t)
    t = t.replace("#", " number ")
    # Whisper often splits the name; "hell fire" must not trip the "fire" safety word.
    t = re.sub(r"\bhell fires?\b", "hellfire", t)
    t = _FILLER.sub(" ", t)
    # A leading "and" / "so" / "then" is just how people chain radio calls ("and send it").
    t = re.sub(r"^\s*(?:and|so|then|also)\s+", " ", t)
    t = re.sub(r"\bthe\b|\ba\b|\bmy\b|\bto me\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _to_int(token: str) -> Optional[int]:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _match_target(t: str, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """'lock target 2' / 'select the second one' / 'lock priority target', else None."""
    if not _SELECT_TARGET_VERB.search(t) or _WEAPON_WORDS.search(t):
        return None     # "select the second weapon" is not a target command

    idx: Optional[int] = None
    m = _TARGET_BY_NUMBER.search(t)
    if m:
        idx = _to_int(m.group(1))
    else:
        m = _TARGET_BY_ORDINAL.search(t)
        if m:
            idx = _to_int(m.group(1))
        elif _TARGET_PRIORITY.search(t):
            _, highest, _ = evaluate_tactical_environment(snapshot)
            idx = highest.get("wheel_index") if highest else None
            if idx is None:
                return _result("NONE", "Negative, no targets in the list.")
    if idx is None:
        return None
    return _result("SELECT_TARGET_INDEX", f"Target {idx}.", idx)


def _dedupe(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse hits that mean the same thing, so only genuinely different intents remain."""
    keys = {(h["action"], h.get("setting")) for h in hits}
    actions = {a for a, _ in keys}
    drop_actions, drop_weapon = set(), False
    # "change gun burst" / "rocket quantity 4": the weapon word is an adjective here.
    if any(s and s != "weapon" for _, s in keys):
        drop_weapon = True
        drop_actions.add("CYCLE_WEAPON")
    # "search again" also contains "search"
    if "TADS_SEARCH" in actions:
        drop_actions.add("PHS_SEARCH")
    # "point search" / "start area search" also contain "search"
    if actions & {"POINT_SEARCH", "START_AREA_SEARCH", "OPEN_SEARCH_LIST"}:
        drop_actions.add("PHS_SEARCH")
    # A named search beats the generic search / point-search / start-area-search patterns.
    if actions & {"AREA_SEARCH", "POINT_SEARCH_AT"}:
        drop_actions |= {"PHS_SEARCH", "POINT_SEARCH", "START_AREA_SEARCH", "OPEN_SEARCH_LIST",
                         "OPEN_POINT_LIST", "TADS_SEARCH"}
    # "track target 2" is a target selection, not "track the highlighted one"
    if "SELECT_TARGET_INDEX" in actions:
        drop_actions.add("TRACK_TARGET")
    out, seen = [], set()
    for h in hits:
        k = (h["action"], h.get("setting"))
        if h["action"] in drop_actions or k in seen:
            continue
        if drop_weapon and h.get("setting") == "weapon":
            continue
        seen.add(k)
        out.append(h)
    return out


# "engage targets 1, 3 and 4" / "engage targets one through four" / "engage the priority target"
_ENGAGE = re.compile(r"^(?:engage|attack|hit|service|kill|shoot list)\b")
_NUM_TOKEN = re.compile(r"\b(\d{1,2}|" + "|".join(k for k in _NUMBER_WORDS if not k.endswith(("st", "nd", "rd", "th")))
                        + r"|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b")
_RANGE = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:through|thru|to)\s+"
                    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b")


def _match_engage(t: str, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _ENGAGE.match(t) or _WEAPON_WORDS.search(t):
        return None
    rest = _ENGAGE.sub("", t, count=1).strip()
    if _TARGET_PRIORITY.search(rest) and not _NUM_TOKEN.search(rest):
        _, highest, _ = evaluate_tactical_environment(snapshot)
        if not highest:
            return _result("NONE", "Negative, no targets in the list.")
        return _result("ENGAGE_TARGETS", f"Engaging target {highest['wheel_index']}.",
                       targets=[highest["wheel_index"]])
    # Numbers only count after "target(s)" / "contact(s)" / "number(s)", or when the rest of the
    # sentence is nothing but numbers ("engage 1 3 and 4"); "engage that one" goes to the LLM.
    words = rest.split()
    only_numbers = bool(words) and all(_NUM_TOKEN.fullmatch(w) or w in ("and", "through", "thru", "to")
                                       for w in words)
    if not (re.search(r"\b(?:targets?|contacts?|numbers?)\b", rest) or only_numbers):
        return None
    idxs: List[int] = []
    m = _RANGE.search(rest)
    if m:
        a, b = _to_int(m.group(1)), _to_int(m.group(2))
        if a and b and a <= b <= a + 9:
            idxs = list(range(a, b + 1))
    if not idxs:
        for tok in _NUM_TOKEN.findall(rest):
            n = _to_int(tok)
            if n and n not in idxs:
                idxs.append(n)
    if not idxs:
        return None
    if len(idxs) == 1:
        speech = f"Engaging target {idxs[0]}."
    else:
        speech = "Engaging targets " + ", ".join(map(str, idxs[:-1])) + f" and {idxs[-1]}."
    return _result("ENGAGE_TARGETS", speech, targets=idxs)


# --- searches by name: "search forward area", "own pfz search", "point search tango zero seven"
_AREA_NAMES = [
    ("FWD AREA", re.compile(r"\b(?:forward|front|fwd) area\b")),
    ("PHS AREA", re.compile(r"\b(?:helmet(?: sight)?|phs|p h s) area\b")),
    ("FCR TGTS", re.compile(r"\b(?:fcr|f c r|radar) (?:targets?|tgts)\b")),
    ("OWN PFZ", re.compile(r"\bown (?:pfz|p f z|priority fire zone|fire zone)\b")),
    ("ACTV PFZ", re.compile(r"\b(?:active|actv) (?:pfz|p f z|priority fire zone|fire zone)\b")),
]
_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_POINT_LETTER = {"tango": "T", "t": "T", "charlie": "C", "c": "C", "whiskey": "W", "w": "W", "hotel": "H", "h": "H"}
_DW = "|".join(_DIGIT_WORDS)
_POINT_ID = re.compile(r"\b(tango|charlie|whiskey|hotel|t|c|w|h)\s*(\d{1,2}|(?:" + _DW + r")(?:\s+(?:" + _DW + r"))?)\b")
_SEARCH_WORD = re.compile(r"\b(?:search|scan|look)\b")


def _match_named_search(t: str) -> Optional[Dict[str, Any]]:
    if not _SEARCH_WORD.search(t):
        # A bare area name ("forward area", "own pfz") is also an order to search it.
        for code, pattern in _AREA_NAMES:
            m = pattern.search(t)
            if m and m.group(0) == t:
                return _result("AREA_SEARCH", "Copy.", value=code)
        return None
    for code, pattern in _AREA_NAMES:
        if pattern.search(t):
            return _result("AREA_SEARCH", "Copy.", value=code)
    if re.search(r"\bpoint\b|\bat\b", t):
        m = _POINT_ID.search(t)
        if m:
            letter = _POINT_LETTER[m.group(1)]
            raw = m.group(2)
            digits = raw if raw.isdigit() else "".join(_DIGIT_WORDS[w] for w in raw.split())
            if 1 <= len(digits) <= 2:
                return _result("POINT_SEARCH_AT", "Copy.", value=f"{letter}{int(digits):02d}")
    return None


_WHERE = re.compile(r"^(?:where(?: is|'s| s)?|locate|position of|bearing to)\b")


def _match_where(t: str, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """'where is target 2' / 'where's the priority target' -> TARGET_INFO."""
    if not _WHERE.match(t):
        return None
    m = _TARGET_BY_NUMBER.search(t) or _TARGET_BY_ORDINAL.search(t)
    if m:
        idx = _to_int(m.group(1))
        return _result("TARGET_INFO", f"Target {idx}.", idx) if idx else None
    if _TARGET_PRIORITY.search(t) or re.search(r"\b(?:threat|target)\b", t):
        _, highest, _ = evaluate_tactical_environment(snapshot)
        if not highest:
            return _result("NONE", "Negative, no targets in the list.")
        return _result("TARGET_INFO", "Priority target.", highest.get("wheel_index"))
    return None


def match_fast(text: str, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """See module docstring. Never raises; any internal problem means 'defer to the LLM'."""
    try:
        t = _normalise(text)
        if not t or len(t.split()) > MAX_WORDS:
            return None
        if _HARD_NEG.search(t):
            return None

        # Multi-target engagement: its wording ("engage", "and") trips the guards below.
        engage = _match_engage(t, snapshot)
        if engage is not None:
            return engage

        # Instant commands: checked before the defer guard because their wording trips it.
        if not _CONJ.search(t):
            question = bool(_QUESTION_START.match(t))
            prio = [_result(a, s, setting=st, value=v) for a, p, s, st, v in _PRIORITY
                    if p.search(t) and (a == "CEASE" or not question)]   # "is master arm on?" asks
            if len(prio) == 1:
                return prio[0]
            if len(prio) > 1:
                return None

        where = _match_where(t, snapshot)
        if where is not None:
            return where
        if _DEFER.search(t):
            return None
        # Questions ("which hellfire do I have?") go to the LLM, except plain status asks.
        is_question = bool(_QUESTION_START.match(t))

        hits: List[Dict[str, Any]] = []
        named = _match_named_search(t)        # "radar targets search" is a search, not RF missiles
        if named is not None and not is_question:
            return named
        pilot = bool(re.search(r"\bmy\b", text.lower()))     # _normalise drops "my"
        for matcher in (lambda x: _match_target(x, snapshot), _match_setting,
                        lambda x: _match_weapon(x, pilot)):
            r = matcher(t)
            if r is _DEFER_HIT:
                return None
            if r is not None:
                hits.append(r)
        for action, pattern, speech in _PATTERNS:
            if pattern.search(t):
                hits.append(_result(action, speech))

        hits = _dedupe(hits)
        # Exactly one clear intent, or defer.
        if len(hits) != 1:
            return None
        if is_question and hits[0]["action"] != "STATUS":
            return None
        return hits[0]
    except Exception as e:      # a bug here must never take a command down with it
        print(f"[FAST] matcher error, deferring to LLM: {type(e).__name__}: {e}")
        return None
