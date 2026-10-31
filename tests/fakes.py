"""
tests/fakes.py
Test doubles for the George AI middleware. IMPORT THIS FIRST in every test module: it sets fast
key timings in config before any project module reads them.

  * FakeKeyboard  - records presses; never touches your real keyboard.
  * FakeGeorge    - a simulated DCS George: reacts to the keys like the real wheel (main ring,
                    Target / Search / Point lists, shoot list, burst, MFI) and "draws" indicator-26
                    text, which goes through the real parser into the real state store, a few
                    tens of milliseconds later, like the exporter would.
  * wait_until    - poll a condition instead of fixed sleeps.
"""
import threading
import time

import config

# Fast, deterministic timings for tests (the real values are in config.py).
config.GEORGE_SHORT_PRESS_S = 0.02
config.GEORGE_LONG_PRESS_S = 0.12
config.GEORGE_EXTRA_LONG_PRESS_S = 0.2
config.GEORGE_INTER_KEY_GAP_S = 0.02
config.GEORGE_OPEN_SETTLE_S = 0.05
config.GEORGE_UI_SETTLE_S = 0.2
config.GEORGE_KEY_MFI = ("f",)
config.GEORGE_KEY_STORE_TARGET = ("g",)
config.GEORGE_IDLE_CLOSE_MAIN_S = None          # tests that need auto-hide set it themselves
config.TRAINING_LIST_PATH = None                # never read or write the PC's logs/training_groups.json
config.OLYMPUS_GM_PASSWORD = ""                 # tests never use a real Game Master login
config.INSTRUCTOR_DATA_DIR = None               # never read or write the PC's data/instructor (memory only)

from core.state import state_store              # noqa: E402  (after the config overrides)
from core.george_ui import parse_indication     # noqa: E402

SEP = "-" * 41
LONG_THRESHOLD = 0.07                            # between the short and long test press times


def keyname(k) -> str:
    """'w', 'ctrl_l', ... for both real pynput keys and plain strings."""
    c = getattr(k, "char", None)
    if c:
        return c
    n = getattr(k, "name", None)
    if n:
        return n
    return str(k).strip("<>").replace("Key.", "")


