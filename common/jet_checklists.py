"""
common/jet_checklists.py - checklists for the A-10C II and F/A-18C, shown on the kneeboard's Checklist
page only (no voice: jets have no crew). Items are from the DCS manuals (Hornet Early Access Guide,
"Procedures"; A-10C II Flight Manual, "Checklists"), shortened.

The page follows along by itself: an item with a cockpit check ticks when DCS-BIOS shows it done,
and the list moves on. An item without a check (something you do or watch) is passed when the next
checked item after it is done. Ticks are kept once seen, so "APU generator PWR" stays ticked after a
later "APU generator OFF". With no list asked for ("show the taxi checklist"), the first list whose
checked items aren't all done is shown, so cold start is followed by taxi, and so on.

Switch addresses come from DCS-BIOS's own module JSON (Saved Games\\DCS\\Scripts\\DCS-BIOS\\doc\\json),
read at start: nothing is hard-coded but the control names. Position names come from that JSON where
it lists them, else from NAMES below (marked: check them once with jet_probe.py).

Thread-safe: view() runs on the kneeboard thread, reset() on mission load.
"""
import json
import os
import re
import threading
from typing import Callable, Dict, List, Optional, Tuple

MODULE_FOR = {"FA-18C_hornet": "FA-18C_hornet", "A-10C_2": "A-10C", "A-10C": "A-10C"}
DISPLAY = {"FA-18C_hornet": "F/A-18C", "A-10C_2": "A-10C II", "A-10C": "A-10C"}

# Position names in DCS-BIOS value order (0, 1, 2...) where the JSON gives none.
# Hornet two-position toggles: value 1 = the first name in DCS's description ("NORM/OFF" -> 1 = NORM).
# A-10C descriptions list positions low to high ("OFF - ON", "OFF - TEST - ON").
NAMES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "FA-18C_hornet": {
        "L_GEN_SW": ("OFF", "NORM"), "R_GEN_SW": ("OFF", "NORM"), "OBOGS_SW": ("OFF", "ON"),
        "MASTER_ARM_SW": ("SAFE", "ARM"), "RADAR_SW": ("OFF", "STBY", "OPR", "EMERG"),
        "EJECTION_SEAT_ARMED": ("SAFE", "ARMED"), "APU_READY_LT": ("OFF", "ON"),
        # DCS-BIOS: "0 = held left/down, 1 = centered, 2 = held right/up"; which side is A doesn't
        # matter to the check (both must be seen)
        "FIRE_TEST_SW": ("TEST B", "OFF", "TEST A"),
    },
    "A-10C": {
        "EPP_BATTERY_PWR": ("OFF", "ON"), "EPP_INVERTER": ("TEST", "OFF", "STBY"),
        "EPP_AC_GEN_PWR_L": ("OFF", "PWR"), "EPP_AC_GEN_PWR_R": ("OFF", "PWR"), "EPP_APU_GEN_PWR": ("OFF", "PWR"),
        "ENGINE_APU_START": ("OFF", "START"), "AAP_CDUPWR": ("OFF", "ON"), "AAP_EGIPWR": ("OFF", "ON"),
        "AHCP_TGP": ("OFF", "ON"), "AHCP_CICU": ("OFF", "ON"), "AHCP_JTRS": ("OFF", "ON"),
        "AHCP_IFFCC": ("OFF", "TEST", "ON"), "AHCP_MASTER_ARM": ("TRAIN", "SAFE", "ARM"),
        "AHCP_GUNPAC": ("GUNARM", "SAFE", "ARM"), "AHCP_LASER_ARM": ("TRAIN", "SAFE", "ARM"),
        "LMFD_PWR": ("OFF", "NT", "DAY"), "RMFD_PWR": ("OFF", "NT", "DAY"),
        "SASP_YAW_SAS_L": ("OFF", "ENGAGE"), "SASP_YAW_SAS_R": ("OFF", "ENGAGE"),
        "SASP_PITCH_SAS_L": ("OFF", "ENGAGE"), "SASP_PITCH_SAS_R": ("OFF", "ENGAGE"),
        "FLAPS_SWITCH": ("DN", "MVR", "UP"), "ENVCP_PITOT_HEAT": ("OFF", "ON"),
        "SEAT_ARM": ("ARMED", "SAFE"), "CMSP_MODE": ("OFF", "STBY", "MAN", "SEMI", "AUTO"),
        "TACAN_MODE": ("OFF", "REC", "T/R", "A/A REC", "A/A T/R"), "ILS_PWR": ("OFF", "ON"),
    },
}
# Checked against the cockpit in flight (jet_probe.py); the rest are best readings of DCS-BIOS's docs
VERIFIED: set = set()

