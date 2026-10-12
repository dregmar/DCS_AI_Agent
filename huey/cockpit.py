"""
huey/cockpit.py
UH-1H cockpit switches through DCS-BIOS (commands out on UDP 7778), each confirmed from the
DCS-BIOS stream (UDP 5010) before George says it's done.

SWITCHES: key -> (DCS-BIOS identifier, {state name: value}, spoken label, assumed)
`assumed` = the value meaning is inferred, not documented by DCS-BIOS. Check those once with
huey_probe.py (flip the switch in the cockpit and watch the value) and fix the table if needed.
"""
import time
from typing import Callable, Dict, Optional, Tuple

from huey.bios_map import decode_int, decode_string

SWITCHES = {
    "master_arm":   ("MASTER_ARM_SW", {"OFF": 0, "SAFE": 1, "ARMED": 2}, "master arm", False),
    "arm_sel":      ("ROCKET_SEL", {"GUNS": 0, "ROCKETS": 1, "GRENADES": 2}, "armament selector", False),
    "gun_sel":      ("GUN_SEL", {"LEFT": 0, "BOTH": 1, "RIGHT": 2}, "gun selector", False),
    "rocket_pair":  ("ROCKET_PAIR", {str(n): n for n in range(8)}, "rocket pairs", False),
    "search_light": ("SEARCH_LIGHT_SW", {"STOW": 0, "OFF": 1, "ON": 2}, "search light", False),
    "nav_lts":      ("NAV_LTS_SW", {"STEADY": 0, "OFF": 1, "FLASH": 2}, "nav lights", False),
    "main_gen":     ("MAIN_GEN_SW", {"RESET": 0, "OFF": 1, "ON": 2}, "main generator", False),
    "anticoll":     ("ANTICOLL_LTS_SW", {"OFF": 0, "ON": 1}, "anti-collision light", True),
    "pos_lts":      ("POS_LTS_SW", {"DIM": 0, "BRIGHT": 1}, "position lights", True),
    "cm_arm":       ("CM_ARM_SW", {"SAFE": 0, "ARMED": 1}, "flares", True),
    "starter_gen":  ("STARTER_GEN_SW", {"START": 0, "STBY GEN": 1}, "starter generator", True),
    "ldg_light":    ("LDG_LIGHT_SW", {"OFF": 0, "ON": 1}, "landing light", True),
    "pitot":        ("PITOT_HEAT", {"OFF": 0, "ON": 1}, "pitot heat", True),
    "battery":      ("BAT_SW", {"OFF": 0, "ON": 1}, "battery", True),
    "main_fuel":    ("MAIN_FUEL_SW", {"OFF": 0, "ON": 1}, "main fuel", True),
    "force_trim":   ("FORCE_TRIM_SW", {"OFF": 0, "ON": 1}, "force trim", True),
    "hyd_cont":     ("HYD_CONT_SW", {"OFF": 0, "ON": 1}, "hydraulic control", True),
    "low_rpm_audio": ("LOW_RPM_AUDIO", {"OFF": 0, "AUDIO": 1}, "low RPM audio", True),
    "emer_gov":     ("EMER_GOV_SW", {"AUTO": 0, "EMER": 1}, "governor", True),
    "inverter":     ("INVERTER_SW", {"SPARE": 0, "OFF": 1, "MAIN": 2}, "inverter", True),
    "chip_det":     (None, {"TAIL ROTOR": 0, "BOTH": 1, "XMSN": 2}, "chip detector", True),
}

# radio key -> (MHz range, step in MHz, spoken name). Tuned by turning the cockpit dials (the
# radios follow their dials; setting the frequency directly is undone by the dials a moment later).
RADIOS = {
    "uhf":    ((225.0, 399.95), 0.05, "UHF"),
    "vhf_am": ((116.0, 149.975), 0.025, "VHF"),
    "vhf_fm": ((30.0, 75.95), 0.05, "FM"),
}
DIAL_STEP_SEC = 0.8          # wait this long for one dial click to show on the stream
MAX_CLICKS = 45

READBACK_SEC = 2.0
STREAM_FRESH_SEC = 3.0


