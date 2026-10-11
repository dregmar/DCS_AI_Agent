"""
core/wpn_page_listener.py
Your actioned weapon and selected sight, read from the WPN page as DCS draws it.

DCS-BIOS has no output for either, but Saved Games\\DCS\\Scripts\\george_export.lua streams the
text of every MPD showing a WPN page to UDP 127.0.0.1:5006 once per second:
    "IND <id>\\n" + list_indication(<id>) text      (empty body = that MPD isn't on WPN)
On that page the actioned weapon's label (GUN / MSL / RKT) is BOXED, which shows up as an extra
element named "<label element>_b"; the sight is in SightStatusWindow_text_2. This was confirmed
with george_dump.lua (Logs\\george_selection_log.txt).

Only updates the state store when a WPN page is actually on screen; the state's wpn_page_age
says how fresh the reading is. Never sends anything to DCS.
"""
import socket
import threading
import time

import config
from core.george_ui import _fields
from core.state import state_store

WPN_PAGE_PORT = getattr(config, "WPN_PAGE_PORT", 5006)
_WEAPONS = ("GUN", "MSL", "RKT")
_SIGHTS = {"HMD": "HMD", "TADS": "TADS", "FCR": "FCR", "LINK": "LINK"}


def parse_wpn_page(text: str):
    """WPN page text -> (actioned weapon or None if unclear, sight or None)."""
    f = _fields(text)
    if "SightStatusWindow_text_2" not in f:
        return None, None
    sight = _SIGHTS.get(f.get("SightStatusWindow_text_2", "").strip().upper())
    boxed, plain = set(), set()
    for name, value in f.items():
        v = value.strip().upper()
        if v in _WEAPONS:
            (boxed if f"{name}_b" in f else plain).add(v)
    if len(boxed) == 1:
        was = boxed.pop()
    elif not boxed and plain:
        was = "NONE"
    else:
        was = None          # all boxed (page redrawing) or nothing found: don't guess
    return was, sight


def listen_wpn_page() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", WPN_PAGE_PORT))
    print(f"[WPN PAGE] Listening on UDP port {WPN_PAGE_PORT}...")
    last_err = 0.0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            head, _, body = data.decode("utf-8", errors="replace").partition("\n")
            if not head.startswith("IND") or not body.strip():
                continue
            was, sight = parse_wpn_page(body)
            if was or sight:
                state_store.update_wpn_page(was=was, sight=sight)
        except Exception as e:
            now = time.monotonic()
            if now - last_err > 5.0:
                print(f"[WPN PAGE ERROR] {type(e).__name__}: {e}")
                last_err = now
            time.sleep(0.05)


def start_wpn_page_thread() -> threading.Thread:
    t = threading.Thread(target=listen_wpn_page, daemon=True, name="wpn-page")
    t.start()
    return t