# Item: (text, check). check: None (you do it) | ("sw", control, (positions...)) |
#   ("both", control, (positions...)): each position seen at some point (a held test switch) |
#   ("frac", control, lo, hi, word when in range, word when not): a 0..1 output (canopy, hook, gauges)
CLOSED = ("frac", "CANOPY_POS", 0.0, 0.03, "closed", "open")
CHECKLISTS: Dict[str, Dict[str, Tuple[str, List[tuple]]]] = {
    "FA-18C_hornet": {
        "cold_start": ("Cold start", [
            ("Battery ON", ("sw", "BATTERY_SW", ("ON",))),
            ("Left generator NORM", ("sw", "L_GEN_SW", ("NORM",))),
            ("Right generator NORM", ("sw", "R_GEN_SW", ("NORM",))),
            ("Fire test A, then B", ("both", "FIRE_TEST_SW", ("TEST A", "TEST B"))),
            ("APU ON, wait for APU READY", ("sw", "APU_READY_LT", ("ON",))),
            ("Engine crank RIGHT", None),
            ("Right throttle IDLE at 25% rpm", None),
            ("Above 60%: bleed air knob NORM round to NORM", None),
            ("Caution, warning and advisory lights test", None),
            ("Left DDI on", ("sw", "LEFT_DDI_BRT_SELECT", ("NIGHT", "DAY"))),
            ("Right DDI on", ("sw", "RIGHT_DDI_BRT_SELECT", ("NIGHT", "DAY"))),
            ("MPCD and HUD on; FCS page left, BIT page right", None),
            ("COMM 1 and COMM 2 as required", None),
            ("Right engine 63-70%: engine crank LEFT", None),
            ("Left throttle IDLE at 25% rpm", None),
            ("Above 60%: INS GND (CV on the carrier)", ("sw", "INS_SW", ("GND", "CV"))),
            ("Radar OPR", ("sw", "RADAR_SW", ("OPR",))),
            ("OBOGS ON, oxygen flow ON", ("sw", "OBOGS_SW", ("ON",))),
            ("FCS RESET, check the FCS page", None),
            ("Flaps AUTO, takeoff trim", None),
            ("FCS BIT (hold the BIT switch, press FCS)", None),
            ("Four down test; flaps HALF", ("sw", "FLAP_SW", ("HALF",))),
            ("Parking brake released", None),
            ("Bingo set on the IFEI", None),
            ("Standby altimeter set", None),
            ("Radar altimeter 200 ft (40 ft on the carrier)", None),
            ("Standby attitude indicator uncaged; attitude source AUTO", None),
        ]),
        "taxi": ("Before taxi", [
            ("Ejection seat ARMED", ("sw", "EJECTION_SEAT_ARMED", ("ARMED",))),
            ("Canopy closed", CLOSED),
            ("Hook up", ("frac", "EXT_HOOK", 0.0, 0.05, "up", "down")),
            ("Flaps HALF", ("sw", "FLAP_SW", ("HALF",))),
            ("Wings spread", ("frac", "EXT_WING_FOLDING", 0.0, 0.05, "spread", "folded")),
            ("Launch bar up", ("frac", "EXT_LAUNCH_BAR", 0.0, 0.05, "up", "down")),
            ("Master arm SAFE", ("sw", "MASTER_ARM_SW", ("SAFE",))),
            ("Oxygen on", ("sw", "OBOGS_SW", ("ON",))),
            ("No warning lights; trim set for weight", None),
            ("Anti-skid off (carrier); countermeasures off", None),
            ("Left DDI checklist page, right DDI FCS page", None),
        ]),
        "takeoff": ("Takeoff", [
            ("Lined up, nosewheel straight", None),
            ("Left DDI HUD page", None),
            ("Afterburner; rotate to 6-8 degrees nose up", None),
            ("Positive climb: gear UP", None),
            ("Flaps AUTO", ("sw", "FLAP_SW", ("AUTO",))),
            ("Right DDI air-to-air radar", None),
        ]),
        "carrier_launch": ("Catapult launch", [
            ("Wings spread", ("frac", "EXT_WING_FOLDING", 0.0, 0.05, "spread", "folded")),
            ("Launch bar down, hooked up to the shuttle", ("frac", "EXT_LAUNCH_BAR", 0.5, 1.01, "down", "up")),
            ("Takeoff trim: 16 below 44,000 lb, 17 to 48,000, 19 above (burner)", None),
            ("Military power, wipe out the controls", None),
            ("Afterburner, hand off the stick", None),
            ("Positive climb: gear UP, flaps AUTO", ("sw", "FLAP_SW", ("AUTO",))),
            ("Clearing turn, then 7 miles at 500 ft / 350 kt", None),
        ]),
        "landing": ("Landing (airfield)", [
            ("NAV master mode, master arm SAFE", ("sw", "MASTER_ARM_SW", ("SAFE",))),
            ("Hook up", ("frac", "EXT_HOOK", 0.0, 0.05, "up", "down")),
            ("Below 250 kt: gear DOWN", None),
            ("Flaps FULL", ("sw", "FLAP_SW", ("FULL",))),
            ("On-speed AoA (8.1 degrees), trimmed", None),
            ("Touchdown: throttles IDLE", None),
        ]),
        "carrier_landing": ("Landing (carrier)", [
            ("Master arm SAFE", ("sw", "MASTER_ARM_SW", ("SAFE",))),
            ("Hook DOWN", ("frac", "EXT_HOOK", 0.9, 1.01, "down", "up")),
            ("Gear DOWN", None),
            ("Flaps FULL", ("sw", "FLAP_SW", ("FULL",))),
            ("On-speed AoA, trimmed", None),
        ]),
    },
    "A-10C": {
        "cold_start": ("Cold start", [
            ("Battery ON", ("sw", "EPP_BATTERY_PWR", ("ON",))),
            ("Inverter STBY", ("sw", "EPP_INVERTER", ("STBY",))),
            ("Left AC generator PWR", ("sw", "EPP_AC_GEN_PWR_L", ("PWR",))),
            ("Right AC generator PWR", ("sw", "EPP_AC_GEN_PWR_R", ("PWR",))),
            ("ITT below 150 C; signal lights test; fuel about 6,000 lb", None),
            ("Oxygen supply on; radios on and set", None),
            ("APU START", ("sw", "ENGINE_APU_START", ("START",))),
            ("APU at 100%, EGT 400-450 C", ("frac", "APU_RPM", 0.95, 1.01, "at 100%", "spooling")),
            ("APU generator PWR", ("sw", "EPP_APU_GEN_PWR", ("PWR",))),
            ("Position lights FLASH; request engine start", ("sw", "LCP_POSITION", ("FLASH",))),
            ("Left throttle IDLE: core 56%, hydraulics 2,800-3,350 psi", None),
            ("Right throttle IDLE: core 56%, hydraulics 2,800-3,350 psi", None),
            ("Cycle the speed brakes", None),
            ("APU generator OFF", ("sw", "EPP_APU_GEN_PWR", ("OFF",))),
            ("APU OFF", ("sw", "ENGINE_APU_START", ("OFF",))),
        ]),
        "preflight": ("Pre-flight set up", [
            ("CDU ON", ("sw", "AAP_CDUPWR", ("ON",))),
            ("EGI ON; let BIT and alignment finish, then NAV", ("sw", "AAP_EGIPWR", ("ON",))),
            ("Load the flight plan", None),
            ("Targeting pod ON", ("sw", "AHCP_TGP", ("ON",))),
            ("CICU ON", ("sw", "AHCP_CICU", ("ON",))),
            ("JTRS ON", ("sw", "AHCP_JTRS", ("ON",))),
            ("IFFCC TEST", ("sw", "AHCP_IFFCC", ("TEST", "ON"))),
            ("Left MFCD on", ("sw", "LMFD_PWR", ("NT", "DAY"))),
            ("Right MFCD on", ("sw", "RMFD_PWR", ("NT", "DAY"))),
            ("Datalink: group and own ID on the TAD network page", None),
            ("STAT page: no malfunctions", None),
            ("Takeoff trim", None),
            ("Yaw SAS engaged (left and right)", ("sw", "SASP_YAW_SAS_L", ("ENGAGE",))),
            ("Pitch SAS engaged (left and right)", ("sw", "SASP_PITCH_SAS_L", ("ENGAGE",))),
            ("NMSP: EGI", None),
            ("Emergency pitch/roll trim tested, back to NORM", None),
        ]),
        "taxi": ("Final checks and taxi", [
            ("Canopy closed", CLOSED),
            ("Throttle check: IDLE to MAX and back, core under 70%", None),
            ("Nosewheel steering engaged", None),
            ("Flaps DN (20 degrees)", ("sw", "FLAPS_SWITCH", ("DN",))),
            ("Oxygen NORMAL; strobe OFF, nav lights DIM FLASH", None),
            ("Taxi at 15-25 kt", None),
        ]),
        "runup": ("Engine run up", [
            ("Strobe ON, nav lights STEADY (taxi lights at night)", None),
            ("Pitot heat ON", ("sw", "ENVCP_PITOT_HEAT", ("ON",))),
            ("Hold the toe brakes", None),
            ("Throttles to 90% core rpm", None),
            ("Engine gauges normal", None),
        ]),
        "landing": ("Landing approach", [
            ("Master arm SAFE", ("sw", "AHCP_MASTER_ARM", ("SAFE",))),
            ("TACAN or ILS set for the field", None),
            ("Speed brakes 40%, gear DOWN", None),
            ("Flaps DN", ("sw", "FLAPS_SWITCH", ("DN",))),
            ("On-speed AoA, 150 KIAS on final", None),
        ]),
        "shutdown": ("Shut down", [
            ("Pitot heat OFF", ("sw", "ENVCP_PITOT_HEAT", ("OFF",))),
            ("Position lights FLASH, anti-collision OFF", None),
            ("Speed brakes closed, parking brake set, anti-skid OFF", None),
            ("Canopy open", ("frac", "CANOPY_POS", 0.5, 1.01, "open", "closed")),
            ("TACAN OFF", ("sw", "TACAN_MODE", ("OFF",))),
            ("ILS OFF", ("sw", "ILS_PWR", ("OFF",))),
            ("IFFCC OFF", ("sw", "AHCP_IFFCC", ("OFF",))),
            ("CICU OFF", ("sw", "AHCP_CICU", ("OFF",))),
            ("Left MFCD OFF", ("sw", "LMFD_PWR", ("OFF",))),
            ("Right MFCD OFF", ("sw", "RMFD_PWR", ("OFF",))),
            ("Landing / taxi lights OFF", ("sw", "LANDING_LIGHTS", ("OFF",))),
            ("Countermeasures OFF", ("sw", "CMSP_MODE", ("OFF",))),
            ("Flaps UP", ("sw", "FLAPS_SWITCH", ("UP",))),
            ("EGI OFF", ("sw", "AAP_EGIPWR", ("OFF",))),
            ("CDU OFF", ("sw", "AAP_CDUPWR", ("OFF",))),
            ("Throttles OFF after 5 minutes at IDLE", None),
            ("Inverter OFF", ("sw", "EPP_INVERTER", ("OFF",))),
            ("Battery OFF", ("sw", "EPP_BATTERY_PWR", ("OFF",))),
        ]),
    },
}

