"""
core/focus.py
Is DCS the window in front? Simulated key presses go to whichever window has focus, so the
George driver checks this before every key it sends. Windows only (ctypes, no extra packages);
on any other OS, or if the check itself fails, it reports True so it can never block George.

Config:
    GEORGE_REQUIRE_FOCUS = True                 # set False to disable the check
    GEORGE_FOCUS_EXES = ["DCS.exe", "DCS_mt.exe"]
"""
import os

import config

REQUIRE_FOCUS = getattr(config, "GEORGE_REQUIRE_FOCUS", True)
FOCUS_EXES = {n.lower() for n in getattr(config, "GEORGE_FOCUS_EXES", ["DCS.exe", "DCS_mt.exe"])}

_api = None
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes

        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _user32.GetForegroundWindow.restype = wintypes.HWND
        _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        _kernel32.OpenProcess.restype = wintypes.HANDLE
        _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _api = (ctypes, wintypes)
    except Exception as e:      # never let the check stop George from starting
        print(f"[FOCUS] Focus check unavailable: {e}")
        _api = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_exe() -> str:
    """File name of the process owning the foreground window ('' if unknown)."""
    if _api is None:
        return ""
    ctypes, wintypes = _api
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(len(buf))
            if not _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        return ""


def dcs_has_focus() -> bool:
    """True if DCS is in front, or if the check is disabled or can't run."""
    if not REQUIRE_FOCUS or _api is None:
        return True
    exe = foreground_exe()
    if not exe:
        return True             # couldn't tell: don't block
    return exe.lower() in FOCUS_EXES
