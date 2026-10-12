"""
huey/gauges.py
UH-1H cockpit draw arguments (sent by george_huey_export.lua) -> engineering units.
Calibration tables copied from Mods/aircraft/Uh-1H/Cockpit/Scripts/mainpanel_init.lua: the game
draws needle = interp(value, input, output); we invert that.
Lamp arguments from Cockpit/Scripts/MAIN_PANEL/lamps.lua.
"""
from typing import Dict, Optional

# arg -> (key, input values, needle positions)
GAUGES = {
    113: ("eng_oil_psi", (-3.0, 0.0, 100.0), (0.0, 0.029, 1.0)),
    114: ("eng_oil_c", (-70.0, -50.0, 0.0, 100.0, 150.0), (0.0, 0.13, 0.38, 0.71, 1.0)),
    115: ("xmsn_oil_psi", (-3.0, 0.0, 100.0), (0.0, 0.029, 1.0)),
    116: ("xmsn_oil_c", (-70.0, -50.0, 0.0, 100.0, 150.0), (0.0, 0.13, 0.38, 0.71, 1.0)),
    119: ("n1_pct", (0.0, 50.0, 100.0, 109.0), (0.0, 0.45, 0.91, 1.0)),
    121: ("egt_c", (0.0, 500.0, 600.0, 900.0, 1000.0), (0.0, 0.508, 0.604, 0.904, 1.0)),
    122: ("engine_rpm", (0.0, 7200.0), (0.0, 1.0)),
    123: ("rotor_rpm", (0.0, 360.0), (0.0, 1.0)),
    124: ("torque_psi", (-3.0, 0.0, 100.0), (0.0, 0.029, 1.0)),
    126: ("fuel_psi", (-2.0, 0.0, 50.0), (0.0, 0.015, 1.0)),
    149: ("dc_volts", (0.0, 30.0), (0.0, 1.0)),
    150: ("ac_volts", (0.0, 150.0), (0.0, 1.0)),
    239: ("fuel_lbs", (0.0, 1580.0), (0.0, 1.0)),
}

# arg -> spoken name. Caution panel segments plus the master warning lights.
LAMPS = {
    91: "engine oil pressure", 92: "engine icing", 93: "engine ice detected",
    94: "engine chip detector", 95: "left fuel boost pump", 96: "right fuel boost pump",
    97: "engine fuel pump", 98: "twenty minute fuel", 99: "fuel filter",
    100: "governor emergency", 101: "aux fuel low", 102: "transmission oil pressure",
    103: "transmission oil hot", 104: "hydraulic pressure", 105: "engine inlet air",
    106: "instrument inverter", 107: "DC generator", 108: "external power",
    109: "chip detector", 110: "IFF",
    254: "armed", 255: "safe", 275: "fire", 276: "low RPM", 277: "master caution",
}
CAUTION_ARGS = tuple(range(91, 111))
LAMP_ON = 0.3


def needle_to_value(needle: float, inp, out) -> float:
    """Inverse of the game's piecewise-linear gauge table."""
    if needle <= out[0]:
        return inp[0]
    for i in range(1, len(out)):
        if needle <= out[i]:
            span = out[i] - out[i - 1]
            f = 0.0 if span <= 0 else (needle - out[i - 1]) / span
            return inp[i - 1] + f * (inp[i] - inp[i - 1])
    return inp[-1]


def parse_args(text: str) -> Dict[int, float]:
    """'113=0.5120,114=0.4' -> {113: 0.512, 114: 0.4}. Bad entries are skipped."""
    out = {}
    for part in (text or "").split(","):
        a, _, v = part.partition("=")
        try:
            out[int(a)] = float(v)
        except ValueError:
            continue
    return out


def convert(args: Dict[int, float]) -> Dict[str, Optional[float]]:
    """Gauge values by name (None when the argument wasn't sent) plus 'lamps': names lit."""
    vals: Dict[str, object] = {}
    for arg, (key, inp, out) in GAUGES.items():
        vals[key] = round(needle_to_value(args[arg], inp, out), 1) if arg in args else None
    vals["lamps"] = sorted(LAMPS[a] for a in LAMPS if args.get(a, 0.0) > LAMP_ON)
    vals["cautions"] = sorted(LAMPS[a] for a in CAUTION_ARGS if args.get(a, 0.0) > LAMP_ON)
    return vals