def switch_state(words: Dict[int, int], key: str) -> Optional[str]:
    """Current state name of a switch from the DCS-BIOS words, or None if unknown."""
    v = decode_int(words, key)
    if v is None:
        return None
    for name, val in SWITCHES[key][1].items():
        if val == v:
            return name
    return None


def spoken(state: str) -> str:
    return {"STBY GEN": "standby generator", "XMSN": "transmission"}.get(state, state.lower())


class Cockpit:
    """Sets switches and radios and reads them back. All methods block for at most a couple of
    seconds and are meant for the Huey crew worker thread, never the telemetry threads.

    sender:   object with send_command(identifier, argument) -> bool (core.dcs_bios.cmd_sender)
    words:    () -> {address: word} (state_store.bios_words_snapshot)
    bios_age: () -> seconds since the last DCS-BIOS frame
    """

    def __init__(self, sender, words: Callable[[], Dict[int, int]], bios_age: Callable[[], float],
                 readback_sec: float = READBACK_SEC):
        self.sender, self.words, self.bios_age = sender, words, bios_age
        self.readback_sec = readback_sec
        self._inc_up: Dict[str, bool] = {}       # per dial: does INC turn the number up?

    def live(self) -> bool:
        return self.bios_age() <= STREAM_FRESH_SEC

    def _wait(self, check: Callable[[], bool]) -> bool:
        end = time.monotonic() + self.readback_sec
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.05)
        return check()

    def set_switch(self, key: str, state: str) -> Tuple[bool, str]:
        """Returns (confirmed, sentence to speak)."""
        ident, states, label, _ = SWITCHES[key]
        if ident is None or state not in states:
            return False, f"Unable, {label}."
        if not self.live():
            return False, "No cockpit data. Check DCS-BIOS."
        if switch_state(self.words(), key) == state:
            return True, f"{label.capitalize()} already {spoken(state)}."
        if not self.sender.send_command(ident, str(states[state])):
            return False, f"Unable, {label}."
        if self._wait(lambda: switch_state(self.words(), key) == state):
            return True, f"{label.capitalize()} {spoken(state)}."
        now = switch_state(self.words(), key)
        return False, f"{label.capitalize()} didn't move" + (f", still {spoken(now)}." if now else ".")

    # ------------------------------------------------------------ radios
    def dial_freq(self, key: str) -> Optional[float]:
        """Frequency the cockpit dials show, in MHz (None if unknown)."""
        w = self.words()
        if key == "vhf_fm":
            d = [decode_int(w, k) for k in ("fm_tens", "fm_units", "fm_tenths", "fm_fifty")]
            if None in d:
                return None
            return round((d[0] + 3) * 10 + d[1] + d[2] / 10 + d[3] * 0.05, 3)
        text = decode_string(w, "uhf_dial" if key == "uhf" else "vhf_dial")
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    def radio(self, key: str) -> Optional[str]:
        f = self.dial_freq(key)
        return None if f is None else f"{f:.3f}"

    def _click_to(self, ident: str, read: Callable[[], Optional[int]], target: int) -> bool:
        """Clicks a fixed-step dial (DCS-BIOS INC / DEC) until read() == target, one click at a
        time, each confirmed on the stream. Learns which way INC turns it."""
        inc_up = self._inc_up.get(ident, True)
        for _ in range(MAX_CLICKS):
            cur = read()
            if cur is None:
                return False
            if cur == target:
                return True
            up = target > cur
            self.sender.send_command(ident, "INC" if up == inc_up else "DEC")
            end = time.monotonic() + DIAL_STEP_SEC
            new = cur
            while time.monotonic() < end:
                new = read()
                if new is not None and new != cur:
                    break
                time.sleep(0.03)
            if new is None or new == cur:
                return False                       # the dial didn't move
            wrapped = abs(new - cur) > 3           # dials wrap round (e.g. .95 -> .00)
            moved_up = (new < cur) if wrapped else (new > cur)
            if moved_up != up:                     # turned the other way: INC is "down" here
                inc_up = not inc_up
                self._inc_up[ident] = inc_up
            elif not wrapped and new != target and (new > target) != (cur > target):
                return False                       # jumped past it: not a position this dial has
        return read() == target

    def set_radio(self, key: str, mhz: float) -> Tuple[bool, str]:
        (lo, hi), step, name = RADIOS[key]
        if not lo <= mhz <= hi:
            return False, f"{name} range is {lo:g} to {hi:g}."
        n = round(mhz / step)
        if abs(n * step - mhz) > 0.001:
            return False, f"{name} tunes in {step * 1000:g} kilohertz steps."
        mhz = round(n * step, 3)
        if not self.live():
            return False, "No cockpit data. Check DCS-BIOS."
        if self.dial_freq(key) is None:
            return False, f"Can't read the {name} dials."
        f = lambda: self.dial_freq(key)
        if key == "vhf_fm":
            tenths = int(round(mhz * 100)) % 100
            for ident, v in (("VHFFM_FREQ1", int(mhz // 10) - 3), ("VHFFM_FREQ2", int(mhz) % 10),
                             ("VHFFM_FREQ3", tenths // 10), ("VHFFM_FREQ4", 1 if tenths % 10 else 0)):
                self.sender.send_command(ident, str(v))
            ok = self._wait(lambda: f() is not None and abs(f() - mhz) < 0.001)
        else:
            if key == "uhf":
                parts = (("UHF_10MHZ", lambda x: int(x // 10), int(mhz // 10)),
                         ("UHF_1MHZ", lambda x: int(x) % 10, int(mhz) % 10),
                         ("UHF_50KHZ", lambda x: int(round((x - int(x)) * 20)), int(round((mhz - int(mhz)) * 20))))
            else:
                parts = (("VHFCOMM_MHZ", lambda x: int(x), int(mhz)),
                         ("VHFCOMM_KHZ", lambda x: int(round((x - int(x)) * 40)), int(round((mhz - int(mhz)) * 40))))
            ok = True
            for ident, digit, want in parts:
                def read(digit=digit):
                    v = f()
                    return None if v is None else digit(v)
                if not self._click_to(ident, read, want):
                    ok = False
                    break
        now = f()
        if not ok or now is None or abs(now - mhz) > 0.001:
            return False, f"{name} not set" + (f", dials read {say_freq(now)}." if now is not None else ".")
        if key == "uhf":
            tuned = decode_string(self.words(), "uhf")
            try:
                if tuned and abs(float(tuned) - mhz) > 0.001:
                    return True, f"UHF dials {say_freq(mhz)}. Radio isn't following, check the mode dial is on manual."
            except ValueError:
                pass
        return True, f"{name} {say_freq(mhz)}."

    def dispense_flare(self, flare_count: Callable[[], Optional[int]], auto_arm: bool = True
                       ) -> Tuple[bool, str]:
        """Arms the dispenser if needed (auto_arm), presses the flare button, confirms from the
        flare counter dropping."""
        if not self.live():
            return False, "No cockpit data. Check DCS-BIOS."
        prefix = ""
        if switch_state(self.words(), "cm_arm") != "ARMED":
            if not auto_arm:
                return False, "Flares are safe."
            ok, _ = self.set_switch("cm_arm", "ARMED")
            if not ok:
                return False, "Unable to arm flares."
            prefix = "Flares armed. "
        before = flare_count()
        if before == 0:
            return False, prefix + "No flares left."
        self.sender.send_command("CM_FLARE_BTN", "1")
        time.sleep(0.15)
        self.sender.send_command("CM_FLARE_BTN", "0")
        if before is None:
            return True, prefix + "Flare pressed."
        if self._wait(lambda: (flare_count() or before) < before):
            left = flare_count()
            return True, prefix + "Flare away" + (f", {left} left." if left is not None else ".")
        return False, prefix + "No flare out."


def say_freq(mhz: float) -> str:
    """251.0 -> '251 decimal 0', 124.525 -> '124 decimal 525'."""
    whole, _, frac = f"{mhz:.3f}".partition(".")
    frac = frac.rstrip("0") or "0"
    return f"{whole} decimal {frac}"
