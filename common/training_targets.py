"""
common/training_targets.py - feature 8: "Overlord, spawn two T-72s 5 km north of me" (design approved
30 Oct 2026: https://claude.ai/artifact/VnjTM9sDPBaN4YFwtwGTXn).

Everything goes through the step 7 gate (common/training.py): the Game Master client only sends what
its guard allows (training on, alone, enemy side, within 50 km, the group / unit limits, only the
units it spawned). This module only builds the request:

    parse_spawn(rest, snap, units)   the spoken request -> SpawnRequest, or why not (fixed rules only,
                                     never the model: a misheard request is refused, not guessed)
    Spawner.handle(rest)             sends the spawn, answers at once, then a thread waits for the
                                     group in Export.lua's enemy-side Olympus list (30 s), tasks it
                                     (weapons hold / live, static / moving, helicopters 150 m AGL),
                                     says "training group is up" (or "failed to appear"), and records
                                     each unit destroyed with its time since the spawn (debrief).

Defaults (approved): weapons hold, static, difficulty average (DCS "Good"), no place = ahead of you
(5 km in helicopters, 20 nm in jets). Live aircraft / helicopters get the loadout named in
data/training_units.json; held ones fly clean. Spawns closer than 1 km to you are refused.

1 Nov (one training mode): "Overlord, spawn ..." or "Instructor, spawn ..." while training mode is on;
every reply and call is in the instructor's voice ("Instructor, ..."), and each kill also goes to the
instructor's score log (on_kill).
"""
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

from common.training import VOICE as V

UNITS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                          "training_units.json")
COMMAND = {"ground": "spawnGroundUnits", "heli": "spawnHelicopters", "air": "spawnAircrafts"}
SKILL = {"easy": "Average", "average": "Good", "hard": "Excellent"}
DEFAULT_AHEAD_M = {"helo": 5000.0, "jet": 20 * 1852.0}
MIN_DIST_M = 1000.0
MAX_COUNT = 8
GROUND_SPACING_M = 30.0
SITE_SPACING_M = 300.0
AIR_SPACING_M = 150.0
AIR_ALT_M = 3000.0
HELI_AGL_M = 150.0
HELI_SPAWN_ABOVE_M = 300.0          # above your ground height, until the AGL order takes over
MOVE_GROUND_M = 3000.0
MOVE_AIR_M = 10000.0
APPEAR_S = 30.0
POLL_S = 1.0
KILL_POLL_S = 2.0

_SPAWN = re.compile(r"\bspawn(?:ed|ing|s)?\b")
_COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "to": 2, "too": 2, "pair": 2, "couple": 2,
                "three": 3, "four": 4, "section": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12, "twenty": 20}   # over 8: refused
_LIVE = re.compile(r"\b(?:live|hot|weapons free|shooting back|fire back|shoot back)\b")
_HOLD = re.compile(r"\b(?:weapons hold|hold fire|cold|inert|not shooting)\b")
_MOVING = re.compile(r"\b(?:moving|mobile|on the move|driving|patrolling)\b")
_STATIC = re.compile(r"\b(?:static|stationary|parked|not moving)\b")
_DIFF = [("easy", re.compile(r"\b(?:easy|novice|beginner)\b")),
         ("hard", re.compile(r"\b(?:hard|veteran|expert|ace|difficult)\b")),
         ("average", re.compile(r"\b(?:average|medium|normal)\b"))]
_NUMBER_SAY = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}


