import math
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

INF = float("inf")

# Targets older than this are dropped from snapshots so George never "locks" a ghost.
TARGET_STALE_SEC = 5.0
# George's Target List stays usable this long after it was last seen on the wheel, so
# "lock target 2" still works after the pilot closes the list.
GEORGE_LIST_STALE_SEC = 120.0
# Export.lua rescans the enemy / air-defence / Olympus lists once a second; a failed scan keeps sending
# the previous lists. Past this age (model seconds since the last scan + time since the packet) they're
# stale: no kill calls or new tasking from them (audit step 3). An old Export.lua sends no scan time:
# then the age is None and nothing changes.
ENEMY_LIST_STALE_SEC = 5.0


def enemy_lists_stale(snap: Dict[str, Any]) -> bool:
    age = snap.get("enemy_lists_age")
    return age is not None and age > ENEMY_LIST_STALE_SEC


def stale_reason(snap: Dict[str, Any]) -> Optional[str]:
    """"my picture is 12 seconds old" / "I have no picture from DCS" when stale, else None."""
    if not enemy_lists_stale(snap):
        return None
    age = snap["enemy_lists_age"]
    if age == INF:
        return "I have no picture from DCS"
    return f"my picture is {int(age)} seconds old"
# George's list shows classifications, not types, until he has identified a contact.
_GENERIC_NAMES = {"GROUND", "ARMORED", "AAA", "AIR", "HELICOPTER", "NAVAL", "UNKNOWN", "TRACKED", ""}

# DCS-BIOS memory addresses we actually care about. The parser only calls into the
# state store for these, which avoids taking the lock for every word in the stream.
from config import BIOS_OUTPUTS, MASTER_ARM_ARMED_VALUE
from huey.bios_map import watched_addresses as _huey_addresses
from apache.bios_map import watched_addresses as _apache_word_addresses
_APACHE_ADDRESSES = {addr for addr, _, _ in BIOS_OUTPUTS.values()}
# Raw words kept for on-demand decoding: Huey switches/radios, Apache checklist switches + EUFD
_HUEY_ADDRESSES = _huey_addresses() | _apache_word_addresses()
WATCHED_ADDRESSES = _APACHE_ADDRESSES | _HUEY_ADDRESSES


def watch_raw(addresses) -> None:
    """More DCS-BIOS words to keep raw (the jet checklists). Call before the DCS-BIOS thread starts;
    the sets are changed in place, so the parser's reference sees them."""
    _HUEY_ADDRESSES.update(addresses)
    WATCHED_ADDRESSES.update(addresses)

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


FLIGHT_RING = 600          # a minute at 10 Hz


