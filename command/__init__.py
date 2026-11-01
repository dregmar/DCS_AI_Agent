"""
command/ - Phase 2 command roles (design: "DCS George AI — Design: Tactical Picture & Threat Fusion",
Phase 2 build plan, approved 1 Nov 2026).

WARLORD (mission commander), OVERLORD (AWACS / air control), DARKSIDE (JTAC / FAC) and the aircraft
are deterministic code modules over the shared mission state (the tactical picture). None of them is an
LLM agent. Built in steps:

    roles.py        role names (P2-1; callsigns and voices come in P2-2 / P2-3)
    permissions.py  the one permission table every Olympus command passes, and the acting role (P2-1)
    tasking.py      the tasking registry: one record per request, its owner and its state (P2-1)
    wake.py         "Warlord, ..." / "Darkside, ..." wake words (P2-3)
    rules.py        no-strike / priority zones, danger close, the "warlord" block (P2-5 / P2-6)

The roles themselves: common/warlord.py (P2-5), common/darkside.py (P2-4 / P2-6), common/overlord.py.

The pilot's own weapons and George's fire never pass through any of this.
"""