def _norm(text: str) -> str:
    t = (text or "").lower().replace("-", " ").replace("_", " ")
    t = re.sub(r"[^a-z0-9.\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------ the unit list
class UnitList:
    """data/training_units.json: spoken names -> one DCS type each. Nothing else can be spawned."""

    def __init__(self, path: Optional[str] = UNITS_FILE, entries: Optional[List[dict]] = None):
        if entries is None:
            entries = []
            try:
                with open(path, encoding="utf-8") as f:
                    entries = json.load(f).get("units") or []
            except (OSError, ValueError) as e:
                print(f"[TRAINING] Can't read {path}: {type(e).__name__}: {e}")
        self.entries = []
        for e in entries:
            if not e.get("type") or e.get("cat") not in COMMAND:
                continue
            pats = [r"\b" + r"\s*".join(re.escape(w) for w in _norm(s).split()) + r"s?\b"
                    for s in e.get("say") or [] if _norm(s)]
            if pats:
                self.entries.append(dict(e, _rx=re.compile("|".join(pats))))

    def find(self, t: str) -> Optional[Tuple[dict, int, int]]:
        """The longest spoken name in t: (entry, start, end)."""
        best = None
        for e in self.entries:
            for m in e["_rx"].finditer(t):
                if best is None or (m.end() - m.start()) > (best[2] - best[1]):
                    best = (e, m.start(), m.end())
        return best


# ------------------------------------------------------------------ parsing
@dataclass
class SpawnRequest:
    unit: dict
    count: int
    point: Tuple[float, float]
    where: str
    live: bool
    moving: bool
    difficulty: str
    dist_m: float

    @property
    def cat(self) -> str:
        return self.unit["cat"]


def wanted(rest: str) -> bool:
    return bool(_SPAWN.search(_norm(rest)))


def _count(segment: str) -> Union[int, str]:
    words = segment.split()
    if "pair" in words or "couple" in words:
        return 2
    if "section" in words and "four" in words:
        return 4
    n = None
    for w in words:
        if w.isdigit():
            n = int(w)
        elif w in _COUNT_WORDS:
            n = _COUNT_WORDS[w]
    n = 1 if n is None else n
    if not 1 <= n <= MAX_COUNT:
        return f"at most {MAX_COUNT} at a time"
    return n


def parse_spawn(rest: str, snap: dict, units: UnitList) -> Union[SpawnRequest, str]:
    """The request, or why not ("say what to spawn", "I can't spawn that", a place problem...)."""
    from common import geo
    from common.aircraft import profile, say_dist
    from common.overlord import parse_where, parse_distance, dist_brg
    t = _norm(rest)
    sp = _SPAWN.search(t)
    if not sp:
        return "say again"
    found = units.find(t)
    if found is None:
        after = t[sp.end():].strip()
        return "I can't spawn that" if re.search(r"[a-z]{2,}", after.replace("training", "")) else "say what to spawn"
    unit, a, b = found
    count = _count(t[sp.end():a])
    if isinstance(count, str):
        return count
    live = bool(_LIVE.search(t)) and not _HOLD.search(t)
    moving = bool(_MOVING.search(t)) and not _STATIC.search(t)
    difficulty = next((name for name, rx in _DIFF if rx.search(t)), "average")
    place = t[:a] + " " + t[b:]                                   # the unit's name can't be a place
    me = snap.get("my_ll")
    if not me:
        return "I don't have your position"
    point, where = parse_where(place, snap)
    if point is None:
        if not where.startswith("say where"):
            return where                                          # "there's no waypoint 7"
        kind = "jet" if profile(snap).kind == "jet" else "helo"   # no direction: ahead of you
        dist = parse_distance(place)
        metres = dist[0] if dist else DEFAULT_AHEAD_M[kind]
        hdg = math.radians(snap.get("hdg") or 0.0)
        point = geo.offset_ll(me[0], me[1], metres * math.cos(hdg), metres * math.sin(hdg))
        where = f"{dist[1] if dist else say_dist(metres, snap)} ahead of you"
    d = dist_brg((me[0], me[1]), (point[0], point[1]))[0]
    if d < MIN_DIST_M:
        return "too close to you, at least 1 kilometre"
    return SpawnRequest(unit, count, (point[0], point[1]), where, live, moving, difficulty, d)


# ------------------------------------------------------------------ the spawn order
def build_order(req: SpawnRequest, snap: dict) -> Tuple[str, dict]:
    """(Olympus command, parameters). Units face you; ground 30 m apart (sites 300 m), air 150 m."""
    from common import geo
    from common.overlord import dist_brg
    me = snap.get("my_ll")
    brg_to_me = dist_brg(req.point, (me[0], me[1]))[1]
    heading = math.radians(brg_to_me)
    side = (brg_to_me + 90.0) % 360.0                              # units spread across your line
    skill = SKILL.get(req.difficulty, "Good")
    site = req.cat == "ground" and req.unit["type"].endswith(("SAM Battery", "site"))
    spacing = SITE_SPACING_M if site else GROUND_SPACING_M if req.cat == "ground" else AIR_SPACING_M
    units = []
    for i in range(req.count):
        off = (i - (req.count - 1) / 2.0) * spacing
        lat, lon = geo.offset_ll(req.point[0], req.point[1], off * math.cos(math.radians(side)),
                                 off * math.sin(math.radians(side)))
        u = {"unitType": req.unit["type"], "location": {"lat": lat, "lng": lon}, "heading": heading,
             "skill": skill, "liveryID": ""}
        if req.cat in ("air", "heli"):
            ground = max(0.0, ((snap.get("alt_ft") or 0.0) - max(0.0, snap.get("agl_ft") or 0.0)) / 3.28084)
            u["altitude"] = AIR_ALT_M if req.cat == "air" else ground + HELI_SPAWN_ABOVE_M
            u["loadout"] = req.unit.get("live_loadout", "") if req.live else ""
        units.append(u)
    mine = snap.get("coalition")
    params = {"coalition": {1: "blue", 2: "red"}.get(mine, ""), "country": "", "immediate": True,
              "spawnPoints": 10 * req.count, "units": units}
    if req.cat in ("air", "heli"):
        params["airbaseName"] = ""
    return COMMAND[req.cat], params


def unit_label(label: str) -> str:
    """'two T-72B' -> 'T-72B' (one kill is one unit); 'SA-6 site' stays."""
    words = (label or "").split()
    return " ".join(words[1:]) if len(words) > 1 and words[0].lower() in _COUNT_WORDS else (label or "")


def describe(req: SpawnRequest, n_units: int) -> str:
    """What the instructor says back: "two T-72B, 5 kilometres north of you, weapons hold, static"."""
    name = req.unit.get("name") or req.unit["type"]
    site = n_units > req.count
    what = (f"{name}, {n_units} units" if site and req.count == 1 else
            f"{_NUMBER_SAY[req.count]} {name}s, {n_units} units" if site else f"{_NUMBER_SAY[req.count]} {name}")
    where = req.where if not req.where.startswith(("waypoint", "the objective")) else f"at {req.where}"
    parts = [what, where, "live" if req.live else "weapons hold"]
    if req.cat == "ground" or req.moving:
        parts.append("moving" if req.moving else "static")
    if req.difficulty != "average":
        parts.append(req.difficulty)
    return ", ".join(parts) + "." + (" They will engage." if req.live else "")


# ------------------------------------------------------------------ the spawner
class Spawner:
    def __init__(self, training, snapshot: Callable[[], dict], speak: Callable[[str], None],
                 units: Optional[UnitList] = None, debrief=None,
                 on_kill: Optional[Callable[[str, float], None]] = None,
                 appear_s: float = APPEAR_S, poll_s: float = POLL_S, kill_poll_s: float = KILL_POLL_S,
                 clock: Callable[[], float] = time.monotonic):
        self.training, self.snapshot, self.speak = training, snapshot, speak
        self.units = units if units is not None else UnitList()
        self.debrief = debrief
        self.on_kill = on_kill            # (what, seconds after the spawn): the instructor's score log
        self.appear_s, self.poll_s, self.kill_poll_s, self.clock = appear_s, poll_s, kill_poll_s, clock

    def wanted(self, rest: str) -> bool:
        return wanted(rest)

    def handle(self, rest: str) -> str:
        """The answer now (the instructor's voice, 1 Nov); the group is followed on its own thread."""
        tr = self.training
        if not tr.on:
            return V + "unable, training mode is off. Say training mode on."
        why = tr.spawn_unavailable() if hasattr(tr, "spawn_unavailable") else None
        if why:
            return V + f"unable, training targets unavailable, {why}."
        snap = self.snapshot()
        req = parse_spawn(rest, snap, self.units)
        if isinstance(req, str):
            if req in ("say what to spawn", "say again"):
                return V + f"{req}."
            if req == "I can't spawn that":
                return V + "I can't spawn that."
            return V + f"unable, {req}."
        client = tr.client()
        if client is None:
            return V + "unable, no Game Master login in config."
        cmd, params = build_order(req, snap)
        n_units = tr.units_in(params, ground=(cmd == "spawnGroundUnits")) or req.count
        known = {u[0] for u in snap.get("olympus_enemy") or []}
        ok, why = client.command(cmd, params)                   # the step 7 guard decides
        if not ok:
            return V + f"unable, {why}."
        said = describe(req, n_units)
        label = said.split(",")[0]                               # "two T-72B" / "SA-6 site"
        entry = tr.add_group(None, [], label, requested=n_units)
        record = None
        if self.debrief is not None and hasattr(self.debrief, "training_spawn"):
            record = self.debrief.training_spawn(label, n_units)
        print(f"[TRAINING] Spawned {label} ({cmd}, {n_units} unit(s)) at {req.where}.")
        threading.Thread(target=self._follow, args=(entry, req, known, label, record),
                         daemon=True, name="training-spawn").start()
        return V + said

    # -------------------------------------------------------------- after the spawn
    def _follow(self, entry: dict, req: SpawnRequest, known: set, label: str, record) -> None:
        try:
            t0 = self.clock()
            group = units = None
            while self.clock() - t0 < self.appear_s:
                new = self.training.new_groups(self.snapshot(), known)
                if new:
                    group, units = max(new.items(), key=lambda kv: len(kv[1]))
                    break
                time.sleep(self.poll_s)
            if group is None:
                self.training.drop(entry, "failed")
                print(f"[TRAINING] {label}: no group appeared in {self.appear_s:.0f} s.")
                if entry.get("ended") == "failed":                   # not cleared meanwhile
                    self.speak(V + "training targets failed to appear. Check Olympus.")
                return
            if not self.training.bind(entry, group, units):            # cleared / new mission meanwhile
                if entry.get("ended") == "cleared":
                    self.training.discard(group, units, label)
                return
            self._task(req, units)
            print(f"[TRAINING] {label} is up: {group}, {len(units)} unit(s).")
            self.speak(V + "training group is up.")
            self._watch(entry, units, t0, label, record)
        except Exception as e:
            print(f"[TRAINING] {label}: {type(e).__name__}: {e}")

    def _task(self, req: SpawnRequest, units: List[Tuple[str, int]]) -> None:
        from common import geo
        from common.overlord import dist_brg
        c = self.training.client()
        if c is None:
            return
        lead = int(units[0][1])
        c.command("setROE", {"ID": lead, "ROE": 0 if req.live else 4})          # weapons free / hold
        if req.cat == "heli":
            c.command("setAltitudeType", {"ID": lead, "altitudeType": "AGL"})
            c.command("setAltitude", {"ID": lead, "altitude": HELI_AGL_M})
        if not req.moving and req.cat in ("air", "heli"):
            # 30 Oct flight: a "static" MiG-21 flew straight on past you. Sent back to its spawn point, it
            # ends its path there and Olympus leaves it idle (aircraft circle where they are).
            c.command("setPath", {"ID": lead, "path": [{"lat": req.point[0], "lng": req.point[1]}]})
        if req.moving:
            snap = self.snapshot()
            me = snap.get("my_ll") or req.point
            side = (dist_brg(req.point, (me[0], me[1]))[1] + 90.0) % 360.0
            far = MOVE_GROUND_M if req.cat == "ground" else MOVE_AIR_M
            lat, lon = geo.offset_ll(req.point[0], req.point[1], far * math.cos(math.radians(side)),
                                     far * math.sin(math.radians(side)))
            c.command("setPath", {"ID": lead, "path": [{"lat": lat, "lng": lon},
                                                       {"lat": req.point[0], "lng": req.point[1]}]})

    def _watch(self, entry: dict, units: List[Tuple[str, int]], t0: float, label: str, record) -> None:
        """Each unit gone from the (fresh) enemy-side list is a kill, timed from the spawn."""
        from core.state import enemy_lists_stale
        ids = {int(i) for _, i in units}
        dead: set = set()
        while dead != ids:
            if entry.get("ended") in ("cleared", "forgotten", "failed"):
                return
            snap = self.snapshot()
            if snap.get("enemy_lists_age") is not None and not enemy_lists_stale(snap):
                alive = {int(u[2]) for u in snap.get("olympus_enemy") or []}
                for uid in sorted(ids - alive - dead):
                    dead.add(uid)
                    secs = self.clock() - t0
                    print(f"[TRAINING] {label}: unit {uid} destroyed after {secs:.0f} s.")
                    if record is not None and self.debrief is not None:
                        self.debrief.training_kill(record, secs)
                    if self.on_kill is not None:
                        try:
                            self.on_kill(unit_label(label), secs)
                        except Exception as e:
                            print(f"[TRAINING] score log: {type(e).__name__}: {e}")
            if entry.get("ended") == "destroyed" and dead == ids:
                return
            time.sleep(self.kill_poll_s)
