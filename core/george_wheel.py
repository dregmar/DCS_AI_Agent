"""
George AI interface-wheel driver for the DCS AH-64D (CPG George).

Source for the layout: the community AH-64D guide at gamepressure.com (Nov 2023). It may be
out of date against current DCS builds, so every mapping lives in WHEEL_STEPS below and is
easy to change. Controls:
    Ctrl+V show/hide the wheel     W up   S down   A left   D right
    Each direction has a SHORT press and a LONG press (held > 0.5 s).

CPG wheel, as documented:
  Weapon-control screen (right after the wheel opens):
      Left  short  change active weapon  GUN / MSL / RKT
      Left  long   change Hellfire type (SAL/RF) or rocket type
  Target-designation screen (after a weapon is chosen):
      Up    short  slave TADS to the pilot's helmet line of sight and search
      Up    long   toggle ROE: weapons hold / weapons free
      Right short  Hellfire: LOBL<->LOAL | Rockets: QTY | Gun: BURST   (cycles one step)
      Right long   Hellfire trajectory (Hi / Lo / Direct)               (cycles one step)
      Down  short  hold fire, stop tracking, slave TADS forward
      Down  long   new search at the current TADS line of sight
      Left  short  cancel target-list selection
  Target list:
      Up short moves selection up, Right short selects (and fires if Weapons Free).

LIMITS YOU MUST KNOW ABOUT
  * The wheel has no readback. Every "cycle" and "toggle" is a blind single step; George can
    say what he did, never what the setting is now.
  * The same key does different things on different screens (Left short = change weapon on
    the first screen, cancel list on the second). Context is DCS's, not ours.
  * Wheel visibility is TRACKED, not observed: we count our own Ctrl+V presses and the
    pilot's keyboard Ctrl+V presses. A HOTAS button bound inside DCS can't be seen, so if the
    wheel is toggled that way our belief drifts. "toggle wheel" re-syncs by flipping it.
"""
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import config as _cfg

# Read from config.py, with defaults so an older config.py can't stop George from starting.
KEY_DELAY = getattr(_cfg, "KEY_DELAY", 0.08)
WHEEL_SHORT_HOLD_SEC = getattr(_cfg, "WHEEL_SHORT_HOLD_SEC", 0.15)
WHEEL_LONG_HOLD_SEC = getattr(_cfg, "WHEEL_LONG_HOLD_SEC", 0.70)     # DCS: held > 0.5 s = long
WHEEL_OPEN_SETTLE_SEC = getattr(_cfg, "WHEEL_OPEN_SETTLE_SEC", 0.20)
WHEEL_AUTO_HIDE_SEC = getattr(_cfg, "WHEEL_AUTO_HIDE_SEC", 0.0)      # 0 = leave the wheel open

UP, DOWN, LEFT, RIGHT = "w", "s", "a", "d"
TOGGLE_VK = 0x56            # 'V'; pynput reports Ctrl+V as a control char, so match on vk


@dataclass(frozen=True)
class WheelStep:
    key: str
    long: bool = False


# action name -> single wheel step. (SELECT_TARGET_INDEX and the show/hide actions are
# sequences, handled in GeorgeWheel.run.)
WHEEL_STEPS: Dict[str, WheelStep] = {
    "CYCLE_WEAPON":       WheelStep(LEFT),
    "CYCLE_WEAPON_TYPE":  WheelStep(LEFT, long=True),
    "CANCEL_TARGET_LIST": WheelStep(LEFT),
    "SEARCH_LOS":         WheelStep(UP),
    "SLAVE_TADS":         WheelStep(UP),
    "TOGGLE_ROE":         WheelStep(UP, long=True),
    "CYCLE_FIRE_MODE":    WheelStep(RIGHT),               # burst / rocket qty / LOBL<->LOAL
    "CYCLE_TRAJECTORY":   WheelStep(RIGHT, long=True),    # Hellfire Hi / Lo / Direct
    "HOLD_FIRE":          WheelStep(DOWN),
    "NEW_SEARCH":         WheelStep(DOWN, long=True),
}

WHEEL_ACTIONS = set(WHEEL_STEPS) | {"SHOW_HIDE", "WHEEL_SHOW", "WHEEL_HIDE", "SELECT_TARGET_INDEX"}