def wait_until(cond, timeout=5.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class FakeKeyboard:
    def __init__(self, george: "FakeGeorge"):
        self.george = george
        self.log = []

    def press(self, k):
        name = keyname(k)
        self.log.append(name)
        self.george.press(name)

    def release(self, k):
        self.george.release(keyname(k))


class FakeGeorge:
    """Simulated George CPG wheel. weapons = the weapons he can cycle through (loadout)."""

    def __init__(self, weapons=("NO WPN", "GUN", "MSL", "RKT"), targets=None):
        self.lock = threading.RLock()
        self.visible = False
        self.weapons = list(weapons)
        self.weapon = "NO WPN"
        self.roe = "HOLD FIRE"
        self.tads = "FIXED"
        self.burst = "20"
        self.targets = targets or ["ZSU-23-4", "LARC-V", "LARC-V", "T-72B", "LARC-V", "BMP-2"]
        self.kind = None            # None / "TGT" / "SEARCH" / "POINT"
        self.list = []
        self.cursor = 0
        self.shoot = []
        self.sel_area = None
        self.sel_point = None
        self.filter = "TGT"
        self.points = {"TGT": ["T05", "T06", "T07", "T08"], "CTRLM": ["C01", "C02"],
                       "WPTHZ": ["W01", "W02", "W03", "H01"]}
        self.burst_responds = True
        self.events = []            # what George "did": TRACK x, ENGAGE a,b, AREA SEARCH x ...
        self._down = {}
        self._mods = set()
        self._stop = False
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # ------------------------------------------------------------ drawing
    def text(self) -> str:
        with self.lock:
            if not self.visible:
                return f"{SEP}\n#1#\n\nchildren are {{\n}}\n"

            def el(n, v):
                return f"{SEP}\n{n}\n{v}\n"
            out = f"{SEP}\n#1#\n\nchildren are {{\n}}\n{SEP}\nweap_contol\n\nchildren are {{\n"
            if not self.kind:
                far_right = ("BRST\n" + self.burst) if self.weapon == "GUN" else "POINT"
                out += (el("weapon_center", "POINT\n-----\nAREA") + el("weapon_up", "PHS SEARCH")
                        + el("weapon_right", "NOT\nLASING") + el("weapon_down", self.tads)
                        + el("weapon_left", self.weapon.replace(" ", "\n")) + el("weapon_far_up", self.roe)
                        + el("weapon_far_right", far_right) + el("weapon_far_down", "THREAT WARN")
                        + el("weapon_far_left", "FWD\nAREA") + "}\n")
                return out
            up, right = {"TGT": ("PREV TGT", "TRACK\nTGT"), "SEARCH": ("PREV SEARCH", "SELECT\nSEARCH"),
                         "POINT": ("PREV POINT", "SELECT\nPOINT")}[self.kind]
            out += (el("weapon_center", "SELECT\n-----\nFLIR") + el("weapon_up", up) + el("weapon_right", right)
                    + el("weapon_left", "EXIT\nLIST") + "}\n")
            out += f"{SEP}\nmiddle_list_holder\n\nchildren are {{\n"
            at = lambda i: self.list[i] if 0 <= i < len(self.list) else ""
            c = self.cursor
            out += f"{SEP}\nmiddle_list_text\n{at(c)}\nchildren are {{\n"
            for n, i in (("upper_upper_list_text", c - 2), ("upper_list_text", c - 1),
                         ("lower_list_text", c + 1), ("lower_lower_list_text", c + 2)):
                out += f"{SEP}\n{n}\n{at(i)}\nchildren are {{\n}}\n"
            return out + f"{SEP}\nmiddle_list_arrow\n\n}}\n}}\n"

    def publish(self, delay=0.04):
        txt, vis = self.text(), self.visible
        threading.Timer(delay, lambda: state_store.update_george_ui(
            parse_indication(txt) if vis else {"visible": False})).start()

    def _heartbeat(self):
        while not self._stop:
            self.publish(0)
            time.sleep(0.3)

    def stop(self):
        self._stop = True

    # ------------------------------------------------------------- keys
    def press(self, k):
        with self.lock:
            self._down[k] = time.monotonic()
            if k == "ctrl_l":
                self._mods.add(k)

    def release(self, k):
        with self.lock:
            held = time.monotonic() - self._down.pop(k, time.monotonic())
            long = held > LONG_THRESHOLD
            if k == "ctrl_l":
                self._mods.discard(k)
                return
            if k == "v" and "ctrl_l" in self._mods:
                self.visible = not self.visible
                if not self.visible:
                    self.kind, self.list = None, []
            elif k == "f":
                self._mfi(long)
            elif self.visible and self.kind:
                self._list_key(k, long)
            elif self.visible:
                self._ring_key(k, long)
        self.publish()

    def _ring_key(self, k, long):
        if k == "w" and not long:
            self.tads = "SEARCHING"
            threading.Timer(0.25, self._targets_found).start()
        elif k == "w" and long:
            self.roe = "FREE FIRE" if self.roe == "HOLD FIRE" else "HOLD FIRE"
        elif k == "a" and not long:
            i = self.weapons.index(self.weapon)
            self.weapon = self.weapons[(i + 1) % len(self.weapons)]
        elif k == "a" and long and self.weapon == "NO WPN":
            self.kind, self.list, self.cursor = "SEARCH", ["FWD AREA", "PHS AREA", "FCR TGTS", "OWN PFZ", "ACTV PFZ"], 0
        elif k == "d" and long and self.weapon == "NO WPN":
            self.kind, self.filter = "POINT", "TGT"
            self.list = list(self.points["TGT"])
            self.cursor = len(self.list) - 1               # opens on the most recent point
        elif k == "d" and not long and self.weapon == "GUN" and self.burst_responds:
            seq = ["10", "20", "50", "100", "ALL"]
            self.burst = seq[(seq.index(self.burst) + 1) % len(seq)]
        elif k == "s" and not long:
            self.tads = "FIXED"

    def _targets_found(self):
        with self.lock:
            if self.visible and not self.kind:
                self.kind, self.list, self.cursor, self.shoot = "TGT", list(self.targets), 0, []
        self.publish(0)

    def _list_key(self, k, long):
        if k == "s" and not long:
            self.cursor = min(self.cursor + 1, len(self.list) - 1)
        elif k == "w" and not long:
            self.cursor = max(0, self.cursor - 1)
        elif k == "a" and not long:
            self.kind, self.list = None, []
        elif k == "d" and not long:
            name = self.list[self.cursor]
            if self.kind == "TGT":
                if self.shoot and self.cursor in self.shoot:
                    self.events.append("ENGAGE " + ",".join(self.list[i] for i in self.shoot))
                else:
                    self.events.append("TRACK " + name)
                self.tads = "TRACKING"
            elif self.kind == "SEARCH":
                self.sel_area = name
            else:
                self.sel_point = name
            self.kind, self.list, self.shoot = None, [], []
        elif self.kind == "POINT" and k == "a" and long:
            self.filter = "WPTHZ" if self.filter == "CTRLM" else "CTRLM"
            self.list, self.cursor = list(self.points[self.filter]), 0
        elif self.kind == "POINT" and k == "d" and long:
            self.filter = "TGT"
            self.list, self.cursor = list(self.points["TGT"]), len(self.points["TGT"]) - 1

    def _mfi(self, long):
        if self.kind == "TGT" and not long:
            if self.cursor in self.shoot:
                self.shoot.remove(self.cursor)
            else:
                self.shoot.append(self.cursor)
        elif long and self.sel_area and not self.kind:
            self.events.append("AREA SEARCH " + self.sel_area)
            self.tads = "SEARCHING"
        elif not long and self.sel_point and self.tads == "FIXED":
            self.events.append("SLAVE " + self.sel_point)
            self.tads = "SLAVED"
        elif not long and self.tads == "SLAVED":
            self.events.append("POINT SEARCH " + self.sel_point)
            self.tads = "SEARCHING"
        else:
            self.events.append("MFI " + ("long" if long else "short"))


def send_telemetry(contacts=None, ammo=(8, 38, 300), hdg=90.0):
    state_store.update_telemetry({"alt_ft": 100, "speed_kts": 0, "hdg": hdg,
                                  "contacts": contacts or [], "ammo": list(ammo)})


DEFAULT_CONTACTS = [
    {"id": 11, "type": "LARC-V", "dist_m": 2100, "brg": 150},
    {"id": 12, "type": "LARC-V", "dist_m": 2600, "brg": 180},
    {"id": 13, "type": "ZSU-23-4 Shilka", "dist_m": 4300, "brg": 90},
    {"id": 14, "type": "LARC-V", "dist_m": 5000, "brg": 0},
    {"id": 15, "type": "T-72B", "dist_m": 5200, "brg": 45},
]


def pipeline_module():
    """tests/test_pipeline.py, imported once whatever name it was loaded under. `python -m unittest
    discover tests` loads it as "test_pipeline", `-s tests -t .` as "tests.test_pipeline"; importing it
    a second time under the other name would point main's speech at a second SPOKEN list (29 Oct:
    seven failures on the PC)."""
    import importlib
    import sys
    for name in ("test_pipeline", "tests.test_pipeline"):
        if name in sys.modules:
            return sys.modules[name]
    return importlib.import_module("tests.test_pipeline")


def isolate_unit_types():
    """Tests never read or write the unit types DCS taught this PC (data/unit_types.json): the
    threat ranking and SEAD target tests assume only the built-in name rules (29 Oct: the PC's
    learned types made "Kub 2P25 ln" a launcher, correctly, and failed two tests)."""
    from common import unit_types
    with unit_types.TYPES._lock:
        unit_types.TYPES.cache_file = None
        unit_types.TYPES._by_key.clear()


isolate_unit_types()
