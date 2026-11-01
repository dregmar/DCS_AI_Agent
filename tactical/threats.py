"""
tactical/threats.py - which known units are threats, in what order, and why (design section 8).

    assessments = assess(tracks, me_ll, me_vel, fns)

Kept apart from track existence and identity: it reads tracks, never changes them, and never acts.

    who           hostile tracks, not GONE or LOST (those are "last known", not threats), weight >= MIN_WEIGHT
    category      from DCS attributes when DCS has described the type (Basis.WORLD), else from the name
                  rules in intelligence/threat_engine.py (Basis.INFERRED, said as "by its name")
    weight        threat_engine.threat_weight (DCS attributes first, then names), unchanged
    ring          the kneeboard's approximate table (kneeboard/model.py ring_m): always INFERRED, always
                  "approximate"; no ring and no attribute -> "capability unknown"
    range         from your position and the track's (WORLD); inside_ring only when both are known
    closing       only from a measured range rate (two scans, INFERRED) - never from range alone
    priority      fresh before stale / out of coverage, then weight, then inside a ring, then range

Every assessment lists the facts behind it (models.Evidence). Pure module: the lookups are passed in
(ThreatFns), so tests need no DCS data.
"""
import math
from dataclasses import dataclass
from typing import Callable, FrozenSet, List, Optional, Sequence, Tuple

from tactical.models import Affiliation, Basis, Category, Evidence, Status, ThreatAssessment, Track
from tactical.sources import dist_m

MIN_WEIGHT = 30                  # artillery, ships, infantry and up; trucks and buildings aren't threats
CLOSING_MPS = 5.0                # a range rate past this is "closing" / "opening"

_AIR = (Category.AIR, Category.AIR_FIXED, Category.AIR_ROTARY)

# (category, DCS attributes that make it) - first match wins, most dangerous first
_ATTR_CATS = [
    ("radar SAM", {"SAM TR", "SAM SR", "LR SAM", "MR SAM", "SR SAM", "RADAR_BAND1_FOR_ARM", "RADAR_BAND2_FOR_ARM"}),
    ("IR SAM", {"IR Guided SAM"}),
    ("MANPADS", {"MANPADS"}),
    ("SAM", {"SAM", "SAM elements", "SAM LL", "SAM CC"}),
    ("AAA", {"AAA", "AA_flak", "Static AAA", "Mobile AAA"}),
    ("fighter", {"Fighters", "Interceptors", "Multirole fighters"}),
    ("attack aircraft", {"Battleplanes", "Battle airplanes", "Bombers", "Strategic bombers"}),
    ("attack helicopter", {"Attack helicopters"}),
    ("helicopter", {"Helicopters", "Transport helicopters"}),
    ("armour", {"Tanks", "IFV", "APC", "Armored vehicles", "HeavyArmoredUnits", "ATGM"}),
    ("artillery", {"Artillery", "MLRS", "Indirect fire", "SS_missile"}),
    ("infantry", {"Infantry"}),
    ("ship", {"Ships", "Armed ships", "Heavy armed ships"}),
]
# by weight when DCS hasn't described the type (threat_engine's name rules decided the weight)
_WEIGHT_CATS = [(100, "SAM"), (95, "MANPADS"), (90, "AAA"), (70, "attack aircraft"), (60, "armour"), (45, "armour"),
                (40, "helicopter"), (35, "infantry"), (30, "artillery")]


@dataclass(frozen=True)
class ThreatFns:
    weight: Callable[[str], int]                              # threat_engine.threat_weight
    attrs: Callable[[str], FrozenSet[str]] = lambda n: frozenset()   # unit_types.TYPES.attrs
    ring_m: Callable[[str], Optional[float]] = lambda n: None        # kneeboard.model.ring_m


def default_fns() -> ThreatFns:
    """The program's own lookups (imported here, not at module load, so tests stay pure)."""
    from common.unit_types import TYPES
    from intelligence.threat_engine import threat_weight
    from kneeboard.model import ring_m
    return ThreatFns(weight=threat_weight, attrs=TYPES.attrs, ring_m=lambda n: ring_m(n, TYPES.is_air_defence))


