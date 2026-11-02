import atexit
import os
import re
import queue
import threading
import time

import numpy as np
import sounddevice as sd
from pynput.keyboard import Controller, Key, Listener
from faster_whisper import WhisperModel

# --- CONFIG & MODULE IMPORTS ---
import config
from config import WPN_PAGE_FRESH_SEC, WAS_READBACK_SEC
from config import PTT_MODE, JOY_NAME_CONTAINS, JOY_DEVICE_INDEX, JOY_BUTTON, JOY_BACKEND
from config import (PTT_KEY, SAMPLE_RATE, KEY_DELAY, MIC_DEVICE, MIN_UTTERANCE_SEC,
                    TRAILING_CAPTURE_SEC, WHISPER_MODEL, WHISPER_CPU_THREADS, STT_PROMPT,
                    READBACK_TIMEOUT_SEC, TELEMETRY_STALE_SEC, UTTERANCE_MAX_AGE_SEC)
from core.state import state_store, enemy_lists_stale
from core.telemetry_listener import start_telemetry_thread
from core.dcs_bios import start_dcs_bios_thread, cmd_sender
from core.george_interface import GeorgeInterface, TOGGLE_VK
from core.george_ui import start_george_ui_thread
from core.console_log import start_console_log
from core.wpn_page_listener import start_wpn_page_thread
from core.joystick_ptt import JoystickPTT
from intelligence.llm_client import (query_qwen, sanitize_result, warmup_llm, remember_turn,
                                     AutonomousAnnunciator, chat_json, prime_prompt, GEORGE_SYSTEM)
from huey import commands as huey_commands
from huey.link import start_huey_link_thread
from huey.profile import HueyProfile
from huey.crew_chief import CrewChief
from apache.crew import ApacheCrew
from common import comms_menu, nav_commands, wingmen
from common import overlord as overlord_mod
from common import briefing, position, targets as targets_mod
from common.user_waypoints import USER_WAYPOINTS
from common.debrief import DEBRIEF
from common.airfields import AIRFIELDS
from common import aircraft as aircraft_mod
from common.fuel_watch import FuelWatch
from common import carrier as carrier_mod
from intelligence import ollama_chat
from core.focus import dcs_has_focus
from intelligence.threat_engine import evaluate_tactical_environment
from intelligence.engagement_monitor import EngagementMonitor
from intelligence.fast_path import match_fast
from intelligence import router as router_mod
from audio.tts import (speak_george_radio, speak_overlord, speak_instructor, speak_warlord, speak_darkside,
                       speak_radio, PRIORITY_ROUTINE, start_speech_worker, ptt_active,
                       PRIORITY_ALERT, PRIORITY_PILOT)
from audio import tts as tts_mod
from kneeboard.comms_log import LOG as COMMS_LOG
from core.metrics import METRICS
from common.orders import ORDERS
from common import training as training_mod
from core import lanes
from core.lanes import LANES
from kneeboard import pages as kb_pages
from huey.checklists import CHECKLISTS as HUEY_CHECKLISTS
from apache.checklists import APACHE_CHECKLISTS
from common.jet_checklists import JetChecklists


# --- GEORGE CPG (all wheel / MFI keystrokes live in core/george_interface.py) ---
# The old driver, core/george_wheel.py, is kept as a fallback but no longer used.
keyboard = Controller()


# Carrier recovery: our UNPROMPTED speech is held from Inbound until you're on deck (common/carrier.py).
QUIET = carrier_mod.QuietWindow()


def _unprompted(text: str, speak) -> None:
    """Speech nobody asked for (callouts, Overlord's calls, Outcast reports): held while quiet."""
    if not QUIET.hold(text, speak):
        speak(text)


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


# --- UH-1H HUEY CREW (huey/). Used automatically while you fly the UH-1H. ---
def huey_llm(system_prompt, history, text):
    """The LLM for the Huey, except while the radio menu is up and driven by keys."""
    if comms.blocks_llm():
        return {"action": "NONE", "speech": "Say again, which F key?"}
    if ROUTER_ON and "\nPILOT:" in text:            # who can be given orders (for ROUTE)
        head, tail = text.split("\nPILOT:", 1)
        text = f"{head} | {routing_state()}\nPILOT:{tail}"
    return chat_json(system_prompt, history, text)


# --- DCS Olympus: wingman orders beyond the radio menu (common/olympus_orders.py) ---
def _george_top(snap: dict):
    """"My target": the JTAC's current target when Overlord gave one (jets, any aircraft), else
    George's priority target (Apache)."""
    jt = JTAC.my_target() if "JTAC" in globals() else None
    if jt is not None:
        return jt
    if state_store.profile() != "apache":
        return None
    _, top, _ = evaluate_tactical_environment(snap)
    return top


# --- BRIEFING KNOWLEDGE BASE (common/briefing.py): data/*.json + the DCS mission briefing ---
KB = briefing.KnowledgeBase(os.path.join(config.BASE_DIR, "data"), extra=briefing.checklist_entries)
def _on_brief(text: str) -> None:
    """A mission briefing from the hook (only sent on when it changed): a new mission."""
    DEBRIEF.new_mission(briefing.parse_brief_message(text)[0])
    KB.on_brief_message(text)


comms_menu.ON_BRIEF = _on_brief                    # george_comms_hook.lua sends "BRIEF ..."
comms_menu.ON_AIRBASES = AIRFIELDS.on_airbases_message   # ... "AIRBASES ..." (fields and FARPs)


def _on_mission_loaded() -> None:
    """DCS loaded or restarted a mission: unit ids and Olympus group names start again, so the last
    mission's Overlord flights, Outcasts and claimed groups are forgotten (28 Oct: a restart left
    Olympus-1 'claimed', the next clone was refused, and Overlord called the old escort down)."""
    print("[MISSION] Loaded: forgetting the last mission's flights, Outcasts and home plate.")
    state_store.set_callsign(None)          # the new mission's callsign comes from the hook (P2-2)
    if DARKSIDE is not None:
        DARKSIDE.reset()                    # kills called, aborts: unit ids start again
    if OVERLORD is not None:
        OVERLORD.reset()
    if wingmen.INDEPENDENTS is not None:
        wingmen.INDEPENDENTS.reset()
    if wingmen.EXECUTOR is not None and hasattr(wingmen.EXECUTOR.oly, "reset_claims"):
        wingmen.EXECUTOR.oly.reset_claims()
    FUEL_WATCH.reset()
    JTAC.reset()
    JET_CHECKLISTS.reset()
    QUIET.end("new mission")
    TRAINING.forget()                       # training targets went with the old mission
    TRACK.reset()                           # the kneeboard track starts again with the mission
    ALERTS.reset()                          # the last mission's alerts go to the Alerts history
    if INSTRUCTOR is not None:
        INSTRUCTOR.on_mission_loaded()      # a running exercise is cancelled (stored, not scored)
    if TACTICAL is not None:
        TACTICAL.mission_loaded()           # unit ids start again: a new picture, new track numbers
    KB_PAGES.clear_contact()                # the last mission's track numbers mean nothing now


comms_menu.ON_LOADED = lambda: _on_mission_loaded()


# Training targets: the gate (common/training.py, audit step 7). The Game Master client is made on
# first use, only when Olympus is on and config.py has a Game Master password.
def _training_client(guard):
    pw = getattr(config, "OLYMPUS_GM_PASSWORD", "")
    if not (getattr(config, "OLYMPUS_ENABLED", False) and pw):
        return None
    from common import olympus
    return olympus.Olympus(config.OLYMPUS_URL, getattr(config, "OLYMPUS_GM_USER", "") or config.OLYMPUS_USER,
                           pw, allowed=training_mod.TRAINING_COMMANDS, guard=guard)


TRAINING = training_mod.Training(state_store.snapshot, _training_client, mission_name=KB.mission_name,
                                 olympus_enabled=lambda: bool(getattr(config, "OLYMPUS_ENABLED", False)))
comms_menu.ON_PLAYERS = TRAINING.note_players
TRAINING.on_destroyed = lambda names: _alert_training(names)     # Alerts tab (defined below)
atexit.register(TRAINING.shutdown)
# Feature 8: "Overlord, spawn two T-72s 5 km north of me" (common/training_targets.py), through the gate.
# "Group is up" / "failed to appear" come later, from its own thread, in the instructor's voice (1 Nov:
# one training mode); each kill also goes to the instructor's score log.
from common import training_targets as training_targets_mod
SPAWNER = training_targets_mod.Spawner(
    TRAINING, state_store.snapshot,
    lambda text: _unprompted(text, lambda t: speak_instructor(t, priority=PRIORITY_PILOT)), debrief=DEBRIEF,
    on_kill=lambda what, secs: INSTRUCTOR.record_kill(what, secs) if INSTRUCTOR is not None else None)
from common.unit_types import TYPES as UNIT_TYPES
comms_menu.ON_TYPES = UNIT_TYPES.on_types_message   # ... and "TYPES ..." (what each unit type is)

# --- INSTRUCTOR MODE (instructor/, feature 7): "Instructor, hover exercise" / "begin" / "how did I do".
# Read-only: it reads the state store on its own thread and speaks; it never sends anything to DCS.
# config.INSTRUCTOR_ENABLED = False turns it off (then "Instructor, ..." goes to George as before).
from instructor import session as instructor_mod
INSTRUCTOR_FARP_KM = 10.0


