import re
import time
import json
import threading
from collections import deque
from typing import Dict, Any, Optional, Tuple

from config import LLM_MODEL, LLM_TIMEOUT_SEC, LLM_MAX_TOKENS, TELEMETRY_STALE_SEC
from core.state import state_store
from intelligence.threat_engine import evaluate_tactical_environment
from intelligence import ollama_chat
from core.george_interface import ALL_ACTIONS as GEORGE_ACTIONS, SETTERS as GEORGE_SETTERS
from intelligence.router import ROUTE_PROMPT

# Sliding conversation history buffer (last 6 messages: 3 user, 3 assistant).
# Only the voice-worker thread touches this, so no lock is needed.
# Recent turns for follow-ups ("lock the second one"). Trimmed in batches, not one turn at a
# time: Ollama reuses the part of the prompt that is unchanged since the last call, and dropping
# the oldest turn on every command changed the start of the history each time (~3 s per answer
# in flight instead of ~1 s).
CONVERSATION_HISTORY = deque()
HISTORY_MAX = 8          # messages (4 exchanges); then cut back to the last HISTORY_KEEP
HISTORY_KEEP = 4


def trim_history(history) -> None:
    if len(history) > HISTORY_MAX:
        while len(history) > HISTORY_KEEP:
            history.popleft()

# DCS-BIOS switch actions (the PILOT's own switches, read back from the 5010 stream),
# plus every George CPG action from core/george_interface.py.
BIOS_ACTION_NAMES = {
    "SET_ARMED", "SET_DISARMED",
    "SELECT_HELLFIRE", "SELECT_GUN", "SELECT_ROCKETS", "SELECT_WAS_OFF",
}
VALID_ACTIONS = BIOS_ACTION_NAMES | set(GEORGE_ACTIONS) | {"STATUS", "NONE", "TARGET_INFO"}


# An LLM action only runs if the pilot said a word related to it. Whisper turns noise and half
# words into sentences ("See you well!", "A few will"), and the model will still pick an action
# for them: in the Oct 2026 flight those became CEASE and "weapon gun". (Same idea as the Huey's
# _GATES.) Matched against the lower-cased transcript.
_NUM_WORDS = r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|last)\b"
_ACTION_GATES = {
    "CONSENT_TO_FIRE": r"fire|shoot|cleared|hot|send it|engage|smoke (?:him|it|them)|kill",
    "CEASE": r"cease|stop|cancel|abort|hold|knock it off|check fire|belay|disregard|reset",
    "SELECT_TARGET_INDEX": r"target|contact|track|lock|select|designate|number|pick|" + _NUM_WORDS,
    "TARGET_INFO": r"where|target|contact|threat|priority|bearing|range|how far|" + _NUM_WORDS,
    "ENGAGE_TARGETS": r"engage|fire|shoot|kill|destroy|take out|hit|attack",
    "NEXT_TARGET": r"next|skip|move on",
    "LIST_UP": r"up|previous|back", "LIST_DOWN": r"down|next", "LIST_EXIT": r"exit|close|cancel|back out",
    "TRACK_TARGET": r"track|lock|select|that one",
    "PHS_SEARCH": r"search|scan|look|find|slave|helmet|eyes",
    "TADS_SEARCH": r"search|scan|look|find",
    "AREA_SEARCH": r"search|scan|look|area|forward|front|ahead|zone|pfz",
    "POINT_SEARCH_AT": r"point|search|scan|look|waypoint|target|tango|hotel|charlie|whiskey",
    "OPEN_SEARCH_LIST": r"search|area|list", "START_AREA_SEARCH": r"search|area|start",
    "OPEN_POINT_LIST": r"point|list", "SLAVE_TO_POINT": r"slave|point", "POINT_SEARCH": r"point|search",
    "TOGGLE_LASER": r"laser|lase|designat|spot",
    "TOGGLE_FOV": r"fov|field|wide|narrow|medium|zoom|view",
    "ZOOM_IN": r"zoom|closer|magnif|in\b", "ZOOM_OUT": r"zoom|wider|magnif|out\b",
    "TOGGLE_SENSOR": r"flir|tv|thermal|sensor|video|ir\b|day|night|optic",
    "STORE_TARGET": r"store|save|mark|record",
    "TOGGLE_THREAT_WARN": r"threat|warning|warn",
    "WHEEL_SHOW": r"wheel|menu|show|open", "WHEEL_HIDE": r"wheel|menu|hide|close",
    "SHOW_HIDE": r"wheel|menu|toggle|show|hide",
    "SET_ARMED": r"\barm|hot|master", "SET_DISARMED": r"safe|disarm|cold|master",
    "SELECT_HELLFIRE": r"hellfire|missile|msl", "SELECT_GUN": r"gun|cannon",
    "SELECT_ROCKETS": r"rocket|hydra|rkt", "SELECT_WAS_OFF": r"was|action|off|de ?action",
}
# GEORGE_SET: the word must fit the setting being changed.
_SETTING_GATES = {
    "weapon": r"gun|cannon|missile|hellfire|helfire|rocket|hydra|weapon|msl|rkt|no weapon",
    "roe": r"weapons|free|hold|tight|hot|roe|rules|engage|fire",
    "gun_burst": r"burst|round|gun",
    "rocket_qty": r"rocket|salvo|ripple|pair|quantity|qty",
    "rocket_type": r"rocket|warhead|type|explosive|\bhe\b|smoke|illum|flechette|m151|m229|m257|m274",
    "missile_type": r"missile|hellfire|type|seeker|radar|\brf\b|laser|\bsal|longbow",
    "missile_method": r"lobl|loal|lock on|lock|before|after|launch",
    "missile_traj": r"traj|profile|direct|high|low|\bhi\b|\blo\b",
}
# NONE answers come straight from the model; it invents facts there ("Objective is to secure the
# area", "Master Arm is DISARMED" when it was armed). Speech that states facts becomes "Unable."
_FACT_WORDS = re.compile(r"\d|\b(?:arm|armed|disarmed|safe|fuel|feet|ft|knots|kts|miles|meters|km|"
                         r"altitude|airspeed|heading|bearing|objective|mission|waypoint|target|targets|"
                         r"threat|tads|weapon|weapons|missile|gun|rocket|hold|free|masked|unmasked|"
                         r"detected|range|position|full|empty|enemy|enemies|contact|contacts)\b", re.I)


