"""
core/joystick_ptt.py
Push-to-talk on a joystick / throttle button, alongside (or instead of) the keyboard.

Runs one daemon thread that polls the button ~100x per second and calls on_down() / on_up() on
each change. It never blocks the PTT, telemetry or George threads, and it keeps working while
DCS has focus. The device is found by NAME first (JOY_NAME_CONTAINS), then by JOY_DEVICE_INDEX;
if it isn't plugged in yet, or gets unplugged, it keeps looking every few seconds.

Two ways to read the stick ("backends"):
  * pygame   - best: real device names, more than 32 buttons. Needs `pip install pygame-ce`
               (or pygame). Told to keep reading in the background while DCS has focus.
  * winmm    - Windows' built-in joystick API through ctypes; nothing to install. Sees the
               first 32 buttons and up to 16 devices; names come from the registry.
JOY_BACKEND = "auto" uses pygame if it's installed, otherwise winmm.

joy_probe.py (project folder) lists devices with the same backends, so its [index] numbers match.
"""
import os
import threading
import time
from typing import Callable, List, Optional, Tuple


# ============================================================== backends
class PygameBackend:
    """SDL joystick through pygame. Must be used from ONE thread (the poller's)."""
    name = "pygame"

    def __init__(self):
        # Keep receiving button changes while DCS (another window) has focus.
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame                                   # ImportError -> caller falls back
        self.pg = pygame
        self._ready = False

    def _init(self):
        if not self._ready:
            # pygame only updates joystick state while its event queue is pumped, and pumping
            # needs the display module initialised. No window is ever created.
            self.pg.display.init()
            self.pg.joystick.init()
            self._ready = True

    def devices(self) -> List[Tuple[int, str, int]]:
        """[(index, name, button count)]. Re-scans so newly plugged devices appear."""
        self._init()
        self.pg.joystick.quit()
        self.pg.joystick.init()
        out = []
        for i in range(self.pg.joystick.get_count()):
            try:
                j = self.pg.joystick.Joystick(i)
                out.append((i, j.get_name(), j.get_numbuttons()))
            except Exception:
                pass
        return out

    def open(self, index: int):
        self._init()
        j = self.pg.joystick.Joystick(index)
        if hasattr(j, "init"):
            j.init()
        return j

    def button(self, handle, number: int) -> Optional[bool]:
        """number is 1-based. None = device gone."""
        try:
            # Pump AND empty the event queue: we only read button state, and an un-emptied
            # queue would collect every stick event for the whole flight.
            self.pg.event.clear()
            if hasattr(handle, "get_init") and not handle.get_init():
                return None
            if number > handle.get_numbuttons():
                return False
            return bool(handle.get_button(number - 1))
        except Exception:
            return None


