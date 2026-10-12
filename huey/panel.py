"""
huey/panel.py
Parses the UH-1H gunners' Crew Status panel (Cockpit/Scripts/AI/ControlPanel/g_panel_page.lua)
from its list_indication() text.

The panel's elements, one row per crew member (index 0..3 = pilot, co-pilot, LH gunner, RH gunner):
    txt_mem<i>     name          PILOT / CO-PILOT / LH GUNNER / RH GUNNER
    txt_status<i>  ROE / state   -  HOLD  RET.FIRE  FREE FIRE  PLAYER  DEAD  NET  FAIL
    txt_ammo<i>    ammo          "85%" or " -"
    txt_burst<i>   burst         "  -"  SHORT  LONG
"""
import re
from typing import Dict, Optional

from core.george_ui import _fields

SEATS = ("pilot", "copilot", "left", "right")
SEAT_NAMES = {"pilot": "pilot", "copilot": "co-pilot", "left": "left gunner", "right": "right gunner"}
ROE_WORDS = {"HOLD": "HOLD", "RET.FIRE": "RETURN", "RET. FIRE": "RETURN", "FREE FIRE": "FREE"}
STATE_WORDS = {"PLAYER", "DEAD", "NET", "FAIL", "-"}
SPOKEN_ROE = {"HOLD": "weapons hold", "RETURN": "return fire", "FREE": "free fire"}


def _clean(v: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (v or "")).strip()


def parse_panel(text: str) -> Dict[str, dict]:
    """{} when the panel isn't listed. Otherwise {seat: {"roe": HOLD/RETURN/FREE or None,
    "state": PLAYER/DEAD/NET/FAIL/- or None, "ammo": int % or None, "burst": SHORT/LONG or None}}
    for every seat whose status is listed."""
    if not text or "CREW STATUS" not in text:
        return {}
    f = _fields(text)
    out = {}
    for i, seat in enumerate(SEATS):
        status = _clean(f.get(f"txt_status{i}"))
        if not status and f"txt_status{i}" not in f:
            continue
        up = status.upper()
        roe = ROE_WORDS.get(up)
        state = None if roe else (up if up in STATE_WORDS else (up or "-"))
        m = re.search(r"(\d+)\s*%", f.get(f"txt_ammo{i}", ""))
        burst = _clean(f.get(f"txt_burst{i}")).upper()
        out[seat] = {"roe": roe, "state": state, "ammo": int(m.group(1)) if m else None,
                     "burst": burst if burst in ("SHORT", "LONG") else None}
    return out
