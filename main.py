import os
import queue
import threading
import time

import numpy as np
import sounddevice as sd
from pynput.keyboard import Controller, Key, Listener
from faster_whisper import WhisperModel

# --- CONFIG & MODULE IMPORTS ---
from config import WPN_PAGE_FRESH_SEC, WAS_READBACK_SEC
from config import (PTT_KEY, SAMPLE_RATE, KEY_DELAY, MIC_DEVICE, MIN_UTTERANCE_SEC,
                    TRAILING_CAPTURE_SEC, WHISPER_MODEL, WHISPER_CPU_THREADS, STT_PROMPT,
                    READBACK_TIMEOUT_SEC, TELEMETRY_STALE_SEC, UTTERANCE_MAX_AGE_SEC)
from core.state import state_store
from core.telemetry_listener import start_telemetry_thread
from core.dcs_bios import start_dcs_bios_thread, cmd_sender
from core.george_interface import GeorgeInterface, TOGGLE_VK
from core.george_ui import start_george_ui_thread
from core.console_log import start_console_log
from core.wpn_page_listener import start_wpn_page_thread
from intelligence.llm_client import (query_qwen, sanitize_result, warmup_llm, remember_turn,
                                     AutonomousAnnunciator)
from intelligence.threat_engine import evaluate_tactical_environment
from intelligence.engagement_monitor import EngagementMonitor
from intelligence.fast_path import match_fast
from audio.tts import (speak_george_radio, start_speech_worker, ptt_active,
                       PRIORITY_ALERT, PRIORITY_PILOT)


# --- GEORGE CPG (all wheel / MFI keystrokes live in core/george_interface.py) ---
# The old driver, core/george_wheel.py, is kept as a fallback but no longer used.
keyboard = Controller()


def _george_event(event: str, data: dict) -> None:
    """Called on the George worker thread. Speech goes through the non-blocking radio queue."""
    if event == "list_opened":
        speak_george_radio(list_readout(), priority=PRIORITY_PILOT)
    elif event == "say" and data.get("text"):
        speak_george_radio(data["text"], priority=PRIORITY_PILOT)
    elif event == "tracking":
        state_store.set_engagement([data["index"]])
        if data.get("name"):
            speak_george_radio(f"Tracking {data['name']}.", priority=PRIORITY_PILOT)
    elif event == "engaging":
        state_store.set_engagement(data["indices"])
        names = data.get("names") or []
        first = f" First, {names[0]}." if names and names[0] else ""
        n = len(data["indices"])
        speak_george_radio((f"Engaging {n} targets." if n > 1 else "Engaging.") + first,
                           priority=PRIORITY_PILOT)
    elif event == "rejected" and data.get("reason"):
        speak_george_radio(data["reason"], priority=PRIORITY_PILOT)


# George reads his own interface back from DCS (george_ai_export.lua -> core/george_ui.py) and
# publishes his Target List to the state store, so "target 2" means George's #2 on the wheel.
george = GeorgeInterface(keyboard, on_event=_george_event,
                         ui_source=state_store.george_ui_snapshot,
                         list_sink=state_store.set_george_list)


# --- ACTION EXECUTION ---

# Your own cockpit switches through DCS-BIOS (port 7778), read back to confirm:
#   master arm  <- ARM indicator light (DCS-BIOS 5010 stream)
#   your weapon <- boxed weapon on your WPN page (george_export.lua -> 5006)
# name -> (weapon or arm state wanted, state key, freshness key, spoken label)
BIOS_ACTIONS = {
    "SET_ARMED":       ("ARMED",    "master_arm", "bios_age",     "master arm"),
    "SET_DISARMED":    ("DISARMED", "master_arm", "bios_age",     "master arm"),
    "SELECT_HELLFIRE": ("MSL",      "was_state",  "wpn_page_age", "missile selection"),
    "SELECT_GUN":      ("GUN",      "was_state",  "wpn_page_age", "gun selection"),
    "SELECT_ROCKETS":  ("RKT",      "was_state",  "wpn_page_age", "rocket selection"),
    "SELECT_WAS_OFF":  ("NONE",     "was_state",  "wpn_page_age", "weapon off"),
}