def _instructor_target(text: str, snap: dict):
    """The approach exercise's landing point (decision, 30 Oct): a waypoint named in the words, else
    the nearest helipad / FARP within INSTRUCTOR_FARP_KM. Returns {"name", "ll", "kind"}, a refusal to
    say, or None. Waypoints (31 Oct flight: "approach, waypoint 1" / "approach for waypoint 4" were missed):
      * a mission route waypoint by number, heard as George hears it (common/nav_commands.py), placed on
        the map by the map-to-lat/lon fit (common/geo.py: needs a minute of flying or contacts first)
      * one of your named points (Alpha, Bravo ... objective), with or without "to" / "on" before it."""
    from common import user_waypoints as uw_mod
    from common import geo, nav, nav_commands
    t = nav_commands._norm(text)
    m = nav_commands._NUMBERED.search(t)
    if m:
        n = nav_commands._num(m.group(1))
        p = nav.find_waypoint(snap.get("route") or [], n)
        if p is None:
            return f"Instructor, no waypoint {n} in the mission route."
        geo.FIT.add_snapshot(snap)
        ll = geo.FIT.to_ll(float(p[1]), float(p[2]))
        if ll is None:
            return (f"Instructor, I can't place waypoint {n} on the map yet. Fly a minute and ask again, "
                    f"or use a named waypoint or a FARP.")
        return {"name": f"waypoint {n}", "ll": ll, "kind": "route"}
    name = uw_mod.named_in(text)
    if not name:
        nm = re.search(r"\b" + uw_mod.NAME + r"\b", (text or "").lower())
        name = uw_mod.canon(nm.group(1)) if nm else None
    if name:
        ll = USER_WAYPOINTS.ll(name, snap)
        if ll is None:
            return f"Instructor, waypoint {name} isn't set. Mark it first, or say approach exercise for the nearest FARP."
        return {"name": f"waypoint {name}", "ll": ll, "kind": "waypoint"}
    got = AIRFIELDS.nearest(snap, farp=True)
    if got is None or got[1] > INSTRUCTOR_FARP_KM * 1000:
        return None
    return {"name": got[0]["name"], "ll": (got[0]["lat"], got[0]["lon"]), "kind": "farp"}


def _instructor_fuel(snap: dict):
    from core.fuel import current_lbs
    return current_lbs(snap)


def _instructor_reserve(snap: dict) -> float:
    """Landing reserve for the fuel exercise: the Huey's own (the 20-minute light), else the aircraft profile's."""
    if state_store.profile() == "huey":
        return float(getattr(config, "HUEY_FUEL_RESERVE_LBS", 0) or 0)
    return float(aircraft_mod.profile(snap).reserve_lbs or 0)


def _fast_flag_path() -> "str | None":
    """Export.lua sends the 10 Hz flight block (I5) while this file exists: config.INSTRUCTOR_FAST_FLAG,
    else Saved Games\\DCS\\Scripts\\george_fast_telemetry.flag (None when that Scripts folder is missing)."""
    p = getattr(config, "INSTRUCTOR_FAST_FLAG", None)
    if p:
        return p
    scripts = os.path.join(os.path.expanduser("~"), "Saved Games", "DCS", "Scripts")
    return os.path.join(scripts, "george_fast_telemetry.flag") if os.path.isdir(scripts) else None


_FAST_NOTED = [False]


def _instructor_fast(on: bool) -> None:
    """Called from the instructor's observer thread when an exercise starts / ends (never per packet)."""
    path = _fast_flag_path()
    if path is None:
        if on and not _FAST_NOTED[0]:
            _FAST_NOTED[0] = True
            print("[INSTRUCTOR] No Saved Games\\DCS\\Scripts folder: 10 Hz telemetry not requested "
                  "(set INSTRUCTOR_FAST_FLAG in config.py).")
        return
    try:
        if on:
            with open(path, "w", encoding="utf-8") as f:
                f.write("George AI instructor: Export.lua sends flight data at 10 Hz while this file exists.\n")
        elif os.path.exists(path):
            os.remove(path)
        print(f"[INSTRUCTOR] 10 Hz telemetry {'requested' if on else 'off'}.")
    except OSError as e:
        print(f"[INSTRUCTOR] Couldn't {'create' if on else 'remove'} {path}: {e}")


INSTRUCTOR = (instructor_mod.build(config, state_store.snapshot, lambda text: _speak_instructor(text),
                                   mission_name=KB.mission_name, targets=_instructor_target, fuel=_instructor_fuel,
                                   reserve=_instructor_reserve, flight=state_store.flight_since,
                                   fast=_instructor_fast, enabled=lambda: TRAINING.on)
              if getattr(config, "INSTRUCTOR_ENABLED", True) else None)
# One training mode (1 Nov): "Training mode on / off", spawns and the instructor's exercises together,
# all in the instructor's voice. The gate (TRAINING) and the instructor stay separate modules.
from common import training_mode as training_mode_mod
TRAINING_MODE = training_mode_mod.TrainingMode(TRAINING, SPAWNER, INSTRUCTOR)


def _active_prompt():
    """The active crew's system prompt and its name (the one Ollama should keep cached)."""
    p = state_store.profile()
    if p == "huey":
        return huey_commands.llm_system(), "Huey crew"
    if p == "other":
        return router_mod.ROUTER_SYSTEM, "Router"
    return GEORGE_SYSTEM, "George"


def _kb_llm(messages):
    """The local model for long briefing extracts. Its prompt replaces the crew's in Ollama's
    cache, so the crew's is read back in afterwards (in the background)."""
    try:
        return ollama_chat.chat(messages, config.LLM_MODEL, timeout=config.LLM_TIMEOUT_SEC, max_tokens=120)
    finally:
        threading.Thread(target=prime_prompt, args=_active_prompt(), daemon=True).start()


def kb_aircraft() -> str:
    """Which procedures / manual apply: the crews' labels, else the DCS type name (manuals are
    imported per type: manual_import.py --aircraft FA-18C_hornet)."""
    p = state_store.profile()
    if p in ("huey", "apache"):
        return {"huey": "UH-1H", "apache": "AH-64D"}[p]
    return state_store.snapshot().get("aircraft") or "OTHER"


def briefing_answer(text: str, speaker: str = "george") -> str:
    return KB.answer(text, speaker, kb_aircraft(), llm=_kb_llm)


OVERLORD = None
# Phase 2 command roles (command/): every command the Blue commander client sends passes the one
# permission table (P2-1), enforced from P2-6 (PERMISSIONS_ENFORCE, default True while the roles are on;
# False = refusals only logged). COMMAND_ROLES_ENABLED = False: no Warlord / Darkside, as before Phase 2.
COMMAND_ROLES_ON = bool(getattr(config, "COMMAND_ROLES_ENABLED", True))
from command import exchange as exchange_mod     # P2-7: full (default) or short radio exchange
exchange_mod.configure(short=bool(getattr(config, "COMMAND_SHORT_FORM", False)),
                       ai_spoken=bool(getattr(config, "AI_TO_AI_SPOKEN", False)))
from command.permissions import Guard as _PermissionGuard
from command import roles as roles_mod
OPERATOR_CALLSIGN = getattr(config, "OPERATOR_CALLSIGN", roles_mod.DEFAULT_CALLSIGN)   # P2-2 fallback


def my_callsign() -> str:
    """What the roles call you: the mission's callsign ("Enfield 1-1"), else OPERATOR_CALLSIGN."""
    return roles_mod.callsign(state_store.snapshot(), OPERATOR_CALLSIGN)[0]


PERMISSIONS = _PermissionGuard(enforce=COMMAND_ROLES_ON and bool(getattr(config, "PERMISSIONS_ENFORCE", True)))
DANGER_CLOSE_M = float(getattr(config, "DARKSIDE_DANGER_CLOSE_M", 1000))
# WARLORD (common/warlord.py, P2-5): reviews every flight Overlord would spawn; his rules read the
# hand-written "warlord" block of data/campaign_briefing.json (command/rules.py)
WARLORD = None
if COMMAND_ROLES_ON:
    from command import rules as _rules
    from common.warlord import Warlord
    WARLORD = Warlord(state_store.snapshot, callsign=lambda: my_callsign(),
                      block=_rules.load_block(os.path.join(config.BASE_DIR, "data", "campaign_briefing.json")),
                      max_flights=int(getattr(config, "WARLORD_MAX_FLIGHTS", 4)), danger_close_m=DANGER_CLOSE_M,
                      # the tactical picture turned off (TACTICAL_ENABLED = False): he judges without it
                      picture_active=lambda: TACTICAL is None or _picture_link_active())


def _picture_link_active() -> bool:
    from tactical import link
    return link.active()
if getattr(config, "OLYMPUS_ENABLED", False):
    from common import olympus, olympus_orders
    _oly = olympus.Olympus(config.OLYMPUS_URL, config.OLYMPUS_USER, config.OLYMPUS_PASSWORD, guard=PERMISSIONS)
    wingmen.EXECUTOR = olympus_orders.Executor(_oly, state_store.snapshot, _george_top)
    # "two, go independent" -> Outcast 2, an AI group of its own that Olympus can task
    from common import independents
    wingmen.INDEPENDENTS = independents.Independents(
        wingmen.EXECUTOR, state_store.snapshot,
        lambda text: _unprompted(text, lambda t: speak_george_radio(t, priority=PRIORITY_PILOT)))
    # Overlord (C2, "Overlord, requesting SEAD ..."): same Olympus login, his own voice
    OVERLORD = overlord_mod.Overlord(
        _oly, state_store.snapshot,
        lambda text: _unprompted(text, lambda t: speak_overlord(t, priority=PRIORITY_PILOT)),
        my_target=lambda snap: (JTAC.my_target()                                  # "CAS on my target":
                                or targets_mod.my_target(snap, _george_top(snap))),   # JTAC's, George's, the nose
        ask=lambda q: briefing_answer(q, "overlord"),                              # briefing questions
        warlord=WARLORD)                                                           # P2-5: asked before every spawn


# Overlord as JTAC: "Overlord, request target" / "mark target" / "BDA" (common/jtac.py)
from common.jtac import Jtac
JTAC = Jtac(state_store.snapshot, oly=wingmen.EXECUTOR.oly if wingmen.EXECUTOR else None,
            speak=lambda text: _unprompted(text, lambda t: speak_overlord(t, priority=PRIORITY_PILOT)))
