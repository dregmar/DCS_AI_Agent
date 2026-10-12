import threading
import time
from typing import Dict, List, Any

INF = float("inf")

# Targets older than this are dropped from snapshots so George never "locks" a ghost.
TARGET_STALE_SEC = 5.0
# George's Target List stays usable this long after it was last seen on the wheel, so
# "lock target 2" still works after the pilot closes the list.
GEORGE_LIST_STALE_SEC = 120.0
# George's list shows classifications, not types, until he has identified a contact.
_GENERIC_NAMES = {"GROUND", "ARMORED", "AAA", "AIR", "HELICOPTER", "NAVAL", "UNKNOWN", "TRACKED", ""}

# DCS-BIOS memory addresses we actually care about. The parser only calls into the
# state store for these, which avoids taking the lock for every word in the stream.
from config import BIOS_OUTPUTS, MASTER_ARM_ARMED_VALUE
from huey.bios_map import watched_addresses as _huey_addresses
_APACHE_ADDRESSES = {addr for addr, _, _ in BIOS_OUTPUTS.values()}
_HUEY_ADDRESSES = _huey_addresses()
WATCHED_ADDRESSES = _APACHE_ADDRESSES | _HUEY_ADDRESSES

# The Huey link (george_huey_export.lua) only sends while you fly the UH-1H, so a live link
# means the Huey profile; otherwise the aircraft name from Export.lua decides.
HUEY_LINK_FRESH_SEC = 3.0
HUEY_NAMES = {"UH-1H"}


def clock_position(brg_deg, hdg_deg):
    """Absolute bearing to a target + own heading -> clock position 1..12 (None if unknown)."""
    if brg_deg is None or hdg_deg is None or hdg_deg < 0:
        return None
    rel = (float(brg_deg) - float(hdg_deg)) % 360.0
    h = int(round(rel / 30.0)) % 12
    return 12 if h == 0 else h