def _flight_record(seq: int, d: Dict[str, Any], aircraft: str) -> Dict[str, Any]:
    """This packet's own flight fields (None = not in it); "fast" = the 10 Hz flight block."""
    def num(k):
        v = d.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def vec(k, n):
        v = d.get(k)
        if isinstance(v, list) and len(v) == n:
            try:
                return tuple(float(x) for x in v)
            except (TypeError, ValueError):
                return None
        return None
    return {"seq": seq, "t": num("t"), "aircraft": aircraft, "fast": d.get("k") == "f",
            "pos": vec("pos", 3), "vel": vec("vel", 3), "agl_ft": num("agl_ft"), "vs_fpm": num("vs_fpm"),
            "hdg": num("hdg"), "speed_kts": num("speed_kts"), "pitch": num("pitch"), "roll": num("roll"),
            "g": num("g"), "gmax": num("gmax"), "ll": vec("ll", 2)}


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
        self.pos = None                     # (north_m, east_m, alt_m) in DCS world coordinates
        self.vel = None                     # (north, east, up) m/s over the ground
        self.route: List[Any] = []          # [(waypoint number, north_m, east_m)] from LoGetRoute
        self.wp_next = -1                   # active waypoint number, -1 = unknown
        # The mission route from george_comms_hook.lua, for aircraft where LoGetRoute gives nothing
        # (AH-64D, UH-1H). Its "next" waypoint is George's own guess: the route in order, moving on
        # when we pass within WP_REACHED_M of a waypoint (the TSD may be set to another one).
        self.mission_route: List[Any] = []
        self.mission_wp = -1
        # Your flight, from the mission (comms hook): group name and [(n, unit name, skill, is_me)];
        # flight_alive: unit names of the others that Export.lua still sees (None = not reported)
        self.flight_group = ""
        self.flight_units: List[Any] = []
        self.flight_alive = None
        self.flight_alive_rx = 0.0
        self.my_group = ""                  # your real group / unit name (Export.lua)
        self.my_unit = ""
        # For DCS Olympus orders: your lat/lon and object id, your wingmen [(name, id, lat, lon)]
        self.my_ll = None
        self.self_id = -1
        self.wing: List[Any] = []
        # For Overlord (common/overlord.py): enemy air-defence radars [(type, id, lat, lon)], nearest
        # first, and the units of AI flights DCS Olympus spawned [(group, unit, id, lat, lon, alt_m)]
        self.air_defence: List[Any] = []
        self.olympus_units: List[Any] = []
        self.olympus_enemy: List[Any] = []       # enemy-side "Olympus-" units: training targets (step 7)
        # ... and for CAP / escort / CAS: enemy aircraft [(type, id, lat, lon, alt_m)] within 120 km
        # and enemy ground units [(type, id, lat, lon)] within 40 km, nearest first
        self.coalition = 0                  # your coalition (Export.lua CoalitionID: 1 red, 2 blue)
        self.enemy_air: List[Any] = []
        self.enemy_ground: List[Any] = []
        self.scan_lag: Optional[float] = None    # model s from Export.lua's last world scan to its packet
        self.scan_rx = 0.0                       # when that packet arrived
        self.scan_t: Optional[float] = None      # that scan's own model time (-1: none yet; None: old Export.lua)
        self.cargo = None                   # nearest sling-load cargo (north_m, east_m, alt_m) or None
        self.fuel_raw = None                # (internal, external) from LoGetEngineInfo: kg, or 0..1
        self.dcs_fuel = None                # (fraction, max internal kg, unit name) from the comms hook
        self.dcs_fuel_t = 0.0
        self.start_pos = None               # where this flight started: "home" when there's no route

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

        # Export.lua's model time of the latest packet (None = not sent), and a count of telemetry packets
        # received, so a sampler can tell a new packet from the same one read twice (Instructor Mode)
        self.t_model: Optional[float] = None
        self.tel_seq = 0
        # every packet's flight fields, as sent (full 4 Hz packets and the 10 Hz flight block, I5), so the
        # instructor's 10 Hz observer gets each one exactly once (flight_since) instead of sampling
        self.flight_ring: deque = deque(maxlen=FLIGHT_RING)
        self.g: Optional[float] = None

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
            def _vec(v, n):
                if isinstance(v, list) and len(v) == n:
                    try:
                        return tuple(float(x) for x in v)
                    except (TypeError, ValueError):
                        return None
                return None
            if "pos" in telemetry_data:
                gap = now - self.telemetry_last_rx if self.telemetry_last_rx else 1e9
                ac = telemetry_data.get("ac")
                self.pos = _vec(telemetry_data.get("pos"), 3)
                if self.pos and (self.start_pos is None or gap > 30.0 or (ac and ac != self.aircraft)):
                    self.start_pos = self.pos
                self._advance_mission_wp()
            if "vel" in telemetry_data:
                self.vel = _vec(telemetry_data.get("vel"), 3)
            if "cargo" in telemetry_data:
                self.cargo = _vec(telemetry_data.get("cargo"), 3)
            route = telemetry_data.get("route")
            if isinstance(route, list):
                self.route = [tuple(p) for p in route if isinstance(p, list) and len(p) == 3]
            try:
                self.wp_next = int(telemetry_data.get("wp", self.wp_next))
            except (TypeError, ValueError):
                pass
            ac = telemetry_data.get("ac")
            if isinstance(ac, str) and ac:
                self.aircraft = ac
            t_model = telemetry_data.get("t")
            self.t_model = float(t_model) if isinstance(t_model, (int, float)) and not isinstance(t_model, bool) else None
            self.tel_seq += 1
            self.telemetry_last_rx = now
            g = telemetry_data.get("g")
            self.g = float(g) if isinstance(g, (int, float)) and not isinstance(g, bool) else None
            self.flight_ring.append(_flight_record(self.tel_seq, telemetry_data, self.aircraft))

            # Targets now come from George's own Target List (set_george_list); the telemetry
            # only brings nearby enemy units, used to put ranges on George's entries.
            contacts = telemetry_data.get("contacts")
            if isinstance(contacts, list):
                self.contacts = [c for c in contacts if isinstance(c, dict)]
            if isinstance(telemetry_data.get("group"), str):
                self.my_group = telemetry_data["group"]
                self.my_unit = str(telemetry_data.get("unit") or "")
            ll = telemetry_data.get("ll")
            if isinstance(ll, list) and len(ll) == 2:
                try:
                    self.my_ll = (float(ll[0]), float(ll[1]))
                except (TypeError, ValueError):
                    pass
            if isinstance(telemetry_data.get("self_id"), (int, float)):
                self.self_id = int(telemetry_data["self_id"])
            wing = telemetry_data.get("wing")
            if isinstance(wing, list):
                self.wing = [tuple(w) for w in wing if isinstance(w, list) and len(w) == 4]
            ad = telemetry_data.get("ad")
            if isinstance(ad, list):
                self.air_defence = [tuple(a) for a in ad if isinstance(a, list) and len(a) in (4, 5)]  # + alt (m)
            oly = telemetry_data.get("oly")
            if isinstance(oly, list):
                self.olympus_units = [tuple(o) for o in oly if isinstance(o, list) and len(o) == 6]
            olyx = telemetry_data.get("olyx")
            if isinstance(olyx, list):
                self.olympus_enemy = [tuple(o) for o in olyx if isinstance(o, list) and len(o) == 6]
            if isinstance(telemetry_data.get("coal"), (int, float)) and telemetry_data["coal"] > 0:
                self.coalition = int(telemetry_data["coal"])
            air = telemetry_data.get("air")
            if isinstance(air, list):
                self.enemy_air = [tuple(a) for a in air if isinstance(a, list) and len(a) == 5]
            gnd = telemetry_data.get("gnd")
            if isinstance(gnd, list):
                self.enemy_ground = [tuple(g) for g in gnd if isinstance(g, list) and len(g) in (4, 5)]  # + alt (m)
            t, scan_t = telemetry_data.get("t"), telemetry_data.get("scan_t")
            if isinstance(t, (int, float)) and isinstance(scan_t, (int, float)):
                self.scan_lag = INF if scan_t < 0 else max(0.0, float(t) - float(scan_t))
                self.scan_rx = now
                self.scan_t = float(scan_t)
            flight = telemetry_data.get("flight")
            if isinstance(flight, list):
                self.flight_alive = [str(n) for n in flight]
                self.flight_alive_rx = now
            fuel = telemetry_data.get("fuel")
            if isinstance(fuel, list) and len(fuel) == 2:
                try:
                    self.fuel_raw = (float(fuel[0]), float(fuel[1]))
                except (TypeError, ValueError):
                    pass
            ammo = telemetry_data.get("ammo")
            if isinstance(ammo, list) and len(ammo) == 3:
                def _n(v):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        return None
                    return v if v >= 0 else None
                self.ammo = {"msl": _n(ammo[0]), "rkt": _n(ammo[1]), "gun": _n(ammo[2])}

    def flight_since(self, seq: int) -> List[Dict[str, Any]]:
        """Flight records newer than seq, oldest first (copies)."""
        with self._lock:
            return [dict(r) for r in self.flight_ring if r["seq"] > seq]

    def set_dcs_fuel(self, fraction: float, max_kg: float, unit: str, source: str = "") -> None:
        """Fuel straight from DCS (Unit:getFuel and the airframe's fuelMassMax, via the comms hook).
        source: "player" (DCS's player unit) or "name" (found by the unit name we sent)."""
        with self._lock:
            self.dcs_fuel = (float(fraction), float(max_kg), str(unit or ""), str(source or ""))
            self.dcs_fuel_t = time.monotonic()

    def set_flight(self, group: str, units: List[Any]) -> None:
        with self._lock:
            self.flight_group, self.flight_units = group, [tuple(u) for u in units]

    WP_REACHED_M = 800.0

    def set_mission_route(self, route: List[Any]) -> None:
        """[(n, north_m, east_m)] from the mission (comms hook)."""
        with self._lock:
            route = [tuple(p) for p in route]
            if route == self.mission_route:
                return
            self.mission_route = route
            later = [p[0] for p in route if p[0] > 0]
            self.mission_wp = min(later) if later else -1
            self._advance_mission_wp()

    def _advance_mission_wp(self) -> None:
        """Lock held. Next mission waypoint: the one after the furthest one we've reached (so
        skipping waypoints works too)."""
        if not self.mission_route or not self.pos or self.mission_wp < 0:
            return
        nums = sorted(p[0] for p in self.mission_route)
        reached = [p[0] for p in self.mission_route if p[0] >= self.mission_wp
                   and math.hypot(p[1] - self.pos[0], p[2] - self.pos[1]) <= self.WP_REACHED_M]
        if not reached:
            return
        later = [n for n in nums if n > max(reached)]
        self.mission_wp = min(later) if later else max(reached)   # last waypoint: stays "next"

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
        """'huey' while flying the UH-1H, 'apache' in the AH-64D (and before DCS has said which
        aircraft), 'other' in anything else (22 Oct: a Hornet got George's wheel and TADS callouts)."""
        now = time.monotonic()
        with self._lock:
            if self.huey_rx and now - self.huey_rx <= HUEY_LINK_FRESH_SEC:
                return "huey"
            if self.aircraft in HUEY_NAMES:
                return "huey"
            if not self.aircraft or self.aircraft.upper().startswith("AH-64"):
                return "apache"
            return "other"

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
                "pos": self.pos,
                "vel": self.vel,
                "route": list(self.route) if self.route else list(self.mission_route),
                "wp_next": self.wp_next if self.route else self.mission_wp,
                "route_source": "LoGetRoute" if self.route else ("mission" if self.mission_route else ""),
                "cargo": self.cargo,
                "start_pos": self.start_pos,
                "fuel_raw": self.fuel_raw,
                "dcs_fuel": self.dcs_fuel,
                "dcs_fuel_age": (time.monotonic() - self.dcs_fuel_t) if self.dcs_fuel else float("inf"),
                "flight_group": self.flight_group,
                "my_group": self.my_group,
                "my_unit": self.my_unit,
                "my_ll": self.my_ll,
                "self_id": self.self_id,
                "wing": list(self.wing),
                "air_defence": list(self.air_defence),
                "olympus_units": list(self.olympus_units),
                "olympus_enemy": list(self.olympus_enemy),
                "enemy_air": list(self.enemy_air),
                "coalition": self.coalition,
                "enemy_ground": list(self.enemy_ground),
                "enemy_lists_age": (None if self.scan_lag is None
                                    else self.scan_lag + (now - self.scan_rx)),
                # For the tactical picture (tactical/sources.py): which world scan the lists come from, and
                # George's Target List as he shows it (names only) with its age. Read-only additions.
                "scan_t": self.scan_t,
                "george_list": list(self.george_list),
                "george_list_age": list_age,
                "flight_units": list(self.flight_units),
                "flight_alive": list(self.flight_alive) if self.flight_alive is not None else None,
                "flight_alive_age": self._age(self.flight_alive_rx, now),
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
                "t_model": self.t_model,
                "tel_seq": self.tel_seq,
                "g": self.g,
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