def _fresh(snap: dict, age_key: str) -> bool:
    limit = WPN_PAGE_FRESH_SEC if age_key == "wpn_page_age" else TELEMETRY_STALE_SEC
    return snap[age_key] <= limit


def run_bios_action(name: str):
    """Sends a DCS-BIOS command, then reads the result back.
    Returns (confirmed: bool, spoken_label: str, note: str)."""
    want, key, age_key, label = BIOS_ACTIONS[name]
    snap = state_store.snapshot()
    known = _fresh(snap, age_key)
    # Both controls TOGGLE (a second press un-does the first), so never press when already there.
    if known and snap[key] == want:
        print(f"[ACTION] {name}: already {want}, no press needed")
        return True, label, ""
    if age_key == "bios_age" and not known:
        return False, label, "Switch state unavailable."
    if name == "SELECT_WAS_OFF":
        if not known:
            return False, label, "Put your WPN page up first."
        sent = cmd_sender.set_was_mode("OFF", current=snap["was_state"])
    elif key == "was_state":
        sent = cmd_sender.set_was_mode(want)    # another direction swaps weapons directly
    else:
        sent = cmd_sender.set_master_arm("ARM" if want == "ARMED" else "SAFE")
    if not sent:
        return False, label, ""
    print(f"[ACTION] DCS-BIOS: {name}")
    timeout = WAS_READBACK_SEC if key == "was_state" else READBACK_TIMEOUT_SEC
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = state_store.snapshot()
        if _fresh(snap, age_key) and snap[key] == want:
            return True, label, ""
        time.sleep(0.05)
    if key == "was_state" and not _fresh(state_store.snapshot(), age_key):
        return False, label, "Pressed, but your WPN page isn't up to confirm."
    return False, label, ""


def run_george_action(action: str, target_index=None, setting=None, value=None, speech=None,
                      targets=None):
    """George CPG actions. Validates and queues the keystrokes (never blocks).
    Returns (accepted: bool, reason_to_speak: str)."""
    ok, reason = george.run_action(action, target_index=target_index, setting=setting, value=value,
                                   speech=speech, targets=targets)
    if not ok:
        print(f"[ACTION] George rejected '{action}': {reason}")
    return ok, reason


# --- TARGET DESCRIPTIONS (clock position and range come from Export.lua contacts) ---

def _where(t: dict) -> str:
    """'2 o'clock, 2.1 kilometers' / '2 o'clock' / '' from a target dict."""
    parts = []
    if t.get("clock"):
        parts.append(f"{t['clock']} o'clock")
    if t.get("dist_m") is not None:
        parts.append(f"{t['dist_m'] / 1000:.1f} kilometers")
    return ", ".join(parts)


def describe_target(idx: int) -> str:
    targets = state_store.snapshot()["targets"]
    if not 1 <= idx <= len(targets):
        return f"Target {idx} not in the list."
    t = targets[idx - 1]
    where = _where(t)
    return f"Target {idx}, {t['type']}" + (f", {where}." if where else ", position unknown.")


def list_readout(limit: int = 4) -> str:
    """'Target list: 1 LARC-V at 2 o'clock, 2 LARC-V at 3 o'clock, and 2 more.'"""
    snap = state_store.snapshot()
    targets, complete = snap["targets"], snap.get("george_list_complete", False)
    if not targets:
        return "No targets."
    items = [f"{t['id']} {t['type']}" + (f" at {t['clock']} o'clock" if t.get("clock") else "")
             for t in targets[:limit]]
    if len(targets) > limit:
        more = f", and {len(targets) - limit} more" if complete else ", and more"
    else:
        more = "" if complete else ", and more"
    return "Target list: " + ", ".join(items) + more + "."


# --- STATUS REPORTS ARE BUILT FROM STATE, NOT BY THE LLM ---