class GeorgeWheel:
    """Drives the wheel. run() is for the voice-worker thread only; note_user_toggle() is
    safe to call from the keyboard hook thread (it never waits on a key sequence)."""

    def __init__(self, keyboard, ctrl_key,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._kb = keyboard
        self._ctrl = ctrl_key
        self._sleep = sleep
        self._clock = clock
        self._seq_lock = threading.RLock()      # one key sequence at a time
        self._state_lock = threading.Lock()     # guards the two fields below, held for microseconds
        self._visible = False                   # ASSUMPTION: wheel starts hidden
        self._inject_until = 0.0                # ignore our own Ctrl+V echoing back via the hook
        self._last_used = 0.0

    # ---- state ----
    @property
    def visible(self) -> bool:
        with self._state_lock:
            return self._visible

    def note_user_toggle(self) -> None:
        """Call from the key hook when the pilot physically presses Ctrl+V."""
        with self._state_lock:
            if self._clock() < self._inject_until:
                return                          # that was our own synthetic press
            self._visible = not self._visible
            self._last_used = self._clock()     # a pilot-opened wheel must not be auto-hidden at once
        print(f"[WHEEL] Pilot toggled wheel -> assumed {'open' if self.visible else 'hidden'}")

    # ---- low-level keys ----
    def _toggle(self) -> None:
        with self._state_lock:
            self._inject_until = self._clock() + 0.5
        self._kb.press(self._ctrl)
        self._kb.press("v")
        self._sleep(0.1)
        self._kb.release("v")
        self._kb.release(self._ctrl)
        self._sleep(KEY_DELAY)
        with self._state_lock:
            self._visible = not self._visible

    def _press(self, step: WheelStep) -> None:
        hold = WHEEL_LONG_HOLD_SEC if step.long else WHEEL_SHORT_HOLD_SEC
        self._kb.press(step.key)
        try:
            self._sleep(hold)
        finally:
            self._kb.release(step.key)
        self._sleep(KEY_DELAY)

    # ---- visibility ----
    def ensure_open(self) -> None:
        if not self.visible:
            self._toggle()
            self._sleep(WHEEL_OPEN_SETTLE_SEC)

    def ensure_closed(self) -> None:
        if self.visible:
            self._toggle()

    # ---- public API ----
    def run(self, action: str, target_index: Optional[int] = None) -> bool:
        """Executes one wheel action. Returns False if the action isn't a wheel action."""
        if action not in WHEEL_ACTIONS:
            return False
        with self._seq_lock:
            print(f"[WHEEL] {action}" + (f" #{target_index}" if target_index else "")
                  + f" (wheel {'open' if self.visible else 'hidden'})")
            if action == "SHOW_HIDE":
                self._toggle()
            elif action == "WHEEL_SHOW":
                self.ensure_open()
            elif action == "WHEEL_HIDE":
                self.ensure_closed()
            elif action == "SELECT_TARGET_INDEX":
                self._select_target(target_index)
            else:
                self.ensure_open()
                self._press(WHEEL_STEPS[action])
            self._last_used = self._clock()
        return True

    def _select_target(self, idx: Optional[int]) -> None:
        if not idx or idx < 1:
            return
        self.ensure_open()
        # NOTE: kept exactly as the original code: slot 1 presses Up, slot N>=2 presses Down
        # (N-1) times, then Right short confirms. Verify against the real list cursor
        # (possible off-by-one; the guide documents Up/Right for the list, not Down).
        if idx == 1:
            self._press(WheelStep(UP))
        else:
            for _ in range(idx - 1):
                self._press(WheelStep(DOWN))
        self._press(WheelStep(RIGHT))

    # ---- optional: hide the wheel after N idle seconds (WHEEL_AUTO_HIDE_SEC, 0 = off) ----
    def start_autohide(self) -> None:
        if WHEEL_AUTO_HIDE_SEC <= 0:
            return

        def loop():
            while True:
                time.sleep(0.5)
                if not self.visible or self._clock() - self._last_used < WHEEL_AUTO_HIDE_SEC:
                    continue
                if self._seq_lock.acquire(blocking=False):
                    try:
                        if self.visible and self._clock() - self._last_used >= WHEEL_AUTO_HIDE_SEC:
                            self._toggle()
                    finally:
                        self._seq_lock.release()

        threading.Thread(target=loop, daemon=True, name="george-wheel-autohide").start()