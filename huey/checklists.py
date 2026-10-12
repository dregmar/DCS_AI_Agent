"""
huey/checklists.py
Spoken UH-1H checklists from the DCS UH-1H Flight Manual, section 9 (9.1.1 Before Starting
Engine, 9.1.2 Starting Engine, 9.1.3 Engine Runup, 9.2.1 Before Take-off, 9.6 Engine Shutdown).

George reads each item. Where he can see the answer himself (a switch on the DCS-BIOS stream, a
gauge or the caution panel on the Huey link) he checks it and moves on ("Battery, on. Check.");
otherwise he waits for you:
    "check" / "next" / "done"   item done (George re-checks the ones he can see)
    "set it"                    George sets that switch for you (switch items only)
    "skip"                      move on without it
    "repeat" / "say again"      read the item again
    "stop checklist"            abandon the checklist
"""
from typing import Callable, Dict, List, Optional, Tuple

from huey.cockpit import SWITCHES, spoken

# Item: (what George says, check) where check is one of
#   None                                 you confirm it
#   ("switch", key, (states...))         DCS-BIOS switch in one of these states
#   ("gauge", key, lo, hi, words)        gauge value in [lo, hi]; words = how to say the value
#   ("cautions",)                        no caution lights
CHECKLISTS: Dict[str, Tuple[str, List[tuple]]] = {
    "before_start": ("Before starting engine", [
        ("Circuit breakers in, except armament", None),
        ("Inverter, off", ("switch", "inverter", ("OFF",))),
        ("Main generator, on", ("switch", "main_gen", ("ON",))),
        ("Starter generator, start", ("switch", "starter_gen", ("START",))),
        ("Battery, on", ("switch", "battery", ("ON",))),
        ("Fire warning light, test", None),
        ("Flares, safe", ("switch", "cm_arm", ("SAFE",))),
        ("Governor, auto", ("switch", "emer_gov", ("AUTO",))),
        ("Main fuel, on", ("switch", "main_fuel", ("ON",))),
        ("Caution lights, test and reset", None),
        ("Hydraulic control, on", ("switch", "hyd_cont", ("ON",))),
        ("Force trim, on", ("switch", "force_trim", ("ON",))),
        ("Chip detector, both", ("switch", "chip_det", ("BOTH",))),
        ("Master arm, off", ("switch", "master_arm", ("OFF", "SAFE"))),
        ("Flight controls free, collective full down", None),
        ("Altimeters, set", None),
    ]),
    "start": ("Starting engine", [
        ("Throttle, set for start, just below the idle stop", None),
        ("Start switch, press and hold. Note the time", None),
        ("Rotor turning by fifteen percent N1", None),
        ("Release the starter at forty percent N1 or forty seconds", None),
        ("Throttle, advance to idle", None),
        ("N1, sixty-eight to seventy-two percent", ("gauge", "n1_pct", 67.5, 72.5, "N1 {:.0f} percent")),
        ("Inverter, main on", ("switch", "inverter", ("MAIN",))),
        ("Engine oil pressure", ("gauge", "eng_oil_psi", 25, 100, "{:.0f} PSI")),
        ("Transmission oil pressure", ("gauge", "xmsn_oil_psi", 30, 70, "{:.0f} PSI")),
    ]),
    "runup": ("Engine runup", [
        ("Avionics, on", None),
        ("Starter generator, standby", ("switch", "starter_gen", ("STBY GEN",))),
        ("Fuel, engine and transmission, no cautions", ("cautions",)),
        ("AC volts, one twelve to one eighteen", ("gauge", "ac_volts", 112, 118, "{:.0f} volts")),
        ("DC volts, twenty-seven to twenty-eight and a half", ("gauge", "dc_volts", 27, 28.5, "{:.1f} volts")),
        ("Throttle full open, sixty-six hundred RPM", ("gauge", "engine_rpm", 6500, 6700, "{:.0f} RPM")),
    ]),
    "before_takeoff": ("Before take-off", [
        ("RPM, sixty-six hundred", ("gauge", "engine_rpm", 6500, 6700, "{:.0f}")),
        ("Engine, transmission, electrical and fuel, no cautions", ("cautions",)),
        ("Avionics, as required", None),
    ]),
    "shutdown": ("Engine shutdown", [
        ("Throttle, idle for two minutes", None),
        ("Force trim, on", ("switch", "force_trim", ("ON",))),
        ("Starter generator, start", ("switch", "starter_gen", ("START",))),
        ("Throttle, off", None),
        ("Main fuel, off", ("switch", "main_fuel", ("OFF",))),
        ("Avionics, off", None),
        ("Inverter, off", ("switch", "inverter", ("OFF",))),
        ("Pitot heat, off", ("switch", "pitot", ("OFF",))),
        ("Nav lights, off", ("switch", "nav_lts", ("OFF",))),
        ("Landing light, off", ("switch", "ldg_light", ("OFF",))),
        ("Search light, off", ("switch", "search_light", ("OFF", "STOW"))),
        ("Battery, off", ("switch", "battery", ("OFF",))),
    ]),
}

