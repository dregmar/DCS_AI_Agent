"""
huey/bios_map.py
UH-1H DCS-BIOS outputs we read (addresses from Scripts/DCS-BIOS/doc/json/UH-1H.json).
No project imports here: core/state.py imports this to know which stream words to keep.
"""

# name -> (address, mask, shift)
HUEY_BIOS_INTS = {
    "master_arm":   (0x1502, 0x1800, 11),   # 0 OFF, 1 SAFE, 2 ARMED
    "gun_sel":      (0x1502, 0x6000, 13),   # 0 LEFT, 1 BOTH, 2 RIGHT
    "search_light": (0x1502, 0x0003, 0),    # 0 STOW, 1 OFF, 2 ON
    "arm_sel":      (0x1516, 0x0003, 0),    # 0 7.62, 1 2.75, 2 40 mm
    "rocket_pair":  (0x1516, 0x001C, 2),    # 0..7
    "cm_arm":       (0x151E, 0x0010, 4),
    "flare_count":  (0x152A, 0x01F8, 3),
    "chaff_count":  (0x152A, 0x7E00, 9),
    "ldg_light":    (0x14FC, 0x8000, 15),
    "anticoll":     (0x14FC, 0x4000, 14),
    "pos_lts":      (0x14FC, 0x2000, 13),   # 0 DIM, 1 BRIGHT
    "nav_lts":      (0x14FC, 0x1800, 11),   # 0 STEADY, 1 OFF, 2 FLASH
    "pitot":        (0x14B8, 0x0002, 1),
    "main_gen":     (0x14B8, 0x000C, 2),    # 0 RESET, 1 OFF, 2 ON
    "main_fuel":    (0x14B8, 0x0200, 9),
    "battery":      (0x1416, 0x2000, 13),
    "starter_gen":  (0x1416, 0x4000, 14),
    "inverter":     (0x14AE, 0x0060, 5),
    "force_trim":   (0x14BE, 0x0400, 10),
    "hyd_cont":     (0x14BE, 0x0200, 9),
    "emer_gov":     (0x14BE, 0x0100, 8),
    "low_rpm_audio": (0x14BE, 0x0020, 5),
    "chip_det":     (0x14BE, 0x00C0, 6),
    "seat":         (0x1538, 0x6000, 13),   # 0 pilot, 1 copilot, 2 left door, 3 right door
    # Radio dials
    "uhf_mode":     (0x14E8, 0x0003, 0),    # UHF frequency mode dial (preset / manual / guard)
    "fm_tens":      (0x14E8, 0x00E0, 5),    # VHF FM dials: tens of MHz (0..4 = 3..7)
    "fm_units":     (0x14E8, 0x0F00, 8),    #   MHz 0..9
    "fm_tenths":    (0x14E8, 0xF000, 12),   #   0.1 MHz 0..9
    "fm_fifty":     (0x14EC, 0x0100, 8),    #   0.05 MHz: 0 = .x0, 1 = .x5
    "fm_mode":      (0x14EC, 0x0600, 9),    # 0 OFF, 1 T/R, 2 RETRAN, 3 HOME
}

# name -> (address, length): DCS-BIOS strings
HUEY_BIOS_STRINGS = {
    # what each radio is actually tuned to, e.g. "251.000"
    "uhf":    (0x154E, 7),
    "vhf_am": (0x1546, 7),
    "vhf_fm": (0x1556, 5),
    # what the cockpit dials show (these are what the radio follows)
    "uhf_dial": (0x14E2, 6),     # "232.00"
    "vhf_dial": (0x14D6, 7),     # "124.750"
    "nav_dial": (0x14F2, 6),     # VHF NAV (ARN-82) "108.00"
}


def watched_addresses():
    out = {a for a, _, _ in HUEY_BIOS_INTS.values()}
    for addr, length in HUEY_BIOS_STRINGS.values():
        out.update(addr + 2 * i for i in range((length + 1) // 2))
    return out


def decode_int(words: dict, name: str):
    """Field value, or None if that word hasn't been seen."""
    addr, mask, shift = HUEY_BIOS_INTS[name]
    w = words.get(addr)
    return None if w is None else (w & mask) >> shift


def decode_string(words: dict, name: str):
    addr, length = HUEY_BIOS_STRINGS[name]
    chars = []
    for i in range(length):
        w = words.get(addr + (i // 2) * 2)
        if w is None:
            return None
        b = (w >> (8 * (i % 2))) & 0xFF
        if b == 0:
            break
        chars.append(chr(b))
    return "".join(chars).strip()