# DARKSIDE (common/darkside.py, P2-4 / P2-6): the JTAC calls in his voice, and terminal control of
# Overlord's CAS flights (weapons hold, IP, cleared hot by his checks, "Darkside, abort").
DARKSIDE = None
if COMMAND_ROLES_ON:
    from common.darkside import Darkside
    DARKSIDE = Darkside(JTAC, state_store.snapshot,
                        lambda text: _unprompted(text, lambda t: speak_darkside(t, priority=PRIORITY_PILOT)),
                        callsign=lambda: my_callsign(), oly=JTAC.oly, warlord=WARLORD,
                        log=lambda who, text: COMMS_LOG.add(who, text), danger_close_m=DANGER_CLOSE_M,
                        say_ai=lambda text, who: _unprompted(       # AI_TO_AI_SPOKEN: held in the carrier quiet window
                            text, lambda t: speak_radio(t, who, priority=PRIORITY_ROUTINE, max_age=20.0)))
    JTAC.speak = DARKSIDE.jtac_speak                 # "good effect" in Darkside's voice
    if OVERLORD is not None:
        OVERLORD.darkside = DARKSIDE                 # CAS under his control (P2-6)
        OVERLORD.callsign = lambda: my_callsign()    # the full exchange names you (P2-7)
_JTAC_WAKE = re.compile(r"^\s*(?:jtac|j tac|jay tac|j-tac)[\s,.!?]+(.*)$", re.I)


# --- TACTICAL PICTURE (tactical/, design approved 31 Oct): one track per DCS object, with where each fact
# came from. Step T3: it runs on its own thread and shows a Status row; nothing reads it yet.
ENGAGEMENT = None                               # the engagement monitor, once started (last shot time)


def _attacker_name(snap: dict, aid: int) -> str:
    """Who an Olympus attackUnit came from: an Overlord flight, an Outcast or a wingman."""
    if OVERLORD is not None:
        for m in OVERLORD.active():
            ids = {int(u[2]) for u in (OVERLORD._flight(snap, m) or [])}
            if m.lead_id == aid or aid in ids:
                return f"{m.cs} flight"
    ind = wingmen.INDEPENDENTS
    for n in (ind.numbers() if ind else []):
        u = ind._unit(snap, n)
        if u and int(u[2]) == aid:
            return f"Outcast {n}"
    for w in snap.get("wing") or []:
        if int(w[1]) == aid:
            return str(w[0])
    return "an AI flight"


def _tactical_hints(snap: dict, attacks: dict):
    """Evidence that a vanished unit was destroyed: whose attack it was, and your / George's last shot."""
    from tactical.models import dcs_key
    from tactical.tracks import KillHints
    targeted = {key: _attacker_name(snap, aid) for key, aid in attacks.items()}
    if OVERLORD is not None:
        for m in OVERLORD.active():
            key = dcs_key((m.current or {}).get("id"))
            if key:
                targeted[key] = f"{m.cs} flight"
    for e in snap.get("engagement") or []:          # George's shoot list
        key = dcs_key(e.get("contact_id"))
        if key:
            targeted[key] = "George"
    last = getattr(ENGAGEMENT, "_last_shot", None)
    return KillHints(last_shot_mono=last if isinstance(last, (int, float)) and last > 0 else None, targeted=targeted)


TACTICAL = None
PICTURE_MAX_AGE_S = 5.0                         # the map uses the picture only while it's this fresh



def tactical_answer(text: str):
    """Questions about the tactical picture (tactical/queries.py, step T6): "tactical picture", "what threats
    are known", "unknown contacts", "what has JTAC reported", "which contacts are stale", "what's changed".
    The answer, or None if it isn't one of them. Fixed templates from the picture only (D6)."""
    from tactical import queries
    q = queries.match(text)
    if q is None or TACTICAL is None:
        return None
    pic = _KB_CTX.picture()                           # only while the service keeps it current
    if pic is None:
        return "The tactical picture isn't updating."
    snap = state_store.snapshot()
    me = snap.get("my_ll")
    say = queries.Say(dist=lambda m: aircraft_mod.say_dist(m, snap), type=overlord_mod.say_type,
                      compass=overlord_mod.compass)
    ctx = queries.Context(me_ll=(me[0], me[1]) if me else None, say=say, jtac=JTAC.current,
                          events=TACTICAL.events(), stale=TACTICAL.stale, stale_reason=TACTICAL.stale_reason)
    return queries.answer(q, pic, ctx)


def _picture_events(events) -> None:
    """New picture events (tactical/events.py) -> kneeboard comms log lines of kind "picture". Runs on the
    tactical thread; never speaks. Which ones: tactical/queries.py worth_reporting (also "what's changed")."""
    from kneeboard.model import short_tid
    from tactical.queries import worth_reporting
    from tactical.sources import dist_m, true_bearing
    pic = TACTICAL.picture() if TACTICAL is not None else None
    me = state_store.snapshot().get("my_ll")
    imperial = aircraft_mod.profile(state_store.snapshot()).imperial
    for e in events:
        if not worth_reporting(e, pic):
            continue
        if e.track_id is None:                           # the picture as a whole: stale, current, new mission
            COMMS_LOG.add("picture", e.text)
            continue
        t = pic.track(e.track_id)
        text = e.text.replace(f"({t.track_id})", f"({short_tid(t.track_id)})")
        if e.kind in ("new", "reappeared") and me and t.position is not None:
            d = dist_m((me[0], me[1]), (t.position.lat, t.position.lon))
            b = true_bearing((me[0], me[1]), (t.position.lat, t.position.lon))
            dist = f"{d / 1852:.0f} nm" if imperial else f"{d / 1000:.0f} km"
            text = text.rstrip(".") + f", {dist} {overlord_mod.compass(b)} of you."
        COMMS_LOG.add("picture", text)
if getattr(config, "TACTICAL_ENABLED", True):
    from tactical.service import TacticalService
    from tactical.tracks import Thresholds as _TacticalThresholds
    TACTICAL = TacticalService(state_store.snapshot, hints=_tactical_hints, jtac=lambda: JTAC.current,
                               thresholds=_TacticalThresholds.from_config(config))
    if getattr(config, "OLYMPUS_ENABLED", False):
        olympus.ON_COMMAND = TACTICAL.note_command
    TACTICAL.on_events = _picture_events            # picture changes on the kneeboard comms log (T5)
    comms_menu.ON_EVENTS = TACTICAL.note_dcs_events  # DCS's own "unit dead / killed" as evidence (T8)
    from tactical import link as _tactical_link     # kill / BDA, "closing", JTAC position read it (T7)
    _tactical_link.set_provider(_tactical_link.ServiceLink(TACTICAL, max_age=PICTURE_MAX_AGE_S))


def _bullseye():
    """Your coalition's bullseye from Olympus, or None when it isn't running."""
    ex = wingmen.EXECUTOR
    return position.olympus_bullseye(ex.oly) if ex is not None else None


def olympus_watch() -> None:
    """Feeds the map-coordinate fit (waypoints -> lat/lon) and connects to Olympus once it's up."""
    from common import geo
    tries = 0
    while True:
        time.sleep(2.0)
        try:
            geo.FIT.add_snapshot(state_store.snapshot())
            tries += 1
            if wingmen.EXECUTOR and not wingmen.EXECUTOR.oly.connected and tries % 15 == 0:
                wingmen.EXECUTOR.oly.connect()
            TRAINING.recover()                       # training targets left by a crash (cheap when none)
        except Exception as e:
            print(f"[OLYMPUS] {type(e).__name__}: {e}")


huey = HueyProfile(speak=lambda text: speak_george_radio(text, priority=PRIORITY_PILOT),
                   ask=lambda q: briefing_answer(q, "george"),
                   route=lambda text, command: ROUTER_ON and ROUTER.dispatch(text, command),
                   state=state_store, bios_sender=cmd_sender, llm=huey_llm,
                   press_keys=lambda keys: press_menu_keys(keys))


# --- SHARED: radio menu, navigation; Apache checklists / EUFD callouts ---
def _speak_pilot(text: str) -> None:
    speak_george_radio(text, priority=PRIORITY_PILOT)


def _speak_overlord(text: str) -> None:
    speak_overlord(text, priority=PRIORITY_PILOT)


def _speak_warlord(text: str) -> None:
    speak_warlord(text, priority=PRIORITY_PILOT)


def _speak_darkside(text: str) -> None:
    speak_darkside(text, priority=PRIORITY_PILOT)


def _speak_instructor(text: str) -> None:
    # Alert priority (never dropped as stale): "clock running" and the result are time-critical; on the
    # 30 Oct flight they waited 8-19 s behind the crew chief's callouts at pilot priority.
    speak_instructor(text, priority=PRIORITY_ALERT)


def _key(name: str):
    """'f5' / 'ctrl_r' / 'shift_r' -> pynput Key; anything else is a character."""
    return getattr(Key, name) if len(name) > 1 and hasattr(Key, name) else name


def press_menu_keys(keys) -> bool:
    """Presses keys in DCS: '\\', 'f1'..'f12', or combos like 'ctrl_r+shift_r+l'.
    Used for the radio menu when george_comms_hook.lua isn't running, and the cargo hook key."""
    if not dcs_has_focus():
        return False
    for combo in keys:
        parts = [_key(p) for p in (combo.split("+") if len(combo) > 1 else [combo])]
        for k in parts:
            keyboard.press(k)
            time.sleep(KEY_DELAY)
        for k in reversed(parts):
            keyboard.release(k)
            time.sleep(KEY_DELAY)
    return True


comms = comms_menu.CommsMenu(speak=_speak_pilot, press_keys=press_menu_keys)
# Jets: Overlord's joker / bingo calls (common/fuel_watch.py; numbers per type in common/aircraft.py)
FUEL_WATCH = FuelWatch(state_store.snapshot,
                       lambda text: (_alert_fuel(text),           # the Alerts tab at once, even if speech waits
                                     _unprompted(text, lambda t: speak_overlord(t, priority=PRIORITY_ALERT, max_age=30.0))))
# Supercarrier by voice: "Marshal, inbound" / "commencing" / "platform" pick its radio-menu items
CARRIER = carrier_mod.CarrierCalls(
    comms, QUIET, is_jet=lambda: aircraft_mod.profile(state_store.snapshot()).kind == "jet",
    carriers=lambda: any(b["cat"] == 2 for b in AIRFIELDS.bases()))
apache_crew = ApacheCrew(_speak_pilot, state_store, cmd_sender)


def as_overlord(said: str) -> str:
    """"Steer 270, 12 miles." -> "Overlord, steer 270, 12 miles." (a name or type keeps its capital:
    "SA-8, ..." -> "Overlord, SA-8, ...")."""
    said = (said or "").strip()
    if not said or said.lower().startswith("overlord"):
        return said
    word = said.split()[0]
    if len(word) > 1 and word[1:].islower():
        said = said[0].lower() + said[1:]
    return "Overlord, " + said