_WAS_WORDS = {"NONE": "off", "GUN": "gun", "MSL": "missile", "RKT": "rockets"}

def build_status_report(snap: dict) -> str:
    parts = []
    if snap["telemetry_age"] > TELEMETRY_STALE_SEC:
        parts.append("No flight data.")
    else:
        parts.append(f"Altitude {snap['alt_ft']:.0f} feet, airspeed {snap['speed_kts']:.0f} knots.")
    if snap["bios_age"] <= TELEMETRY_STALE_SEC:
        parts.append(f"Master arm {'armed' if snap['master_arm'] == 'ARMED' else 'safe'}.")
    if snap["wpn_page_age"] <= WPN_PAGE_FRESH_SEC and snap["was_state"] in _WAS_WORDS:
        parts.append(f"Your weapon {_WAS_WORDS[snap['was_state']]}.")
    parts.append(george.describe(include_list=False))
    targets, top, _ = evaluate_tactical_environment(snap)
    if targets:
        parts.append(list_readout())
    if top:
        where = _where(top)
        parts.append(f"Priority {top['type']}" + (f", {where}." if where else "."))
    else:
        parts.append("No targets.")
    return " ".join(parts)


# --- COMMAND PIPELINE (runs on the voice worker thread, never on the keyboard hook) ---

def _target_unchanged(prompt_targets: list, idx: int) -> bool:
    """True if the target at wheel slot idx is still the same type as when the LLM saw it."""
    now = state_store.snapshot()["targets"]
    if idx > len(now) or idx > len(prompt_targets):
        return False
    return now[idx - 1].get("type") == prompt_targets[idx - 1].get("type")


# A fire order is only valid for a few seconds: if the pipeline was busy (e.g. a slow LLM call),
# a "send it" said earlier must not fire now.
FIRE_MAX_AGE_SEC = 3.0


def handle_command(text: str, captured: float = None) -> None:
    t0 = time.perf_counter()

    # Fast path: simple unambiguous commands skip the LLM entirely (milliseconds, not seconds).
    # match_fast returns None whenever it is unsure, and the LLM handles those.
    snap = state_store.snapshot()
    res = match_fast(text, snap)
    source = "FAST"
    if res is None:
        source = "LLM"
        try:
            res, snap = query_qwen(text, george.describe())
        except Exception as e:
            print(f"[ERROR] LLM request failed: {type(e).__name__}: {e}")
            speak_george_radio("Say again, system fault.", priority=PRIORITY_PILOT)
            return
    t_llm = time.perf_counter() - t0

    # Validate against the SAME snapshot the LLM was shown.
    # George's list may continue below the entries seen so far; then allow higher numbers and
    # let the driver scroll down and check (it refuses past the real end of the list).
    known = len(snap["targets"])
    limit = known if snap.get("george_list_complete") or known == 0 else max(known, 30)
    action, target_idx, speech, setting, value, targets = sanitize_result(res, limit)

    if action == "ENGAGE_TARGETS" and any(i <= known and not _target_unchanged(snap["targets"], i)
                                          for i in targets):
        print("[ACTION] Target list changed during LLM call; aborting engage.")
        action, targets, speech = "NONE", None, "Target list changed, say again."
    if (action == "SELECT_TARGET_INDEX" and target_idx <= known
            and not _target_unchanged(snap["targets"], target_idx)):
        print("[ACTION] Target list changed during LLM call; aborting select.")
        action, target_idx, speech = "NONE", None, "Target list changed, say again."

    if action == "CONSENT_TO_FIRE" and captured is not None:
        age = time.monotonic() - captured
        if age > FIRE_MAX_AGE_SEC:
            print(f"[ACTION] Fire order is {age:.1f}s old; not firing.")
            action, speech = "NONE", "Fire order expired, say again."
    remember_turn(text, action, target_idx, speech, setting,
                  value if targets is None else ",".join(map(str, targets)))
    print(f"[GEORGE]: action={action} target={target_idx} setting={setting} value={value} "
          f"speech='{speech}'")

    if action == "STATUS":
        speak_george_radio(build_status_report(state_store.snapshot()), priority=PRIORITY_PILOT)
    elif action == "TARGET_INFO":
        speak_george_radio(describe_target(target_idx), priority=PRIORITY_PILOT)
    elif action in BIOS_ACTIONS:
        confirmed, label, note = run_bios_action(action)
        if confirmed:
            speak_george_radio(speech, priority=PRIORITY_PILOT)
        else:
            # Never claim success we couldn't observe.
            print(f"[WARN] {action}: not confirmed. {note}")
            speak_george_radio(note or f"Unable to confirm {label}.", priority=PRIORITY_PILOT)
    elif action != "NONE":
        # Queue the keystrokes first (instant), then speak; a rejected action speaks its reason.
        # Settings and toggles are spoken by George himself once his wheel shows the result.
        ok, reason = run_george_action(action, target_idx, setting, value, speech, targets)
        if not ok:
            speak_george_radio(reason, priority=PRIORITY_PILOT)
        else:
            if action == "CEASE":
                state_store.set_engagement([])
            if not george.confirms(action):
                speak_george_radio(speech, priority=PRIORITY_PILOT)
    else:
        speak_george_radio(speech, priority=PRIORITY_PILOT)

    print(f"[TIMING] {source} {t_llm:.2f}s | total {time.perf_counter() - t0:.2f}s")


