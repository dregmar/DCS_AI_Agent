"""
tactical/fusion.py - George's Target List -> tracks, as explicitly uncertain associations (design section 6,
tier 2). Never a merge: an entry points at one track or at none, and keeps every track it could be.

    assocs = associate(george_reports, tracks, me_ll, attrs=TYPES.attrs, previous=last_assocs)

For each list entry, in George's order:
    candidates   ACTIVE or STALE, not friendly, within RANGE_M (15 km, Export's contacts window) of you,
                 whose DCS type matches the entry's name (equal, one starts with the other, or one inside
                 the other, as core/state.py does today), or whose display name does
    UNIQUE       exactly one unit matches the name                         -> that track
    BEST_OF_N    several match: the nearest not taken by an earlier entry   -> that track, all candidates kept
                 (or the one this entry had last time, while it's still free: no flip-flopping)
    CLASS_ONLY   George shows only a class (ARMORED, GROUND, AAA ...)       -> no track, the candidates listed
    NONE         nothing fits, or every match is taken by earlier entries   -> no track, no position
Name differs: when no name matches but the entry's type is known to DCS, units sharing its family
(tanks, IFV / APC, SAM, AAA ...) are candidates as BEST_OF_N with the reason "name differs from the DCS
type"; identity stays DCS's type name.

Two entries never point at one track. Pure module: attrs (DCS attributes per type name,
common.unit_types.TYPES.attrs) is passed in.
"""
from typing import Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from tactical.models import (Affiliation, Association, BEST_OF_N, CLASS_ONLY, Category, NONE, Report, Status, Track,
                             UNIQUE)
from tactical.sources import dist_m

RANGE_M = 15_000.0

# George's classifications -> what makes a unit of that class (DCS attributes, or the track's category)
_CLASS_ATTRS = {
    "ARMORED": {"Armored vehicles", "Tanks", "IFV", "APC", "HeavyArmoredUnits", "LightArmoredUnits"},
    "TRACKED": {"Tanks", "IFV", "HeavyArmoredUnits"},
    "AAA": {"AAA", "AA_flak", "Static AAA", "Mobile AAA"},
    "AIR DEFENSE": {"Air Defence", "SAM", "SAM elements", "AAA", "SAM TR", "SAM SR", "SAM LL", "MANPADS"},
    "SAM": {"SAM", "SAM elements", "SAM TR", "SAM SR", "SAM LL", "MANPADS", "IR Guided SAM"},
    "HELICOPTER": {"Helicopters", "Attack helicopters", "Transport helicopters"},
    "AIR": {"Air", "Planes", "Helicopters"},
    "NAVAL": {"Ships", "Naval"},
}
_CLASS_CATEGORY = {"GROUND": {Category.GROUND, Category.SURFACE}, "NAVAL": {Category.NAVAL},
                   "AIR": {Category.AIR, Category.AIR_FIXED, Category.AIR_ROTARY},
                   "HELICOPTER": {Category.AIR_ROTARY}}
# A type's family, for "name differs": one unit of the family can be mistaken for another
_FAMILIES = [{"Tanks"}, {"IFV", "APC", "Infantry carriers"}, {"SAM TR", "SAM SR", "SAM LL", "SAM", "SAM elements"},
             {"AAA"}, {"MANPADS", "IR Guided SAM"}, {"Attack helicopters"}, {"Transport helicopters"},
             {"Artillery", "MLRS", "Indirect fire"}, {"Trucks", "Unarmed vehicles"}, {"Ships"}]


def _names_match(name: str, typ: Optional[str]) -> bool:
    if not typ:
        return False
    a, b = name.strip().lower(), typ.strip().lower()
    return bool(a) and (a == b or a.startswith(b) or b.startswith(a) or a in b)


def _family(attrs: FrozenSet[str]) -> Optional[frozenset]:
    for f in _FAMILIES:
        if attrs & f:
            return frozenset(f)
    return None


def _usable(t: Track) -> bool:
    return (t.status in (Status.ACTIVE, Status.STALE) and t.affiliation != Affiliation.FRIENDLY
            and t.position is not None)


def associate(entries: Iterable[Report], tracks: Sequence[Track], me_ll: Optional[Tuple[float, float]],
              attrs: Callable[[str], FrozenSet[str]] = lambda name: frozenset(),
              previous: Optional[Mapping[Tuple[int, str], str]] = None,
              range_m: float = RANGE_M) -> List[Association]:
    """One Association per George list entry, in list order. previous: {(list index, name): track_id}
    from the last call, so an entry keeps its unit while that unit is still a candidate and free."""
    previous = previous or {}
    entries = sorted((e for e in entries if e.source == "george.list"), key=lambda e: e.get("list_index") or 0)

    def dist(t: Track) -> float:
        if me_ll is None:
            return 0.0
        return dist_m(me_ll, (t.position.lat, t.position.lon))

    pool = [t for t in tracks if _usable(t) and (me_ll is None or dist(t) <= range_m)]
    pool.sort(key=lambda t: (dist(t), t.track_id))
    taken: Dict[str, int] = {}
    out: List[Association] = []
    for e in entries:
        idx = int(e.get("list_index") or 0)
        name = str(e.get("name") or "")
        if e.type_name is None:                                  # a class, or nothing at all
            cands = _class_candidates(e.klass, pool, attrs)
            out.append(Association(idx, name or "UNKNOWN", CLASS_ONLY if e.klass else NONE, None,
                                   tuple(t.track_id for t in cands),
                                   f"George shows only the class {e.klass}" if e.klass else "no name on George's list"))
            continue
        cands = [t for t in pool if _names_match(name, t.type_name) or _names_match(name, t.display)]
        reason = ""
        differs = False
        if not cands:
            fam = _family(attrs(name))
            if fam:
                cands = [t for t in pool if t.type_name and attrs(t.type_name) & fam]
                differs = bool(cands)
                reason = "name differs from the DCS type"
        if not cands:
            out.append(Association(idx, name, NONE, None, (), "no unit in the picture fits"))
            continue
        ids = tuple(t.track_id for t in cands)
        free = [t for t in cands if t.track_id not in taken]
        if not free:
            out.append(Association(idx, name, NONE, None, ids, "every unit that fits is on an earlier entry"))
            continue
        keep = previous.get((idx, name))
        chosen = next((t for t in free if t.track_id == keep), free[0])
        strength = UNIQUE if len(cands) == 1 and not differs else BEST_OF_N
        if not reason:
            reason = "one unit fits" if strength == UNIQUE else f"nearest of {len(cands)} that fit"
        taken[chosen.track_id] = idx
        out.append(Association(idx, name, strength, chosen.track_id, ids, reason))
    return out


def _class_candidates(klass: Optional[str], pool: List[Track], attrs) -> List[Track]:
    if not klass:
        return []
    want_attrs = _CLASS_ATTRS.get(klass, set())
    want_cat = _CLASS_CATEGORY.get(klass, set())
    return [t for t in pool if (t.category in want_cat) or (t.type_name and attrs(t.type_name) & want_attrs)]


def previous_of(assocs: Iterable[Association]) -> Dict[Tuple[int, str], str]:
    """The `previous` argument for the next call."""
    return {(a.list_index, a.name): a.track_id for a in assocs if a.track_id is not None}