# Replies that stay in the crew voice in a jet: other aircraft talking, and the radio menu
JET_CREW_SOURCES = ("wingmen", "radio", "kneeboard")


def shared_reply(text: str):
    """Radio menu and navigation, for any aircraft; Apache checklists and EUFD questions.
    Runs the order and returns (what to say, speaker, who answered), or None if no shared
    handler takes it. Speaks nothing itself (handle_shared does).
    Jets (profile "other", no crew; Andre, 1 Nov): replies that aren't a wingman, an Outcast or the
    radio menu are Overlord's ("Overlord, steer 270, 12 miles.")."""
    got = _shared_reply(text)
    if got is None or state_store.profile() != "other":
        return got
    said, speaker, source = got
    if speaker == "george" and source not in JET_CREW_SOURCES and said:
        return as_overlord(said), "overlord", source
    return got


# Command roles P2-3: "Warlord, ..." / "Darkside, ..." answer in their own voices. Until their modules
# arrive (Darkside P2-4, Warlord P2-5) they answer a radio check and say where tasking goes for now.
_RADIO_CHECK = re.compile(r"\b(?:radio|comms?|mic)\s+check\b|\bhow (?:do )?(?:you|d'?you|ya) read\b|"
                          r"\bdo you read\b|\bcheck(?:ing)? in\b", re.I)


def role_reply(text: str):
    """(said, speaker, source) for an utterance addressed to Warlord or Darkside, else None."""
    if not COMMAND_ROLES_ON:
        return None
    from command import wake
    got = wake.addressed(text)
    if got is None:
        return None
    role, rest = got
    name, speaker = ("Warlord", "warlord") if role == roles_mod.WARLORD else ("Darkside", "darkside")
    cs = my_callsign()
    if not rest:
        said = f"{cs}, {name}, go ahead."
    elif _RADIO_CHECK.search(rest):
        said = f"{cs}, {name}, loud and clear."
    elif role == roles_mod.WARLORD:
        said = WARLORD.handle(rest) if WARLORD is not None else None     # override, objectives, ROE (P2-5)
        if said is None:
            said = f"{cs}, {name}, standing by. Request tasking through Overlord."
    else:
        said = DARKSIDE.handle(rest) if DARKSIDE is not None else None   # targets, mark, BDA, abort (P2-4/6)
        if said is None:
            said = f"{cs}, {name}, say again. I can give targets, mark, BDA, or abort a CAS run."
    return said, speaker, speaker


def _shared_reply(text: str):
    tm = TRAINING_MODE.wanted(text)                  # "Training mode on", "Overlord / Instructor, spawn ..."
    if tm is not None:
        return TRAINING_MODE.handle(*tm), "instructor", "instructor"
    got = role_reply(text)                           # "Warlord, ..." / "Darkside, ..." (P2-3)
    if got is not None:
        return got
    rest = overlord_mod.addressed(text)
    if rest is not None:                             # "Overlord, ...": only Overlord answers
        said = tactical_answer(rest)                 # "Overlord, threats" / "Overlord, tactical picture" (T6)
        if said is not None:                         # ("Overlord, picture" stays his air picture)
            return "Overlord, " + said[0].lower() + said[1:], "overlord", "overlord"
        if JTAC.wanted(rest):                        # "Overlord, request target" / "mark target" / "BDA"
            if DARKSIDE is not None:                 # P2-4: Overlord hands it to Darkside
                said = DARKSIDE.handle(rest)
                if said is not None:
                    pre = [] if exchange_mod.short() else [("Overlord, contact Darkside.", "overlord")]
                    return roles_mod.Said(said, pre=pre), "darkside", "darkside"
            said = JTAC.handle(rest)
            if said is not None:
                return said, "overlord", "overlord"
        if FUEL_WATCH.wanted(rest) and aircraft_mod.profile(state_store.snapshot()).fuel_calls:   # "Overlord, say bingo"
            a = FUEL_WATCH.answer(state_store.snapshot())
            return "Overlord, " + a[0].lower() + a[1:], "overlord", "overlord"
        if WARLORD is not None and WARLORD.held() is not None:     # "Overlord, override" (1 Nov flight)
            from common.warlord import _OVERRIDE as _WARLORD_OVERRIDE
            if _WARLORD_OVERRIDE.search(rest):
                return WARLORD.override(), "warlord", "warlord"
        if OVERLORD is not None:
            said = OVERLORD.handle(text)
        elif position.wanted(rest):
            said = "Overlord, " + position.handle(rest, state_store.snapshot())
        elif AIRFIELDS.handle(rest, state_store.snapshot()) is not None:
            got = AIRFIELDS.handle(rest, state_store.snapshot())
            said = "Overlord, " + got[0].lower() + got[1:]
        elif overlord_mod.is_question(rest.lower()):
            said = briefing_answer(rest, "overlord")
        else:
            said = "Overlord, unable, Olympus is turned off in config."
        return (said if said is not None else ""), "overlord", "overlord"   # keep a Said's other voices
    if INSTRUCTOR is not None and INSTRUCTOR.wanted(text):   # "Instructor, ...": only the instructor answers
        return INSTRUCTOR.handle(text), "instructor", "instructor"
    page = kb_pages.match(text, bare_scroll=state_store.profile() != "apache") if KNEEBOARD_ON else None
    if page is not None:                             # "show the briefing" / "scroll down": the page answers
        print(f"[KNEEBOARD] {page}")
        if page["kind"] == "ack":                    # "kneeboard, acknowledge (all)": the Alerts tab
            n = ALERTS.ack_all() if page["all"] else int(ALERTS.ack_newest())
            print(f"[ALERTS] Acknowledged {n}.")
        elif page["kind"] == "track":                # "kneeboard, track on / off": this session only
            TRACK.visible = page["on"]
        else:
            KB_PAGES.apply(page)
        return "", "george", "kneeboard"
    said = tactical_answer(text)                     # "tactical picture", "what's changed" ...: the crew (T6)
    if said is not None:
        return said, "george", "tactical"
    m = _JTAC_WAKE.match(text or "")                 # "JTAC, request target": Darkside (else Overlord)
    if m and JTAC.wanted(m.group(1)):
        said = DARKSIDE.handle(m.group(1)) if DARKSIDE is not None else JTAC.handle(m.group(1))
        if said is not None:
            return (said, "darkside", "darkside") if DARKSIDE is not None else (said, "overlord", "overlord")
    said = wingmen.handle(text, comms, state_store.snapshot(), _speak_pilot)
    if said is not None:
        return said, "george", "wingmen"
    # 28 Oct: a bare "next target" / "BDA" / "mark target" after a JTAC talk-on. Jets only, or while a
    # JTAC target is up (never the Apache: there "next target" is George's)
    if (state_store.profile() != "apache" and JTAC.wanted(text)
            and (aircraft_mod.profile(state_store.snapshot()).kind == "jet" or JTAC.active())):
        said = DARKSIDE.handle(text) if DARKSIDE is not None else JTAC.handle(text)
        if said is not None:
            return (said, "darkside", "darkside") if DARKSIDE is not None else (said, "overlord", "overlord")
    if said is None:
        said = CARRIER.handle(text)         # Supercarrier calls (jets, a ship in the mission)
    if said is None:
        said = comms_menu.handle(comms, text)
    if said is not None:                    # the radio menu / carrier calls: the crew voice everywhere
        return said, "george", "radio"
    if said is None:                # "debrief" / "mission summary"
        said = DEBRIEF.handle(text)
    if said is None:                # "nearest friendly field" / "nearest FARP" / "divert"
        said = AIRFIELDS.handle(text, state_store.snapshot())
    if said is None:                # "mark our position as waypoint Alpha" / "where is Bravo?"
        said = USER_WAYPOINTS.handle(text, state_store.snapshot())
    if said is None:                # "what's our grid?" / "where are we?" / "say bullseye"
        said = position.handle(text, state_store.snapshot(), _bullseye)
    if (said is None and FUEL_WATCH.wanted(text)        # jets: "say bingo" / "what's my joker"
            and aircraft_mod.profile(state_store.snapshot()).fuel_calls):
        said = FUEL_WATCH.answer(state_store.snapshot())
    if said is None:
        snap = state_store.snapshot()
        huey_now = state_store.profile() == "huey"
        prof = aircraft_mod.profile(snap)
        if prof.kind == "jet":      # jets: DCS fuel, the type's burn and reserve
            from core.fuel import current_lbs
            said = nav_commands.handle(text, snap, current_lbs(snap), prof.burn_lbs_hr, prof.reserve_lbs)
        else:
            fuel = (snap.get("huey") or {}).get("fuel_lbs") if huey_now else None
            said = nav_commands.handle(text, snap, fuel, config.HUEY_FUEL_BURN_LBS_HR,
                                       config.HUEY_FUEL_RESERVE_LBS)
        if said is None and state_store.profile() == "apache":
            said = apache_crew.handle(text)
    if said is None:
        return None
    return said, "george", "crew"


def _speak_as(said: str, speaker: str) -> None:
    for text, who in getattr(said, "pre", ()):       # another voice first ("Overlord, contact Darkside.")
        _speak_as(text, who)
    _speak_one(said, speaker)
    for text, who in getattr(said, "then", ()):      # ... or after (Warlord's denial, Overlord's launch)
        _speak_as(text, who)


def _speak_one(said: str, speaker: str) -> None:
    if not said:                    # '' = done and nothing to say (wingman orders that went through)
        return
    print(f"[{ {'overlord': 'OVERLORD', 'instructor': 'INSTRUCTOR', 'warlord': 'WARLORD',
               'darkside': 'DARKSIDE'}.get(speaker, 'CREW') }]: '{said}'")
    {"overlord": _speak_overlord, "instructor": _speak_instructor, "warlord": _speak_warlord,
     "darkside": _speak_darkside}.get(speaker, _speak_pilot)(said)