# --- PTT RECORDING (hook callbacks only flip flags and enqueue) ---

utterances: "queue.Queue[tuple[float, np.ndarray]]" = queue.Queue()
rec_lock = threading.Lock()
audio_frames = []
is_recording = False
key_down = False       # filters Windows key auto-repeat
session = 0            # lets a quick re-press cancel the previous release's trailing capture

def record_callback(indata, frames, time_info, status):
    if is_recording:
        with rec_lock:
            audio_frames.append(indata.copy())

_CTRL_KEYS = (Key.ctrl_l, Key.ctrl_r, Key.ctrl)
_ctrl_down = False
_v_down = False        # filters Windows key auto-repeat on the Ctrl+V toggle

def _track_wheel_toggle(key, pressed: bool) -> None:
    """Hook helper: notices the PILOT pressing Ctrl+V (our own presses are ignored inside wheel)."""
    global _ctrl_down, _v_down
    if key in _CTRL_KEYS:
        _ctrl_down = pressed
    elif TOGGLE_VK is not None and getattr(key, "vk", None) == TOGGLE_VK:
        if pressed and _ctrl_down and not _v_down:
            george.note_user_toggle()
        _v_down = pressed

def on_press(key):
    global is_recording, key_down, session, audio_frames
    _track_wheel_toggle(key, True)
    if key != PTT_KEY or key_down:
        return
    key_down = True
    with rec_lock:
        session += 1
        audio_frames = []
        is_recording = True
        ptt_active.set()          # inside the lock: ordered against the finalizer's clear()
    print("\n[PTT] Listening...")

def _finalize_recording(my_session: int) -> None:
    global is_recording, audio_frames
    time.sleep(TRAILING_CAPTURE_SEC)        # catch the tail of the last word
    with rec_lock:
        if my_session != session:           # PTT was pressed again during the tail
            return
        is_recording = False
        frames, audio_frames = audio_frames, []
        ptt_active.clear()                  # inside the lock: a re-press can't be clobbered
    if not frames:
        return
    audio = np.concatenate(frames, axis=0).flatten().astype(np.float32)
    if len(audio) < MIN_UTTERANCE_SEC * SAMPLE_RATE:
        print("[PTT] Tap ignored (too short).")
        return
    utterances.put((time.monotonic(), audio))

def on_release(key):
    global key_down
    _track_wheel_toggle(key, False)
    if key != PTT_KEY or not key_down:
        return
    key_down = False
    print("[PTT] Analyzing input...")
    threading.Thread(target=_finalize_recording, args=(session,), daemon=True).start()


# --- VOICE WORKER: STT -> LLM -> ACTION -> SPEECH ---

