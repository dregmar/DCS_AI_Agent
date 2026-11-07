"""
core/settings.py - settings changed from the kneeboard's Settings page (S1 of "Kneeboard Settings page -
design", approved by Andre 6 Nov 2026).

A saved setting (settings_local.json, next to main.py) wins over config.py; resetting it brings config.py's
value back. config.py is never written. Only the 37 settings in SETTINGS can be saved; each is checked
against its type and limits. Passwords, logins, ports, the GPU / LLM / Whisper set-up, key bindings and data
paths are not on the list and can't be.

    get("NAME", default)    the saved value, else config.py's, else default (the setting's own if None)
    set_value("NAME", v)    -> (ok, why): checked, saved (whole file to a temp file, then renamed), logged,
                            then the setting's bound callbacks run (outside the lock)
    reset("NAME")           -> (ok, why): back to config.py's value
    bind("NAME", fn)        fn(value) on every change: how a live setting reaches a running object
    describe()              every setting with its value and where it came from (the page, step S2)
    voices()                the .onnx voices in the program folder and its voices subfolder

Tests never touch Andre's file: GEORGE_SETTINGS_FILE (tests/fakes.py) names another file, and when unittest
is loaded without it the store keeps its values in memory only.
"""
import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import config

FILE_NAME = "settings_local.json"
_MISSING = object()


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    group: str
    kind: str                      # "bool" | "int" | "float" | "choice" | "text" | "voice"
    default: Any
    lo: Optional[float] = None
    hi: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    choices: Tuple[str, ...] = ()
    live: bool = True
    help: str = ""


GROUPS = ("Crew", "Command roles", "Overlord", "Warlord", "Darkside", "Tactical and kneeboard", "Training", "Voices")