def handle_shared(text: str, deferred: list = None) -> bool:
    """shared_reply, spoken. True if the utterance was handled (and answered).
    deferred: when given, a "say again" from Overlord or a wingman is not spoken but put in
    this list as a function that speaks it, and False is returned, so the model can try to
    work out what was meant (intelligence/router.py) before the pilot hears "say again"."""
    got = shared_reply(text)
    if got is None:
        return False
    said, speaker, source = got
    if (deferred is not None and ROUTER_ON and source in ("overlord", "wingmen")
            and router_mod.MISSED.search(said or "")):
        print(f"[ROUTER] {source} said '{said}': asking the model first.")
        deferred.append(lambda: _speak_as(said, speaker))
        return False
    _speak_as(said, speaker)
    return True


# --- ROUTER (intelligence/router.py): the model rewrites orders no fixed phrase caught ---
ROUTER_ON = getattr(config, "ROUTER_ENABLED", True)


def _router_shared(text: str):
    got = shared_reply(text)
    return None if got is None else got[:2]


def _understands(command: str):
    """False when we know no handler would take a rewritten order (checked before "Confirm?")."""
    from common import independents
    oc = independents.parse_outcast(command)
    if oc is not None:
        return independents.understands(oc[1])
    return None


ROUTER = router_mod.Router(_router_shared, _speak_as, understands=_understands)


def routing_state() -> str:
    """Who can be given orders right now, for the model's STATE line."""
    ind = wingmen.INDEPENDENTS
    snap = state_store.snapshot()
    ro = wingmen.roster(snap) or {}
    say = {2: "two", 3: "three", 4: "four"}
    wing = [say.get(w["n"], str(w["n"])) for w in ro.get("wingmen") or []
            if w.get("ai") and w.get("alive") is not False and not (ind and ind.known(w["n"]))]
    up = bool(wingmen.EXECUTOR and wingmen.EXECUTOR.oly.connected)
    return router_mod.routing_state(ind.numbers() if ind else [], wing, USER_WAYPOINTS.names(), up)


def stt_prompt() -> str:
    """Whisper's prompt for this aircraft, plus the callsigns and waypoint names in use now."""
    if state_store.profile() == "huey":
        base = config.HUEY_STT_PROMPT
    elif aircraft_mod.profile(state_store.snapshot()).kind == "jet":     # carrier calls, bingo, Overlord
        base = getattr(config, "JET_STT_PROMPT", STT_PROMPT)
    else:
        base = STT_PROMPT
    if INSTRUCTOR is not None and state_store.profile() != "other":   # helicopters: the hover exercise (decision 5)
        base += " Instructor: hover, approach, fuel, bingo, begin, stop, easier, harder."   # short: Whisper keeps the last 224 tokens
    if COMMAND_ROLES_ON:
        base += " Warlord, Darkside, radio check, override, abort, continue, cleared hot."
    ind = wingmen.INDEPENDENTS
    return base + router_mod.stt_hints(ind.numbers() if ind else [], USER_WAYPOINTS.names())


# --- VR KNEEBOARD (kneeboard/): the map and status pages read the state here, never the other way ---
class _KneeboardContext:
    """What the map needs beyond the snapshot. Called on the kneeboard's HTTP thread: only reads
    locked registries and the snapshot."""
    @staticmethod
    def xz_to_ll(x: float, z: float):
        from common import geo
        return geo.FIT.to_ll(x, z)

    @staticmethod
    def is_air_defence(type_name: str) -> bool:
        return bool(UNIT_TYPES.is_air_defence(type_name))

    @staticmethod
    def imperial(snap: dict) -> bool:
        return aircraft_mod.profile(snap).imperial

    @staticmethod
    def named_waypoints(snap: dict):
        out = []
        for name in USER_WAYPOINTS.names():
            ll = USER_WAYPOINTS.ll(name, snap)
            if ll:
                out.append({"name": name, "lat": round(ll[0], 6), "lon": round(ll[1], 6)})
        return out

    @staticmethod
    def outcasts(snap: dict):
        ind = wingmen.INDEPENDENTS
        out = []
        for n in (ind.numbers() if ind else []):
            u = ind._unit(snap, n)
            if u:
                out.append({"n": n, "lat": round(float(u[3]), 6), "lon": round(float(u[4]), 6)})
        return out

    @staticmethod
    def flights(snap: dict):
        if OVERLORD is None:
            return []
        out = []
        for m in OVERLORD.active():
            pts = [[round(float(u[3]), 6), round(float(u[4]), 6)] for u in OVERLORD._flight(snap, m) or []]
            if pts:
                out.append({"cs": m.cs, "kind": m.kind.key, "state": m.state, "pts": pts})
        return out

    @staticmethod
    def jtac(snap: dict):
        t = JTAC.current
        return {"type": t["type"], "lat": t["lat"], "lon": t["lon"]} if t else None

    @staticmethod
    def picture():
        """The tactical picture (tactical/, step T5) while its service keeps it current, else None:
        the map then falls back to Export.lua's lists as before."""
        if TACTICAL is None or TACTICAL.last_tick_mono is None:
            return None
        if time.monotonic() - TACTICAL.last_tick_mono > PICTURE_MAX_AGE_S:
            return None
        return TACTICAL.picture()

    @staticmethod
    def contact():
        """The track whose details the map shows ("kneeboard, contact 42"), or None."""
        return KB_PAGES.contact()


_KB_CTX = _KneeboardContext()


def kneeboard_map() -> dict:
    from kneeboard.model import build_map
    return build_map(state_store.snapshot(), _KB_CTX)


KB_PAGES = kb_pages.PageControl()               # voice page / scroll requests (kneeboard/pages.py)
# A-10C / Hornet checklists, kneeboard only: they follow DCS-BIOS (addresses from DCS-BIOS's own JSON)
JET_CHECKLISTS = JetChecklists(state_store.bios_words_snapshot,
                               lambda: state_store.snapshot()["bios_age"] <= TELEMETRY_STALE_SEC,
                               json_dir=getattr(config, "DCS_BIOS_JSON_DIR", None))
from core.state import watch_raw as _watch_raw
_watch_raw(JET_CHECKLISTS.addresses())
KNEEBOARD_ON = False                            # set when the server starts

# Alerts tab (kneeboard/alerts.py) and your track on the map (kneeboard/track.py), agreed 30 Oct.
from kneeboard.alerts import ALERTS, LinkWatch
from kneeboard.track import Track
TRACK = Track(state_store.snapshot, visible=getattr(config, "KNEEBOARD_TRACK", True))
_LINKS = LinkWatch(ALERTS)


def _quiet_text(text: str) -> str:
    """'Overlord, bingo fuel, ...' -> 'Bingo fuel, ...' for the Alerts tab."""
    t = re.sub(r"^\s*overlord,\s*", "", text or "", flags=re.I).strip()
    return t[:1].upper() + t[1:]


def _alert_fuel(text: str) -> None:
    t = (text or "").lower()
    stage = "bingo" if "bingo fuel" in t else "joker" if "joker fuel" in t else None
    if stage:
        ALERTS.raise_("fuel", "critical" if stage == "bingo" else "warning", _quiet_text(text),
                      resolved=lambda: not FUEL_WATCH.stage_active(stage))


def _no_close_bandits() -> bool:
    snap = state_store.snapshot()
    me = snap.get("my_ll")
    if not me or enemy_lists_stale(snap):
        return False                                 # can't tell: keep it
    close = aircraft_mod.profile(snap).air_close_m or overlord_mod.AirWarning.CLOSE_M
    return not overlord_mod.bandits_near(snap, (me[0], me[1]), close * 1.5)


def _alert_air(text: str) -> None:
    t = (text or "").lower()
    if "bandit" in t and "closing" in t:
        ALERTS.raise_("bandit", "critical", _quiet_text(text), resolved=_no_close_bandits)
    elif "pop-up group" in t:
        ALERTS.raise_("popup", "info", _quiet_text(text))


_ANNUNCIATOR = None                              # set in main() (the Apache's threat / altitude calls)


def _alert_annunciator(text: str) -> None:
    t = (text or "").lower()
    a = _ANNUNCIATOR
    if t.startswith("warning,") and "detected" in t:
        ALERTS.raise_("airdefence", "critical", text,
                      resolved=lambda: a is not None and not a.active("priority_air_defense"))
    elif t.startswith("altitude low"):
        ALERTS.raise_("lowalt", "warning", text, resolved=lambda: a is not None and not a.active("low_alt"))


def _alert_eufd(kind: str, text: str) -> None:
    from apache.eufd import speakable
    ALERTS.raise_(f"eufd:{kind}:{text}", "critical" if kind == "warning" else "warning",
                  f"{'Warning' if kind == 'warning' else 'Caution'}: {speakable(text)}",
                  resolved=lambda: not apache_crew.monitor.showing(kind, text))


def _alert_training(names) -> None:
    ALERTS.raise_("training", "info", "Training target destroyed: " + ", ".join(names))


def alerts_watch() -> None:
    """Once a second: alerts whose cause is gone clear; lost / restored links (Status page) alert."""
    while True:
        time.sleep(1.0)
        try:
            ALERTS.tick()
            _LINKS.update(kneeboard_status()["links"])
        except Exception as e:
            print(f"[ALERTS] {type(e).__name__}: {e}")


def _checklists_now():
    """(runner, lists, aircraft) of the crew in this aircraft; no runner without a crew."""
    p = state_store.profile()
    if p == "apache":
        return apache_crew.checklist, APACHE_CHECKLISTS, "AH-64D"
    if p == "huey":
        return huey.checklist, HUEY_CHECKLISTS, "UH-1H"
    return None, None, state_store.snapshot().get("aircraft") or "this aircraft"


def kneeboard_checklist() -> dict:
    if state_store.profile() == "other":            # jets: the page follows DCS-BIOS, no voice
        return JET_CHECKLISTS.view(state_store.snapshot().get("aircraft"), KB_PAGES.chosen_checklist(jet=True))
    runner, lists, ac = _checklists_now()
    if runner is not None and runner.active:     # a run replaces "show the ... checklist"
        KB_PAGES.clear_checklist()
    return kb_pages.build_checklist(runner, lists, ac, KB_PAGES.chosen_checklist(),
                                    getattr(config, "KNEEBOARD_AUTO_CHECKLIST", True))


