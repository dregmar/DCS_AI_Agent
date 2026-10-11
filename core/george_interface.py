"""
core/george_interface.py
George AI (as CPG) driver for the DCS AH-64D, player in the Pilot seat.

Source of truth: DCS AH-64D manual, "Player-as-Pilot 'George' AI Controls"
(pp. 638-665): the "Player-as-Pilot AI Commands" and "AI Helper Controls" tables.
Project copy of those tables: claude/george_cpg_controls_reference.md

Input model (manual p.638-639)
  * AI Helper Controls (Up/Down/Left/Right) only work while the AI Helper Interface
    is SHOWN. Defaults in the "AH-64D George AI Helper" control mode: W / S / A / D.
  * AI Commands (Show/Hide = LCtrl+V, Multifunctional Input = MFI, TADS Store Target)
    work whether or not the interface is shown. MFI and Store have no DCS default key.
  * Short press < 0.5 s, long press > 0.5 s, some actions need > 1 s.

Threading
  All keystrokes run on one daemon worker fed by a queue. run_action() validates and
  enqueues, then returns at once, so the voice worker, hook and telemetry threads never
  wait on key timing. Commands keep their order.

State tracking
  DCS does not export George's wheel state, so this module keeps its own model (assigned
  weapon, ROE, TADS state, weapon settings, open list, list cursor) and updates it from the
  keys it sends. It drifts if the pilot uses the wheel by hand; sync() corrects it.

  Policy: PILOT INTENT WINS. The pilot can see the wheel and we can't, so when a spoken
  command belongs to a context ("next target" -> a list), the driver assumes that context
  and sends the key, logging any disagreement with the model, instead of refusing.

  Wheel visibility is tracked, not observed: our own Ctrl+V presses plus the pilot's
  keyboard Ctrl+V (main.py's hook calls note_user_toggle()). A HOTAS button bound inside
  DCS can't be seen; "toggle wheel" re-syncs by flipping it.

LIVE INTERFACE (preferred whenever it is fresh)
  george_ai_export.lua streams George's interface text (indicator 26) and core/george_ui.py
  parses it. While that feed is live, the model is corrected from what DCS actually shows:
  wheel shown/hidden, open list and its five-line window, and on the main ring the real
  weapon, ROE, TADS state and weapon settings. Frames older than our last key press plus
  UI_SETTLE_S are ignored, so a frame drawn before our key landed can't undo it.
  The driver also keeps George's Target List (names in list order, cursor position) and
  hands it to list_sink() so the LLM / fast path see the same numbering as the wheel.
"""

from __future__ import annotations

import logging
import re
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple

from pynput.keyboard import Controller, Key, KeyCode

import config
from core.focus import dcs_has_focus

log = logging.getLogger("george")

# ---------------------------------------------------------------------------
# Tunables (override any of these in config.py)
# ---------------------------------------------------------------------------
SHORT_S = getattr(config, "GEORGE_SHORT_PRESS_S", 0.10)            # < 0.5 s
LONG_S = getattr(config, "GEORGE_LONG_PRESS_S", 0.75)              # > 0.5 s
EXTRA_LONG_S = getattr(config, "GEORGE_EXTRA_LONG_PRESS_S", 1.30)  # > 1 s (start-up / shutdown)
GAP_S = getattr(config, "GEORGE_INTER_KEY_GAP_S", getattr(config, "KEY_DELAY", 0.12))
OPEN_SETTLE_S = getattr(config, "GEORGE_OPEN_SETTLE_S", 0.20)      # pause after showing the wheel
# Idle seconds before hiding the wheel while on the main ring. None = leave it open.
# Only used while the live interface feed shows the wheel (so we never "hide" it open by mistake).
IDLE_CLOSE_MAIN_S = getattr(config, "GEORGE_IDLE_CLOSE_MAIN_S", 6.0)
# Hiding the interface cancels any open list, so lists never auto-close by default.
IDLE_CLOSE_LIST_S = getattr(config, "GEORGE_IDLE_CLOSE_LIST_S", None)
# After a search, never auto-hide for this long: area searches only present a Target List
# if the interface is shown within 3 s of a detection.
SEARCH_HOLD_OPEN_S = getattr(config, "GEORGE_SEARCH_HOLD_OPEN_S", 25.0)
# Live interface feed (core/george_ui.py): frames older than this are not "live".
UI_LIVE_S = getattr(config, "GEORGE_UI_LIVE_S", 1.5)
# A frame must arrive this long after our last key press to be trusted (exporter runs at 5 Hz).
UI_SETTLE_S = getattr(config, "GEORGE_UI_SETTLE_S", 0.25)
# Longest wait for a frame drawn after our key press before carrying on without it.
UI_WAIT_S = getattr(config, "GEORGE_UI_WAIT_S", 1.5)

# Bindings: tuple of pynput key names, e.g. ("ctrl_l", "v"). None = unbound.
BIND_TOGGLE = getattr(config, "GEORGE_SHOW_HIDE", ("ctrl_l", "v"))
BIND_UP = getattr(config, "GEORGE_KEY_UP", ("w",))
BIND_DOWN = getattr(config, "GEORGE_KEY_DOWN", ("s",))
BIND_LEFT = getattr(config, "GEORGE_KEY_LEFT", ("a",))
BIND_RIGHT = getattr(config, "GEORGE_KEY_RIGHT", ("d",))
BIND_MFI = getattr(config, "GEORGE_KEY_MFI", None)              # no DCS default
BIND_STORE = getattr(config, "GEORGE_KEY_STORE_TARGET", None)   # no DCS default

# Virtual-key code of the Show/Hide letter, for main.py's Ctrl+V hook (pynput reports
# Ctrl+V as a control character, so the hook matches on vk).
TOGGLE_VK = ord(BIND_TOGGLE[-1].upper()) if len(BIND_TOGGLE[-1]) == 1 else None

# Only loaded weapons/types appear in George's cycles; set these from your loadout.
WEAPON_CYCLE = getattr(config, "GEORGE_WEAPON_CYCLE", ["NO_WPN", "GUN", "MSL", "RKT"])
MSL_TYPE_CYCLE = getattr(config, "GEORGE_MSL_TYPE_CYCLE", ["SAL2", "RF"])
RKT_TYPE_CYCLE = getattr(config, "GEORGE_RKT_TYPE_CYCLE", ["HE", "ILL", "SMK"])
GUN_BURST_CYCLE = ["10", "20", "50", "100", "ALL"]
RKT_QTY_CYCLE = ["1", "2", "4", "8", "12", "24", "ALL"]
MSL_TRAJ_CYCLE = ["DIR", "LO", "HI"]
MSL_METHOD_CYCLE = ["LOBL", "LOAL"]
ROE_CYCLE = ["HOLD", "FREE"]


def _combo(names):
    if not names:
        return None
    if isinstance(names, str):          # ("f") in config.py is just the string "f"
        names = (names,)
    return tuple(KeyCode.from_char(n) if len(n) == 1 else getattr(Key, n) for n in names)


class FocusLost(Exception):
    """DCS is not the foreground window: stop sending keys."""


class Ctx(str, Enum):
    MAIN = "MAIN"
    SEARCH_LIST = "SEARCH_LIST"
    POINT_LIST = "POINT_LIST"
    TARGET_LIST = "TARGET_LIST"


LISTS = (Ctx.SEARCH_LIST, Ctx.POINT_LIST, Ctx.TARGET_LIST)