def _f(data: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


class ApacheStateStore:
    def __init__(self):
        self._lock = threading.Lock()

        # Flight Dynamics
        self.alt_ft: float = 0.0
        self.speed_kts: float = 0.0
        self.pitch_deg: float = 0.0
        self.roll_deg: float = 0.0
        self.yaw_deg: float = 0.0
        self.hdg_deg: float = -1.0          # true heading from Export.lua; -1 = unknown
        self.agl_ft: float = -1.0           # height above ground; -1 = unknown
        self.vs_fpm: float = 0.0            # vertical speed, ft/min (+ = climbing)
        self.aircraft: str = ""             # LoGetSelfData().Name, e.g. "AH-64D_BLK_II", "UH-1H"

        # UH-1H crew link (huey/link.py): gauges, lamps, Crew Status panel, flares
        self.huey: Dict[str, Any] = {}
        self.huey_rx = 0.0
        # Raw DCS-BIOS words for the Huey addresses (huey/bios_map.py decodes them)
        self.bios_words: Dict[int, int] = {}

        # Cockpit Switch States (DCS-BIOS)
        self.master_arm: str = "DISARMED"  # DISARMED / ARMED
        self.sight_select: str = "UNKNOWN" # FCR / HMD / TADS, from the WPN page
        self.was_state: str = "UNKNOWN"    # NONE / GUN / MSL / RKT, from the WPN page
        self.wpn_page_rx = 0.0             # when the WPN page was last seen

        # George's AI Helper Interface (core/george_ui.py) and his Target List
        self.george_ui: Dict[str, Any] = {"visible": False}
        self.george_ui_rx = 0.0
        self.george_list: List[str] = []          # names in George's list order (index 0 = #1)
        self.george_list_complete = False         # True once the end of the list has been seen
        self.george_list_rx = 0.0

        # Nearby enemy units from Export.lua (LoGetWorldObjects): only used for ranges
        self.contacts: List[Dict[str, Any]] = []
        # Own stores from Export.lua (LoGetPayloadInfo); None = unknown
        self.ammo: Dict[str, Any] = {"msl": None, "rkt": None, "gun": None}
        # Targets George is engaging, in order: [{"index", "type", "contact_id"}]
        self.engagement: List[Dict[str, Any]] = []
        self.engagement_total = 0                 # how many targets the engagement started with

        # Target Tracking List (Raw & Evaluated)
        self.raw_targets: List[Dict[str, Any]] = []
        self.evaluated_targets: List[Dict[str, Any]] = []
        self.highest_threat: Dict[str, Any] = {}

        # Constraint Alerts
        self.constraints: Dict[str, Any] = {
            "tads_masked": False,
            "out_of_range": False,
            "arm_disarmed_warning": False
        }

        # Freshness (time.monotonic(); 0.0 means "never received")
        self.telemetry_last_rx = 0.0
        self.targets_last_rx = 0.0
        self.bios_last_rx = 0.0

    def update_telemetry(self, telemetry_data: Dict[str, Any]):
        """Updates flight dynamics and raw target streams safely."""
        now = time.monotonic()
        with self._lock:
            self.alt_ft = _f(telemetry_data, "alt_ft", self.alt_ft)
            self.speed_kts = _f(telemetry_data, "speed_kts", self.speed_kts)
            self.pitch_deg = _f(telemetry_data, "pitch", self.pitch_deg)
            self.roll_deg = _f(telemetry_data, "roll", self.roll_deg)
            self.yaw_deg = _f(telemetry_data, "yaw", self.yaw_deg)
            self.hdg_deg = _f(telemetry_data, "hdg", self.hdg_deg)
            self.agl_ft = _f(telemetry_data, "agl_ft", self.agl_ft)
            self.vs_fpm = _f(telemetry_data, "vs_fpm", self.vs_fpm)
            ac = telemetry_data.get("ac")
            if isinstance(ac, str) and ac:
                self.aircraft = ac
            self.telemetry_last_rx = now

            # Targets now come from George's own Target List (set_george_list); the telemetry
            # only brings nearby enemy units, used to put ranges on George's entries.
            contacts = telemetry_data.get("contacts")
            if isinstance(contacts, list):
                self.contacts = [c for c in contacts if isinstance(c, dict)]
            ammo = telemetry_data.get("ammo")
            if isinstance(ammo, list) and len(ammo) == 3:
                def _n(v):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        return None
                    return v if v >= 0 else None
                self.ammo = {"msl": _n(ammo[0]), "rkt": _n(ammo[1]), "gun": _n(ammo[2])}

    def update_george_ui(self, ui: Dict[str, Any]):
        """Latest parsed George interface (core/george_ui.py)."""
        with self._lock:
            self.george_ui = ui
            self.george_ui_rx = time.monotonic()

    def george_ui_snapshot(self):
        """(ui dict, receive time in time.monotonic(); 0.0 = never)."""
        with self._lock:
            return dict(self.george_ui), self.george_ui_rx

    def set_george_list(self, names: List[str], complete: bool):
        """George's Target List as far as it has been seen (called by the George driver)."""
        with self._lock:
            self.george_list = list(names)
            self.george_list_complete = bool(complete)
            self.george_list_rx = time.monotonic()
            self.raw_targets = self._targets_from_list_locked()
            self.targets_last_rx = self.george_list_rx

    def _targets_from_list_locked(self) -> List[Dict[str, Any]]:
        """George's list entries with a range from the nearest unused matching contact."""
        used = set()
        out = []
        for i, name in enumerate(self.george_list):
            dist, brg, contact_id, key = None, None, None, name.strip().lower()
            if key and name.strip().upper() not in _GENERIC_NAMES:
                for c in self.contacts:               # contacts are sorted nearest first
                    ctype = str(c.get("type", "")).lower()
                    cid = c.get("id")
                    if cid in used:
                        continue
                    if ctype == key or ctype.startswith(key) or key.startswith(ctype) or key in ctype:
                        used.add(cid)
                        dist, brg, contact_id = c.get("dist_m"), c.get("brg"), cid
                        break
            out.append({"id": i + 1, "type": name or "UNKNOWN", "dist_m": dist, "brg": brg,
                        "clock": clock_position(brg, self.hdg_deg), "contact_id": contact_id})
        return out

    def update_wpn_page(self, was: str = None, sight: str = None):
        """Your actioned weapon / selected sight as shown on the WPN page (core/wpn_page_listener.py)."""
        with self._lock:
            if was:
                self.was_state = was
            if sight:
                self.sight_select = sight
            self.wpn_page_rx = time.monotonic()

    def set_engagement(self, indices: List[int]):
        """George is engaging these entries of his Target List, in this order ([] = none)."""
        with self._lock:
            targets = self._targets_from_list_locked()
            self.engagement = [{"index": i, "type": targets[i - 1]["type"],
                                "contact_id": targets[i - 1]["contact_id"]}
                               for i in indices if 1 <= i <= len(targets)]
            self.engagement_total = len(self.engagement)

    def pop_engagement(self) -> Dict[str, Any]:
        """Drop the current engagement target (destroyed); returns the next one or {}."""
        with self._lock:
            if self.engagement:
                self.engagement.pop(0)
            return dict(self.engagement[0]) if self.engagement else {}

    def mark_bios_frame(self):
        """Called for every DCS-BIOS datagram received (link health)."""
        with self._lock:
            self.bios_last_rx = time.monotonic()

    def update_huey(self, data: Dict[str, Any]):
        """Latest packet from the Huey link (huey/link.py)."""
        with self._lock:
            self.huey = data
            self.huey_rx = time.monotonic()

    def huey_snapshot(self):
        """(huey dict, receive time; 0.0 = never). Cheaper than snapshot() for polling loops."""
        with self._lock:
            return dict(self.huey), self.huey_rx

    def bios_words_snapshot(self) -> Dict[int, int]:
        with self._lock:
            return dict(self.bios_words)

    def profile(self) -> str:
        """'huey' while flying the UH-1H, otherwise 'apache' (the default)."""
        now = time.monotonic()
        with self._lock:
            if self.huey_rx and now - self.huey_rx <= HUEY_LINK_FRESH_SEC:
                return "huey"
            return "huey" if self.aircraft in HUEY_NAMES else "apache"

    def update_cockpit_switches(self, address: int, value: int):
        """Decodes DCS-BIOS words using the (address, mask, shift) table in config.BIOS_OUTPUTS.
        Huey words are kept raw in bios_words (decoded on demand by huey/bios_map.py)."""
        with self._lock:
            if address in _HUEY_ADDRESSES:
                self.bios_words[address] = value
                if address not in _APACHE_ADDRESSES:
                    return
            for name, (addr, mask, shift) in BIOS_OUTPUTS.items():
                if addr != address:
                    continue
                field = (value & mask) >> shift
                if name == "master_arm":
                    self.master_arm = "ARMED" if field == MASTER_ARM_ARMED_VALUE else "DISARMED"
                elif name == "sight_select":
                    self.sight_select = {0: "FCR", 1: "HMD", 2: "TADS"}.get(field, "HMD")
                elif name == "was_state":
                    self.was_state = {0: "NONE", 1: "GUN", 2: "MSL", 3: "RKT"}.get(field, "NONE")

    def set_evaluated_tactical_data(self, evaluated_targets: List[Dict[str, Any]],
                                    highest_threat: Dict[str, Any], constraints: Dict[str, Any]):
        """Stores calculated threat rankings and constraint alerts."""
        with self._lock:
            self.evaluated_targets = evaluated_targets
            self.highest_threat = highest_threat
            self.constraints = constraints

    @staticmethod
    def _age(last: float, now: float) -> float:
        return INF if last == 0.0 else now - last

    def snapshot(self) -> Dict[str, Any]:
        """Thread-safe copy of current state.

        NOTE: "targets" is the RAW target list (what the threat engine consumes).
        Evaluated/ranked results are under "evaluated_targets".
        """
        now = time.monotonic()
        with self._lock:
            list_age = self._age(self.george_list_rx, now)
            raw = [] if list_age > GEORGE_LIST_STALE_SEC else [dict(t) for t in self._targets_from_list_locked()]
            return {
                "alt_ft": self.alt_ft,
                "speed_kts": self.speed_kts,
                "pitch": self.pitch_deg,
                "roll": self.roll_deg,
                "yaw": self.yaw_deg,
                "hdg": self.hdg_deg,
                "agl_ft": self.agl_ft,
                "vs_fpm": self.vs_fpm,
                "aircraft": self.aircraft,
                "huey": dict(self.huey),
                "huey_age": self._age(self.huey_rx, now),
                "master_arm": self.master_arm,
                "sight_select": self.sight_select,
                "was_state": self.was_state,
                "targets": raw,
                "evaluated_targets": [dict(t) for t in self.evaluated_targets],
                "highest_threat": dict(self.highest_threat),
                "constraints": dict(self.constraints),
                "telemetry_age": self._age(self.telemetry_last_rx, now),
                "bios_age": self._age(self.bios_last_rx, now),
                "wpn_page_age": self._age(self.wpn_page_rx, now),
                "george_ui": dict(self.george_ui),
                "george_ui_age": self._age(self.george_ui_rx, now),
                "george_list_complete": self.george_list_complete,
                "contacts": [dict(c) for c in self.contacts],
                "ammo": dict(self.ammo),
                "engagement": [dict(e) for e in self.engagement],
                "engagement_total": self.engagement_total,
            }


# Instantiate global thread-safe state store
state_store = ApacheStateStore()