"""
tactical/sources.py - state_store.snapshot() -> Reports, plus each source's health and coverage
(design sections 2 and 3).

    world_reports(snap, st)          Export.lua's world lists (contacts, gnd, ad, air, oly, olyx, wing,
                                     and from T8 neu / fr: neutrals and your own side)
    george_reports(snap, now, gen)   George's Target List: names only, no position, no id
    jtac_report(current, now, gen)   the JTAC's current target: a pointer (key + tag), not a sighting

st is the tactical.clock.ScanTime for this snapshot. Every row Export.lua sends comes from one
LoGetWorldObjects() scan: simulator truth (Basis.WORLD), not a detection. Rows are parsed defensively:
a bad row is counted in the source's health, never raised.

Coordinates: lat / lon in WGS-84 degrees as DCS gives them, altitude in metres MSL. The "contacts"
list's bearing is a GRID bearing (atan2 on DCS x / z); it is kept as "brg_grid", and the true bearing
from your lat / lon as "brg_true" (design section 3).

Coalition (T8, I-T2): an Export.lua that sends the neutral list ("neutral_units") keeps neutrals out of
the enemy lists, so their rows are the other side's (coalition 3 - yours); neutrals come as coalition 0.
An older Export.lua: enemy rows stay "coalition_unreported" (enemy or neutral), as before.
Not here yet: AIRBASES and tasking targets (later steps).
Pure module: no threads, no I/O, no imports from the rest of the program.
"""
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tactical.clock import AGE_UNKNOWN, NO_SCAN, ScanTime
from tactical.models import (Basis, Category, Position, Relation, Report, SourceHealth, dcs_key, valid_ll)

# Export.lua's own filters (dcs_scripts/Export.lua): (snapshot key, source name, range m, max rows)
WORLD_LISTS = {
    "world.contacts": ("contacts", 15_000.0, 20),
    "world.gnd": ("enemy_ground", 40_000.0, 40),
    "world.ad": ("air_defence", 80_000.0, 25),
    "world.air": ("enemy_air", 120_000.0, 20),
    "world.oly": ("olympus_units", None, 40),
    "world.olyx": ("olympus_enemy", None, 40),
    "world.wing": ("wing", None, None),
    "world.neu": ("neutral_units", 40_000.0, 40),      # T8 (I-T2): ground / naval, neither side
    "world.fr": ("friendly_units", 40_000.0, 40),      # T8 (I-T3): your side; aircraft out to 120 km
}
_SIDE_CATEGORY = {"a": Category.AIR, "g": Category.GROUND, "n": Category.NAVAL}
_ENEMY_LISTS = ("world.contacts", "world.gnd", "world.ad", "world.air")

# George's Target List shows a classification until he has identified a contact (manual p.655), and
# the wheel sometimes shows these words. A superset of core.state._GENERIC_NAMES (checked by a test).
GEORGE_CLASSES = frozenset({"GROUND", "ARMORED", "AAA", "AIR", "HELICOPTER", "NAVAL", "UNKNOWN", "TRACKED",
                            "AIR DEFENSE", "SAM"})


# ------------------------------------------------------------------ geometry (flat earth, as common/overlord.py)
def dist_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    n = (b[0] - a[0]) * 111_320.0
    e = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(n, e)