S = Setting
SETTINGS: Tuple[Setting, ...] = (
    S("FUEL_CALLS", "Fuel calls", "Crew", "bool", True, help="Joker and bingo fuel calls (they always show on the Alerts tab)."),
    S("APACHE_EUFD_CALLOUTS", "Apache EUFD callouts", "Crew", "bool", True, help="George calls new EUFD warnings and cautions."),
    S("HUEY_CALLOUTS", "Huey callouts", "Crew", "bool", True, help="The crew chief's engine, contact and threat calls in the UH-1H."),
    S("HUEY_TALKDOWN", "Huey talk-down", "Crew", "bool", True, help="The crew chief talks you down to a hover or landing."),
    S("HUEY_AUTO_ARM_FLARES", "Huey auto-arm flares", "Crew", "bool", True, help="Arm the flare system when you ask for flares."),
    S("ROUTER_ENABLED", "LLM router for unmatched phrases", "Crew", "bool", True, live=False,
      help="When nothing matches, the local LLM may map what you said to a standard order."),
    S("COMMAND_ROLES_ENABLED", "Command roles", "Command roles", "bool", True, live=False,
      help="Warlord and Darkside; off = Overlord alone, as before Phase 2."),
    S("COMMAND_SHORT_FORM", "Short radio form", "Command roles", "bool", False,
      help="Shorter calls; denials, danger close and aborts keep their full words."),
    S("AI_TO_AI_SPOKEN", "Speak AI-to-AI traffic", "Command roles", "bool", False,
      help="Calls between AI (\"Hog 1, cleared hot\") are spoken, not only logged."),
    S("PERMISSIONS_ENFORCE", "Enforce the permission table", "Command roles", "bool", True,
      help="Off: a role's order outside its permissions is only logged."),
    S("OPERATOR_CALLSIGN", "Callsign if DCS gives none", "Command roles", "text", "NOMAD",
      help="What the roles call you when the mission gives no callsign."),
    S("OVERLORD_AIR_WARNINGS", "Air threat warnings", "Overlord", "bool", True,
      help="Overlord calls pop-up groups and bandits closing on his own."),
    S("DEFER_WAIT_S", "Deferred request wait", "Overlord", "int", 300, 60, 900, 30, "s",
      help="How long a request Warlord defers is kept and rechecked."),
    S("WARLORD_MAX_FLIGHTS", "Flights up at once", "Warlord", "int", 4, 1, 8, 1,
      help="More than this and Warlord defers the next request."),
    S("WARLORD_CAUTION_M", "MANPADS / AAA caution radius", "Warlord", "int", 5000, 1000, 10000, 500, "m",
      help="Short-range air defence this close to a CAS target gets a caution."),
    S("DIRECT_ORDERS_ZONES", "Your own attack orders into a no-strike zone", "Warlord", "choice", "warn",
      choices=("warn", "block"), help="Wingman and Outcast attacks: Warlord warns, or the order isn't sent."),
    S("DARKSIDE_DANGER_CLOSE_M", "Danger close distance", "Darkside", "int", 1000, 500, 2000, 100, "m",
      help="A friendly this close to a CAS target holds the run."),
    S("DANGER_CLOSE_AGL_M", "Danger close height (your aircraft)", "Darkside", "int", 300, 100, 1000, 50, "m",
      help="You, wingmen or Outcasts this low near the target hold a CAS run."),
    S("JTAC_GROUND_LASER_M", "Ground JTAC laser range", "Darkside", "int", 8000, 2000, 15000, 500, "m",
      help="A ground JTAC this close (with line of sight) can lase."),
    S("JTAC_DRONE_LASER_M", "Drone laser range", "Darkside", "int", 20000, 5000, 40000, 1000, "m",
      help="A drone this close, slant range, can lase."),
    S("NINE_LINE_READBACK", "9-line readback", "Darkside", "bool", False,
      help="Read back lines 4 and 6 before your IN call."),
    S("NINE_LINE_AI_TO_AI", "9-line to Hog flights", "Darkside", "bool", False,
      help="Darkside also briefs each Hog flight at the IP."),
    S("TACTICAL_ENABLED", "Tactical picture", "Tactical and kneeboard", "bool", True, live=False,
      help="Off: the roles and map use DCS's raw lists only."),
    S("FRIENDLY_SHOW_M", "Friendlies shown within", "Tactical and kneeboard", "int", 5000, 1000, 20000, 500, "m",
      help="Your side's units this close are drawn on the map."),
    S("KNEEBOARD_AUTO_CHECKLIST", "Checklist step follows the aircraft", "Tactical and kneeboard", "bool", True,
      help="The Checklist page turns to a run when it starts."),
    S("KNEEBOARD_TRACK", "Your track on the map", "Tactical and kneeboard", "bool", True,
      help="Shows your flown track (it's always recorded)."),
    S("INSTRUCTOR_ENABLED", "Instructor", "Training", "bool", True, live=False,
      help="Jenny and the training exercises."),
    S("INSTRUCTOR_HOVER_LEVEL", "Hover exercise start level", "Training", "int", 1, 1, 5, 1,
      help="The first level of a hover exercise (then adaptive)."),
    S("TRAINING_MAX_RANGE_KM", "Training target range", "Training", "int", 50, 5, 100, 5, "km",
      help="Training targets only this close to you."),
    S("TRAINING_MAX_GROUPS", "Training groups at once", "Training", "int", 6, 1, 10, 1),
    S("TRAINING_MAX_UNITS", "Training units at once", "Training", "int", 24, 1, 40, 1),
    S("TRAINING_VOICE_SWITCH", "Jenny speaks for training", "Training", "bool", True,
      help="Training replies in the instructor's voice, even after \"Overlord,\"."),
    S("PIPER_MODEL_PATH", "George / crew voice", "Voices", "voice", None, live=False),
    S("OVERLORD_MODEL_PATH", "Overlord voice", "Voices", "voice", None, live=False),
    # the roles' default files when config.py names none (audio/tts.py)
    S("WARLORD_MODEL_PATH", "Warlord voice", "Voices", "voice", "en_US-norman-medium.onnx", live=False),
    S("DARKSIDE_MODEL_PATH", "Darkside voice", "Voices", "voice", "en_US-bryce-medium.onnx", live=False),
    S("INSTRUCTOR_MODEL_PATH", "Jenny (instructor) voice", "Voices", "voice", "jenny.onnx", live=False),
)
del S
BY_NAME: Dict[str, Setting] = {s.name: s for s in SETTINGS}
_CALLSIGN = re.compile(r"^[A-Za-z][A-Za-z ]{1,15}$")
_ON = {"on", "true", "yes", "1"}
_OFF = {"off", "false", "no", "0"}


