"""
apache/checklists.py
AH-64D pilot checklists, George reading them from the front seat. Items George can see on the
DCS-BIOS stream (switch positions, the APU and engine lights, the power levers) he checks himself;
the rest wait for "check". Same replies as the Huey: check / next, set it, skip, repeat, stop checklist.
Item checks: ("switch", key, (states...)) with keys from apache.bios_map plus "pl1", "pl2" and
"levers" (both power levers); ("cautions",) = no warnings or cautions on the EUFD.
"""

APACHE_CHECKLISTS = {
    "before_start": ("Before starting engines", [
        ("Canopy, closed", ("switch", "canopy", ("CLOSED",))),
        ("Parking brake, set", ("switch", "park_brake", ("PULL",))),
        ("Master ignition, battery", ("switch", "master_ign", ("BATT", "EXT PWR"))),
        ("Rotor brake, off", ("switch", "rotor_brk", ("OFF",))),
        ("Power levers, off", ("switch", "levers", ("OFF",))),
        ("Lights, as required", None),
    ]),
    "start": ("Starting engines", [
        ("APU, start. APU light on", ("switch", "apu_on", ("ON",))),
        ("Engine one start switch, start", None),
        ("Power lever one, idle", ("switch", "pl1", ("IDLE",))),
        ("Engine two start switch, start", None),
        ("Power lever two, idle", ("switch", "pl2", ("IDLE",))),
        ("Engines stabilised at idle", None),
        ("Power levers, fly", ("switch", "levers", ("FLY",))),
        ("APU, off", ("switch", "apu_on", ("OFF",))),
    ]),
    "before_takeoff": ("Before take-off", [
        ("Canopy, closed", ("switch", "canopy", ("CLOSED",))),
        ("Power levers, fly", ("switch", "levers", ("FLY",))),
        ("Parking brake, released", ("switch", "park_brake", ("STOW",))),
        ("CMWS, on", ("switch", "cmws_pwr", ("ON",))),
        ("Flares, armed", ("switch", "cmws_arm", ("ARM",))),
        ("Tail wheel, locked", ("switch", "tail_wheel", ("LOCKED",))),
        ("Engine and caution check, no warnings", ("cautions",)),
    ]),
    "shutdown": ("Engine shutdown", [
        ("Parking brake, set", ("switch", "park_brake", ("PULL",))),
        ("Flares, safe", ("switch", "cmws_arm", ("SAFE",))),
        ("Power levers, idle", ("switch", "levers", ("IDLE",))),
        ("Cool down two minutes", None),
        ("Power levers, off", ("switch", "levers", ("OFF",))),
        ("Rotor brake, as required", None),
        ("Lights, off", ("switch", "nav_lts", ("OFF",))),
        ("Master ignition, off", ("switch", "master_ign", ("OFF",))),
    ]),
}

# DCS-BIOS identifiers George may set for "set it" (the rest are buttons, levers or lights)
SETTABLE = {
    "master_ign": "PLT_MASTER_IGN_SW", "rotor_brk": "PLT_ROTOR_BRK", "park_brake": "PLT_PARK_BRAKE",
    "canopy": "PLT_CANOPY", "nav_lts": "PLT_EXTL_NAV_L_SW", "anticoll": "PLT_EXTL_ACOL_L_SW",
    "cmws_pwr": "PLT_CMWS_PW", "cmws_arm": "PLT_CMWS_ARM", "cmws_mode": "PLT_CMWS_MODE",
}

SAY = {"BATT": "battery", "EXT PWR": "external power", "PULL": "set", "STOW": "released",
       "CMWS": "C M W S", "BRK": "brake", "IGN ORIDE": "ignition override",
       "MID": "between idle and fly"}


def say_state(state: str) -> str:
    return SAY.get(state, state.lower())