REPLIES = ("CHECK", "SET", "SKIP", "REPEAT", "STOP")


class ChecklistRunner:
    """One checklist at a time. Not thread-safe: use from the Huey worker thread only.

    switch_state: key -> current state name or None (unknown / no DCS-BIOS)
    gauge:        key -> value or None
    cautions:     () -> list of lit caution names, or None if unknown
    set_switch:   (key, state) -> (confirmed, sentence)
    """

    def __init__(self, switch_state: Callable[[str], Optional[str]],
                 gauge: Callable[[str], Optional[float]],
                 cautions: Callable[[], Optional[List[str]]],
                 set_switch: Callable[[str, str], Tuple[bool, str]]):
        self.switch_state, self.gauge, self.cautions, self.set_switch = switch_state, gauge, cautions, set_switch
        self.name: Optional[str] = None
        self.index = 0

    @property
    def active(self) -> bool:
        return self.name is not None

    def _items(self):
        return CHECKLISTS[self.name][1]

    def _check(self, item) -> Tuple[Optional[bool], str]:
        """(True/False, what George sees) or (None, '') when he can't tell."""
        check = item[1]
        if check is None:
            return None, ""
        if check[0] == "switch":
            now = self.switch_state(check[1])
            if now is None:
                return None, ""
            return now in check[2], spoken(now)
        if check[0] == "gauge":
            v = self.gauge(check[1])
            if v is None:
                return None, ""
            return check[2] <= v <= check[3], check[4].format(v)
        if check[0] == "cautions":
            lit = self.cautions()
            if lit is None:
                return None, ""
            return not lit, ("no cautions" if not lit else "caution " + ", ".join(lit))
        return None, ""

    def _run(self, prefix: str = "") -> str:
        """Reads from the current item, auto-checking what George can see, until an item
        needs the pilot or the list ends."""
        out = [prefix] if prefix else []
        items = self._items()
        while self.index < len(items):
            item = items[self.index]
            ok, seen = self._check(item)
            if ok:
                out.append(f"{item[0]}. Check.")
                self.index += 1
                continue
            if ok is False:
                out.append(f"{item[0]}. Reads {seen}.")
            else:
                out.append(f"{item[0]}.")
            return " ".join(out)
        title = CHECKLISTS[self.name][0]
        self.name = None
        out.append(f"{title} checklist complete.")
        return " ".join(out)

    def start(self, name: str) -> str:
        self.name, self.index = name, 0
        return self._run(f"{CHECKLISTS[name][0]} checklist.")

    def reply(self, word: str) -> str:
        if not self.active:
            return "No checklist running."
        if word == "STOP":
            title = CHECKLISTS[self.name][0]
            self.name = None
            return f"{title} checklist stopped."
        item = self._items()[self.index]
        if word == "REPEAT":
            ok, seen = self._check(item)
            return f"{item[0]}." + (f" Reads {seen}." if ok is False else "")
        if word == "SKIP":
            self.index += 1
            return self._run("Skipped.")
        if word == "SET":
            check = item[1]
            if not check or check[0] != "switch" or SWITCHES[check[1]][0] is None:
                return "I can't set that one."
            ok, sentence = self.set_switch(check[1], check[2][0])
            if not ok:
                return sentence
            self.index += 1
            return self._run(sentence)
        # CHECK: re-check what George can see; trust the pilot for the rest
        ok, seen = self._check(item)
        if ok is False:
            return f"Still reads {seen}. Say set it, or skip."
        self.index += 1
        return self._run()