def true_bearing(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    n = (b[0] - a[0]) * 111_320.0
    e = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.degrees(math.atan2(e, n)) % 360


# ------------------------------------------------------------------ helpers
def _f(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _own_ll(snap: dict) -> Optional[Tuple[float, float]]:
    ll = snap.get("my_ll")
    if isinstance(ll, (list, tuple)) and len(ll) == 2 and valid_ll(ll[0], ll[1]):
        return float(ll[0]), float(ll[1])
    return None


def _extra(d: Dict[str, Any]) -> Tuple[Tuple[str, Any], ...]:
    return tuple(sorted((k, v) for k, v in d.items() if v is not None))


def _parse_row(source: str, row: Any) -> Optional[Dict[str, Any]]:
    """One list row -> {"id", "type", "lat", "lon", "alt", ...}; None if it can't be read."""
    if source == "world.contacts":
        if not isinstance(row, dict):
            return None
        return {"id": row.get("id"), "type": row.get("type"), "lat": row.get("lat"), "lon": row.get("lon"),
                "alt": None, "dist_m": _f(row.get("dist_m")), "brg_grid": _f(row.get("brg"))}
    if not isinstance(row, (list, tuple)):
        return None
    if source in ("world.gnd", "world.ad"):           # [type, id, lat, lon(, alt)]
        if len(row) not in (4, 5):
            return None
        return {"id": row[1], "type": row[0], "lat": row[2], "lon": row[3],
                "alt": row[4] if len(row) == 5 else None}
    if source == "world.air":                         # [type, id, lat, lon, alt]
        if len(row) != 5:
            return None
        return {"id": row[1], "type": row[0], "lat": row[2], "lon": row[3], "alt": row[4]}
    if source in ("world.oly", "world.olyx"):         # [group, unit, id, lat, lon, alt]
        if len(row) != 6:
            return None
        return {"id": row[2], "type": None, "lat": row[3], "lon": row[4], "alt": row[5],
                "group": row[0], "unit": row[1]}
    if source in ("world.neu", "world.fr"):           # [type, id, lat, lon, alt, "a"|"g"|"n"]
        if len(row) != 6:
            return None
        return {"id": row[1], "type": row[0], "lat": row[2], "lon": row[3], "alt": row[4],
                "cat": _SIDE_CATEGORY.get(row[5])}
    if source == "world.wing":                        # [unit name, id, lat, lon]
        if len(row) != 4:
            return None
        return {"id": row[1], "type": None, "lat": row[2], "lon": row[3], "alt": None, "unit": row[0]}
    return None


_CATEGORY = {"world.contacts": Category.SURFACE, "world.gnd": Category.GROUND, "world.ad": Category.GROUND,
             "world.air": Category.AIR, "world.oly": Category.UNKNOWN, "world.olyx": Category.UNKNOWN,
             "world.wing": Category.AIR, "world.neu": Category.GROUND, "world.fr": Category.UNKNOWN}
_RELATION = {"world.oly": Relation.OWN, "world.wing": Relation.OWN, "world.fr": Relation.OWN}


# ------------------------------------------------------------------ world scan
def world_reports(snap: dict, st: ScanTime) -> Tuple[List[Report], Dict[str, SourceHealth]]:
    """Reports from every Export.lua world list, and each list's health. With no world scan yet
    there are no reports; every list is down with the reason."""
    health: Dict[str, SourceHealth] = {}
    if st.kind == NO_SCAN:
        for name, (_, rng, cap) in WORLD_LISTS.items():
            health[name] = SourceHealth(name, False, range_m=rng, cap=cap, note="no world scan yet")
        return [], health
    me = _own_ll(snap)
    mine = snap.get("coalition")
    sides_known = isinstance(snap.get("neutral_units"), (list, tuple)) and mine in (1, 2)
    reports: List[Report] = []
    for name, (key, rng, cap) in WORLD_LISTS.items():
        rows = snap.get(key)
        if not isinstance(rows, (list, tuple)):
            health[name] = SourceHealth(name, False, age_s=st.age_s, range_m=rng, cap=cap, note="not sent")
            continue
        seen_keys = set()
        dup = bad = 0
        farthest = None
        out: List[Report] = []
        for row in rows:
            r = _parse_row(name, row)
            if r is None:
                bad += 1
                continue
            k = dcs_key(r["id"])
            if k is not None and k in seen_keys:
                dup += 1
                continue
            flags = set()
            if k is None:
                flags.add("no_id")
            else:
                seen_keys.add(k)
            pos = None
            alt = _f(r.get("alt"))
            if valid_ll(r["lat"], r["lon"]) and st.seen_mono is not None:
                pos = Position(float(r["lat"]), float(r["lon"]), alt, Basis.WORLD, st.seen_model, st.seen_mono)
                if me is not None:
                    d = dist_m(me, (pos.lat, pos.lon))
                    farthest = d if farthest is None else max(farthest, d)
            else:
                flags.add("no_position")
            if alt is None:
                flags.add("no_altitude")
            relation = _RELATION.get(name, Relation.OTHER)
            coalition = None
            if name == "world.neu":
                coalition = 0
            elif name in _ENEMY_LISTS and sides_known:
                coalition = 3 - mine                  # Export keeps neutrals out of these (I-T2)
            elif name == "world.fr" and mine in (1, 2):
                coalition = mine
            if relation == Relation.OTHER and coalition is None:
                flags.add("coalition_unreported")     # older Export.lua: enemy or neutral
            if not st.age_known:
                flags.add("age_unknown")
            extra = {k2: r[k2] for k2 in ("group", "unit", "dist_m", "brg_grid") if r.get(k2) is not None}
            if name == "world.contacts" and pos is not None and me is not None:
                extra["brg_true"] = round(true_bearing(me, (pos.lat, pos.lon)), 1)
            typ = r.get("type")
            typ = str(typ).strip() if typ is not None and str(typ).strip() else None
            if name == "world.ad":
                flags.add("air_defence_by_name")      # Export picks these by words in the type name
            out.append(Report(source=name, basis=Basis.WORLD, key=k, relation=relation,
                              category=r.get("cat") or _CATEGORY[name], type_name=typ, klass=None,
                              coalition=coalition,
                              position=pos, seen_model=st.seen_model, seen_mono=st.seen_mono,
                              age_known=st.age_known, generation=st.generation,
                              flags=frozenset(flags), extra=_extra(extra)))
        reports.extend(out)
        n = len(rows) - bad - dup
        health[name] = SourceHealth(name, True, rows=n, age_s=st.age_s, range_m=rng, cap=cap,
                                    full=cap is not None and len(rows) >= cap, farthest_m=farthest,
                                    duplicates=dup, bad_rows=bad,
                                    note="ages unknown (old Export.lua)" if st.kind == AGE_UNKNOWN else "")
    return reports, health


def coverage_edge_m(h: SourceHealth) -> Optional[float]:
    """How far from you an absent unit can still be judged by this list: 95 % of its range window,
    or, when the list is full, its farthest row (nearer units would have been listed). math.inf: the
    list has no range window and isn't full (Olympus units, your wingmen), so every unit is covered.
    None: the list is down, or full with no distances to judge by - absence can't be judged."""
    if not h.up:
        return None
    if h.full and h.farthest_m is None:
        return None
    edges = []
    if h.range_m is not None:
        edges.append(h.range_m * 0.95)
    if h.full:
        edges.append(h.farthest_m)
    return min(edges) if edges else math.inf


# ------------------------------------------------------------------ George's Target List
def george_reports(snap: dict, now_mono: float, generation: int) -> Tuple[List[Report], SourceHealth]:
    """George's list as he shows it: one report per entry, no key and no position. A classification
    (ARMORED, GROUND ...) is a class, not a type. The old name-guess (contact_id) is not carried."""
    names = snap.get("george_list")
    age = snap.get("george_list_age")
    age_f = None if age is None or (isinstance(age, float) and math.isinf(age)) else _f(age)
    if not isinstance(names, (list, tuple)) or age_f is None:
        return [], SourceHealth("george.list", False, note="list not read yet")
    seen_mono = now_mono - age_f
    out = []
    for i, raw in enumerate(names, start=1):
        name = str(raw or "").strip()
        is_class = name.upper() in GEORGE_CLASSES or not name
        out.append(Report(source="george.list", basis=Basis.SENSOR, key=None, relation=None,
                          category=Category.UNKNOWN,
                          type_name=None if is_class else name,
                          klass=(name.upper() if name and name.upper() != "UNKNOWN" else None) if is_class else None,
                          coalition=None, position=None, seen_model=None, seen_mono=seen_mono,
                          age_known=True, generation=generation, flags=frozenset({"no_position"}),
                          extra=_extra({"list_index": i, "name": name,
                                        "complete": bool(snap.get("george_list_complete"))})))
    return out, SourceHealth("george.list", True, rows=len(out), age_s=age_f,
                             note="" if snap.get("george_list_complete") else "end of list not reached")


# ------------------------------------------------------------------ JTAC
def jtac_report(current: Optional[dict], now_mono: float, generation: int) -> Optional[Report]:
    """The JTAC's current target (common/jtac.py Jtac.current) as a tag on its DCS object. Its lat / lon
    were copied when it was assigned and never updated, so they're kept as "assigned_lat/lon" and NOT
    used as a position: the track's position comes from the world scan."""
    if not isinstance(current, dict):
        return None
    k = dcs_key(current.get("id"))
    if k is None:
        return None
    typ = current.get("type")
    return Report(source="jtac", basis=Basis.REPORTED, key=k, relation=Relation.OTHER,
                  category=Category.GROUND, type_name=str(typ) if typ else None, klass=None,
                  coalition=None, position=None, seen_model=None, seen_mono=now_mono, age_known=True,
                  generation=generation, flags=frozenset({"assignment"}),
                  extra=_extra({"tag": "jtac_target", "assigned_lat": _f(current.get("lat")),
                                "assigned_lon": _f(current.get("lon")), "from_list": current.get("src")}))


def by_key(reports: Iterable[Report]) -> Dict[str, List[Report]]:
    """Reports grouped by key (tier 1 correlation: same DCS object = same entity). Keyless reports are left out."""
    out: Dict[str, List[Report]] = {}
    for r in reports:
        if r.key is not None:
            out.setdefault(r.key, []).append(r)
    return out


# ------------------------------------------------------------------ DCS unit events (T8, I-T4)
_EVENT_WORDS = {"DEAD": "it dead", "LOST": "it lost", "CRASH": "it crashed"}


def dcs_events(text: str) -> Dict[str, str]:
    """george_comms_hook.lua's "EVENTS\\n<kind;victim id;name;type;killer id;name;type>..." ->
    {key: what DCS said}, e.g. {"dcs:16777473": "a kill by Hog 1-1"}. A KILL (it names the killer) wins
    over DEAD / LOST / CRASH for the same unit. Bad lines are skipped."""
    out: Dict[str, str] = {}
    if not isinstance(text, str) or not text.startswith("EVENTS"):
        return out
    for line in text.split("\n")[1:]:
        f = line.strip().split(";")
        if len(f) != 7:
            continue
        kind, vid, _vname, _vtype, _kid, kname, ktype = (x.strip() for x in f)
        key = dcs_key(vid)
        if key is None:
            continue
        if kind == "KILL":
            by = kname or ktype
            out[key] = f"a kill by {by}" if by else "a kill"
        elif kind in _EVENT_WORDS and not out.get(key, "").startswith("a kill"):
            out.setdefault(key, _EVENT_WORDS[kind])
    return out