# "show the cold start checklist" -> key; also the crews' names said in a jet
_NAMES = [
    ("carrier_landing", r"\b(?:carrier|ship|boat) (?:landing|recovery|trap)\b|\bcase (?:one|1|i)\b|\brecovery\b"),
    ("carrier_launch", r"\b(?:carrier |catapult |cat )(?:launch|takeoff|take off|shot)\b|\bcat shot\b|\blaunch\b"),
    ("cold_start", r"\bcold (?:and dark )?start\b|\bstart ?up\b|\bbefore start\b|\bstart(?:ing)?\b"),
    ("preflight", r"\bpre ?flight\b|\bset ?up\b"),
    ("taxi", r"\btaxi\b|\bfinal checks?\b"),
    ("runup", r"\brun ?up\b"),
    ("takeoff", r"\btake ?off\b"),
    ("landing", r"\blanding\b|\bapproach\b|\brecovery\b"),
    ("shutdown", r"\bshut ?down\b"),
]
_NAMES = [(k, re.compile(rx, re.I)) for k, rx in _NAMES]
# what the crews' list names mean in a jet
_ALIAS = {"start": "cold_start", "before_start": "cold_start", "before_takeoff": "taxi"}


def match_name(text: str) -> Optional[str]:
    for key, rx in _NAMES:
        if rx.search(text or ""):
            return key
    return None


