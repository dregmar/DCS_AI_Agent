"""
common/unit_types.py - what every unit type in the mission IS, straight from DCS (no hard-coded lists).

george_comms_hook.lua asks the mission scripting state for each unit's type name, display name,
category and DCS attributes (Unit:getDesc().attributes: "Tanks", "IFV", "SAM TR", "AAA", "MANPADS",
"RADAR_BAND1_FOR_ARM", "Infantry", "Trucks", "Helicopters", ...) and sends them as "TYPES ...".
They're kept here and cached in data/unit_types.json, so types met once are known in later missions
too, before the hook has reported.

Used for:
  * threat weights (intelligence/threat_engine.py): attributes first, the name patterns only when
    DCS hasn't told us about a type;
  * SEAD targets (common/overlord.py): anything DCS says an anti-radiation missile can home on;
  * "the tank" / "the truck" (common/targets.py): by attribute as well as by name;
  * George's target list: his display names ("ZBD-04") map to the same attributes as the type name
    ("ZBD04A").
Thread-safe: updated on the comms-listener thread, read anywhere.
"""
import json
import os
import re
import threading
from typing import Dict, FrozenSet, Optional

try:
    import config
    CACHE_FILE = os.path.join(config.BASE_DIR, "data", "unit_types.json")
except Exception:                                     # tools without config.py
    CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                              "unit_types.json")

# DCS attribute -> threat weight (same scale as threat_engine; the highest that applies wins)
ATTR_WEIGHTS = [
    ({"SAM TR", "SAM SR", "SAM LL", "SAM CC", "SAM", "SAM elements", "LR SAM", "MR SAM", "SR SAM",
      "RADAR_BAND1_FOR_ARM", "RADAR_BAND2_FOR_ARM"}, 100),
    ({"MANPADS", "IR Guided SAM"}, 95),
    ({"AAA", "EWR"}, 90),
    ({"Attack helicopters", "Fighters", "Interceptors", "Multirole fighters", "Strategic bombers",
      "Bombers", "Battleplanes"}, 70),
    ({"Tanks", "HeavyArmoredUnits"}, 60),
    ({"IFV", "ATGM"}, 50),
    ({"APC", "Armored vehicles", "LightArmoredUnits", "Armed vehicles"}, 45),
    ({"Helicopters"}, 40),
    ({"Infantry", "Infantry carriers"}, 35),
    ({"Artillery", "MLRS", "SS_missile", "Indirect fire"}, 30),
    ({"Ships", "Heavy armed ships", "Armed ships", "Light armed ships", "Unarmed ships"}, 30),
    ({"Planes", "Transports", "Tankers", "AWACS"}, 20),
    ({"Fortifications", "Buildings", "Static"}, 15),
    ({"Trucks", "Unarmed vehicles", "Cars", "Ground vehicles"}, 10),
]
ARM_ATTRS = {"RADAR_BAND1_FOR_ARM", "RADAR_BAND2_FOR_ARM"}          # a HARM can home on it
BIG_SAM_ATTRS = {"LR SAM", "MR SAM"}                                 # left to SEAD, not CAS

