import re
import time
import json
import threading
from collections import deque
from typing import Dict, Any, Optional, Tuple

from openai import OpenAI
from config import LLM_MODEL, LLM_TIMEOUT_SEC, LLM_MAX_TOKENS, TELEMETRY_STALE_SEC
from core.state import state_store
from intelligence.threat_engine import evaluate_tactical_environment
from core.george_interface import ALL_ACTIONS as GEORGE_ACTIONS, SETTERS as GEORGE_SETTERS

# Initialize Ollama client
ollama_client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",
    timeout=LLM_TIMEOUT_SEC,
    max_retries=0,          # default is 2, which silently multiplies the timeout
)

# Sliding conversation history buffer (last 6 messages: 3 user, 3 assistant).
# Only the voice-worker thread touches this, so no lock is needed.
CONVERSATION_HISTORY = deque(maxlen=6)

# DCS-BIOS switch actions (the PILOT's own switches, read back from the 5010 stream),
# plus every George CPG action from core/george_interface.py.
BIOS_ACTION_NAMES = {
    "SET_ARMED", "SET_DISARMED",
    "SELECT_HELLFIRE", "SELECT_GUN", "SELECT_ROCKETS", "SELECT_WAS_OFF",
}
VALID_ACTIONS = BIOS_ACTION_NAMES | set(GEORGE_ACTIONS) | {"STATUS", "NONE", "TARGET_INFO"}


def sanitize_result(res: Dict[str, Any], target_count: int
                    ) -> Tuple[str, Optional[int], str, Optional[str], Optional[str], Optional[list]]:
    """Validates LLM output before it is allowed to drive any input.
    Returns (action, target_index, speech, setting, value, targets)."""
    action = str(res.get("action", "NONE")).strip().upper()
    if action not in VALID_ACTIONS:
        print(f"[LLM] Rejected unknown action: {action!r}")
        action = "NONE"

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

