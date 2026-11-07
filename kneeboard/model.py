"""
kneeboard/model.py - what the kneeboard map shows, built from a state snapshot. Pure functions (no
threads, no I/O), so they're cheap to call 5 times a second and easy to test.

    build_map(snap, ctx) -> dict (JSON-ready)
    ring_m(type_name)    -> approximate engagement radius in metres for air defence, or None
    contact_panel(pic, n, snap, imperial) -> the "kneeboard, contact 42" panel, or None

ctx (main.py) supplies what isn't in the snapshot: Outcasts, Overlord's flights, named waypoints,
the JTAC target, lat/long for DCS x/z route points and, since step T5, the tactical picture
(ctx.picture(), tactical/) and the contact asked for (ctx.contact()).

With a tactical picture the enemy markers come from its tracks (one per DCS object, design section 9):
the same keys as before plus "tid" (T42), "status", "age_s" (last seen, for markers that aren't
current), "conf", "george" (his list number), "neutral", and (1 Nov) "aff" (hostile / neutral /
unknown: the page colours by it) and "cat" (air / ground / naval ...: the page's layer filters).
Markers from the raw lists have no "aff" / "cat": they're enemy lists, so the page draws them hostile.

1 Nov (evening): "me" carries "grid", your MGRS position at the precision your aircraft uses for target
grids (8 digits; 10 in the A-10C), and "friends" lists your own side's units within FRIENDLY_SHOW_M
(config, default 5 km): Export.lua's own-side list (already without you, your group and Olympus flights)
plus your wingmen, nearest first. Units out of coverage or lost are
drawn hollow at their last known position. Without a picture (service off, not started, not
updating) the map is built from the raw lists exactly as before.

Threat rings are APPROXIMATE (typical maximum engagement range by system), drawn so you can see where
not to go; they're labelled "approx" on the page. TODO: Olympus's unit database, if it lists ranges.
"""
import math
import re
from typing import Callable, Dict, List, Optional

FT_PER_M = 3.28084
MAX_GROUND = 120
MAX_AIR = 40
MAX_FRIENDS = 40
FRIENDLY_SHOW_M = 5000.0      # default for config FRIENDLY_SHOW_M
SELF_GUARD_M = 15.0           # a friendly row this close to you is taken to be you (Export's name mix-ups)
AGE_STEP_S = 10            # ages on the map move in 10 s steps, so the feed doesn't change every second

# (pattern on the DCS type name, radius km) - first match wins. Typical max engagement ranges.
_RINGS = [
    (r"s-300|sa-10|5p85|40b6|64h6", 75), (r"buk|sa-11|9a310|9s18|9s470", 35), (r"s-125|sa-3|snr", 25),
    (r"s-75|sa-2|sam sa-2", 40), (r"kub|sa-6|2p25|1s91", 25), (r"patriot", 70), (r"hawk", 40),
    (r"nasams", 25), (r"tor|sa-15|9a331", 12), (r"osa|sa-8|9a33", 10), (r"tunguska|2s6|sa-19", 8),
    (r"roland", 8), (r"rapier", 7), (r"hq-7", 12), (r"pantsir", 18),
    (r"strela-10|sa-13|9a35", 5), (r"strela-1|sa-9|9p31", 4), (r"avenger|stinger|igla|manpad|sa-18|sa-24", 5),
    (r"chaparral", 6), (r"s-60|57mm", 6), (r"gepard", 4), (r"zsu-23|shilka", 2.5), (r"zsu-57", 4),
    (r"zu-23|vulcan|m163", 2), (r"flak|bofors|ks-19", 5),
]


def ring_m(type_name: str, is_air_defence: Optional[Callable[[str], bool]] = None) -> Optional[float]:
    t = (type_name or "").lower()
    for rx, km in _RINGS:
        if re.search(rx, t):
            return km * 1000.0
    if is_air_defence is not None and is_air_defence(type_name):
        return 5000.0                              # air defence DCS knows about, system not in the list
    return None


def _ll(lat, lon) -> Optional[List[float]]:
    try:
        return [round(float(lat), 6), round(float(lon), 6)]
    except (TypeError, ValueError):
        return None


def short_tid(track_id: str) -> str:
    """"T0042" -> "T42" (what the map shows and what you say: "kneeboard, contact 42")."""
    try:
        return "T" + str(int(str(track_id)[1:]))
    except ValueError:
        return str(track_id)