def _gate_ok(action: str, setting: Optional[str], text: str) -> bool:
    t = text.lower()
    gate = _SETTING_GATES.get(setting or "") if action == "GEORGE_SET" else _ACTION_GATES.get(action)
    return gate is None or re.search(gate, t) is not None


def sanitize_result(res: Dict[str, Any], target_count: int, text: Optional[str] = None
                    ) -> Tuple[str, Optional[int], str, Optional[str], Optional[str], Optional[list]]:
    """Validates LLM output before it is allowed to drive any input.
    text: what the pilot said (LLM results only): the action needs a related word in it.
    Returns (action, target_index, speech, setting, value, targets)."""
    action = str(res.get("action", "NONE")).strip().upper()
    if action not in VALID_ACTIONS:
        print(f"[LLM] Rejected unknown action: {action!r}")
        action = "NONE"
    if text is not None:
        set_name = str(res.get("setting") or "").strip().lower()
        if action != "NONE" and not _gate_ok(action, set_name, text):
            print(f"[LLM] {action} {set_name} rejected: nothing in {text!r} asks for it")
            return "NONE", None, "Say again.", None, None, None
        if action == "NONE":
            said = str(res.get("speech") or "").strip()
            if said.lower().strip(" .!?,") == text.lower().strip(" .!?,"):
                return "NONE", None, "Say again.", None, None, None     # echoing a mis-heard phrase
            if _FACT_WORDS.search(said):
                print(f"[LLM] NONE answer {said!r} states facts it can't know: saying 'Unable.'")
                return "NONE", None, "Unable.", None, None, None

    idx = res.get("target_index")
    try:
        idx = int(idx) if idx is not None and not isinstance(idx, bool) else None
    except (TypeError, ValueError):
        idx = None

    speech = str(res.get("speech") or "Copy").strip()[:200]
    setting = value = targets = None

    if action == "ENGAGE_TARGETS":
        raw = res.get("targets") or ([idx] if idx is not None else [])
        targets = []
        for v in raw if isinstance(raw, list) else [raw]:
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= v <= target_count and v not in targets:
                targets.append(v)
        if target_count == 0:
            return "NONE", None, "Negative, no targets in the list.", None, None, None
        if not targets:
            return "NONE", None, "Say again, which targets?", None, None, None
        return action, None, speech, None, None, targets[:8]

    if action in ("SELECT_TARGET_INDEX", "TARGET_INFO"):
        if target_count == 0:
            return "NONE", None, "Negative, no targets in the list.", None, None, None
        if idx is None or not (1 <= idx <= target_count):
            return "NONE", None, "Say again, which target?", None, None, None
    else:
        idx = None

    if action in ("AREA_SEARCH", "POINT_SEARCH_AT"):
        value = str(res.get("value") or "").strip().upper()
        value = {"FORWARD AREA": "FWD AREA", "FORWARD": "FWD AREA", "FWD": "FWD AREA",
                 "PHS": "PHS AREA", "HELMET AREA": "PHS AREA", "FCR TARGETS": "FCR TGTS",
                 "RADAR TARGETS": "FCR TGTS", "OWN": "OWN PFZ", "ACTIVE PFZ": "ACTV PFZ",
                 "ACTIVE": "ACTV PFZ"}.get(value, value)
        if action == "AREA_SEARCH" and value not in ("FWD AREA", "PHS AREA", "FCR TGTS", "OWN PFZ", "ACTV PFZ"):
            return "NONE", None, "Say again, which area search?", None, None, None
        if action == "POINT_SEARCH_AT" and not re.fullmatch(r"[TCWH]\d{2}", value):
            return "NONE", None, "Say again, which point?", None, None, None

    if action == "GEORGE_SET":
        setting = str(res.get("setting") or "").strip().lower()
        value = str(res.get("value") or "").strip().upper()
        if setting not in GEORGE_SETTERS:
            return "NONE", None, "Say again, which setting?", None, None, None
        options = GEORGE_SETTERS[setting][0]
        if value != "NEXT" and value not in options:
            return "NONE", None, f"Unable, options are {', '.join(options)}.", None, None, None

    return action, idx, speech, setting, value, targets