def kneeboard_briefing() -> dict:
    d = kb_pages.build_briefing(KB, kb_aircraft())
    sec = INSTRUCTOR.briefing_section() if INSTRUCTOR is not None else None
    if sec:                                     # the selected instructor exercise, first on the page
        d["sections"] = [sec] + list(d.get("sections") or [])
    return d


def kneeboard_instructor() -> dict:
    """The kneeboard Training tab (1 Nov): the instructor's view plus the training targets."""
    d = INSTRUCTOR.view() if INSTRUCTOR is not None else {"state": "off"}
    d["training"] = TRAINING.view()
    return d


def _latency_rows() -> list:
    """Status page: median / worst over the last utterances (core/metrics.py), and the counts."""
    s = METRICS.summary()
    rows = []
    for key, name in (("to_audio", "Release to first word"), ("stt", "Speech recognition (measured)"),
                      ("model", "Model calls"), ("handler", "Handling"), ("synth", "Voice synthesis")):
        v = s.get(key)
        if v:
            rows.append({"name": name, "value": f"median {v['median']:.2f} s, worst {v['max']:.2f} s "
                                                 f"(last {v['n']})"})
    counts = {k: n for k, n in (s.get("counts") or {}).items() if n}
    if counts:
        rows.append({"name": "Dropped / failed", "value": ", ".join(f"{k.replace('_', ' ')} {n}"
                                                                     for k, n in sorted(counts.items()))})
    return rows


def _enemy_picture_link(snap: dict) -> dict:
    """Status page row: how old Export.lua's enemy / air-defence / Olympus lists are (audit step 3)."""
    from core.state import ENEMY_LIST_STALE_SEC
    age = snap.get("enemy_lists_age")
    if age is None:
        return {"name": "Enemy picture (Export.lua scan)", "ok": None,
                "detail": "age unknown: copy the new Export.lua to Saved Games\\DCS\\Scripts"}
    if age == float("inf"):
        return {"name": "Enemy picture (Export.lua scan)", "ok": False, "detail": "no world scan yet"}
    ok = age <= ENEMY_LIST_STALE_SEC
    return {"name": "Enemy picture (Export.lua scan)", "ok": ok,
            "detail": "fresh" if ok else f"{int(age)} s old: no kill calls or new tasking until it updates"}


def order_to_comms_log(r) -> None:
    """An order's outcome (common/orders.py) as a kneeboard comms log line of kind "order"."""
    COMMS_LOG.add("order", r.text())


def _olympus_link(ex) -> dict:
    """Status page row for Olympus: up or why not, how long ago, failure counts (audit step 2)."""
    if ex is None:
        return {"name": "DCS Olympus", "ok": None, "detail": "off in config"}
    st = ex.oly.status() if hasattr(ex.oly, "status") else {"connected": ex.oly.connected}
    ago = lambda a: f"{a:.0f} s ago" if a < 120 else f"{a / 60:.0f} min ago"
    parts = ["connected" if st["connected"] else "not connected"]
    err, err_age, ok_age = st.get("last_error"), st.get("error_age"), st.get("ok_age")
    if err and err_age is not None and (not st["connected"] or ok_age is None or err_age < ok_age):
        parts.append(f"last error {ago(err_age)}: {err}")       # newer than the last success
    elif st["connected"] and ok_age is not None:
        parts.append(f"last answer {ago(ok_age)}")
    fails = st.get("fails") or {}
    if fails:
        parts.append("failures: " + ", ".join(f"{k} {n}" for k, n in sorted(fails.items())))
    return {"name": "DCS Olympus", "ok": st["connected"], "detail": "; ".join(parts)}


def kneeboard_status() -> dict:
    """The status page: which links are up, and the last timings."""
    snap = state_store.snapshot()
    age = lambda a: f"{a:.1f} s ago" if a != float("inf") and a < 1e6 else "never"
    links = [
        {"name": "DCS telemetry (Export.lua)", "ok": snap["telemetry_age"] <= TELEMETRY_STALE_SEC,
         "detail": f"{snap.get('aircraft') or 'no aircraft'}, last {age(snap['telemetry_age'])}"},
        {"name": "DCS-BIOS", "ok": snap["bios_age"] <= TELEMETRY_STALE_SEC if snap.get("bios_age") is not None else None,
         "detail": f"last {age(snap.get('bios_age', float('inf')))}"},
        _enemy_picture_link(snap),
        *([TACTICAL.status_link()] if TACTICAL is not None else []),
        {"name": "Comms hook", "ok": comms.live(), "detail": "radio menu, mission, fuel" if comms.live() else "not heard"},
        {"name": "DCS fuel (hook)", "ok": snap.get("dcs_fuel_age", float("inf")) <= 15.0,
         "detail": f"last {age(snap.get('dcs_fuel_age', float('inf')))}"},
    ]
    links.append(_olympus_link(wingmen.EXECUTOR))
    links.append(PERMISSIONS.link())
    links.append(roles_mod.callsign_link(snap, OPERATOR_CALLSIGN))
    links.append(TRAINING.link())
    if INSTRUCTOR is not None:
        links.append(INSTRUCTOR.link())
    last = ollama_chat.last_call()
    links.append({"name": f"Ollama ({config.LLM_MODEL})", "ok": last.get("ok"),
                  "detail": (f"last call {last['secs']:.2f} s, {age(time.time() - last['when'])}"
                             + (f" ({last['error']})" if last.get("error") else "")) if last else "not used yet"})
    voices = getattr(tts_mod, "VOICES", {}) or {}
    fallback = getattr(tts_mod, "FALLBACK", {}) or {}            # a role without its file uses another voice
    links.append({"name": "Voices (Piper)",
                  "ok": bool(voices) and all(v is not None for k, v in voices.items() if k not in fallback),
                  "detail": ", ".join(f"{k} {'ok' if v is not None else ('missing, uses ' + fallback[k] + chr(39) + 's voice' if k in fallback else 'missing')}"
                                      for k, v in voices.items())})
    with _stats_lock:
        stt = _STATS.get("stt")
    times = [{"name": "Profile", "value": state_store.profile()},
             *_latency_rows(),
             {"name": "Speech recognition", "value": f"{stt[0]:.2f} s ({WHISPER_MODEL})" if stt else f"not used yet ({WHISPER_MODEL})"},
             {"name": "Carrier quiet window", "value": "on" if QUIET.active() else "off"}]
    return {"links": links, "times": times}


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

def fuel_report(snap: dict, quiet: bool = False) -> str:
    """'Fuel 2490 pounds.' Straight from DCS when the comms hook reports it (Unit:getFuel() x the
    airframe's fuelMassMax: right for any aircraft and any tanks), else from LoGetEngineInfo
    (core/fuel.py has the units per aircraft). The raw values are printed to check against the
    cockpit."""
    from core.fuel import from_dcs, say_dcs_fuel
    got = from_dcs(snap)
    if got is not None:
        words = say_dcs_fuel(*got)
        if not quiet:
            frac, max_kg, unit = snap["dcs_fuel"][:3]
            print(f"[FUEL] DCS: {frac:.4f} of {max_kg:.0f} kg max internal ({unit}) -> '{words}'")
        DEBRIEF.fuel(words)
        return words
    raw = snap.get("fuel_raw")
    if not raw or snap["telemetry_age"] > TELEMETRY_STALE_SEC:
        return "No fuel data."
    internal, external = raw
    from core.fuel import say_fuel, known_max_kg
    words = say_fuel(snap.get("aircraft") or "", internal, external, known_max_kg(snap))
    if not quiet:
        why = "" if snap.get("dcs_fuel") is None else f" (hook's reading {snap.get('dcs_fuel_age', 0):.0f}s old or not yours)"
        print(f"[FUEL] from LoGetEngineInfo{why}: internal {internal:.4f}, external {external:.4f} -> '{words}'")
    DEBRIEF.fuel(words)
    return words


def build_status_report(snap: dict) -> str:
    parts = []
    if snap["telemetry_age"] > TELEMETRY_STALE_SEC:
        parts.append("No flight data.")
    else:
        parts.append(f"Altitude {snap['alt_ft']:.0f} feet, airspeed {snap['speed_kts']:.0f} knots.")
        if snap.get("fuel_raw") or snap.get("dcs_fuel"):
            parts.append(fuel_report(snap))
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


_OTHER_SAID = False         # voice-worker thread only
OTHER_AIRCRAFT_SAY = ("Say again. In this aircraft I can do navigation, grid, briefing, the radio menu, "
                      "wingmen and Overlord.")


def _route_other(text: str, deferred: list) -> None:
    """No crew in this aircraft: only the router's model call can work out what was meant."""
    if ROUTER_ON and not comms.blocks_llm():
        try:
            res = chat_json(router_mod.ROUTER_SYSTEM, [], f"STATE: {routing_state()}\nPILOT: {text}")
            if str(res.get("action") or "").upper() == "ROUTE" and ROUTER.dispatch(text, str(res.get("command") or "")):
                return
        except Exception as e:
            print(f"[ERROR] Router request failed: {type(e).__name__}: {e}")
    if deferred:
        deferred[0]()
        return
    global _OTHER_SAID                               # the long version once, then just "say again"
    said = as_overlord("Say again." if _OTHER_SAID else OTHER_AIRCRAFT_SAY)
    _OTHER_SAID = True
    _speak_as(said, "overlord")                      # no crew in a jet: Overlord answers (Andre, 1 Nov)