def _category(t: Track, w: int, a: FrozenSet[str]) -> Tuple[str, Evidence]:
    for cat, group in _ATTR_CATS:
        hit = sorted(a & group)
        if hit:
            return cat, Evidence(f"DCS lists it as {', '.join(hit)}", Basis.WORLD, "DCS unit attributes")
    if t.category in _AIR and w >= 70:
        return "attack aircraft", Evidence("attack aircraft, by its name", Basis.INFERRED, "threat_engine name rules")
    for floor, cat in _WEIGHT_CATS:
        if w >= floor:
            return cat, Evidence(f"{cat}, by its name", Basis.INFERRED, "threat_engine name rules")
    return "unknown", Evidence("kind unknown", Basis.INFERRED, "threat_engine name rules")


def _range_rate(t: Track, me_ll, me_vel) -> Optional[float]:
    """Metres per second, negative = closing. Needs the track's measured velocity; your own velocity
    (north, east) from Export.lua when there is one."""
    if t.velocity is None or me_ll is None or t.position is None:
        return None
    n = (t.position.lat - me_ll[0]) * 111_320.0
    e = (t.position.lon - me_ll[1]) * 111_320.0 * math.cos(math.radians((t.position.lat + me_ll[0]) / 2))
    d = math.hypot(n, e)
    if d < 1.0:
        return None
    vn, ve = t.velocity.north_mps, t.velocity.east_mps
    if me_vel is not None:
        vn, ve = vn - me_vel[0], ve - me_vel[1]
    return (n * vn + e * ve) / d


def assess(tracks: Sequence[Track], me_ll: Optional[Tuple[float, float]], me_vel: Optional[Tuple[float, float]],
           fns: ThreatFns, min_weight: int = MIN_WEIGHT) -> Tuple[ThreatAssessment, ...]:
    rows: List[tuple] = []
    for t in tracks:
        if t.affiliation != Affiliation.HOSTILE or t.status in (Status.GONE, Status.LOST) or not t.type_name:
            continue
        w = int(fns.weight(t.type_name))
        a = frozenset(fns.attrs(t.type_name) or ())
        unknown_air = t.category in _AIR and not a and w < min_weight
        if w < min_weight and not unknown_air:
            continue
        if unknown_air:              # an enemy aircraft DCS hasn't described: still a threat, kind unknown
            cat, cat_ev = "aircraft", Evidence("an aircraft, kind unknown", Basis.INFERRED, "track category")
        else:
            cat, cat_ev = _category(t, w, a)
        ev: List[Evidence] = [cat_ev]
        ring = fns.ring_m(t.type_name) if t.category not in _AIR else None
        rng = dist_m(me_ll, (t.position.lat, t.position.lon)) if me_ll and t.position else None
        inside = (rng <= ring) if ring is not None and rng is not None else None
        if ring is not None:
            ev.append(Evidence(f"engages to about {ring / 1000:.0f} km (approximate table)", Basis.INFERRED,
                               "kneeboard ring table"))
        elif not a:
            ev.append(Evidence("capability unknown: DCS hasn't described this type", Basis.INFERRED, "unit_types"))
        if rng is not None:
            ev.append(Evidence(f"{rng / 1000:.1f} km from you", t.position.basis, "positions"))
        if inside:
            ev.append(Evidence("you are inside its ring", Basis.INFERRED, "kneeboard ring table"))
        rr = _range_rate(t, me_ll, me_vel) if t.category in _AIR else None
        if rr is not None and rr <= -CLOSING_MPS:
            ev.append(Evidence(f"closing at {-rr:.0f} m/s", Basis.INFERRED, "two scans"))
        elif rr is not None and rr >= CLOSING_MPS:
            ev.append(Evidence(f"opening at {rr:.0f} m/s", Basis.INFERRED, "two scans"))
        if t.status != Status.ACTIVE:
            ev.append(Evidence(f"not current: {t.status_reason}", Basis.INFERRED, "track status"))
        if t.george is not None and t.george.track_id == t.track_id:
            ev.append(Evidence(f"on George's list as #{t.george.list_index}", Basis.SENSOR, "George's Target List"))
        ev.append(Evidence(f"threat weight {w}", Basis.INFERRED, "threat_engine"))
        key = (t.status != Status.ACTIVE, -w, inside is not True, rng if rng is not None else math.inf, t.track_id)
        rows.append((key, t, cat, w, ring, rng, inside, tuple(ev)))
    rows.sort(key=lambda r: r[0])
    return tuple(ThreatAssessment(track_id=t.track_id, category=cat, weight=w, ring_m=ring,
                                  ring_basis=Basis.INFERRED if ring is not None else None, range_m=rng,
                                  inside_ring=inside, priority=i + 1, evidence=ev, freshness=t.status)
                 for i, (_, t, cat, w, ring, rng, inside, ev) in enumerate(rows))