class Tads(str, Enum):
    FIXED = "FIXED"          # slaved fixed forward
    SLAVED = "SLAVED"        # slaved to PHS or a TSD point
    LINKED = "LINKED"        # linked to FCR NTS
    DESLAVED = "DESLAVED"
    SEARCHING = "SEARCHING"
    TRACKING = "TRACKING"


class Key4(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    MFI = "MFI"
    STORE = "STORE"


_BINDS = {
    Key4.UP: _combo(BIND_UP), Key4.DOWN: _combo(BIND_DOWN),
    Key4.LEFT: _combo(BIND_LEFT), Key4.RIGHT: _combo(BIND_RIGHT),
    Key4.MFI: _combo(BIND_MFI), Key4.STORE: _combo(BIND_STORE),
}
_TOGGLE = _combo(BIND_TOGGLE)
_NEEDS_WHEEL = {Key4.UP, Key4.DOWN, Key4.LEFT, Key4.RIGHT}


@dataclass(frozen=True)
class Cmd:
    name: str
    key: Key4
    hold: float
    ctx: Optional[Ctx]                       # context it belongs to; None = any / no wheel needed
    desc: str
    weapons: Optional[tuple] = None          # assigned weapons it applies to (None = any)
    tads: Optional[tuple] = None             # TADS states it applies to (None = any)


S, L, XL = SHORT_S, LONG_S, EXTRA_LONG_S
M, SL, PL, TL = Ctx.MAIN, Ctx.SEARCH_LIST, Ctx.POINT_LIST, Ctx.TARGET_LIST
_FREE_TADS = (Tads.FIXED, Tads.DESLAVED, Tads.SEARCHING, Tads.TRACKING)
_NOT_TRACKING = (Tads.FIXED, Tads.DESLAVED, Tads.SEARCHING)

# fmt: off
COMMANDS: dict[str, Cmd] = {c.name: c for c in [
    # ---------------- Main ring: DIRECT SEARCH / WEAPON CONTROL ----------------
    Cmd("cycle_weapon",        Key4.LEFT,  S, M, "Cycle assigned weapon NO WPN -> GUN -> MSL -> RKT"),
    Cmd("open_search_list",    Key4.LEFT,  L, M, "Open the Area Search list", weapons=("NO_WPN",)),
    Cmd("toggle_missile_type", Key4.LEFT,  L, M, "Toggle missile type SAL2 / RF", weapons=("MSL",)),
    Cmd("cycle_rocket_type",   Key4.LEFT,  L, M, "Cycle rocket type HE -> ILL -> SMK", weapons=("RKT",)),
    Cmd("toggle_laser",        Key4.RIGHT, S, M, "Start / stop lasing (no weapon assigned)", weapons=("NO_WPN",)),
    Cmd("cycle_gun_burst",     Key4.RIGHT, S, M, "Cycle gun burst 10 -> 20 -> 50 -> 100 -> ALL", weapons=("GUN",)),
    Cmd("toggle_missile_method", Key4.RIGHT, S, M, "Toggle SAL missile LOBL / LOAL (TRAJ must be DIR)", weapons=("MSL",)),
    Cmd("cycle_rocket_qty",    Key4.RIGHT, S, M, "Cycle rocket salvo 1 -> 2 -> 4 -> 8 -> 12 -> 24 -> ALL", weapons=("RKT",)),
    Cmd("open_point_list",     Key4.RIGHT, L, M, "Open the TSD Point List", weapons=("NO_WPN",)),
    Cmd("cycle_missile_traj",  Key4.RIGHT, L, M, "Cycle SAL trajectory DIR -> LO -> HI (only in LOAL)", weapons=("MSL",)),
    Cmd("phs_search",          Key4.UP,    S, M, "Slave TADS to pilot helmet sight and search"),
    Cmd("toggle_roe",          Key4.UP,    L, M, "Toggle ROE HOLD FIRE / FREE FIRE", tads=_FREE_TADS),
    Cmd("zoom_in",             Key4.UP,    L, M, "TADS zoom in (TADS slaved or linked)", tads=(Tads.SLAVED, Tads.LINKED)),
    Cmd("cease",               Key4.DOWN,  S, M, "Cease all tasks incl. engagements, TADS fixed forward"),
    Cmd("tads_los_search",     Key4.DOWN,  L, M, "Search along current TADS line of sight", tads=(Tads.DESLAVED, Tads.SEARCHING, Tads.TRACKING)),
    Cmd("zoom_out",            Key4.DOWN,  L, M, "TADS zoom out (TADS slaved or linked)", tads=(Tads.SLAVED, Tads.LINKED)),
    Cmd("toggle_threat_warn",  Key4.DOWN,  L, M, "Toggle verbal threat warnings (TADS fixed forward)", tads=(Tads.FIXED,)),
    # ---------------- Lists: SEARCH / POINT / TARGET ----------------
    Cmd("list_up",             Key4.UP,    S, None, "Scroll the open list up"),
    Cmd("list_down",           Key4.DOWN,  S, None, "Scroll the open list down"),
    Cmd("list_exit",           Key4.LEFT,  S, None, "Exit the open list, cancel selections"),
    Cmd("select_area_search",  Key4.RIGHT, S, SL, "Select highlighted Area Search"),
    Cmd("select_point",        Key4.RIGHT, S, PL, "Select highlighted TSD point"),
    Cmd("point_filter_ctrlm_wpthz", Key4.LEFT, L, PL, "Point List filter CTRLM / WPTHZ"),
    Cmd("point_filter_tgt_thrt",    Key4.RIGHT, L, PL, "Point List filter TGT / THRT"),
    Cmd("track_target",        Key4.RIGHT, S, TL, "Track highlighted target / start shoot list"),
    Cmd("target_filter",       Key4.LEFT,  L, TL, "Target List filter HOS UNK / ALL TGTS"),
    Cmd("list_zoom_in",        Key4.UP,    L, TL, "TADS zoom in from the Target List"),
    Cmd("list_zoom_out",       Key4.DOWN,  L, TL, "TADS zoom out from the Target List"),
    # ---------------- AI Commands: Multifunctional Input (no wheel needed) ----------------
    Cmd("consent_to_fire",     Key4.MFI,   S, None, "Fire / consent to fire GUN or MSL", weapons=("GUN", "MSL")),
    Cmd("toggle_fov",          Key4.MFI,   S, None, "TADS FOV Narrow / Zoom (tracking, NO WPN or RKT)", weapons=("NO_WPN", "RKT")),
    Cmd("toggle_sensor",       Key4.MFI,   L, None, "TADS sensor FLIR / DTV"),
    Cmd("shoot_list_toggle",   Key4.MFI,   S, TL, "Add / remove highlighted target on the shoot list"),
    Cmd("slave_to_point",      Key4.MFI,   S, None, "Slave TADS to selected TSD point", tads=_NOT_TRACKING),
    Cmd("start_area_search",   Key4.MFI,   L, None, "Start the selected Area Search", tads=_NOT_TRACKING),
    Cmd("point_search",        Key4.MFI,   S, None, "Point search at slaved TSD point / FCR NTS", tads=(Tads.SLAVED, Tads.LINKED)),
    Cmd("next_target",         Key4.MFI,   L, None, "Skip to next target in a multi-target engagement", tads=(Tads.TRACKING,)),
    Cmd("select_last_stored",  Key4.MFI,   S, PL, "Select the last Target point George stored"),
    Cmd("store_target",        Key4.STORE, S, None, "Store tracked target as a TSD Target point"),
    Cmd("startup_shutdown",    Key4.MFI,  XL, None, "On the ground: CPG start-up or shutdown"),
]}
# fmt: on

# Goal-based settings: name -> (cycle, state key, one-step command, weapon it needs)
SETTERS = {
    "weapon":         (WEAPON_CYCLE,     "weapon",     "cycle_weapon",          None),
    "roe":            (ROE_CYCLE,        "roe",        "toggle_roe",            None),
    "gun_burst":      (GUN_BURST_CYCLE,  "gun_burst",  "cycle_gun_burst",       "GUN"),
    "rocket_qty":     (RKT_QTY_CYCLE,    "rkt_qty",    "cycle_rocket_qty",      "RKT"),
    "rocket_type":    (RKT_TYPE_CYCLE,   "rkt_type",   "cycle_rocket_type",     "RKT"),
    "missile_type":   (MSL_TYPE_CYCLE,   "msl_type",   "toggle_missile_type",   "MSL"),
    "missile_method": (MSL_METHOD_CYCLE, "msl_method", "toggle_missile_method", "MSL"),
    "missile_traj":   (MSL_TRAJ_CYCLE,   "msl_traj",   "cycle_missile_traj",    "MSL"),
}

# Pipeline action names (fast path / LLM) -> driver command. Special actions
# (GEORGE_SET, SELECT_TARGET_INDEX, WHEEL_*) are handled in run_action().
ACTIONS = {
    "PHS_SEARCH": "phs_search", "TADS_SEARCH": "tads_los_search", "CEASE": "cease",
    "CONSENT_TO_FIRE": "consent_to_fire", "CYCLE_WEAPON": "cycle_weapon",
    "TOGGLE_ROE": "toggle_roe", "TOGGLE_LASER": "toggle_laser",
    "TOGGLE_FOV": "toggle_fov", "TOGGLE_SENSOR": "toggle_sensor",
    "ZOOM_IN": "zoom_in", "ZOOM_OUT": "zoom_out", "STORE_TARGET": "store_target",
    "NEXT_TARGET": "next_target", "TOGGLE_THREAT_WARN": "toggle_threat_warn",
    "LIST_UP": "list_up", "LIST_DOWN": "list_down", "LIST_EXIT": "list_exit",
    "TRACK_TARGET": "track_target", "SHOOT_LIST_TOGGLE": "shoot_list_toggle",
    "OPEN_SEARCH_LIST": "open_search_list", "OPEN_POINT_LIST": "open_point_list",
    "SELECT_AREA_SEARCH": "select_area_search", "START_AREA_SEARCH": "start_area_search",
    "SELECT_POINT": "select_point", "SLAVE_TO_POINT": "slave_to_point",
    "POINT_SEARCH": "point_search", "TARGET_FILTER": "target_filter",
}
# Area searches as George's Search List names them (manual p.646).
AREA_SEARCHES = {"FWD AREA": "forward area", "PHS AREA": "helmet sight area", "FCR TGTS": "FCR targets",
                 "OWN PFZ": "own priority fire zone", "ACTV PFZ": "active priority fire zone"}
SPECIAL_ACTIONS = {"AREA_SEARCH", "POINT_SEARCH_AT", "GEORGE_SET", "SELECT_TARGET_INDEX", "ENGAGE_TARGETS", "WHEEL_SHOW", "WHEEL_HIDE", "SHOW_HIDE"}
ALL_ACTIONS = set(ACTIONS) | SPECIAL_ACTIONS

# Actions whose result George reads back from his wheel before speaking (see _confirm).
_CONFIRM_RAW = {"CYCLE_WEAPON": ("cycle_weapon", "weapon"), "TOGGLE_ROE": ("toggle_roe", "roe"),
                "TOGGLE_LASER": ("toggle_laser", "laser"),
                "TOGGLE_THREAT_WARN": ("toggle_threat_warn", "threat_warn")}
CONFIRMED_ACTIONS = {"GEORGE_SET", "ENGAGE_TARGETS", "AREA_SEARCH", "POINT_SEARCH_AT"} | set(_CONFIRM_RAW)
_TRAJ_WORDS = {"HI": "high", "LO": "low", "DIR": "direct"}


def spoken_value(key: str, v) -> str:
    """How George says a setting, e.g. ('roe', 'FREE') -> 'Weapons free.'"""
    if key == "weapon":
        return {"NO_WPN": "No weapon.", "GUN": "Gun.", "MSL": "Missiles.", "RKT": "Rockets."}.get(v, f"{v}.")
    if key == "roe":
        return "Weapons free." if v == "FREE" else "Weapons hold."
    if key == "gun_burst":
        return f"Burst {str(v).lower()}."
    if key == "rkt_qty":
        return f"Rockets {str(v).lower()}."
    if key == "msl_traj":
        return f"Trajectory {_TRAJ_WORDS.get(v, v)}."
    if key == "laser":
        return "Laser on." if v else "Laser off."
    if key == "threat_warn":
        return "Threat warnings on." if v else "Threat warnings off."
    return f"{v}."


_WORDS = {"NO_WPN": "no weapon", "GUN": "gun", "MSL": "missiles", "RKT": "rockets",
          "HOLD": "weapons hold", "FREE": "weapons free"}


class GeorgeInterface:
    """Queue-driven, thread-safe George CPG driver."""

    def __init__(self, keyboard=None, on_event: Optional[Callable[[str, dict], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 ui_source: Optional[Callable[[], Tuple[dict, float]]] = None,
                 list_sink: Optional[Callable[[list, bool], None]] = None,
                 focus_check: Callable[[], bool] = dcs_has_focus):
        """ui_source() -> (parsed interface dict, receive time.monotonic()), e.g.
        state_store.george_ui_snapshot. list_sink(names, complete) receives George's Target
        List, e.g. state_store.set_george_list."""
        self._kb = keyboard or Controller()
        self._ui_source = ui_source
        self._list_sink = list_sink
        self._focus_check = focus_check
        self._focus_warned_t = -10.0
        self._ui_seen = set()       # settings the last main-ring frame actually showed
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.RLock()
        self._clock = clock
        self._on_event = on_event  # e.g. push into ApacheStateStore / log

        self._s = {  # tracked model, guarded by _lock
            "wheel_open": False, "ctx": Ctx.MAIN, "tads": Tads.FIXED, "cursor": 0,
            "weapon": "NO_WPN", "roe": "HOLD",
            "gun_burst": "10", "rkt_qty": "1", "rkt_type": RKT_TYPE_CYCLE[0],
            "msl_type": MSL_TYPE_CYCLE[0], "msl_method": "LOBL", "msl_traj": "DIR",
            "laser": False, "threat_warn": True,
        }
        self._last_key_t = 0.0
        self._hold_open_until = 0.0
        self._inject_until = 0.0   # ignore our own Ctrl+V echoing back through the hook
        self._ui_live = False      # True while the interface feed is fresh
        self._ui_sig = None        # last interface content, to notice the pilot using the wheel
        self._last_activity_t = 0.0
        self._ui_list_open = False # a list was visible in the last live frame
        self._pending_scroll = 0   # our own list scrolls not yet seen in a frame
        self._last_window = None   # previous five-line list window
        self._main_frame_t = 0.0   # receive time of the last accepted main-ring frame
        self._list = []            # George's Target List names, index 0 = #1
        self._list_complete = False

        self._stop = threading.Event()
        threading.Thread(target=self._run, name="george-cpg", daemon=True).start()

    # ============================================================ pipeline API
    def confirms(self, action: str) -> bool:
        """True if the driver speaks this action's result itself, after reading it back from
        George's wheel (so the caller must not speak it first)."""
        return action in CONFIRMED_ACTIONS

    def run_action(self, action: str, target_index: Optional[int] = None,
                   setting: Optional[str] = None, value: Optional[str] = None,
                   speech: Optional[str] = None, targets: Optional[list] = None) -> Tuple[bool, str]:
        """Validate and enqueue one pipeline action. Returns (accepted, reason). Never blocks.
        For CONFIRMED_ACTIONS the driver later emits "say" with the verified result."""
        if not self._focus_check():
            return False, "DCS not in focus."
        if action == "GEORGE_SET":
            return self.set(setting or "", value or "", speech=speech)
        if action in _CONFIRM_RAW:
            name, key = _CONFIRM_RAW[action]
            return self.command(name, confirm=(key, speech))
        if action in ("AREA_SEARCH", "POINT_SEARCH_AT"):
            if _BINDS[Key4.MFI] is None:
                return False, "Negative, fire button not bound."
            self.refresh_from_ui()
            with self._lock:
                live = self._ui_live
            if not live:
                return False, "Unable, I can't read George's lists without the interface feed."
            name = (value or "").upper().strip()
            if action == "AREA_SEARCH":
                if name not in AREA_SEARCHES:
                    return False, "Say again, which area search?"
                self._q.put(("area_search", name))
            else:
                if not re.fullmatch(r"[A-Z]\d{2}", name):
                    return False, "Say again, which point?"
                self._q.put(("point_search_at", name))
            return True, ""
        if action == "ENGAGE_TARGETS":
            idxs = []
            for i in (targets or []):
                if isinstance(i, int) and i >= 1 and i not in idxs:
                    idxs.append(i)
            if not idxs:
                return False, "Say again, which targets?"
            if _BINDS[Key4.MFI] is None:
                return False, "Negative, fire button not bound."
            self.refresh_from_ui()
            with self._lock:
                live, ctx, n, complete = self._ui_live, self._s["ctx"], len(self._list), self._list_complete
            if live and ctx != Ctx.TARGET_LIST:
                return False, "No target list up. Say search."
            if complete and max(idxs) > n:
                return False, f"Negative, only {n} target{'s' if n != 1 else ''}."
            self._q.put(("engage", idxs[:8]))
            return True, ""
        if action == "SELECT_TARGET_INDEX":
            if not target_index or target_index < 1:
                return False, "Say again, which target?"
            self.refresh_from_ui()
            with self._lock:
                live, ctx, n, complete = self._ui_live, self._s["ctx"], len(self._list), self._list_complete
            if live and ctx != Ctx.TARGET_LIST:
                return False, "No target list up. Say search."
            if complete and target_index > n:
                return False, f"Negative, only {n} target{'s' if n != 1 else ''}."
            self._q.put(("select_target", int(target_index)))
            return True, ""
        if action == "WHEEL_SHOW":
            self._q.put(("show", None)); return True, ""
        if action == "WHEEL_HIDE":
            self._q.put(("hide", None)); return True, ""
        if action == "SHOW_HIDE":
            self._q.put(("toggle", None)); return True, ""
        if action in ("ZOOM_IN", "ZOOM_OUT"):
            name, reason = self._resolve_zoom(action == "ZOOM_IN")
            if not name:
                return False, reason
            return self.command(name)
        name = ACTIONS.get(action)
        if not name:
            return False, f"Unknown action {action}."
        return self.command(name)

    def command(self, name: str, confirm: Optional[tuple] = None) -> Tuple[bool, str]:
        cmd = COMMANDS.get(name)
        if cmd is None:
            return False, f"Unknown command {name}."
        if _BINDS[cmd.key] is None:
            what = "fire button" if cmd.key == Key4.MFI else "store target key"
            return False, f"Negative, {what} not bound."
        self.refresh_from_ui()
        with self._lock:
            live, ctx, tads = self._ui_live, self._s["ctx"], self._s["tads"]
        if live and ctx not in LISTS and (cmd.name.startswith("list_") or cmd.ctx in LISTS):
            return False, "No list up."               # the wheel feed shows no list: don't guess
        if cmd.name == "point_search" and tads not in (Tads.SLAVED, Tads.LINKED):
            return False, "Which point? Say point search, then the point, like tango zero one."
        reason = self._check(cmd)
        if reason:
            return False, reason
        if confirm:
            self._q.put(("cmd_confirm", (name, confirm[0], confirm[1])))
        else:
            self._q.put(("cmd", name))
        return True, ""

    def set(self, setting: str, value: str, speech: Optional[str] = None) -> Tuple[bool, str]:
        setting, value = setting.lower().strip(), str(value).upper().strip()
        if setting not in SETTERS:
            return False, "Say again, which setting?"
        cycle = SETTERS[setting][0]
        if value != "NEXT" and value not in cycle:
            return False, f"Unable, {setting.replace('_', ' ')} options are {', '.join(cycle)}."
        if setting == "roe":
            self.refresh_from_ui()
            with self._lock:
                tads = self._s["tads"]
            if tads in (Tads.SLAVED, Tads.LINKED):   # Up-long zooms instead of toggling ROE
                return False, f"Unable, TADS {tads.value.lower()}. Say cease first."
        if setting == "weapon" and value == "NEXT":
            self._q.put(("cmd_confirm", ("cycle_weapon", "weapon", None)))
            return True, ""
        self._q.put(("set", (setting, value, speech)))
        return True, ""

    # ------------------------------------------------------- safety checks
    # Keys whose meaning changes with George's state are refused when the model says they
    # would do something else (e.g. Up-long flips ROE unless the TADS is slaved; MFI-short
    # FIRES when tracking with GUN/MSL). Lenient commands go out regardless.
    _LENIENT = {"consent_to_fire", "toggle_sensor", "store_target", "startup_shutdown",
                "cycle_weapon", "phs_search", "cease"}

    def _check(self, cmd: Cmd) -> str:
        if cmd.name in self._LENIENT or cmd.ctx in LISTS or cmd.name.startswith("list_"):
            return ""
        with self._lock:
            weapon, tads = self._s["weapon"], self._s["tads"]
        if cmd.weapons and weapon not in cmd.weapons:
            return f"Negative, {_WORDS.get(weapon, weapon)} assigned."
        if cmd.tads and tads not in cmd.tads:
            return f"Negative, TADS {tads.value.lower()}."
        return ""

    def _resolve_zoom(self, zoom_in: bool) -> Tuple[Optional[str], str]:
        with self._lock:
            ctx, tads, weapon = self._s["ctx"], self._s["tads"], self._s["weapon"]
        if ctx == Ctx.TARGET_LIST:
            return ("list_zoom_in" if zoom_in else "list_zoom_out"), ""
        if tads in (Tads.SLAVED, Tads.LINKED):
            return ("zoom_in" if zoom_in else "zoom_out"), ""
        if tads == Tads.TRACKING and weapon in ("NO_WPN", "RKT"):
            return "toggle_fov", ""          # MFI-short toggles Narrow / Zoom
        return None, "Unable to zoom from here."

    # ------------------------------------------------------------- wheel sync
    def note_user_toggle(self) -> None:
        """Call from the keyboard hook when the PILOT physically presses Ctrl+V."""
        with self._lock:
            if self._clock() < self._inject_until:
                return                       # that was our own synthetic press
            self._s["wheel_open"] = not self._s["wheel_open"]
            if not self._s["wheel_open"] and self._s["ctx"] in LISTS:
                self._s["ctx"] = Ctx.MAIN    # hiding cancels any list
            self._last_key_t = self._clock() # a pilot-opened wheel isn't auto-hidden at once
            state = "open" if self._s["wheel_open"] else "hidden"
        print(f"[WHEEL] Pilot toggled wheel -> assumed {state}")

    def sync(self, **fields) -> None:
        """Correct the model from telemetry or the pilot, e.g. sync(weapon='MSL', roe='FREE')."""
        with self._lock:
            for k, v in fields.items():
                if k == "ctx":
                    v = Ctx(v)
                elif k == "tads":
                    v = Tads(v)
                if k in self._s:
                    self._s[k] = v

    def snapshot(self) -> dict:
        with self._lock:
            return {k: (v.value if isinstance(v, Enum) else v) for k, v in self._s.items()}

    def describe(self, include_list: bool = True) -> str:
        """Short spoken summary of George's tracked settings (for STATUS)."""
        s = self.snapshot()
        parts = [f"George on {_WORDS.get(s['weapon'], s['weapon'])}, {_WORDS[s['roe']]}"]
        if s["weapon"] == "GUN":
            parts.append(f"burst {s['gun_burst'].lower()}")
        elif s["weapon"] == "MSL":
            parts.append(f"{s['msl_type']} {s['msl_method']} {s['msl_traj']}")
        elif s["weapon"] == "RKT":
            parts.append(f"{s['rkt_type']} quantity {s['rkt_qty'].lower()}")
        text = ", ".join(parts) + "."
        names = self.list_names()
        if names and include_list:
            text += " " + self.list_summary()
        return text

    def list_names(self) -> list:
        with self._lock:
            return list(self._list)

    def list_summary(self, limit: int = 4) -> str:
        """e.g. 'Target list: 1 LARC-V, 2 LARC-V, 3 LARC-V, and more.'"""
        with self._lock:
            names, complete = list(self._list), self._list_complete
        if not names:
            return "No targets."
        shown = ", ".join(f"{i + 1} {n}" for i, n in enumerate(names[:limit]))
        if len(names) > limit:
            more = f", and {len(names) - limit} more" if complete else ", and more"
        else:
            more = "" if complete else ", and more"
        return f"Target list: {shown}{more}."

    # ---------------------------------------------------- live interface feed
    def refresh_from_ui(self) -> None:
        """Correct the model from George's interface as DCS draws it (if the feed is live)."""
        if not self._ui_source:
            return
        try:
            ui, rx = self._ui_source()
        except Exception:
            return
        now = self._clock()
        events = []
        with self._lock:
            self._ui_live = bool(rx) and (now - rx) <= UI_LIVE_S
            if not self._ui_live:
                return
            if rx < self._last_key_t + UI_SETTLE_S:
                return                                   # drawn before our last key landed
            s = self._s
            sig = (bool(ui.get("visible")), tuple(sorted((ui.get("labels") or {}).items())),
                   tuple((ui.get("list") or {}).get("window", ())))
            if sig != self._ui_sig:                      # wheel opened / changed: someone is using it
                self._ui_sig = sig
                self._last_activity_t = now
            s["wheel_open"] = bool(ui.get("visible"))
            lst = ui.get("list") if s["wheel_open"] else None
            if lst:
                kind = Ctx(lst["kind"])
                window = [str(x) for x in lst.get("window", ["", "", "", "", ""])]
                opened = not self._ui_list_open or s["ctx"] != kind
                if opened:
                    s["ctx"], s["cursor"] = kind, 0
                    self._last_window, self._pending_scroll = None, 0
                    if kind == Ctx.TARGET_LIST:
                        self._list, self._list_complete = [], False
                self._update_cursor_locked(window)
                if kind == Ctx.TARGET_LIST:
                    self._learn_list_locked(window)
                    if opened:
                        # Scan to the end of the list on the worker thread, then announce it.
                        self._q.put(("scan_list", None))
                self._ui_list_open = True
            else:
                if s["ctx"] in LISTS:
                    s["ctx"] = Ctx.MAIN
                self._ui_list_open = False
                self._last_window, self._pending_scroll = None, 0
                if s["wheel_open"]:
                    self._main_frame_t = rx
                    self._ui_seen = set()
                    for key in ("weapon", "roe", "gun_burst", "rkt_qty", "msl_method",
                                "msl_traj", "laser", "threat_warn"):
                        if ui.get(key) is not None:
                            s[key] = ui[key]
                            self._ui_seen.add(key)
                    if ui.get("tads"):
                        s["tads"] = Tads(ui["tads"])
                    t = ui.get("type")
                    if t in MSL_TYPE_CYCLE:
                        s["msl_type"] = t
                        self._ui_seen.add("msl_type")
                    elif t in RKT_TYPE_CYCLE:
                        s["rkt_type"] = t
                        self._ui_seen.add("rkt_type")
            sink = self._list_sink is not None and self._ui_list_open and s["ctx"] == Ctx.TARGET_LIST
            names, complete = list(self._list), self._list_complete
        if sink:
            try:
                self._list_sink(names, complete)
            except Exception:
                log.exception("list_sink failed")
        for e, d in events:
            self._emit(e, d)

    def _await_fresh(self, timeout: float = UI_WAIT_S) -> bool:
        """Wait until DCS has drawn a frame after our last key press, then refresh.
        Returns False straight away if the feed isn't live, or after timeout."""
        if not self._ui_source:
            return False
        deadline = self._clock() + timeout
        while True:
            try:
                _, rx = self._ui_source()
            except Exception:
                return False
            now = self._clock()
            with self._lock:
                need = self._last_key_t + UI_SETTLE_S
            if not rx or now - rx > UI_LIVE_S:
                return False
            if rx >= need:
                self.refresh_from_ui()
                return True
            if now >= deadline:
                return False
            time.sleep(0.05)

    def _sync_main_ring(self) -> None:
        """Before a goal-based setting: with the live feed, make sure the main ring is on screen
        and freshly read, because George's weapon / ROE are only shown there."""
        with self._lock:
            live = self._ui_live
        if not live:
            return
        with self._lock:
            ctx, open_ = self._s["ctx"], self._s["wheel_open"]
            fresh = self._main_frame_t >= self._last_key_t + UI_SETTLE_S and open_ and ctx == Ctx.MAIN
        if fresh:
            return
        if not open_:
            self._open_wheel()
        elif ctx in LISTS:
            print(f"[GEORGE] exiting {ctx.value} to read George's settings")
            self._press(_BINDS[Key4.LEFT], SHORT_S)
            self._apply("list_exit")
        self._await_fresh()

    def _update_cursor_locked(self, window: list) -> None:
        """Work out the list cursor from the five-line window (index 2 = highlighted)."""
        s, prev = self._s, self._last_window
        if not window[1] and not window[0]:
            s["cursor"] = 0                              # nothing above: top of the list
        elif not window[0] and window[1]:
            s["cursor"] = 1
        elif self._pending_scroll:
            at_end = prev is not None and window == prev and not window[3] and self._pending_scroll > 0
            if not at_end:
                s["cursor"] = max(0, s["cursor"] + self._pending_scroll)
        elif prev is not None and window != prev:
            if window[:4] == prev[1:]:
                s["cursor"] += 1                         # everything moved up one: scrolled down
            elif window[1:] == prev[:4]:
                s["cursor"] = max(0, s["cursor"] - 1)
        self._pending_scroll = 0
        self._last_window = list(window)

    def _learn_list_locked(self, window: list) -> None:
        c = self._s["cursor"]
        for offset, name in zip((-2, -1, 0, 1, 2), window):
            i = c + offset
            if i < 0 or not name:
                continue
            while len(self._list) <= i:
                self._list.append("")
            self._list[i] = name
        # An empty line below the highlight means the end of the list is in view.
        if not window[3]:
            del self._list[c + 1:]
            self._list_complete = True
        elif not window[4]:
            del self._list[c + 2:]
            self._list_complete = True

    @staticmethod
    def bindings_report() -> list:
        """Human-readable warnings about unbound keys, for start-up."""
        out = []
        if _BINDS[Key4.MFI] is None:
            out.append("GEORGE_KEY_MFI is not set: fire / consent, FOV, sensor, area search "
                       "and point search commands are disabled.")
        if _BINDS[Key4.STORE] is None:
            out.append("GEORGE_KEY_STORE_TARGET is not set: 'store target' is disabled.")
        return out

    def shutdown(self) -> None:
        self._stop.set()
        self._q.put(("stop", None))

    # ================================================================ worker
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                kind, arg = self._q.get(timeout=0.25)
            except queue.Empty:
                self.refresh_from_ui()
                try:
                    self._idle_close()
                except FocusLost:
                    pass                                 # try again once DCS is back in front
                continue
            if kind == "stop":
                break
            self.refresh_from_ui()
            try:
                if kind == "cmd":
                    self._exec(COMMANDS[arg])
                elif kind == "cmd_confirm":
                    name, key, speech = arg
                    self._exec(COMMANDS[name])
                    self._confirm(key, None, speech)
                elif kind == "set":
                    setting, value, speech = arg
                    self._goal(setting, value)
                    self._confirm(SETTERS[setting][1], None if value == "NEXT" else value, speech)
                elif kind == "select_target":
                    self._select_target(arg)
                elif kind == "engage":
                    self._engage(arg)
                elif kind == "area_search":
                    self._area_search(arg)
                elif kind == "point_search_at":
                    self._point_search_at(arg)
                elif kind == "scan_list":
                    self._scan_list()
                elif kind == "show":
                    self._open_wheel()
                elif kind == "hide":
                    self._close_wheel()
                elif kind == "toggle":
                    self._toggle()
            except FocusLost:
                now = self._clock()
                print(f"[GEORGE] DCS lost focus: '{kind}' stopped before sending more keys")
                if now - self._focus_warned_t > 3.0:
                    self._focus_warned_t = now
                    self._emit("rejected", {"reason": "DCS not in focus."})
            except Exception:
                log.exception("George worker error on %s %s", kind, arg)
                print(f"[GEORGE] worker error on {kind} {arg}")

    def _idle_close(self) -> None:
        """Hide the wheel after IDLE_CLOSE_MAIN_S quiet seconds on the main ring, so W/A/S/D are
        yours again. Never while a list is up (hiding cancels it), never within the post-search
        window, and only when the live feed confirms the wheel really is shown."""
        with self._lock:
            if not self._s["wheel_open"] or not self._ui_live:
                return
            now = self._clock()
            if now < self._hold_open_until:
                return
            limit = IDLE_CLOSE_LIST_S if self._s["ctx"] in LISTS else IDLE_CLOSE_MAIN_S
            quiet = now - max(self._last_key_t, self._last_activity_t)
            due = bool(limit) and quiet > limit
        if due:
            print(f"[WHEEL] hiding the wheel after {limit:.0f}s idle")
        if due:
            self._close_wheel()

    # ------------------------------------------------------------ execution
    def _exec(self, cmd: Cmd) -> None:
        with self._lock:
            st = dict(self._s)
        # Pilot intent wins: note disagreements with the model, then send anyway.
        need_ctx = cmd.ctx
        if cmd.name.startswith("list_") and st["ctx"] not in LISTS:
            print(f"[GEORGE] '{cmd.name}': no list in model, assuming Target List is open")
            need_ctx = Ctx.TARGET_LIST
        if need_ctx == Ctx.MAIN and st["ctx"] in LISTS:
            # A main-ring key while a list is up would scroll / select instead: exit first.
            print(f"[GEORGE] '{cmd.name}' needs the main ring, exiting {st['ctx'].value} first")
            self._open_wheel()
            self._press(_BINDS[Key4.LEFT], SHORT_S)
            self._apply("list_exit")
        elif need_ctx and need_ctx != st["ctx"]:
            print(f"[GEORGE] '{cmd.name}' belongs to {need_ctx.value}, model had {st['ctx'].value} - adopting")
            with self._lock:
                self._s["ctx"] = need_ctx
                if need_ctx in LISTS:
                    self._s["cursor"] = 0
        if cmd.weapons and st["weapon"] not in cmd.weapons:
            print(f"[GEORGE] '{cmd.name}' expects weapon {'/'.join(cmd.weapons)}, model has {st['weapon']}")
        if cmd.tads and st["tads"] not in cmd.tads:
            print(f"[GEORGE] '{cmd.name}' expects TADS {'/'.join(t.value for t in cmd.tads)}, "
                  f"model has {st['tads'].value}")

        if cmd.key in _NEEDS_WHEEL:
            self._open_wheel()
        print(f"[WHEEL] {cmd.name} ({cmd.key.value} {'long' if cmd.hold > SHORT_S else 'short'})")
        self._press(_BINDS[cmd.key], cmd.hold)
        self._apply(cmd.name)
        self._emit("executed", {"command": cmd.name, **self.snapshot()})

    def _apply(self, name: str) -> None:
        """Update the model after a key was sent."""
        def nxt(cycle, key):
            cur = self._s[key]
            i = cycle.index(cur) if cur in cycle else -1
            self._s[key] = cycle[(i + 1) % len(cycle)]

        cyc = {"cycle_weapon": (WEAPON_CYCLE, "weapon"), "toggle_roe": (ROE_CYCLE, "roe"),
               "cycle_gun_burst": (GUN_BURST_CYCLE, "gun_burst"),
               "cycle_rocket_qty": (RKT_QTY_CYCLE, "rkt_qty"),
               "cycle_rocket_type": (RKT_TYPE_CYCLE, "rkt_type"),
               "toggle_missile_type": (MSL_TYPE_CYCLE, "msl_type"),
               "toggle_missile_method": (MSL_METHOD_CYCLE, "msl_method"),
               "cycle_missile_traj": (MSL_TRAJ_CYCLE, "msl_traj")}
        with self._lock:
            s = self._s
            if name in cyc:
                nxt(*cyc[name])
            elif name == "toggle_laser":
                s["laser"] = not s["laser"]
            elif name == "toggle_threat_warn":
                s["threat_warn"] = not s["threat_warn"]
            elif name in ("open_search_list", "open_point_list"):
                s["ctx"] = Ctx.SEARCH_LIST if name == "open_search_list" else Ctx.POINT_LIST
                s["cursor"] = 0
            elif name == "list_up":
                if self._ui_live:
                    self._pending_scroll -= 1            # the next frame confirms it
                else:
                    s["cursor"] = max(0, s["cursor"] - 1)
            elif name == "list_down":
                if self._ui_live:
                    self._pending_scroll += 1
                else:
                    s["cursor"] += 1
            elif name in ("list_exit", "select_area_search", "select_point"):
                s["ctx"] = Ctx.MAIN
            elif name == "track_target":
                s["ctx"], s["tads"] = Ctx.MAIN, Tads.TRACKING
            elif name in ("phs_search", "tads_los_search", "start_area_search", "point_search"):
                s["tads"] = Tads.SEARCHING
                self._hold_open_until = self._clock() + SEARCH_HOLD_OPEN_S
            elif name == "slave_to_point":
                s["tads"] = Tads.SLAVED
            elif name == "cease":
                s["tads"], s["ctx"], s["laser"] = Tads.FIXED, Ctx.MAIN, False
        # An area search only presents its Target List if the interface is up within 3 s.
        if name in ("start_area_search", "point_search"):
            self._open_wheel()

    def _goal(self, setting: str, value: str, _top: bool = True) -> None:
        if _top:
            self._sync_main_ring()
        cycle, key, raw, need_weapon = SETTERS[setting]
        if need_weapon:
            with self._lock:
                have = self._s["weapon"]
            if have != need_weapon:
                self._goal("weapon", need_weapon, _top=False)
        if value == "NEXT":
            self._exec(COMMANDS[raw])
            return
        if setting == "missile_traj":            # TRAJ only changes in LOAL
            self._goal("missile_method", "LOAL", _top=False)
        if setting == "missile_method":          # method only changes when TRAJ is DIR
            with self._lock:
                traj = self._s["msl_traj"]
            if traj != "DIR":
                self._goal("missile_traj", "DIR", _top=False)
        if self._closed_loop(key, value, raw, len(cycle)):
            return
        with self._lock:
            cur = self._s[key]
        steps = (cycle.index(value) - cycle.index(cur)) % len(cycle) if cur in cycle else 1
        if steps == 0:
            print(f"[GEORGE] {setting} already {value} (tracked), no press needed")
        for _ in range(steps):
            self._exec(COMMANDS[raw])

    def _closed_loop(self, key: str, value: str, raw: str, n: int) -> bool:
        """With the live feed: press once, read the wheel, repeat until it shows `value`.
        Handles cycles that skip entries (e.g. no rockets loaded: MSL -> NO WPN -> GUN).
        Returns False if the wheel doesn't show this setting, so the caller counts presses."""
        with self._lock:
            live = self._ui_live
        if not live:
            return False
        prev = None
        for attempt in range(n + 2):
            self._await_fresh()
            with self._lock:
                seen = (key in self._ui_seen and self._s["wheel_open"] and self._s["ctx"] == Ctx.MAIN
                        and self._main_frame_t >= self._last_key_t + UI_SETTLE_S)
                cur = self._s[key]
            if not seen:
                return attempt > 0       # lost the reading mid-way: don't fall back to blind presses
            if cur == value:
                return True
            if attempt > 0 and cur == prev:
                print(f"[GEORGE] {key}: wheel didn't change after a press (still {cur}); stopping")
                return True
            prev = cur
            self._exec(COMMANDS[raw])
        print(f"[GEORGE] {key}: couldn't reach {value} (wheel shows {cur})")
        return True

    def _confirm(self, key: str, expected, speech: Optional[str]) -> None:
        """Speak a setting's result only once George's wheel shows it.
        expected=None (a 'next' / toggle) speaks whatever the wheel now shows."""
        with self._lock:
            live = self._ui_live
        if not live:                                      # no feed: old behaviour, unverified
            with self._lock:
                actual = self._s.get(key)
            self._emit("say", {"text": speech or spoken_value(key, actual), "verified": False})
            return
        fresh = self._await_fresh()
        with self._lock:
            actual = self._s.get(key)
            seen = key in self._ui_seen and self._main_frame_t >= self._last_key_t + UI_SETTLE_S
        if not fresh:
            self._emit("rejected", {"reason": "Unable to confirm."})
        elif not seen:                                    # wheel doesn't show this setting
            self._emit("say", {"text": speech or spoken_value(key, actual), "verified": False})
        elif expected is None or actual == expected:
            text = spoken_value(key, actual) if expected is None or not speech else speech
            self._emit("say", {"text": text, "verified": True})
        else:
            print(f"[GEORGE] {key}: wanted {expected}, wheel shows {actual}")
            self._emit("rejected", {"reason": f"Unable. George shows {spoken_value(key, actual).rstrip('.').lower()}."})

    def _scan_list(self, max_steps: int = 20) -> None:
        """A Target List just opened: scroll down one entry at a time (checking each redraw)
        until the end of the list is in view, return to the top, then announce the full list.
        One step at a time so we can never overshoot, even if DCS wraps the list around."""
        with self._lock:
            if self._s["ctx"] != Ctx.TARGET_LIST:
                return
        stepped = 0
        for _ in range(max_steps):
            with self._lock:
                done = self._list_complete or self._s["ctx"] != Ctx.TARGET_LIST
                before = self._s["cursor"]
            if done:
                break
            self._exec(COMMANDS["list_down"])
            stepped += 1
            if not self._await_fresh():
                break                                    # feed stalled: announce what we have
            with self._lock:
                if self._s["cursor"] == before:          # did not move: treat as the end
                    del self._list[before + 1:]
                    self._list_complete = True
                    break
        with self._lock:
            cursor = self._s["cursor"] if self._s["ctx"] == Ctx.TARGET_LIST else 0
        if stepped and cursor:
            for _ in range(cursor):                      # back to the top, as George left it
                self._exec(COMMANDS["list_up"])
            self._await_fresh()
        with self._lock:
            names, complete = list(self._list), self._list_complete
        if self._list_sink:
            try:
                self._list_sink(names, complete)
            except Exception:
                log.exception("list_sink failed")
        print(f"[GEORGE] Target List scanned: {len(names)} entr{'y' if len(names) == 1 else 'ies'}"
              + ("" if complete else " (end not reached)"))
        self._emit("list_opened", {"names": names, "complete": complete})

    def _move_cursor(self, idx: int) -> Tuple[bool, str]:
        """Move the Target List cursor to entry idx (1 = top). With the live interface feed the
        position is checked against what DCS draws. Returns (on target, entry name)."""
        with self._lock:
            if self._s["ctx"] != Ctx.TARGET_LIST:
                print("[GEORGE] target list: none in model, assuming it just opened")
                self._s["ctx"], self._s["cursor"] = Ctx.TARGET_LIST, 0
        for attempt in range(3):
            with self._lock:
                delta = (idx - 1) - self._s["cursor"]
                live = self._ui_live
            if delta == 0:
                break
            name = "list_down" if delta > 0 else "list_up"
            for _ in range(abs(delta)):
                self._exec(COMMANDS[name])
            if not live or not self._await_fresh():
                break                                    # no feed: trust our own count
        with self._lock:
            live, cursor = self._ui_live, self._s["cursor"]
            name = self._list[cursor] if cursor < len(self._list) else ""
        if live and cursor != idx - 1:
            print(f"[GEORGE] target {idx}: list cursor ended on #{cursor + 1}")
            return False, name
        return True, name

    # ------------------------------------------------- searches by name
    def _window(self) -> list:
        """Current five-line list window from the live feed (index 2 = highlighted)."""
        if not self._ui_source:
            return []
        try:
            ui, _ = self._ui_source()
        except Exception:
            return []
        lst = ui.get("list") if ui.get("visible") else None
        return [str(x) for x in lst.get("window", [])] if lst else []

    def _find_in_list(self, match: Callable[[str], bool], max_steps: int = 40) -> bool:
        """Move the highlight of the open list onto the first entry where match(name) is True.
        Looks at all five visible lines each step; scrolls down to the end, then up to the top."""
        direction, went_down = "list_down", False
        for _ in range(max_steps):
            self._await_fresh()
            window = self._window()
            if len(window) != 5:
                return False
            for offset, name in zip((-2, -1, 0, 1, 2), window):
                if name and match(name):
                    step = "list_down" if offset > 0 else "list_up"
                    for _ in range(abs(offset)):
                        self._exec(COMMANDS[step])
                    self._await_fresh()
                    mid = self._window()
                    return len(mid) == 5 and bool(mid[2]) and match(mid[2])
            if direction == "list_down" and not window[3]:
                direction, went_down = "list_up", True        # bottom reached: try upwards
            elif direction == "list_up" and not window[1]:
                return False                                  # top reached too: not in this list
            self._exec(COMMANDS[direction])
        return False

    def _no_weapon_main_ring(self) -> bool:
        """The Search and Point lists only open with no weapon assigned to George."""
        self._sync_main_ring()
        with self._lock:
            weapon = self._s["weapon"]
        if weapon != "NO_WPN":
            self._goal("weapon", "NO_WPN", _top=False)
            self._await_fresh()
        with self._lock:
            return self._s["weapon"] == "NO_WPN"

    def _open_list(self, name: str, kind: "Ctx") -> bool:
        """Press the key that opens a list, then confirm on the wheel that it opened."""
        self._exec(COMMANDS[name])
        self._await_fresh()
        with self._lock:
            return self._s["ctx"] == kind and self._ui_list_open

    def _leave_list(self) -> None:
        with self._lock:
            open_ = self._ui_list_open and self._s["ctx"] in LISTS
        if open_:
            self._exec(COMMANDS["list_exit"])

    def _area_search(self, code: str) -> None:
        if not self._no_weapon_main_ring():
            self._emit("rejected", {"reason": "Unable, George won't drop his weapon."})
            return
        if not self._open_list("open_search_list", Ctx.SEARCH_LIST):
            self._emit("rejected", {"reason": "Unable, search list didn't open."})
            return
        key = code.replace(" ", "")
        if not self._find_in_list(lambda n: n.upper().replace(" ", "") == key):
            self._leave_list()
            self._emit("rejected", {"reason": f"Unable, no {AREA_SEARCHES[code]} search available."})
            return
        self._exec(COMMANDS["select_area_search"])
        self._await_fresh()
        self._exec(COMMANDS["start_area_search"])
        self._emit("say", {"text": f"Searching {AREA_SEARCHES[code]}.", "verified": True})

    def _point_search_at(self, point: str) -> None:
        """Point List -> highlight `point` (e.g. T07) -> select -> slave TADS -> search there.
        If it isn't in the list's current filter, try the filter its letter belongs to."""
        if not self._no_weapon_main_ring():
            self._emit("rejected", {"reason": "Unable, George won't drop his weapon."})
            return
        if not self._open_list("open_point_list", Ctx.POINT_LIST):
            self._emit("rejected", {"reason": "Unable, point list didn't open."})
            return
        match = lambda n: n.upper().replace(" ", "").startswith(point)
        found = self._find_in_list(match)
        if not found:
            filters = (["point_filter_tgt_thrt"] if point[0] == "T"
                       else ["point_filter_ctrlm_wpthz", "point_filter_ctrlm_wpthz"])
            for f in filters:
                self._exec(COMMANDS[f])
                self._await_fresh()
                if self._find_in_list(match):
                    found = True
                    break
        if not found:
            self._leave_list()
            self._emit("rejected", {"reason": f"Unable, no point {self._spell_point(point)}."})
            return
        self._exec(COMMANDS["select_point"])
        self._await_fresh()
        self._exec(COMMANDS["slave_to_point"])         # MFI-short: TADS slaved to the point
        self._await_fresh()
        self._exec(COMMANDS["point_search"])           # MFI-short again: search around it
        self._emit("say", {"text": f"Point search, {self._spell_point(point)}.", "verified": True})

    @staticmethod
    def _spell_point(point: str) -> str:
        letters = {"T": "tango", "C": "charlie", "W": "whiskey", "H": "hotel"}
        digits = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
        return f"{letters.get(point[0], point[0])} " + " ".join(digits[int(d)] for d in point[1:])

    def _select_target(self, idx: int) -> None:
        """Track entry idx of George's Target List."""
        ok, name = self._move_cursor(idx)
        if not ok:
            self._emit("rejected", {"reason": f"Unable to reach target {idx}."})
            return
        self._exec(COMMANDS["track_target"])
        self._emit("tracking", {"index": idx, "name": name})

    def _engage(self, indices: list) -> None:
        """Multi-target engagement (manual p.658): add each entry to George's shoot list with
        MFI-short, put the cursor back on the first one, then Right-short. George then engages
        them in that order, firing per his ROE."""
        names = []
        for idx in indices:
            ok, name = self._move_cursor(idx)
            if not ok:
                self._emit("rejected", {"reason": f"Unable to reach target {idx}."})
                return
            self._exec(COMMANDS["shoot_list_toggle"])
            names.append(name)
        ok, _ = self._move_cursor(indices[0])
        if not ok:
            self._emit("rejected", {"reason": f"Unable to reach target {indices[0]}."})
            return
        self._exec(COMMANDS["track_target"])
        self._emit("engaging", {"indices": list(indices), "names": names})

    # ------------------------------------------------------------- keyboard
    def _toggle(self) -> None:
        with self._lock:
            self._inject_until = self._clock() + 0.5 + SHORT_S
        self._press(_TOGGLE, SHORT_S)
        with self._lock:
            self._s["wheel_open"] = not self._s["wheel_open"]
            if not self._s["wheel_open"] and self._s["ctx"] in LISTS:
                self._s["ctx"] = Ctx.MAIN    # hiding the interface cancels any list
            opened = self._s["wheel_open"]
        if opened:
            time.sleep(OPEN_SETTLE_S)

    def _open_wheel(self) -> None:
        with self._lock:
            if self._s["wheel_open"]:
                return
        self._toggle()

    def _close_wheel(self) -> None:
        with self._lock:
            if not self._s["wheel_open"]:
                return
        self._toggle()

    def _press(self, combo: tuple, hold_s: float) -> None:
        if not self._focus_check():
            raise FocusLost()
        mods, main = combo[:-1], combo[-1]
        try:
            for m in mods:
                self._kb.press(m)
            self._kb.press(main)
            time.sleep(hold_s)
        finally:
            self._kb.release(main)
            for m in reversed(mods):
                self._kb.release(m)
        time.sleep(GAP_S)
        with self._lock:
            self._last_key_t = self._clock()

    def _emit(self, event: str, data: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event, data)
            except Exception:
                log.exception("George on_event callback failed")