def query_qwen(transcript_text: str, george_state: str = "unknown"
               ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Evaluates pilot speech against current state, threat scores, and chat history.
    george_state is GeorgeInterface.describe() (George's TRACKED wheel settings).
    Returns (llm_result, snapshot_used). Validate the result against THAT snapshot."""
    raw_snapshot = state_store.snapshot()
    eval_targets, highest_threat, constraints = evaluate_tactical_environment(raw_snapshot)
    state_store.set_evaluated_tactical_data(eval_targets, highest_threat, constraints)

    target_summary = "None detected"
    if eval_targets:
        target_summary = ", ".join([
            f"#{t['wheel_index']}: {t['type']} ("
            + (f"{t['dist_km']}km" if t['dist_km'] is not None else "range unknown")
            + (f", {t['clock']} o'clock" if t.get('clock') else "")
            + f", Threat Score: {t['threat_score']})"
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
    link_note = "LIVE" if telemetry_live else "NO DATA (flight data unavailable; do not quote numbers)"

    system_prompt = f"""
    You are 'George', the CPG (Co-Pilot/Gunner) in an AH-64D Apache helicopter.
    Analyze the pilot's command in the context of recent conversation history, flight telemetry, and tactical state.

    Telemetry link: {link_note}
    Current Aircraft State:
    - Altitude: {raw_snapshot['alt_ft']:.0f} ft | Airspeed: {raw_snapshot['speed_kts']:.0f} kts
    - Pitch: {raw_snapshot['pitch']:.1f}° | Roll: {raw_snapshot['roll']:.1f}°
    - Master Arm: {raw_snapshot['master_arm']} | WAS Mode: {raw_snapshot['was_state']} | Sight: {raw_snapshot['sight_select']}

    Tactical Threats & Constraints:
    - George's Target List (#N = position N in George's list on the wheel; listed highest threat first): [{target_summary}]
    - Priority Threat: {highest_threat_str}
    - TADS Masked: {constraints['tads_masked']}
    - Target Out of Range: {constraints['out_of_range']}

    George's wheel settings (tracked from our own key presses, may drift): {george_state}

    Rules:
    - Use conversation history to resolve follow-ups (e.g., "Lock the second one", "Engage priority threat").
    - target_index must be the number shown after '#'. "Second one" means the target numbered #2, not the second in the list.
    - You are the CPG. Under WEAPONS HOLD you only fire when the pilot gives consent: use CONSENT_TO_FIRE
      ONLY for an explicit order to fire ("fire", "cleared to fire", "cleared hot"). Never for a question,
      a negation, or "engage" alone. Under WEAPONS FREE you fire on your own once tracking.
    - "Weapons free" / "weapons hold" -> GEORGE_SET setting "roe" value "FREE" / "HOLD".
    - A specific value -> GEORGE_SET with that value. "Cycle"/"next" without a value -> value "NEXT".
    - If ordered to fire while Master Arm is DISARMED, call out the disarmed state.
    - If target is out of weapon range or TADS is masked, note constraint in radio speech.
    - Keep speech under 15 words.

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
    - "NONE": Conversational or unrecognized command.

    Respond STRICTLY in JSON format:
    {{
      "action": "<ACTION_NAME>",
      "target_index": <int or null>,
      "setting": "<GEORGE_SET only, else null>",
      "value": "<GEORGE_SET value, AREA_SEARCH / POINT_SEARCH_AT name, else null>",
      "targets": <ENGAGE_TARGETS only: list of target numbers, else null>,
      "speech": "<Short military callout reply>"
    }}
    """

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(CONVERSATION_HISTORY)
    messages.append({"role": "user", "content": transcript_text})

    response = ollama_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=LLM_MAX_TOKENS,
        response_format={"type": "json_object"},
    )

    result = json.loads(response.choices[0].message.content)
    if not isinstance(result, dict):
        result = {}
    return result, raw_snapshot


def chat_json(system_prompt: str, history, transcript_text: str) -> Dict[str, Any]:
    """One JSON-mode LLM call for another aircraft profile (e.g. huey/profile.py).
    history: iterable of earlier {"role", "content"} messages."""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": transcript_text})
    response = ollama_client.chat.completions.create(
        model=LLM_MODEL, messages=messages, temperature=0.1, max_tokens=LLM_MAX_TOKENS,
        response_format={"type": "json_object"},
    )
    result = json.loads(response.choices[0].message.content)
    return result if isinstance(result, dict) else {}


def remember_turn(transcript_text: str, action: str, target_index: Optional[int], speech: str,
                  setting: Optional[str] = None, value: Optional[str] = None) -> None:
    """Records what George ACTUALLY did (post-sanitize), so a rejected action is never
    remembered as executed. Called from the voice-worker thread only."""
    CONVERSATION_HISTORY.append({"role": "user", "content": transcript_text})
    CONVERSATION_HISTORY.append({
        "role": "assistant",
        "content": json.dumps({"action": action, "target_index": target_index,
                               "setting": setting, "value": value, "speech": speech}),
    })


OLLAMA_NATIVE = "http://localhost:11434/api"
KEEP_WARM_CHECK_SEC = 60


def _ollama(path: str, body: Optional[dict] = None, timeout: float = 5.0) -> dict:
    """Ollama's native API (the OpenAI-compatible one can't set keep_alive)."""
    import urllib.request
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{OLLAMA_NATIVE}/{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _model_loaded() -> bool:
    models = _ollama("ps").get("models") or []
    want = LLM_MODEL if ":" in LLM_MODEL else LLM_MODEL + ":latest"
    return any(m.get("name") == want or m.get("model") == want for m in models)


def _load_model() -> float:
    """Loads the model into VRAM and tells Ollama to keep it there for good (keep_alive -1).
    Loading with DCS running can take far longer than a command's timeout, hence 120 s."""
    t0 = time.perf_counter()
    _ollama("generate", {"model": LLM_MODEL, "keep_alive": -1}, timeout=120)
    return time.perf_counter() - t0


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