def build_map(snap: dict, ctx) -> Dict:
    me_ll = snap.get("my_ll")
    me = None
    if me_ll:
        me = {"lat": round(me_ll[0], 6), "lon": round(me_ll[1], 6), "hdg": round(snap.get("hdg") or 0.0, 1),
              "alt_ft": round(snap.get("alt_ft") or 0.0), "kts": round(snap.get("speed_kts") or 0.0),
              "ac": snap.get("aircraft") or "", "grid": _grid(me_ll[0], me_ll[1], snap)}
    route = []
    for p in snap.get("route") or []:
        try:
            ll = ctx.xz_to_ll(float(p[1]), float(p[2]))
        except Exception:
            ll = None
        if ll:
            route.append({"n": int(p[0]), "lat": round(ll[0], 6), "lon": round(ll[1], 6)})

    pic = _picture(ctx)
    if pic is not None:
        ground, air, jtac, notes = _from_picture(pic, me_ll, ctx, snap)
    else:
        ground, air = _from_lists(snap, ctx, me_ll)
        jtac, notes = ctx.jtac(snap), []
    ground, air = ground[:MAX_GROUND], air[:MAX_AIR]
    jtac = dict(jtac) if isinstance(jtac, dict) else jtac          # the JTAC's own copy stays untouched
    _add_grids(ground + air + ([jtac] if jtac else []), snap)
    out = {
        "units": "nm" if ctx.imperial(snap) else "km",
        "me": me,
        "route": route,
        "wp_next": snap.get("wp_next"),
        "named": ctx.named_waypoints(snap),
        "outcasts": ctx.outcasts(snap),
        "flights": ctx.flights(snap),
        "ground": ground,
        "air": air,
        "jtac": jtac,
        "ips": _ips(ctx, snap),                     # 5 Nov: the 9-line's IP and the Hog flights' IPs
        "friends": friends_near(snap, me_ll),
        "friends_m": friendly_radius_m(),
        "enemy_age": enemy_age(snap),
        "source": "picture" if pic is not None else "lists",
        "george": notes,
        "panel": None,
    }
    want = getattr(ctx, "contact", None)
    n = want() if callable(want) else None
    if n is not None:
        out["panel"] = contact_panel(pic, n, snap, ctx.imperial(snap))
    return out


def _digits(snap: dict) -> int:
    try:
        from common.aircraft import profile
        return max(1, min(5, int(profile(snap).target_grid_digits)))
    except Exception:
        return 4


def _grid(lat: float, lon: float, snap: dict, digits: Optional[int] = None) -> Optional[str]:
    """"38T LM 72180 40180": MGRS at the aircraft's target-grid precision (common/aircraft.py)."""
    try:
        from common.position import mgrs_text
        return mgrs_text(lat, lon, _digits(snap) if digits is None else digits)
    except Exception:
        return None


def _ips(ctx, snap: dict) -> list:
    fn = getattr(ctx, "ips", None)
    try:
        return list(fn(snap) or []) if callable(fn) else []
    except Exception:
        return []


def _add_grids(units, snap: dict) -> None:
    """3 Nov: every target carries its MGRS at your precision, so the kneeboard shows a selected target's
    position in the same format as yours (lat / long are worked out on the page). ~3.5 us a unit."""
    digits = _digits(snap)
    for u in units:
        if isinstance(u, dict) and u.get("lat") is not None and u.get("lon") is not None and "grid" not in u:
            u["grid"] = _grid(u["lat"], u["lon"], snap, digits)


def _flat_m(a, b) -> float:
    n = (b[0] - a[0]) * 111_320.0
    e = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(n, e)


_FR_CAT = {"a": "air", "g": "ground", "n": "naval"}


def friendly_radius_m() -> float:
    """config FRIENDLY_SHOW_M (metres), else 5 km; the page shows it in the legend."""
    try:
        import config
        from core import settings                  # a live setting (6 Nov)
        v = float(settings.get("FRIENDLY_SHOW_M", getattr(config, "FRIENDLY_SHOW_M", FRIENDLY_SHOW_M))
                  or FRIENDLY_SHOW_M)
        return v if v > 0 else FRIENDLY_SHOW_M
    except Exception:
        return FRIENDLY_SHOW_M