# Whisper tends to invent these on near-silence.
_HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thanks"}

def voice_worker(stt_model: WhisperModel) -> None:
    while True:
        captured, audio = utterances.get()
        age = time.monotonic() - captured
        if age > UTTERANCE_MAX_AGE_SEC:
            print(f"[STT] Dropped stale utterance ({age:.1f}s old).")
            continue
        try:
            t0 = time.perf_counter()
            segments, _ = stt_model.transcribe(
                audio, beam_size=1, vad_filter=True,
                initial_prompt=STT_PROMPT, condition_on_previous_text=False,
            )
            text = " ".join(s.text for s in segments).strip()
            t_stt = time.perf_counter() - t0

            if not text or text.lower().strip(" .!?,") in _HALLUCINATIONS:
                print("[STT] No speech detected.")
                continue

            print(f"[PILOT]: '{text}'  (STT {t_stt:.2f}s)")
            handle_command(text, captured)
        except Exception as e:
            print(f"[WORKER ERROR] {type(e).__name__}: {e}")   # one bad utterance never kills the pipeline


def lower_process_priority() -> None:
    """Run below DCS so STT/TTS bursts don't steal its main thread (Windows only)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
    except Exception as e:
        print(f"[WARN] Could not lower process priority: {e}")


def speak_alert(text: str) -> None:
    # Alerts go ahead of routine speech but are dropped if they sit queued for > 5 s.
    speak_george_radio(text, priority=PRIORITY_ALERT, max_age=5.0)


# --- MAIN ---
if __name__ == "__main__":
    start_console_log()             # mirror the console to logs/george_*.log
    print("\n=======================================================")
    print("      DCS AH-64D GEORGE AI CO-PILOT MIDDLEWARE         ")
    print("=======================================================\n")

    lower_process_priority()

    print("[1/5] Starting Export.lua Telemetry Listener...")
    start_telemetry_thread()

    print("[2/5] Starting DCS-BIOS Multicast Listener & Command Sender...")
    start_dcs_bios_thread()
    start_george_ui_thread()      # George's wheel / Target List from george_ai_export.lua
    start_wpn_page_thread()       # your actioned weapon / sight from the WPN page (george_export.lua)

    print("[3/5] Loading Faster-Whisper Speech Recognition Model...")
    try:    # use the cached model without contacting huggingface.co (fully local)
        stt_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                 cpu_threads=WHISPER_CPU_THREADS, local_files_only=True)
    except Exception:   # first run on this PC: the model must be downloaded once
        print("[STT] Whisper model not cached yet - downloading it once...")
        stt_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                 cpu_threads=WHISPER_CPU_THREADS)

    print("[4/5] Starting radio speech queue & warming LLM...")
    start_speech_worker()
    threading.Thread(target=warmup_llm, daemon=True).start()

    print("[5/5] Launching Autonomous Flight Safety & Threat Annunciator...")
    annunciator = AutonomousAnnunciator(tts_callback_func=speak_alert)
    annunciator.start()
    engagement_monitor = EngagementMonitor(speak_alert)   # missile away / target destroyed callouts
    engagement_monitor.start()
    print("[WHEEL] Assuming the George wheel is HIDDEN. If it's open, press Ctrl+V once or say 'toggle wheel'.")
    print("[WHEEL] Assuming George starts on NO WEAPON, WEAPONS HOLD, TADS fixed forward "
          "(corrected automatically once the wheel is shown and george_ai_export.lua is running).")
    for warning in george.bindings_report():
        print(f"[WARN] {warning}")

    threading.Thread(target=voice_worker, args=(stt_model,), daemon=True, name="george-voice").start()

    print("\n>>> ALL SYSTEMS ONLINE <<<")
    print("Hold [SPACEBAR] to talk to George. Press Ctrl+C to exit.\n")

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, device=MIC_DEVICE,
                            callback=record_callback):
            with Listener(on_press=on_press, on_release=on_release) as listener:
                listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        annunciator.stop()
        george.shutdown()
        print("\n[SHUTDOWN] George offline.")