# spoken word -> attributes (for "attack the tank", "smoke the trucks")
SPOKEN_ATTRS = [
    (r"\b(?:tanks?|armou?r|mbts?)\b", {"Tanks"}),
    (r"\b(?:apcs?|ifvs?|bmps?|btrs?)\b", {"IFV", "APC"}),
    (r"\b(?:trucks?|vehicles?|jeeps?|technicals?)\b", {"Trucks", "Unarmed vehicles", "Cars"}),
    (r"\b(?:infantry|soldiers?|troops|men)\b", {"Infantry"}),
    (r"\b(?:artillery|howitzers?|mlrs|rocket launchers?)\b", {"Artillery", "MLRS"}),
    (r"\b(?:sams?|missile sites?|air defen[cs]es?|radars?)\b", {"SAM", "SAM TR", "SAM SR", "SAM LL", "SAM elements",
                                                                "EWR"}),
    (r"\b(?:zsu|shilka|triple a|aaa|flak|guns?|anti ?aircraft)\b", {"AAA"}),
    (r"\b(?:manpads?|stingers?|iglas?)\b", {"MANPADS"}),
    (r"\b(?:helicopters?|helos?)\b", {"Helicopters"}),
    (r"\b(?:ships?|boats?)\b", {"Ships"}),
]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class UnitTypes:
    def __init__(self, cache_file: Optional[str] = CACHE_FILE):
        self._lock = threading.Lock()
        self.cache_file = cache_file
        self._by_key: Dict[str, dict] = {}            # normalised name -> {"type", "display", "cat", "attrs"}
        self._load()

    def _load(self):
        if not self.cache_file:
            return
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                rows = json.load(f)
        except Exception:
            return
        for r in rows if isinstance(rows, list) else []:
            self._add(r.get("type", ""), r.get("display", ""), r.get("cat", ""), r.get("attrs") or [])

    def _add(self, tn: str, display: str, cat: str, attrs) -> bool:
        if not tn:
            return False
        rec = {"type": tn, "display": display or tn, "cat": cat or "", "attrs": frozenset(attrs)}
        changed = self._by_key.get(_key(tn)) != rec
        self._by_key[_key(tn)] = rec
        if display:
            self._by_key.setdefault(_key(display), rec)
        return changed

    def on_types_message(self, text: str) -> int:
        """'TYPES\\n<type>\\t<display>\\t<cat>\\t<a,b,c>...' from the hook. Returns how many changed."""
        changed = 0
        with self._lock:
            for line in (text or "").split("\n")[1:]:
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                attrs = [a for a in parts[3].split(",") if a]
                changed += self._add(parts[0], parts[1], parts[2], attrs)
            if changed:
                self._save_locked()
        if changed:
            print(f"[TYPES] {changed} unit type(s) learned from DCS ({len(self.types())} known).")
        return changed

    def _save_locked(self):
        if not self.cache_file:
            return
        rows, seen = [], set()
        for rec in self._by_key.values():
            if rec["type"] in seen:
                continue
            seen.add(rec["type"])
            rows.append({"type": rec["type"], "display": rec["display"], "cat": rec["cat"],
                         "attrs": sorted(rec["attrs"])})
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(rows, key=lambda r: r["type"]), f, indent=1)
            os.replace(tmp, self.cache_file)
        except OSError as e:
            print(f"[TYPES] Can't save {self.cache_file}: {e}")

    # ------------------------------------------------------------ lookups
    def types(self):
        with self._lock:
            return {r["type"] for r in self._by_key.values()}

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            rec = self._by_key.get(_key(name))
            return dict(rec) if rec else None

    def attrs(self, name: str) -> FrozenSet[str]:
        rec = self.get(name)
        return rec["attrs"] if rec else frozenset()

    def weight(self, name: str) -> Optional[int]:
        """Threat weight from DCS's attributes, or None if DCS hasn't described this type."""
        a = self.attrs(name)
        if not a:
            return None
        for group, w in ATTR_WEIGHTS:
            if a & group:
                return w
        return 5

    def is_arm_target(self, name: str) -> Optional[bool]:
        """True/False if DCS has described the type, else None (fall back to names)."""
        a = self.attrs(name)
        return bool(a & ARM_ATTRS) if a else None

    def is_big_sam(self, name: str) -> Optional[bool]:
        a = self.attrs(name)
        return bool(a & BIG_SAM_ATTRS) if a else None

    def is_air_defence(self, name: str) -> bool:
        return bool(self.attrs(name) & {"Air Defence", "SAM", "SAM elements", "AAA", "SAM TR", "SAM SR",
                                        "SAM LL", "EWR", "MANPADS"})

    def matches_spoken(self, name: str, text: str) -> bool:
        a = self.attrs(name)
        if not a:
            return False
        return any(re.search(rx, text) and (a & group) for rx, group in SPOKEN_ATTRS)


TYPES = UnitTypes()