def friends_near(snap: dict, me_ll, radius_m: Optional[float] = None) -> List[Dict]:
    """Your side's units within radius_m (config FRIENDLY_SHOW_M, 5 km): Export.lua's "fr" rows
    [type, id, lat, lon, alt m, "a"|"g"|"n"] and your wingmen [unit name, id, lat, lon]. Nearest first."""
    if not me_ll:
        return []
    if radius_m is None:
        radius_m = friendly_radius_m()
    me = (float(me_ll[0]), float(me_ll[1]))
    out, seen = [], set()
    rows = [(r, False) for r in snap.get("wing") or []] + [(r, True) for r in snap.get("friendly_units") or []]
    for r, side_list in rows:
        try:
            if side_list:
                typ, oid, lat, lon, alt = str(r[0]), int(r[1]), float(r[2]), float(r[3]), float(r[4])
                cat, name = _FR_CAT.get(str(r[5]), "ground"), None
            else:
                name, oid, lat, lon = str(r[0]), int(r[1]), float(r[2]), float(r[3])
                typ, alt, cat = name, None, "air"
        except (TypeError, ValueError, IndexError):
            continue
        if oid in seen:
            continue
        d = _flat_m(me, (lat, lon))
        if d > radius_m or d < SELF_GUARD_M:
            continue
        seen.add(oid)
        f = {"id": oid, "type": typ, "lat": round(lat, 6), "lon": round(lon, 6), "cat": cat, "wing": not side_list,
             "d": int(d // 50 * 50)}
        if alt is not None and cat == "air":
            f["alt_ft"] = round(alt * FT_PER_M)
        out.append(f)
    out.sort(key=lambda f: f["d"])
    return out[:MAX_FRIENDS]


def _picture(ctx):
    fn = getattr(ctx, "picture", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def _from_lists(snap: dict, ctx, me_ll):
    """The map before the tactical picture (and whenever there is none): Export.lua's lists as sent."""
    seen, ground = set(), []
    is_ad = getattr(ctx, "is_air_defence", None)
    rows = [(r, True) for r in snap.get("air_defence") or []] + [(r, False) for r in snap.get("enemy_ground") or []]
    for r, from_ad in rows:
        try:
            name, oid, lat, lon = str(r[0]), int(r[1]), float(r[2]), float(r[3])
        except (TypeError, ValueError, IndexError):
            continue
        if oid in seen:
            continue
        seen.add(oid)
        ring = ring_m(name, is_ad)
        ground.append({"id": oid, "type": name, "lat": round(lat, 6), "lon": round(lon, 6),
                       "ad": bool(from_ad or ring), "ring_m": ring})
    if me_ll:
        ground.sort(key=lambda g: (g["lat"] - me_ll[0]) ** 2 + (g["lon"] - me_ll[1]) ** 2)
    air = []
    for a in (snap.get("enemy_air") or [])[:MAX_AIR]:
        try:
            air.append({"id": int(a[1]), "type": str(a[0]), "lat": round(float(a[2]), 6),
                        "lon": round(float(a[3]), 6), "alt_ft": round(float(a[4]) * FT_PER_M)})
        except (TypeError, ValueError, IndexError):
            continue
    return ground, air


def _age(pic, t) -> Optional[int]:
    """Seconds since last seen, in AGE_STEP_S steps, for markers that aren't current; None when current."""
    if t.status.value in ("active", "stale"):
        return None
    return int(max(0.0, pic.built_mono - t.last_seen_mono) // AGE_STEP_S * AGE_STEP_S)


def _from_picture(pic, me_ll, ctx, snap):
    from tactical.models import Affiliation, Category, Status
    air_cats = (Category.AIR, Category.AIR_FIXED, Category.AIR_ROTARY)
    rings = {a.track_id: a.ring_m for a in pic.threats if a.ring_m}
    is_ad = getattr(ctx, "is_air_defence", None)
    ground, air, jtac = [], [], None
    for t in pic.tracks:
        if t.affiliation == Affiliation.FRIENDLY or t.status == Status.GONE or t.position is None:
            continue
        p = t.position
        try:
            oid = int(t.key.split(":", 1)[1])
        except (IndexError, ValueError):
            oid = -1
        base = {"id": oid, "tid": short_tid(t.track_id), "type": t.display or t.type_name or "unknown",
                "lat": round(p.lat, 6), "lon": round(p.lon, 6), "status": t.status.value, "age_s": _age(pic, t),
                "conf": t.exist_conf.level.value, "neutral": t.affiliation == Affiliation.NEUTRAL,
                "aff": t.affiliation.value, "cat": t.category.value,
                "george": t.george.list_index if t.george is not None else None}
        if "jtac_target" in t.tags:
            jtac = {"type": t.type_name or t.display, "lat": base["lat"], "lon": base["lon"], "tid": base["tid"]}
        if t.category in air_cats:
            base["alt_ft"] = round((p.alt_m or 0.0) * FT_PER_M)
            air.append(base)
        else:
            ring = rings.get(t.track_id)
            if ring is None and "air_defence" in t.tags:
                ring = ring_m(t.type_name or "", is_ad)
            base.update(ad=bool("air_defence" in t.tags or ring), ring_m=ring)
            ground.append(base)
    if me_ll:
        d2 = lambda g: (g["lat"] - me_ll[0]) ** 2 + (g["lon"] - me_ll[1]) ** 2
        ground.sort(key=lambda g: (g["status"] not in ("active", "stale"), d2(g)))
        air.sort(key=d2)
    if jtac is None:
        jtac = ctx.jtac(snap)                       # the JTAC's target isn't (or is no longer) in the picture
    notes = []
    for a in pic.george:
        if a.track_id is not None:
            continue
        if a.strength == "class_only":
            n = len(a.candidates)
            notes.append(f"#{a.list_index} {a.name}: class only, {n} unit{'s' if n != 1 else ''} could fit")
        else:
            notes.append(f"#{a.list_index} {a.name}: no position")
    return ground, air, jtac, notes


# ------------------------------------------------------------------ "kneeboard, contact 42"
_COMPASS = ["north", "north east", "east", "south east", "south", "south west", "west", "north west"]


def _ago(s: float) -> str:
    if s < 5:
        return "now"
    if s < 90:
        return f"{int(s)} s ago"
    return f"{int(s // 60)} min ago"


def contact_panel(pic, n: int, snap: dict, imperial: bool) -> Optional[Dict]:
    """The detail panel for track T<n>: what it is, where, how we know, how sure, why it's a threat.
    Only facts the picture holds (D6: no model wording)."""
    tid = f"T{int(n):04d}"
    if pic is None:
        return {"tid": f"T{n}", "title": f"T{n}", "rows": [["", "The tactical picture isn't running."]]}
    t = pic.track(tid)
    if t is None:
        return {"tid": f"T{n}", "title": f"T{n}", "rows": [["", "Not in the picture (never seen, or dropped)."]]}
    now = pic.built_mono
    rows = [["Status", f"{t.status.value.replace('_', ' ')}: {t.status_reason}"]]
    me = snap.get("my_ll")
    if t.position is not None:
        where = ""
        if me:
            from tactical.sources import dist_m, true_bearing
            d = dist_m((me[0], me[1]), (t.position.lat, t.position.lon))
            b = true_bearing((me[0], me[1]), (t.position.lat, t.position.lon))
            dist = f"{d / 1852:.1f} nm" if imperial else f"{d / 1000:.1f} km"
            where = f"{dist} {_COMPASS[int((b + 22.5) // 45) % 8]}"
        alt = t.position.alt_m
        if alt is not None:
            where += (", " if where else "") + f"{alt * FT_PER_M:,.0f} ft"
        if t.status.value not in ("active",):
            where += (" " if where else "") + f"(last known, {_ago(now - t.last_seen_mono)})"
        rows.append(["Where", where or "position known"])
    side = t.affiliation.value
    if "coalition_unreported" in t.flags:
        side += " (not your side; coalition not reported)"
    rows.append(["Side", side])
    rows.append(["Known from", "DCS world scan (simulator truth, not a sensor detection): "
                 + ", ".join(m.source.split(".", 1)[-1] for m in t.sources)])
    rows.append(["Seen", f"first {_ago(now - t.first_seen_mono)}, last {_ago(now - t.last_seen_mono)}"])
    rows.append(["Exists", f"{t.exist_conf.level.value}: {t.exist_conf.reason}"])
    rows.append(["Identity", f"{t.ident_conf.level.value}: {t.ident_conf.reason}"])
    if t.velocity is not None:
        spd = math.hypot(t.velocity.north_mps, t.velocity.east_mps)
        rows.append(["Moving", f"{spd * 1.94384:.0f} kt" if imperial else f"{spd * 3.6:.0f} km/h"])
    if t.george is not None:
        rows.append(["George", f"his list #{t.george.list_index} ({t.george.strength.replace('_', ' ')}: {t.george.reason})"])
    if "jtac_target" in t.tags:
        rows.append(["JTAC", "the current JTAC target"])
    threat = next((a for a in pic.threats if a.track_id == tid), None)
    if threat is not None:
        rows.append(["Threat", f"{threat.category}, priority {threat.priority}"])
        for e in threat.evidence:
            rows.append(["", e.fact])
    out = {"tid": short_tid(tid), "title": t.display or t.type_name or "unknown type", "rows": rows}
    if t.position is not None:                      # 3 Nov: shown in your position format (the page picks it)
        out["pos"] = {"lat": round(t.position.lat, 6), "lon": round(t.position.lon, 6),
                      "grid": _grid(t.position.lat, t.position.lon, snap)}
    return out


def enemy_age(snap: dict) -> Optional[int]:
    """None while the enemy lists are fresh (or Export.lua is too old to say); else their age in
    seconds rounded down to 5 (so the feed changes every 5 s, not every frame), -1 = never scanned."""
    from core.state import enemy_lists_stale
    if not enemy_lists_stale(snap):
        return None
    age = snap["enemy_lists_age"]
    return -1 if age == float("inf") else int(age // 5 * 5)