def base_dir() -> str:
    return getattr(config, "BASE_DIR", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def voices(base: Optional[str] = None) -> List[str]:
    """Every Piper voice (.onnx with its .onnx.json) in the program folder and its voices subfolder."""
    base = base or base_dir()
    out = []
    for folder in (base, os.path.join(base, "voices")):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for n in names:
            p = os.path.join(folder, n)
            if n.lower().endswith(".onnx") and os.path.isfile(p) and os.path.isfile(p + ".json"):
                out.append(p)
    return out


def check(name: str, value: Any, base: Optional[str] = None) -> Tuple[bool, Any]:
    """(True, the value as stored) or (False, why not)."""
    s = BY_NAME.get(name)
    if s is None:
        return False, f"{name} can't be changed here"
    if s.kind == "bool":
        if isinstance(value, bool):
            return True, value
        v = str(value).strip().lower()
        if v in _ON or v in _OFF:
            return True, v in _ON
        return False, f"{s.label}: on or off"
    if s.kind in ("int", "float"):
        try:
            v = float(value)
            if isinstance(value, bool) or v != v:
                raise ValueError
        except (TypeError, ValueError):
            return False, f"{s.label}: a number"
        if s.kind == "int":
            if v != int(v):
                return False, f"{s.label}: a whole number"
            v = int(v)
        if (s.lo is not None and v < s.lo) or (s.hi is not None and v > s.hi):
            return False, f"{s.label}: {s.lo:g} to {s.hi:g}{(' ' + s.unit) if s.unit else ''}"
        return True, v
    if s.kind == "choice":
        v = str(value).strip().lower()
        return (True, v) if v in s.choices else (False, f"{s.label}: {' or '.join(s.choices)}")
    if s.kind == "text":
        v = str(value).strip()
        return (True, v.upper()) if _CALLSIGN.match(v) else (False, f"{s.label}: 2 to 16 letters")
    if s.kind == "voice":
        v = os.path.normcase(os.path.abspath(str(value)))
        if v in {os.path.normcase(os.path.abspath(p)) for p in voices(base)}:
            return True, os.path.abspath(str(value))
        return False, f"{s.label}: one of the voices in the program folder or its voices subfolder"
    return False, f"{name}: unknown kind"


class SettingsStore:
    def __init__(self, path: Optional[str], cfg=config, printer: Callable[[str], None] = print,
                 base: Optional[str] = None):
        """path None: in memory only (tests)."""
        self.path, self.cfg, self._print, self.base = path, cfg, printer, base
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._saved: Dict[str, Any] = {}
        self._binds: Dict[str, List[Callable[[Any], None]]] = {}
        self.load()

    # ------------------------------------------------------------------ file
    def load(self) -> None:
        saved: Dict[str, Any] = {}
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                self._print(f"[SETTINGS] {FILE_NAME} unreadable ({e}): using config.py only. "
                            "It's replaced on the next change from the Settings page.")
                raw = {}
            for k, v in raw.items():
                ok, got = check(k, v, self.base)
                if ok:
                    saved[k] = got
                else:
                    self._print(f"[SETTINGS] Ignored {k} in {FILE_NAME}: {got}")
        with self._lock:
            self._saved = saved
        if saved:
            self._print(f"[SETTINGS] {len(saved)} saved setting{'s' if len(saved) != 1 else ''} override config.py: "
                        + ", ".join(f"{k}={_said(v)}" for k, v in sorted(saved.items())))

    def _write(self, data: Dict[str, Any]) -> Optional[str]:
        """The whole file, to a temp file then renamed (a crash never leaves it half-written)."""
        if not self.path:
            return None
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            return None
        except OSError as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return f"couldn't save: {e}"

    # ------------------------------------------------------------------ values
    def get(self, name: str, default: Any = _MISSING) -> Any:
        with self._lock:
            if name in self._saved:
                return self._saved[name]
        if default is _MISSING:
            s = BY_NAME.get(name)
            default = s.default if s is not None else None
        return getattr(self.cfg, name, default)

    def source(self, name: str) -> str:
        with self._lock:
            if name in self._saved:
                return "saved"
        return "config" if hasattr(self.cfg, name) else "default"

    def saved(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._saved)

    def _change(self, name: str, new: Any, who: str) -> Tuple[bool, str]:
        old = self.get(name)
        with self._write_lock:                                   # one writer at a time; readers never wait on it
            with self._lock:
                data = dict(self._saved)
            if new is _MISSING:
                data.pop(name, None)
            else:
                data[name] = new
            why = self._write(data)
            if why:
                return False, why
            with self._lock:
                self._saved = data
        now = self.get(name)
        self._print(f"[SETTINGS] {name} {_said(old)} -> {_said(now)} ({who})"
                    + (" - applies after a restart" if not BY_NAME[name].live else ""))
        for fn in list(self._binds.get(name, ())):               # outside every lock
            try:
                fn(now)
            except Exception as e:
                self._print(f"[SETTINGS] Applying {name} failed: {e}")
        return True, ""

    def set_value(self, name: str, value: Any, who: str = "kneeboard") -> Tuple[bool, str]:
        ok, got = check(name, value, self.base)
        if not ok:
            return False, got
        return self._change(name, got, who)

    def reset(self, name: str, who: str = "kneeboard") -> Tuple[bool, str]:
        if name not in BY_NAME:
            return False, f"{name} can't be changed here"
        return self._change(name, _MISSING, who)

    def bind(self, name: str, fn: Callable[[Any], None], apply_now: bool = True) -> None:
        """fn(value) on every change of name; apply_now: also now, with the current value."""
        self._binds.setdefault(name, []).append(fn)
        if apply_now:
            fn(self.get(name))

    def describe(self) -> List[dict]:
        out = []
        for s in SETTINGS:
            row = {"name": s.name, "label": s.label, "group": s.group, "kind": s.kind, "live": s.live,
                   "help": s.help, "unit": s.unit, "value": self.get(s.name), "source": self.source(s.name)}
            if s.kind in ("int", "float"):
                row.update(lo=s.lo, hi=s.hi, step=s.step)
            if s.kind == "choice":
                row["choices"] = list(s.choices)
            if s.kind == "voice":
                row["choices"] = voices(self.base)
            out.append(row)
        return out


def _said(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, str) and v.lower().endswith(".onnx"):
        return os.path.basename(v)
    return str(v)


# ---------------------------------------------------------------------- the program's store
_STORE: Optional[SettingsStore] = None
_STORE_LOCK = threading.Lock()


def default_path() -> Optional[str]:
    env = os.environ.get("GEORGE_SETTINGS_FILE")
    if env is not None:
        return env or None
    if "unittest" in sys.modules:                 # a test run: never Andre's file
        return None
    return os.path.join(base_dir(), FILE_NAME)


def store() -> SettingsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SettingsStore(default_path())
        return _STORE


def use(s: SettingsStore) -> None:
    """Tests: a store of their own."""
    global _STORE
    with _STORE_LOCK:
        _STORE = s


def get(name: str, default: Any = _MISSING) -> Any:
    return store().get(name, default)


def set_value(name: str, value: Any, who: str = "kneeboard") -> Tuple[bool, str]:
    return store().set_value(name, value, who)


def reset(name: str, who: str = "kneeboard") -> Tuple[bool, str]:
    return store().reset(name, who)


def bind(name: str, fn: Callable[[Any], None], apply_now: bool = True) -> None:
    store().bind(name, fn, apply_now)