def default_json_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Saved Games", "DCS", "Scripts", "DCS-BIOS", "doc", "json")


class BiosModule:
    """Output addresses of one DCS-BIOS module, from its JSON: control -> (address, mask, shift, max, positions)."""

    def __init__(self, path: str, extra_names: Optional[Dict[str, Tuple[str, ...]]] = None,
                 lua_path: Optional[str] = None):
        """lua_path: the module's DCS-BIOS Lua (lib/modules/aircraft_modules/<module>.lua). Its
        define*3PosTumb switches list their positions top first, the reverse of the value order
        (29 Oct: the Hornet's battery read ORIDE as ON), so those lists are turned round."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.reversed = set()
        if lua_path:
            try:
                with open(lua_path, encoding="utf-8", errors="replace") as fh:
                    self.reversed = set(re.findall(r'define\w*3PosTumb\(\s*"(\w+)"', fh.read()))
            except OSError as e:
                print(f"[CHECKLIST] {lua_path} not readable ({e}): 3-position switches may read upside down.")
        self.controls: Dict[str, tuple] = {}
        for cat in data.values():
            for ident, c in cat.items():
                outs = [o for o in c.get("outputs", []) if o.get("type") == "integer"]
                if not outs:
                    continue
                o = outs[0]
                names = tuple(c.get("positions") or ())
                if names and ident in self.reversed:
                    names = names[::-1]
                names = names or tuple((extra_names or {}).get(ident, ()))
                if not names and o.get("max_value") == 1 and c.get("control_type") in ("led", "metadata"):
                    names = ("OFF", "ON")
                self.controls[ident] = (int(o["address"]), int(o["mask"]), int(o["shift_by"]),
                                        int(o["max_value"]), names)

    def raw(self, words: Dict[int, int], ident: str) -> Optional[int]:
        c = self.controls.get(ident)
        if c is None or c[0] not in words:
            return None
        return (words[c[0]] & c[1]) >> c[2]

    def state(self, words: Dict[int, int], ident: str) -> Optional[str]:
        v = self.raw(words, ident)
        names = self.controls[ident][4] if ident in self.controls else ()
        if v is None or v >= len(names):
            return None
        return names[v]

    def fraction(self, words: Dict[int, int], ident: str) -> Optional[float]:
        v = self.raw(words, ident)
        return None if v is None else v / max(1, self.controls[ident][3])


def controls_used(aircraft_key: str) -> List[str]:
    out = []
    for _, items in CHECKLISTS.get(aircraft_key, {}).values():
        for _, chk in items:
            if chk and chk[1] not in out:
                out.append(chk[1])
    return out


class JetChecklists:
    def __init__(self, words: Callable[[], Dict[int, int]], live: Callable[[], bool],
                 json_dir: Optional[str] = None):
        """words: () -> DCS-BIOS words {address: value}; live: () -> DCS-BIOS data is fresh."""
        self.words, self.live = words, live
        self.json_dir = json_dir or default_json_dir()
        self._lock = threading.Lock()
        self._modules: Dict[str, Optional[BiosModule]] = {}
        self._pos: Dict[Tuple[str, str], int] = {}          # (aircraft, list) -> items done so far
        self._seen: Dict[Tuple[str, str], set] = {}         # (aircraft, list) -> item indexes ticked
        self._held: Dict[tuple, set] = {}                    # held test switches: positions seen

    # ------------------------------------------------------------ set up
    @staticmethod
    def key_for(aircraft: Optional[str]) -> Optional[str]:
        a = aircraft or ""
        for k in CHECKLISTS:
            if a == k or a.startswith(k + "_") or (MODULE_FOR.get(a) == k):
                return k
        return None

    def module(self, key: str) -> Optional[BiosModule]:
        with self._lock:
            if key not in self._modules:
                path = os.path.join(self.json_dir, MODULE_FOR.get(key, key) + ".json")
                lua = os.path.join(self.json_dir, os.pardir, os.pardir, "lib", "modules", "aircraft_modules",
                                   MODULE_FOR.get(key, key) + ".lua")
                try:
                    self._modules[key] = BiosModule(path, NAMES.get(key), os.path.normpath(lua))
                except (OSError, ValueError) as e:
                    print(f"[CHECKLIST] DCS-BIOS module for {key} not readable ({path}): {e}")
                    self._modules[key] = None
            return self._modules[key]

    def addresses(self) -> set:
        """Every DCS-BIOS address the jet checklists read (for the DCS-BIOS listener)."""
        out = set()
        for key in CHECKLISTS:
            mod = self.module(key)
            if mod is None:
                continue
            for ident in controls_used(key):
                c = mod.controls.get(ident)
                if c is None:
                    print(f"[CHECKLIST] {key}: control {ident} not in DCS-BIOS (renamed?)")
                    continue
                out.add(c[0])
        return out

    def reset(self) -> None:
        with self._lock:
            self._pos.clear()
            self._seen.clear()
            self._held.clear()

    # ------------------------------------------------------------ reading
    def check(self, mod: BiosModule, words, chk) -> Tuple[Optional[bool], str]:
        if chk is None or mod is None:
            return None, ""
        if chk[0] == "sw":
            s = mod.state(words, chk[1])
            return (None, "") if s is None else (s in chk[2], s.lower())
        if chk[0] == "frac":
            f = mod.fraction(words, chk[1])
            if f is None:
                return None, ""
            ok = chk[2] <= f <= chk[3]
            return ok, chk[4] if ok else chk[5]
        if chk[0] == "both":
            s = mod.state(words, chk[1])
            with self._lock:
                held = self._held.setdefault((id(mod), chk[1]), set())
                if s in chk[2]:
                    held.add(s)
                got = set(held)
            todo = [p for p in chk[2] if p not in got]
            if not todo:
                return True, "done"
            return (None if not got and s is None else False), ", ".join(p.lower() for p in todo) + " to do"
        return None, ""

    def _follow(self, ak: str, name: str, items, readings) -> int:
        """Moves the list on: a checked item when it reads right, an item you do when the next checked
        item after it reads right. Returns how many items are done (never goes back)."""
        with self._lock:
            pos = self._pos.get((ak, name), 0)
            seen = self._seen.setdefault((ak, name), set())
            while pos < len(items):
                ok = readings[pos][0]
                if items[pos][1] is not None:
                    if ok:
                        seen.add(pos)
                        pos += 1
                        continue
                    break
                nxt = next((j for j in range(pos + 1, len(items)) if items[j][1] is not None), None)
                if nxt is not None and readings[nxt][0]:
                    pos += 1
                    continue
                break
            self._pos[(ak, name)] = pos
            return pos

    def lists(self, aircraft: Optional[str]) -> Optional[Dict]:
        k = self.key_for(aircraft)
        return CHECKLISTS.get(k) if k else None

    def resolve(self, aircraft: Optional[str], name: Optional[str]) -> Optional[str]:
        lists = self.lists(aircraft) or {}
        if name in lists:
            return name
        name = _ALIAS.get(name or "", name)
        return name if name in lists else None

    def view(self, aircraft: Optional[str], chosen: Optional[str] = None) -> Dict:
        """The Checklist page's data, in the same shape as kneeboard.pages.build_checklist."""
        ak = self.key_for(aircraft)
        out = {"aircraft": DISPLAY.get(aircraft or "", aircraft), "lists": [], "shown": None, "running": None, "items": [],
               "outcome": None, "auto_show": False}
        if ak is None:
            return out
        lists = CHECKLISTS[ak]
        out["lists"] = [{"key": k, "title": t} for k, (t, _) in lists.items()]
        mod = self.module(ak)
        live = bool(self.live())
        words = self.words() if live else {}
        done_all = {}
        readings_all = {}
        for name, (_, items) in lists.items():
            readings = [self.check(mod, words, chk) if live else (None, "") for _, chk in items]
            readings_all[name] = readings
            pos = self._follow(ak, name, items, readings) if live else self._pos.get((ak, name), 0)
            with self._lock:
                seen = set(self._seen.get((ak, name), set()))
            checked = [i for i, it in enumerate(items) if it[1] is not None]
            done_all[name] = (pos, seen, all(i in seen for i in checked))
        shown = self.resolve(aircraft, chosen) or next((n for n in lists if not done_all[n][2]), next(iter(lists)))
        title, items = lists[shown]
        pos, seen, _ = done_all[shown]
        out.update(shown=shown, title=title, running=shown, live=live)
        if pos >= len(items):
            out["outcome"] = "complete"
        for i, (text, chk) in enumerate(items):
            ok, word = readings_all[shown][i]
            row = {"text": text, "auto": chk is not None}
            if i in seen:
                row["state"] = "seen"
            elif i < pos:
                row["state"] = "passed"
            elif i == pos:
                row["state"] = "current"
            else:
                row["state"] = "pending"
            if row["state"] in ("current", "pending") and chk is not None:
                row["ok"], row["seen"] = ok, word
            out["items"].append(row)
        if not live:
            out["note"] = "No cockpit data (DCS-BIOS): items can't tick."
        return out