class WinmmBackend:
    """Windows multimedia joystick API (joyGetPosEx). No extra packages."""
    name = "winmm"
    MAX_BUTTONS = 32

    def __init__(self):
        if os.name != "nt":
            raise ImportError("winmm is Windows-only")
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes

        class JOYCAPSW(ctypes.Structure):
            _fields_ = ([("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                         ("szPname", wintypes.WCHAR * 32)]
                        + [(n, wintypes.UINT) for n in (
                            "wXmin", "wXmax", "wYmin", "wYmax", "wZmin", "wZmax", "wNumButtons",
                            "wPeriodMin", "wPeriodMax", "wRmin", "wRmax", "wUmin", "wUmax",
                            "wVmin", "wVmax", "wCaps", "wMaxAxes", "wNumAxes", "wMaxButtons")]
                        + [("szRegKey", wintypes.WCHAR * 32), ("szOEMVxD", wintypes.WCHAR * 260)])

        class JOYINFOEX(ctypes.Structure):
            _fields_ = [(n, wintypes.DWORD) for n in (
                "dwSize", "dwFlags", "dwXpos", "dwYpos", "dwZpos", "dwRpos", "dwUpos", "dwVpos",
                "dwButtons", "dwButtonNumber", "dwPOV", "dwReserved1", "dwReserved2")]

        self.JOYCAPSW, self.JOYINFOEX = JOYCAPSW, JOYINFOEX
        self.winmm = ctypes.WinDLL("winmm")
        self.winmm.joyGetNumDevs.restype = wintypes.UINT

    def _caps(self, i: int):
        caps = self.JOYCAPSW()
        if self.winmm.joyGetDevCapsW(i, self.ct.byref(caps), self.ct.sizeof(caps)) != 0:
            return None
        return caps

    def _oem_name(self, i: int, caps) -> str:
        """The product name Windows shows in 'Game Controllers' (registry), else szPname."""
        try:
            import winreg
            base = rf"System\CurrentControlSet\Control\MediaResources\Joystick\{caps.szRegKey}\CurrentJoystickSettings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
                oem, _ = winreg.QueryValueEx(k, f"Joystick{i + 1}OEMName")
            path = rf"System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM\{oem}"
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, path) as k:
                        name, _ = winreg.QueryValueEx(k, "OEMName")
                        if name:
                            return str(name)
                except OSError:
                    continue
        except OSError:
            pass
        return caps.szPname

    def _connected(self, i: int) -> bool:
        info = self.JOYINFOEX()
        info.dwSize = self.ct.sizeof(info)
        info.dwFlags = 0x80                              # JOY_RETURNBUTTONS
        return self.winmm.joyGetPosEx(i, self.ct.byref(info)) == 0

    def devices(self) -> List[Tuple[int, str, int]]:
        out = []
        for i in range(min(int(self.winmm.joyGetNumDevs()), 16)):
            caps = self._caps(i)
            if caps is None or not self._connected(i):
                continue
            out.append((i, self._oem_name(i, caps), int(caps.wNumButtons)))
        return out

    def open(self, index: int):
        if not self._connected(index):
            raise OSError(f"no joystick at winmm id {index}")
        return index

    def button(self, handle, number: int) -> Optional[bool]:
        if number > self.MAX_BUTTONS:
            return False
        info = self.JOYINFOEX()
        info.dwSize = self.ct.sizeof(info)
        info.dwFlags = 0x80                              # JOY_RETURNBUTTONS
        if self.winmm.joyGetPosEx(handle, self.ct.byref(info)) != 0:
            return None                                  # unplugged
        return bool(info.dwButtons & (1 << (number - 1)))


def make_backend(preference: str = "auto"):
    """Returns (backend, note). backend is None if nothing can read joysticks here."""
    order = {"pygame": [PygameBackend], "winmm": [WinmmBackend]}.get(
        preference.lower(), [PygameBackend, WinmmBackend])
    notes = []
    for cls in order:
        try:
            return cls(), "; ".join(notes)
        except Exception as e:
            notes.append(f"{cls.name} unavailable ({type(e).__name__}: {e})")
    return None, "; ".join(notes)


# ============================================================ the poller
class JoystickPTT:
    """Calls on_down() when the button goes down and on_up() when it comes back up."""

    def __init__(self, on_down: Callable[[], None], on_up: Callable[[], None],
                 name_contains: str = "", index: Optional[int] = None, button: int = 1,
                 backend: str = "auto", poll_hz: float = 100.0, rescan_sec: float = 3.0,
                 backend_factory: Callable = None):
        self._on_down, self._on_up = on_down, on_up
        self._name = (name_contains or "").strip().lower()
        self._index = index
        self._button = int(button)
        self._pref = backend
        self._period = 1.0 / poll_hz
        self._rescan = rescan_sec
        self._factory = backend_factory or (lambda: make_backend(self._pref))
        self._stop = threading.Event()
        self.device = ""             # "name [index] via backend" once found
        self.ok = None               # None = starting, True = reading a device, False = can't

    def start(self) -> "JoystickPTT":
        threading.Thread(target=self._run, daemon=True, name="joystick-ptt").start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------- internals
    def _find(self, be):
        """(handle, description) of the configured device, or (None, reason)."""
        devs = be.devices()
        chosen = None
        if self._name:
            chosen = next((d for d in devs if self._name in d[1].lower()), None)
        if chosen is None and self._index is not None:
            chosen = next((d for d in devs if d[0] == self._index), None)
        if chosen is None:
            listing = ", ".join(f"[{i}] {n}" for i, n, _ in devs) or "none"
            return None, f"not found (devices: {listing})"
        i, name, nbuttons = chosen
        if nbuttons and self._button > nbuttons:
            return None, f"{name} has only {nbuttons} buttons, not {self._button}"
        return be.open(i), f"{name} [{i}] via {be.name}"

    def _run(self) -> None:
        be, note = self._factory()
        if be is None:
            self.ok = False
            print(f"[JOY PTT] Can't read joysticks: {note}. Install pygame-ce (pip install pygame-ce).")
            return
        if note:
            print(f"[JOY PTT] {note}; using {be.name}.")
        handle, last_reason, pressed = None, None, False
        while not self._stop.is_set():
            if handle is None:
                try:
                    handle, desc = self._find(be)
                except Exception as e:
                    handle, desc = None, f"error: {type(e).__name__}: {e}"
                if handle is None:
                    if desc != last_reason:              # say it once, not every rescan
                        print(f"[JOY PTT] Button {self._button}: device '{self._name or self._index}' {desc}. "
                              f"Retrying every {self._rescan:.0f}s.")
                        last_reason = desc
                    self.ok = False
                    self._stop.wait(self._rescan)
                    continue
                self.device, self.ok, last_reason = desc, True, None
                print(f"[JOY PTT] Push-to-talk on button {self._button} of {desc}.")
            state = be.button(handle, self._button)
            if state is None:                            # unplugged / lost
                print(f"[JOY PTT] Lost {self.device}; looking for it again.")
                handle, self.ok = None, False
                if pressed:
                    pressed = False
                    self._safe(self._on_up)
                continue
            if state != pressed:
                pressed = state
                self._safe(self._on_down if pressed else self._on_up)
            self._stop.wait(self._period)
        if pressed:
            self._safe(self._on_up)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception as e:
            print(f"[JOY PTT ERROR] {type(e).__name__}: {e}")