def handle_command(text: str, captured: float = None) -> None:
    if ROUTER_ON:
        if ROUTER.answer_pending(text):              # "affirm" to "Confirm, Outcast 2, ...?"
            return
        if state_store.profile() != "huey" and router_mod.bare_answer(text):
            print("[ROUTER] Affirm / negative with nothing to confirm.")   # not a George order
            speak_george_radio("Nothing to confirm.", priority=PRIORITY_PILOT)
            return
        text = ROUTER.apply_learned(text)             # what earlier rewrites taught us
    if ROUTER_ON:                                     # who he's talking to, for follow-ups
        from common import independents as _ind
        oc = _ind.parse_outcast(text)
        if oc is not None:
            ROUTER.note_addressee(f"outcast {oc[0]}")
        elif overlord_mod.addressed(text) is not None or _JTAC_WAKE.match(text or ""):
            ROUTER.note_addressee("overlord")
    deferred = []                                     # Overlord's / a wingman's "say again"
    t_shared = time.perf_counter()
    taken = handle_shared(text, deferred)
    METRICS.stage(METRICS.current(), "shared", time.perf_counter() - t_shared)
    if taken:
        return
    if state_store.profile() == "other":            # not the Apache or Huey: no George, no crew
        if not deferred and (briefing.is_briefing_question(text)       # or "how do I employ the Maverick?"
                             or (briefing.is_manual_question(text) and KB.has_manual(kb_aircraft()))):
            said = as_overlord(briefing_answer(text, "george"))
            _speak_as(said, "overlord")             # no crew in a jet: Overlord answers (Andre, 1 Nov)
            return
        _route_other(text, deferred)
        return
    if state_store.profile() == "huey":
        huey.handle(text, captured, deferred=deferred[0] if deferred else None)
        return
    t0 = time.perf_counter()

    # Fast path: simple unambiguous commands skip the LLM entirely (milliseconds, not seconds).
    # match_fast returns None whenever it is unsure, and the LLM handles those. An order
    # Overlord or a wingman didn't understand goes straight to the model (for ROUTE).
    snap = state_store.snapshot()
    res = None if deferred else match_fast(text, snap)
    source = "FAST"
    if res is None and not deferred and briefing.is_briefing_question(text):    # "what's our package priority?"
        said = briefing_answer(text, "george")
        print(f"[BRIEFING]: '{said}'")
        speak_george_radio(said, priority=PRIORITY_PILOT)
        return
    if res is None and comms.blocks_llm():
        if deferred:
            deferred[0]()
        else:
            speak_george_radio("Say again, which F key?", priority=PRIORITY_PILOT)
        return
    if res is None:
        source = "LLM"
        try:
            res, snap = query_qwen(text, george.describe(),
                                   extra_state=routing_state() if ROUTER_ON else "")
        except Exception as e:
            print(f"[ERROR] LLM request failed: {type(e).__name__}: {e}")
            if deferred:
                deferred[0]()
            else:
                speak_george_radio("Say again, system fault.", priority=PRIORITY_PILOT)
            return
        if str(res.get("action") or "").upper() == "ROUTE":     # an order for someone else
            if not (ROUTER_ON and ROUTER.dispatch(text, str(res.get("command") or ""))):
                deferred[0]() if deferred else speak_george_radio("Say again.", priority=PRIORITY_PILOT)
            print(f"[TIMING] ROUTE {time.perf_counter() - t0:.2f}s")
            return
        if deferred:                                  # meant for Overlord / a wingman, not George
            deferred[0]()
            return
    t_llm = time.perf_counter() - t0

    # Validate against the SAME snapshot the LLM was shown.
    # George's list may continue below the entries seen so far; then allow higher numbers and
    # let the driver scroll down and check (it refuses past the real end of the list).
    known = len(snap["targets"])
    limit = known if snap.get("george_list_complete") or known == 0 else max(known, 30)
    action, target_idx, speech, setting, value, targets = sanitize_result(
        res, limit, text if source == "LLM" else None)

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

    if action == "STATUS" and re.search(r"\bfuel\b", text.lower()):
        speak_george_radio(fuel_report(state_store.snapshot()), priority=PRIORITY_PILOT)
    elif action == "STATUS":
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
session = 0            # lets a quick re-press cancel the previous release's trailing capture
_ptt_sources = set()   # PTT inputs currently held ("keyboard", "joystick"); guarded by rec_lock
                       # (also filters Windows key auto-repeat: a held key is already in the set)

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

def ptt_press(source: str) -> None:
    """A PTT input went down. The first one held starts a transmission; a second one held at
    the same time (SPACE and the stick button) just joins it. Safe from any thread."""
    global is_recording, session, audio_frames
    with rec_lock:
        if source in _ptt_sources:
            return
        first = not _ptt_sources
        _ptt_sources.add(source)
        if not first:
            return
        session += 1
        audio_frames = []
        is_recording = True
        ptt_active.set()          # inside the lock: ordered against the finalizer's clear()
    print(f"\n[PTT] Listening... ({source})")


def ptt_release(source: str) -> None:
    """A PTT input came up. The transmission ends when the LAST held input is released."""
    with rec_lock:
        if source not in _ptt_sources:
            return
        _ptt_sources.discard(source)
        if _ptt_sources:
            return
        my_session = session
    print("[PTT] Analyzing input...")
    threading.Thread(target=_finalize_recording, args=(my_session,), daemon=True).start()


_KEYBOARD_PTT = PTT_MODE in ("keyboard", "both")


def on_press(key):
    _track_wheel_toggle(key, True)
    if _KEYBOARD_PTT and key == PTT_KEY:
        ptt_press("keyboard")

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
    _track_wheel_toggle(key, False)
    if _KEYBOARD_PTT and key == PTT_KEY:
        ptt_release("keyboard")


def start_joystick_ptt():
    """Starts the stick-button PTT thread if PTT_MODE asks for it. In "joystick" mode, falls
    back to SPACE when no joystick can be read at all, so you're never left without PTT."""
    global _KEYBOARD_PTT
    if PTT_MODE not in ("joystick", "both"):
        return None
    joy = JoystickPTT(lambda: ptt_press("joystick"), lambda: ptt_release("joystick"),
                      name_contains=JOY_NAME_CONTAINS, index=JOY_DEVICE_INDEX, button=JOY_BUTTON,
                      backend=JOY_BACKEND).start()
    if PTT_MODE == "joystick":
        wait_until = time.monotonic() + 2.0
        while joy.ok is None and time.monotonic() < wait_until:
            time.sleep(0.05)
        if joy.ok is False and not joy.device:
            _KEYBOARD_PTT = True
            print("[PTT] Joystick PTT not available yet - SPACE works as push-to-talk meanwhile.")
    return joy


# --- VOICE WORKER: STT -> LLM -> ACTION -> SPEECH ---

# Whisper tends to invent these on near-silence.
_HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thanks"}
# Whole utterances Whisper makes of a short word (heard in flight; "fuel" said on its own).
_SOUNDALIKES = {"you will": "fuel", "see you well": "fuel", "a few will": "fuel", "few will": "fuel",
                "the fuel": "fuel", "feel": "fuel", "fuel fuel": "fuel",
                # 22 Oct
                "of you will": "fuel", "for your well": "fuel", "for you will": "fuel", "your will": "fuel",
                "check crew will": "check fuel", "check few will": "check fuel"}

def _repeating(text: str, limit: int = 6) -> bool:
    """Whisper's loop: one word (or two) said over and over."""
    words = re.findall(r"[a-z']+", text.lower())
    run = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        if run >= limit:
            return True
    return len(words) >= 2 * limit and len(set(words)) <= 2


_stats_lock = threading.Lock()
_STATS = {}                                     # last STT time (kneeboard status page)


def _note_stt(secs: float) -> None:
    with _stats_lock:
        _STATS["stt"] = (secs, time.time())


def voice_worker(stt_model: WhisperModel) -> None:
    while True:
        captured, audio = utterances.get()
        age = time.monotonic() - captured
        if age > UTTERANCE_MAX_AGE_SEC:
            print(f"[STT] Dropped stale utterance ({age:.1f}s old).")
            METRICS.count("utterances_dropped_stale")
            continue
        uid = METRICS.start(captured)
        try:
            t0 = time.perf_counter()
            prompt = stt_prompt()           # + "Outcast 2, waypoint Alpha" while they exist
            segments, _ = stt_model.transcribe(
                audio, beam_size=1, vad_filter=True,
                initial_prompt=prompt, condition_on_previous_text=False,
            )
            text = " ".join(s.text for s in segments).strip()
            t_stt = time.perf_counter() - t0

            METRICS.stage(uid, "stt", t_stt)
            if not text or text.lower().strip(" .!?,") in _HALLUCINATIONS:
                print("[STT] No speech detected.")
                METRICS.count("stt_no_speech")
                METRICS.forget(uid)
                continue
            if _repeating(text):            # 28 Oct: "Map, map, map, map, ..." from a long press
                print(f"[STT] Ignored a repeating transcript: '{text[:60]}...'")
                METRICS.count("stt_repeating")
                METRICS.forget(uid)
                continue

            print(f"[PILOT]: '{text}'  (STT {t_stt:.2f}s for {len(audio) / SAMPLE_RATE:.1f}s audio, {WHISPER_MODEL})")
            COMMS_LOG.add("pilot", text)
            _note_stt(t_stt)
            fixed = _SOUNDALIKES.get(re.sub(r"[^a-z ]", "", text.lower()).strip())
            if fixed:
                print(f"[STT] Taking '{text}' as '{fixed}'.")
                text = fixed
            METRICS.text(uid, text)
            if lanes.is_safety_order(text):        # "cease fire" etc.: now, not behind the queue
                if run_urgent(text, captured, uid):
                    continue
            commands.put((captured, text, uid))    # everything else: the command worker, in order
        except Exception as e:
            print(f"[WORKER ERROR] {type(e).__name__}: {e}")   # one bad utterance never kills the pipeline
            METRICS.count("worker_errors")
            METRICS.forget(uid)


# --- Urgent lane (core/lanes.py, audit step 5) ---
# The voice thread above only transcribes. Commands run on the command worker, one at a time, in the
# order spoken. A safety order runs at once on the voice thread (deterministic handlers only) and voids
# everything spoken before it that isn't done: a model answer in progress, commands still queued.
commands: "queue.Queue[tuple]" = queue.Queue()


def _dropped_by_safety(text: str) -> None:
    """Decision 4: logged and on the comms log, never spoken."""
    print(f"[URGENT] Dropped '{text}': a safety order came after it.")
    METRICS.count("dropped_by_safety_order")
    try:
        COMMS_LOG.add("order", f"Dropped after a safety order: '{text}'")
    except Exception as e:
        print(f"[URGENT] comms log: {type(e).__name__}: {e}")