# --- 1. CONVERSATION MEMORY & QWEN REASONING ENGINE ---

# George's system prompt. It never changes, so Ollama keeps it in its cache instead of reading it
# again for every command (that was most of the ~2.3 s per answer). The live state goes in the
# pilot's message instead (see _state_block).
GEORGE_SYSTEM = """You are 'George', the CPG (Co-Pilot/Gunner) in an AH-64D Apache helicopter.
Analyze the pilot's command in the context of recent conversation history, flight telemetry, and tactical state.

Each message from the pilot starts with STATE (as of now), then PILOT: what he said. Earlier
messages show only what he said then. STATE: altitude and speed ("NO DATA" = flight data
unavailable: don't quote numbers) | Master Arm | the pilot's WAS (weapon action switch) | sight |
Wheel = George's wheel settings, tracked from our own key presses (may drift) | Targets = George's
Target List, highest threat first; #N = position N in the list on the wheel, then type, range,
clock position, threat score | Priority threat | TADS-masked / out-of-range when they apply.

Rules:
- Use conversation history to resolve follow-ups (e.g., "Lock the second one", "Engage priority threat").
- target_index must be the number shown after '#'. "Second one" means the target numbered #2, not the second in the list.
- You are the CPG. Under WEAPONS HOLD you only fire when the pilot gives consent: use CONSENT_TO_FIRE
  ONLY for an explicit order to fire ("fire", "cleared to fire", "cleared hot"). Never for a question,
  a negation, or "engage" alone. Under WEAPONS FREE you fire on your own once tracking.
- "Weapons free" / "weapons hold" -> GEORGE_SET setting "roe" value "FREE" / "HOLD".
- A specific value -> GEORGE_SET with that value. "Cycle"/"next" without a value -> value "NEXT".
- If ordered to fire while Master Arm (in STATE) is DISARMED, call out the disarmed state.
- If target is out of weapon range or TADS is masked, note constraint in radio speech.
- Keep speech under 15 words.
- For NONE, say "Say again." (didn't understand) or "Unable." (can't do it). Never state facts in
  it: you only know what STATE shows (not the mission, objective, route or fuel).

Available Actions:
George CPG (wheel):
- "PHS_SEARCH": Slave TADS to the pilot's helmet sight and search there.
- "TADS_SEARCH": Search again along the current TADS line of sight.
- "CEASE": Cease fire / stop tracking / cancel tasks, TADS fixed forward.
- "CONSENT_TO_FIRE": Fire the assigned gun or missile (see rules).
- "GEORGE_SET": Set George's setting (give "setting" and "value"). Settings and values:
    weapon: NO_WPN GUN MSL RKT | roe: HOLD FREE | gun_burst: 10 20 50 100 ALL
    rocket_qty: 1 2 4 8 12 24 ALL | rocket_type: HE ILL SMK | missile_type: SAL2 RF
    missile_method: LOBL LOAL | missile_traj: DIR LO HI   (any setting also accepts NEXT)
- "SELECT_TARGET_INDEX": Track target from George's Target List (provide target_index).
- "LIST_UP" / "LIST_DOWN" / "LIST_EXIT": Scroll or close George's open list.
- "TRACK_TARGET": Track the highlighted list entry.
- "ENGAGE_TARGETS": Engage several targets from George's list in order (provide "targets":
  a list of # numbers, e.g. [1, 3, 4]). George fires on each per his ROE.
- "NEXT_TARGET": Skip to the next target in a multi-target engagement.
- "TOGGLE_LASER": Start/stop lasing (no weapon assigned).
- "TOGGLE_FOV" / "ZOOM_IN" / "ZOOM_OUT" / "TOGGLE_SENSOR": TADS field of view, zoom, FLIR/DTV.
- "STORE_TARGET": Store the tracked target as a TSD point.
- "AREA_SEARCH": Run a named area search (give "value": one of FWD AREA, PHS AREA, FCR TGTS,
  OWN PFZ, ACTV PFZ). E.g. "search the forward area" -> FWD AREA.
- "POINT_SEARCH_AT": Search around a TSD point (give "value" like "T07": letter T/C/W/H + two
  digits; "tango seven" -> T07).
- "OPEN_SEARCH_LIST" / "START_AREA_SEARCH": Choose / start an area search by hand.
- "OPEN_POINT_LIST" / "SLAVE_TO_POINT" / "POINT_SEARCH": Point search workflow.
- "TOGGLE_THREAT_WARN": Turn George's threat warnings on/off.
- "WHEEL_SHOW" / "WHEEL_HIDE" / "SHOW_HIDE": Show, hide, or toggle the George wheel.
Pilot cockpit switches (DCS-BIOS, the pilot's own weapon, NOT George's):
- "SET_ARMED" / "SET_DISARMED": Master Arm.
- "SELECT_HELLFIRE" / "SELECT_GUN" / "SELECT_ROCKETS" / "SELECT_WAS_OFF": only when the pilot
  says it is for HIS weapon ("my WAS", "action my missiles"). Otherwise use GEORGE_SET weapon.
Other:
- "STATUS": Report flight, cockpit, or tactical status.
- "TARGET_INFO": Say where a target from George's list is (provide target_index). Use for
  "where is target 2", "where's the priority threat".
- "ROUTE": an order for someone else (see the end).
- "NONE": Conversational or unrecognized command.

Respond STRICTLY in JSON. Always "action" and "speech"; add only the fields the action uses:
"target_index" (int), "setting" and "value" (GEORGE_SET; "value" also for AREA_SEARCH /
POINT_SEARCH_AT), "targets" (ENGAGE_TARGETS: list of numbers). E.g.
{"action": "GEORGE_SET", "setting": "weapon", "value": "GUN", "speech": "Gun."}
{"action": "SELECT_TARGET_INDEX", "target_index": 2, "speech": "Tracking two."}
""" + ROUTE_PROMPT


