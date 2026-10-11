"""
core/george_ui.py
Reads George's AI Helper Interface as DCS draws it, streamed by
Saved Games\\DCS\\Scripts\\george_ai_export.lua on UDP 127.0.0.1:5007.

DCS exposes the interface as cockpit indicator 26 (found with george_probe.lua). Its
list_indication() text holds every ring label (weapon_up, weapon_far_left, ...) and, when a
list is shown, a five-line window around the highlighted entry:
    upper_upper_list_text, upper_list_text, middle_list_text (highlighted),
    lower_list_text, lower_lower_list_text
This module parses that text into a dict and stores it in the ApacheStateStore.
It never sends anything to DCS.

parse_indication(text) -> {"visible": True, "fields": {...}, "labels": {...},
                           "list": None | {"kind": "TARGET_LIST"/..., "window": [5 names]},
                           "weapon"/"roe"/"tads"/... when the main ring is shown}
"""
import re
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import config
from core.state import state_store

GEORGE_UI_PORT = getattr(config, "GEORGE_UI_PORT", 5007)

_SEP = re.compile(r"^-{20,}\s*$")
_WINDOW_KEYS = ("upper_upper_list_text", "upper_list_text", "middle_list_text",
                "lower_list_text", "lower_lower_list_text")
_TADS_WORDS = {"FIXED": "FIXED", "SLAVED": "SLAVED", "DE-SLAVED": "DESLAVED", "DESLAVED": "DESLAVED",
               "SEARCHING": "SEARCHING", "TRACKING": "TRACKING", "LINKED": "LINKED"}
_WEAPON_WORDS = {"NO WPN": "NO_WPN", "GUN": "GUN", "MSL": "MSL", "RKT": "RKT"}


def _fields(text: str) -> Dict[str, str]:
    """list_indication text -> {element name: value}. Values spanning several lines are joined
    with spaces; an inner '-----' (two options stacked, e.g. POINT / AREA) becomes ' / '."""
    out: Dict[str, str] = {}
    name: Optional[str] = None
    expect_name = False
    vals: List[str] = []

    def flush():
        if name is not None and name not in out:
            out[name] = " ".join(v for v in vals if v).replace(" ----- ", " / ").replace("-----", "/").strip()

    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if _SEP.match(line):
            flush()
            name, vals, expect_name = None, [], True
        elif expect_name:
            name, expect_name = line.strip(), False
        elif line.strip() in ("children are {", "}"):
            flush()
            name, vals = None, []
        elif name is not None:
            vals.append(line.strip())
    flush()
    return out


def parse_indication(text: str) -> Dict[str, Any]:
    f = _fields(text)
    if "weap_contol" not in f and not any(k.startswith("weapon_") for k in f):
        return {"visible": False}
    labels = {k[len("weapon_"):]: v for k, v in f.items() if k.startswith("weapon_")}
    ui: Dict[str, Any] = {"visible": True, "fields": f, "labels": labels, "list": None}

    if "middle_list_text" in f:
        up, right = labels.get("up", "").upper(), labels.get("right", "").upper()
        if "TGT" in up or "TGT" in right:
            kind = "TARGET_LIST"
        elif "POINT" in up or "POINT" in right:
            kind = "POINT_LIST"
        elif "SEARCH" in up or "SEARCH" in right:
            kind = "SEARCH_LIST"
        else:
            kind = "TARGET_LIST"
        ui["list"] = {"kind": kind, "window": [f.get(k, "").strip() for k in _WINDOW_KEYS]}
        return ui

    # Main ring: read George's actual settings from the labels.
    left = labels.get("left", "").upper()
    ui["weapon"] = _WEAPON_WORDS.get(left)
    far_up = labels.get("far_up", "").upper()
    ui["roe"] = "FREE" if "FREE" in far_up else ("HOLD" if "HOLD" in far_up else None)
    ui["tads"] = _TADS_WORDS.get(labels.get("down", "").upper())
    right = labels.get("right", "").upper()
    if "LOBL" in right or "LOAL" in right:
        ui["msl_method"] = "LOAL" if "LOAL" in right else "LOBL"
    if right in ("LASING", "NOT LASING"):
        ui["laser"] = right == "LASING"
    for label in (labels.get("far_right", ""), labels.get("far_left", "")):
        m = re.match(r"^\s*(BRST|BURST|QTY|TRAJ|TYPE)\s+(\S+)", label.upper())
        if not m:
            continue
        key, val = m.groups()
        if key in ("BRST", "BURST"):
            ui["gun_burst"] = val
        elif key == "QTY":
            ui["rkt_qty"] = val
        elif key == "TRAJ":
            ui["msl_traj"] = val
        elif key == "TYPE":
            ui["type"] = val      # SAL2 / RF for missiles, HE / ILL / SMK for rockets
    far_down = labels.get("far_down", "").upper()
    if "WARN" in far_down:
        ui["threat_warn"] = "NO WARN" not in far_down
    return ui


def listen_george_ui() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", GEORGE_UI_PORT))
    print(f"[GEORGE UI] Listening on UDP port {GEORGE_UI_PORT}...")
    last_err = 0.0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            text = data.decode("utf-8", errors="replace")
            head, _, body = text.partition("\n")
            if not head.startswith("GAI"):
                continue
            ui = parse_indication(body) if head.strip() == "GAI 1" else {"visible": False}
            state_store.update_george_ui(ui)
        except Exception as e:
            now = time.monotonic()
            if now - last_err > 5.0:
                print(f"[GEORGE UI ERROR] {type(e).__name__}: {e}")
                last_err = now
            time.sleep(0.05)


def start_george_ui_thread() -> threading.Thread:
    t = threading.Thread(target=listen_george_ui, daemon=True, name="george-ui")
    t.start()
    return t