def run_urgent(text: str, captured: float, uid) -> bool:
    """Runs a safety order now. False when it needs the model: then it goes to the normal queue
    (it has still voided everything older)."""
    LANES.note_safety(captured)
    print(f"[URGENT] Safety order: '{text}'")
    t_handle = time.perf_counter()
    try:
        with LANES.handler.held(), LANES.working(captured, urgent=True), METRICS.bound(uid):
            handle_command(text, captured)
    except lanes.NeedsModel:
        print(f"[URGENT] No fixed handler for '{text}': queued for the model.")
        return False
    METRICS.handled(uid, time.perf_counter() - t_handle)
    return True


def _run_command(captured: float, text: str, uid) -> None:
    t_handle = time.perf_counter()
    try:
        with LANES.handler.held(), LANES.working(captured), METRICS.bound(uid):
            handle_command(text, captured)
    except lanes.Superseded:
        _dropped_by_safety(text)
    finally:
        METRICS.handled(uid, time.perf_counter() - t_handle)


def command_worker() -> None:
    while True:
        captured, text, uid = commands.get()
        try:
            age = time.monotonic() - captured
            if age > UTTERANCE_MAX_AGE_SEC:          # waited too long behind other commands
                print(f"[STT] Dropped stale command '{text}' ({age:.1f}s old).")
                METRICS.count("utterances_dropped_stale")
                METRICS.forget(uid)
                continue
            if LANES.superseded(captured):           # spoken before a safety order: void
                _dropped_by_safety(text)
                METRICS.forget(uid)
                continue
            _run_command(captured, text, uid)
        except Exception as e:
            print(f"[WORKER ERROR] {type(e).__name__}: {e}")
            METRICS.count("worker_errors")
            METRICS.forget(uid)


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
    if QUIET.hold(text, _speak_alert_now):
        return
    _speak_alert_now(text)


def _speak_alert_now(text: str) -> None:
    # Alerts go ahead of routine speech but are dropped if they sit queued for > 5 s.
    speak_george_radio(text, priority=PRIORITY_ALERT, max_age=5.0)


def watch_profile() -> None:
    """Logs which aircraft profile is in use whenever it changes, and pre-loads that crew's LLM
    prompt so the first free-form command isn't slow (Ollama caches one prompt at a time)."""
    last, fuel_t = None, 0.0
    while True:
        if time.monotonic() - fuel_t > 60.0:          # fuel for the debrief (start and latest)
            fuel_t = time.monotonic()
            snap = state_store.snapshot()
            if (snap.get("fuel_raw") or snap.get("dcs_fuel")) and snap["telemetry_age"] <= TELEMETRY_STALE_SEC:
                fuel_report(snap, quiet=True)
        p = state_store.profile()
        if p != last:
            if last is not None or p == "huey":
                print("[PROFILE] " + {"huey": "UH-1H: Huey crew (gunners, crew chief, checklists)",
                                      "apache": "AH-64D: George CPG",
                                      "other": f"{state_store.snapshot().get('aircraft') or 'other aircraft'}: "
                                               "no crew; navigation, grid, briefing, radio menu, wingmen "
                                               "and Overlord only"}[p] + " active.")
            prompt, label = _active_prompt()
            threading.Thread(target=prime_prompt, args=(prompt, label), daemon=True).start()
            last = p
        QUIET.tick(state_store.snapshot())          # carrier recovery over (on deck)?
        try:
            JTAC.tick()                             # "Overlord, good effect, ... destroyed."
        except Exception as e:
            print(f"[JTAC] {type(e).__name__}: {e}")
        time.sleep(1.0)


# --- MAIN ---
if __name__ == "__main__":
    start_console_log()             # mirror the console to logs/george_*.log
    print("\n=======================================================")
    print("    DCS GEORGE AI CREW MIDDLEWARE  (AH-64D / UH-1H)    ")
    print("=======================================================\n")

    lower_process_priority()

    print("[1/5] Starting Export.lua Telemetry Listener...")
    start_telemetry_thread()

    print("[2/5] Starting DCS-BIOS Multicast Listener & Command Sender...")
    start_dcs_bios_thread()
    start_george_ui_thread()      # George's wheel / Target List from george_ai_export.lua
    start_wpn_page_thread()       # your actioned weapon / sight from the WPN page (george_export.lua)
    start_huey_link_thread()      # UH-1H gauges, lamps, Crew Status panel (george_huey_export.lua)
    comms_menu.start(comms)       # radio menu text from Scripts/Hooks/george_comms_hook.lua
    threading.Thread(target=watch_profile, daemon=True, name="profile-watch").start()
    if TACTICAL is not None:
        TACTICAL.start()          # the tactical picture (tactical/): reads the state store 4x a second
    if INSTRUCTOR is not None:
        INSTRUCTOR.start()        # instructor observer: reads the state store 10x a second, sends nothing
        atexit.register(_instructor_fast, False)          # never leave Export.lua on 10 Hz after we quit
    if wingmen.EXECUTOR:
        threading.Thread(target=olympus_watch, daemon=True, name="olympus-watch").start()
    if getattr(config, "FUEL_CALLS", True):              # jets: "Overlord, joker fuel ..."
        FUEL_WATCH.start()
    if getattr(config, "OVERLORD_AIR_WARNINGS", True):   # "Overlord, pop-up group ..." on his own
        overlord_mod.AirWarning(state_store.snapshot,
                                lambda text: (_alert_air(text),
                                              _unprompted(text, lambda t: speak_overlord(t, priority=PRIORITY_ALERT,
                                                                                          max_age=10.0)))).start()

    print("[3/5] Loading Faster-Whisper Speech Recognition Model...")
    try:    # use the cached model without contacting huggingface.co (fully local)
        stt_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                 cpu_threads=WHISPER_CPU_THREADS, local_files_only=True)
    except Exception:   # first run on this PC: the model must be downloaded once
        print("[STT] Whisper model not cached yet - downloading it once...")
        stt_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                 cpu_threads=WHISPER_CPU_THREADS)

    print("[4/5] Starting radio speech queue & warming LLM...")
    tts_mod.ON_SPOKEN = lambda who, text: COMMS_LOG.add(who, text)     # kneeboard comms log
    ORDERS.on_result = order_to_comms_log                                 # order outcomes (audit step 6)
    start_speech_worker()
    if getattr(config, "KNEEBOARD_ENABLED", True):
        try:
            from kneeboard.server import Kneeboard
            Kneeboard(kneeboard_map, kneeboard_status, COMMS_LOG, port=getattr(config, "KNEEBOARD_PORT", 5000),
                      feeds={"briefing": (kneeboard_briefing, 1.0), "checklist": (kneeboard_checklist, 0.5),
                             "ui": (KB_PAGES.view, 0.2), "alerts": (ALERTS.view, 0.5),
                             "track": (TRACK.view, 2.0), "instructor": (kneeboard_instructor, 0.5)}).start()
            KNEEBOARD_ON = True
            TRACK.start()                                        # your track since mission start
            threading.Thread(target=alerts_watch, daemon=True, name="kb-alerts").start()
        except OSError as e:
            print(f"[KNEEBOARD] Not started: {e} (another program on port {getattr(config, 'KNEEBOARD_PORT', 5000)}? "
                  f"Change KNEEBOARD_PORT in config.py.)")
    threading.Thread(target=warmup_llm, daemon=True).start()

    print("[5/5] Launching Autonomous Flight Safety & Threat Annunciator...")
    annunciator = AutonomousAnnunciator(tts_callback_func=lambda text: (_alert_annunciator(text), speak_alert(text)))
    _ANNUNCIATOR = annunciator
    annunciator.start()
    engagement_monitor = EngagementMonitor(speak_alert)   # missile away / target destroyed callouts
    engagement_monitor.start()
    ENGAGEMENT = engagement_monitor                        # its last shot is "destroyed" evidence (tactical/)
    if config.APACHE_EUFD_CALLOUTS:
        apache_crew.monitor.on_new = _alert_eufd             # warnings / cautions on the Alerts tab too
        apache_crew.start_callouts(active=lambda: state_store.profile() == "apache")
    crew_chief = None
    if config.HUEY_CALLOUTS:
        crew_chief = CrewChief(speak_alert, state_store.snapshot,
                               active=lambda: state_store.profile() == "huey",
                               contact_range_m=config.HUEY_CONTACT_CALLOUT_M,
                               talkdown=config.HUEY_TALKDOWN,
                               fuel_burn_lbs_hr=config.HUEY_FUEL_BURN_LBS_HR,
                               reserve_lbs=config.HUEY_FUEL_RESERVE_LBS).start()
        huey.talkdown = crew_chief.talkdown
        if INSTRUCTOR is not None and crew_chief.talkdown is not None:
            crew_chief.talkdown.quiet = INSTRUCTOR.busy     # no hover coaching during a graded hover
    print("[WHEEL] Assuming the George wheel is HIDDEN. If it's open, press Ctrl+V once or say 'toggle wheel'.")
    print("[WHEEL] Assuming George starts on NO WEAPON, WEAPONS HOLD, TADS fixed forward "
          "(corrected automatically once the wheel is shown and george_ai_export.lua is running).")
    for warning in george.bindings_report():
        print(f"[WARN] {warning}")

    threading.Thread(target=command_worker, daemon=True, name="george-commands").start()
    threading.Thread(target=voice_worker, args=(stt_model,), daemon=True, name="george-voice").start()

    joystick_ptt = start_joystick_ptt()

    print("\n>>> ALL SYSTEMS ONLINE <<<")
    ptt_how = {"keyboard": "[SPACEBAR]", "joystick": f"joystick button {JOY_BUTTON}",
               "both": f"[SPACEBAR] or joystick button {JOY_BUTTON}"}.get(PTT_MODE, "[SPACEBAR]")
    print(f"Hold {ptt_how} to talk to George. Press Ctrl+C to exit.\n")

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, device=MIC_DEVICE,
                            callback=record_callback):
            with Listener(on_press=on_press, on_release=on_release) as listener:
                listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        annunciator.stop()
        TRAINING.shutdown()                          # remove training targets before going
        george.shutdown()
        DEBRIEF.write("(program closed)")
        print("\n[SHUTDOWN] George offline.")