def query_qwen(transcript_text: str, george_state: str = "unknown", model: Optional[str] = None,
               extra_state: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Evaluates pilot speech against current state, threat scores, and chat history.
    george_state is GeorgeInterface.describe() (George's TRACKED wheel settings).
    extra_state: more for the STATE line (router.routing_state: Outcasts, waypoints, ...).
    Returns (llm_result, snapshot_used). Validate the result against THAT snapshot."""
    raw_snapshot = state_store.snapshot()
    eval_targets, highest_threat, constraints = evaluate_tactical_environment(raw_snapshot)
    state_store.set_evaluated_tactical_data(eval_targets, highest_threat, constraints)

    target_summary = "None detected"
    if eval_targets:
        target_summary = "; ".join([
            f"#{t['wheel_index']} {t['type']} "
            + (f"{t['dist_km']}km" if t['dist_km'] is not None else "range ?")
            + (f" {t['clock']}h" if t.get('clock') else "")
            + f" threat {t['threat_score']}"
            for t in eval_targets
        ])

    if eval_targets and not raw_snapshot.get("george_list_complete", True):
        target_summary += ", ... (George's list continues below; higher numbers exist)"

    highest_threat_str = "None"
    if highest_threat:
        rng = (f"{highest_threat['dist_km']}km" if highest_threat['dist_km'] is not None
               else "range unknown")
        highest_threat_str = f"#{highest_threat['wheel_index']} {highest_threat['type']} ({rng})"

    telemetry_live = raw_snapshot["telemetry_age"] <= TELEMETRY_STALE_SEC

    # Kept short: on the RX 5700 XT every new token here costs ~5 ms to read (the fixed system
    # prompt comes from Ollama's cache). Explanations of these fields are in GEORGE_SYSTEM.
    flight = (f"{raw_snapshot['alt_ft']:.0f}ft {raw_snapshot['speed_kts']:.0f}kt" if telemetry_live
              else "NO DATA")
    flags = "".join([" TADS-masked" if constraints['tads_masked'] else "",
                     " out-of-range" if constraints['out_of_range'] else ""])
    user_message = (f"STATE: {flight} | MasterArm {raw_snapshot['master_arm']} | WAS {raw_snapshot['was_state']} | "
                    f"sight {raw_snapshot['sight_select']} | Wheel: {george_state} | "
                    f"Targets: {target_summary} | Priority: {highest_threat_str}{flags}"
                    + (f" | {extra_state}" if extra_state else "") + "\n"
                    f"PILOT: {transcript_text}")

    messages = [{"role": "system", "content": GEORGE_SYSTEM}]
    messages.extend(CONVERSATION_HISTORY)
    messages.append({"role": "user", "content": user_message})

    result = ollama_chat.chat(messages, model or LLM_MODEL, timeout=LLM_TIMEOUT_SEC,
                              max_tokens=LLM_MAX_TOKENS)
    return result, raw_snapshot


def chat_json(system_prompt: str, history, transcript_text: str, model: Optional[str] = None) -> Dict[str, Any]:
    """One JSON-mode LLM call for another aircraft profile (e.g. huey/profile.py).
    history: iterable of earlier {"role", "content"} messages."""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": transcript_text})
    return ollama_chat.chat(messages, model or LLM_MODEL, timeout=LLM_TIMEOUT_SEC,
                            max_tokens=LLM_MAX_TOKENS)


def remember_turn(transcript_text: str, action: str, target_index: Optional[int], speech: str,
                  setting: Optional[str] = None, value: Optional[str] = None) -> None:
    """Records what George ACTUALLY did (post-sanitize), so a rejected action is never
    remembered as executed. Called from the voice-worker thread only."""
    CONVERSATION_HISTORY.append({"role": "user", "content": transcript_text})
    turn = {"action": action, "target_index": target_index, "setting": setting, "value": value,
            "speech": speech}
    CONVERSATION_HISTORY.append({
        "role": "assistant",
        "content": json.dumps({k: v for k, v in turn.items() if v is not None}),
    })
    trim_history(CONVERSATION_HISTORY)


OLLAMA_NATIVE = ollama_chat.OLLAMA_BASE + "/api"
KEEP_WARM_CHECK_SEC = 60


def _ollama(path: str, body: Optional[dict] = None, timeout: float = 5.0) -> dict:
    """Ollama's native API (the OpenAI-compatible one can't set keep_alive)."""
    import urllib.request
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{OLLAMA_NATIVE}/{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _model_loaded(model: Optional[str] = None) -> bool:
    model = model or LLM_MODEL
    models = _ollama("ps").get("models") or []
    want = model if ":" in model else model + ":latest"
    return any(m.get("name") == want or m.get("model") == want for m in models)


def _load_model(model: Optional[str] = None, keep_alive=-1) -> float:
    """Loads the model into VRAM and tells Ollama to keep it there for good (keep_alive -1).
    Loading with DCS running can take far longer than a command's timeout, hence 120 s.
    keep_alive 0 unloads it instead (llm_bench.py, between models)."""
    t0 = time.perf_counter()
    _ollama("generate", {"model": model or LLM_MODEL, "keep_alive": keep_alive}, timeout=120)
    return time.perf_counter() - t0


_prime_lock = threading.Lock()


def prime_prompt(system_prompt: str, label: str, model: Optional[str] = None) -> float:
    """Reads a profile's system prompt into Ollama's cache (call from a worker thread). Ollama
    keeps one prompt cached, so this runs at start and whenever the aircraft changes."""
    with _prime_lock:
        t0 = time.perf_counter()
        try:
            ollama_chat.prime(system_prompt, model or LLM_MODEL)
            sec = time.perf_counter() - t0
            print(f"[LLM] {label} prompt cached ({sec:.1f}s).")
            return sec
        except Exception as e:
            print(f"[LLM WARNING] Couldn't pre-load the {label} prompt ({type(e).__name__}: {e}); "
                  f"the first command may be slow.")
            return float("nan")


def warmup_llm() -> None:
    """Loads the model, then keeps it loaded for as long as the program runs.

    Ollama unloads an idle model after 5 minutes by default. Reloading it then takes longer than
    a command's timeout, and a command that times out cancels the reload, so every LLM command
    failed after a few quiet minutes (seen in the Huey test flight). keep_alive -1 prevents the
    unload; the check every minute reloads it if Ollama dropped it anyway (restart, VRAM)."""
    t0 = time.perf_counter()
    try:
        print(f"[LLM] Model warm ({_load_model():.1f}s), kept loaded.")
    except Exception as e:
        print(f"[LLM WARNING] Warm-up failed after {time.perf_counter() - t0:.0f}s ({e}). "
              f"Check that Ollama is running and '{LLM_MODEL}' is pulled: run 'ollama ps' and "
              f"'ollama list'. Simple commands still work through the fast path.")
    warned = False
    while True:
        time.sleep(KEEP_WARM_CHECK_SEC)
        try:
            if not _model_loaded():
                print("[LLM] Model was unloaded; reloading it in the background...")
                print(f"[LLM] Model reloaded ({_load_model():.1f}s).")
            warned = False
        except Exception as e:
            if not warned:
                print(f"[LLM WARNING] Can't reach Ollama to keep the model loaded ({e}).")
                warned = True


# --- 2. AUTONOMOUS BACKGROUND ANNUNCIATOR ENGINE ---

class AutonomousAnnunciator:
    """
    Monitors telemetry & switch states in a background thread to trigger
    unsolicited CPG warnings (e.g., Low Altitude, Masking, Disarmed Engagement).
    tts_callback_func must be non-blocking (the speech queue in audio/tts.py is).
    """

    def __init__(self, tts_callback_func, check_interval_sec: float = 0.5):
        self.tts_callback = tts_callback_func
        self.check_interval = check_interval_sec
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Debounce flags to prevent repetitive spam
        self._alert_states = {
            "low_alt": False,
            "tads_masked": False,
            "priority_air_defense": False,
        }
        self._last_priority_id = None

    def _monitor_loop(self):
        while self._running:
            time.sleep(self.check_interval)
            try:
                self._check()
            except Exception as e:
                print(f"[ANNUNCIATOR ERROR] {type(e).__name__}: {e}")

    def _check(self):
        raw_snapshot = state_store.snapshot()

        # No live flight data (DCS closed, paused, or not yet connected): stay quiet.
        # In the Huey the crew chief (huey/crew_chief.py) does the callouts.
        if raw_snapshot["telemetry_age"] > TELEMETRY_STALE_SEC or state_store.profile() != "apache":
            return

        eval_targets, highest_threat, constraints = evaluate_tactical_environment(raw_snapshot)

        # 1. Low Altitude Warning (< 40 ft while moving > 40 kts)
        if raw_snapshot["alt_ft"] < 40.0 and raw_snapshot["speed_kts"] > 40.0:
            if not self._alert_states["low_alt"]:
                self._alert_states["low_alt"] = True
                self.tts_callback("Altitude low, check 40 feet.")
        else:
            self._alert_states["low_alt"] = False

        # 2. Priority Air Defense Detected (re-announces if the priority threat changes)
        if highest_threat and highest_threat.get("threat_score", 0) >= 90:
            tid = (highest_threat["id"], highest_threat["type"])
            if not self._alert_states["priority_air_defense"] or tid != self._last_priority_id:
                self._alert_states["priority_air_defense"] = True
                self._last_priority_id = tid
                rng = highest_threat.get("dist_km")
                self.tts_callback(f"Warning, {highest_threat['type']} detected"
                                  + (f" at {rng} kilometers." if rng is not None else "."))
        else:
            self._alert_states["priority_air_defense"] = False
            self._last_priority_id = None

        # 3. TADS Masking Warning during active WAS engagement
        if constraints["tads_masked"] and raw_snapshot["was_state"] in ("GUN", "MSL", "RKT"):
            if not self._alert_states["tads_masked"]:
                self._alert_states["tads_masked"] = True
                self.tts_callback("TADS masked, adjust attitude.")
        else:
            self._alert_states["tads_masked"] = False

    def start(self):
        """Starts the background annunciator thread."""
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print("[ANNUNCIATOR] Background flight safety & threat monitor running...")

    def stop(self):
        self._running = False