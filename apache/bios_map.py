"""
apache/bios_map.py
AH-64D DCS-BIOS outputs for the pilot's checklists and the Up-Front Display (EUFD), from
Scripts/DCS-BIOS/doc/json/AH-64D.json. No project imports: core/state.py imports this.

name -> (address, mask, shift, {value: state name}) ; state names in DCS-BIOS position order.
"""
APACHE_SWITCHES = {
    "master_ign": (0x8708, 0x0180, 7, ("OFF", "BATT", "EXT PWR")),
    "rotor_brk":  (0x8700, 0x0C00, 10, ("LOCK", "BRK", "OFF")),
    "park_brake": (0x8706, 0x8000, 15, ("STOW", "PULL")),
    "canopy":     (0x8708, 0x0001, 0, ("CLOSED", "OPEN")),
    "eng1_start": (0x8700, 0xC000, 14, ("IGN ORIDE", "OFF", "START")),
    "eng2_start": (0x8704, 0x0003, 0, ("IGN ORIDE", "OFF", "START")),
    "nav_lts":    (0x801C, 0x0030, 4, ("DIM", "OFF", "BRIGHT")),
    "anticoll":   (0x801C, 0x00C0, 6, ("RED", "OFF", "WHITE")),
    "cmws_pwr":   (0x874C, 0xC000, 14, ("OFF", "ON", "TEST")),
    "cmws_arm":   (0x8712, 0x0020, 5, ("SAFE", "ARM")),
    "cmws_mode":  (0x8712, 0x0040, 6, ("NAV", "CMWS")),
    # indicator lights
    "apu_on":     (0x872C, 0x0002, 1, ("OFF", "ON")),
    "apu_ready":  (0x872A, 0x0001, 0, ("OFF", "ON")),
    "eng1_ready": (0x8712, 0x8000, 15, ("OFF", "ON")),
    "eng2_ready": (0x872A, 0x0002, 1, ("OFF", "ON")),
    "apu_fire":   (0x8712, 0x2000, 13, ("OFF", "ON")),
    "eng1_fire":  (0x8712, 0x1000, 12, ("OFF", "ON")),
    "eng2_fire":  (0x8712, 0x4000, 14, ("OFF", "ON")),
}

# Power levers: a 0..65535 position. Where IDLE and FLY sit is checked with apache_probe.py.
POWER_LEVERS = {"pl1": 0x870A, "pl2": 0x870C}
# Power lever position as a fraction of travel. Measured with apache_probe.py (Oct 2026): FLY
# detent 0.88-0.90, back at the IDLE stop 0.53-0.54 (OFF wasn't measured: below the IDLE stop).
# In between is MID, so a lever on its way up isn't called IDLE (82% used to count as IDLE).
PL_OFF_BELOW = 0.25
PL_IDLE_MAX = 0.62
PL_FLY_MIN = 0.86
PL_IDLE_RANGE = (PL_OFF_BELOW, PL_IDLE_MAX)     # old name, kept for scripts

# Pilot EUFD: 14 lines of 56 characters
EUFD_LINES = [0x80C2 + 0x38 * i for i in range(14)]
EUFD_LEN = 56


def watched_addresses():
    out = {a for a, _, _, _ in APACHE_SWITCHES.values()} | set(POWER_LEVERS.values())
    for addr in EUFD_LINES:
        out.update(addr + 2 * i for i in range(EUFD_LEN // 2))
    return out


def switch_state(words: dict, key: str):
    addr, mask, shift, names = APACHE_SWITCHES[key]
    w = words.get(addr)
    if w is None:
        return None
    v = (w & mask) >> shift
    return names[v] if v < len(names) else None


def power_lever(words: dict, key: str):
    w = words.get(POWER_LEVERS[key])
    if w is None:
        return None
    f = w / 65535.0
    if f < PL_OFF_BELOW:
        return "OFF"
    if f <= PL_IDLE_MAX:
        return "IDLE"
    return "FLY" if f >= PL_FLY_MIN else "MID"


def eufd(words: dict):
    """The 14 EUFD lines as strings (None for a line not received yet)."""
    out = []
    for addr in EUFD_LINES:
        chars = []
        for i in range(EUFD_LEN):
            w = words.get(addr + (i // 2) * 2)
            if w is None:
                chars = None
                break
            b = (w >> (8 * (i % 2))) & 0xFF
            chars.append(chr(b) if 32 <= b < 127 else " ")
        out.append(None if chars is None else "".join(chars))
    return out
