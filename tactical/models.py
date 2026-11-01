"""
tactical/models.py - the tactical picture's typed records (design section 5).

Every value that came from somewhere says how we know it (Basis): the DCS world scan (simulator truth,
NOT a sensor detection), George's own sensor (his Target List), a report or assignment (JTAC, tasking,
mission data), or something this program inferred. Confidence is a level plus its reason, never a
made-up number.

All records are frozen dataclasses: a published picture can be read from any thread without locks.
Pure module: no threads, no I/O, no imports from the rest of the program.
"""
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Tuple


class Basis(str, Enum):
    """How we know a fact."""
    WORLD = "world"          # DCS world scan (LoGetWorldObjects): simulator truth, not a detection
    SENSOR = "sensor"        # George's TADS search (his Target List)
    REPORTED = "reported"    # an assignment or mission data (JTAC, tasking, AIRBASES, briefing)
    INFERRED = "inferred"    # worked out by this program (kill, lost, ring radius, closing)


class Affiliation(str, Enum):
    HOSTILE = "hostile"
    FRIENDLY = "friendly"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class Category(str, Enum):
    AIR = "air"                  # aircraft, fixed or rotary not reported
    AIR_FIXED = "air_fixed"
    AIR_ROTARY = "air_rotary"
    GROUND = "ground"
    NAVAL = "naval"
    SURFACE = "surface"          # ground OR naval (Export's "contacts" list doesn't say which)
    STRUCTURE = "structure"
    UNKNOWN = "unknown"


class Status(str, Enum):
    ACTIVE = "active"                    # listed in the latest fresh scan
    STALE = "stale"                      # the source itself is stale: we can't currently tell
    OUT_OF_COVERAGE = "out_of_coverage"  # fresh scan, not listed, but beyond what the list can show
    LOST = "lost"                        # out of coverage too long
    GONE = "gone"                        # no longer in the DCS world (destroyed only with evidence)


class Level(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Relation(str, Enum):
    """Which side of you a world list puts a unit (Export filters by your coalition)."""
    OWN = "own"          # your coalition (oly, wing)
    OTHER = "other"      # not your coalition: enemy OR neutral until Export sends coalition (I-T2)


# Strengths of a George-list association (design section 6, tier 2)
UNIQUE, BEST_OF_N, CLASS_ONLY, NONE = "unique", "best_of_n", "class_only", "none"
STRENGTHS = (UNIQUE, BEST_OF_N, CLASS_ONLY, NONE)


def dcs_key(object_id: Any) -> Optional[str]:
    """'dcs:16777472' for a DCS object id, None for a missing / negative / non-integer id."""
    if isinstance(object_id, bool):
        return None
    try:
        f = float(object_id)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0 or f != int(f):
        return None
    return f"dcs:{int(f)}"


@dataclass(frozen=True)
class Conf:
    level: Level
    reason: str

    def __post_init__(self):
        if not isinstance(self.level, Level):
            raise TypeError("Conf.level must be a Level")
        if not self.reason or not self.reason.strip():
            raise ValueError("a confidence needs its reason")


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    alt_m: Optional[float]           # metres MSL; None = not reported
    basis: Basis
    seen_model: Optional[float]      # DCS model time it was true at (None = not known)
    seen_mono: float                 # time.monotonic() it was true at (estimated from the packet)

    def __post_init__(self):
        if not valid_ll(self.lat, self.lon):
            raise ValueError(f"bad position {self.lat}, {self.lon}")


def valid_ll(lat: Any, lon: Any) -> bool:
    """A real latitude / longitude. Export.lua writes 0, 0 when an object has no LatLongAlt."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(la) and math.isfinite(lo)):
        return False
    if la == 0.0 and lo == 0.0:
        return False
    return -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0


@dataclass(frozen=True)
class Velocity:
    north_mps: float
    east_mps: float
    up_mps: Optional[float]
    basis: Basis                     # INFERRED from two scans (no other object's velocity is exported)
    from_model: Optional[float]      # the two fixes it was worked out from
    to_model: Optional[float]


@dataclass(frozen=True)
class Report:
    """One source's statement about one entity at one time. `extra` keeps the source's own fields as
    given (sorted (name, value) pairs, so the record stays immutable): look them up with get()."""
    source: str                          # "world.gnd", "world.air", "george.list", "jtac" ...
    basis: Basis
    key: Optional[str]                   # "dcs:<id>", or None (George entries, rows without an id)
    relation: Optional[Relation]         # own / other side for world lists; None when not said
    category: Category
    type_name: Optional[str]             # "T-72B"; None when only a class is known
    klass: Optional[str]                 # "ARMORED" (George's classification) or None
    coalition: Optional[int]             # 0 / 1 / 2 when the source reports it (I-T2); None = not reported
    position: Optional[Position]
    seen_model: Optional[float]
    seen_mono: Optional[float]           # None: when it was true is unknown
    age_known: bool                      # False: old Export.lua, ages are receive times
    generation: int                      # picture generation (mission load)
    flags: frozenset = frozenset()       # "no_position", "no_id", "no_altitude", "coalition_unreported" ...
    extra: Tuple[Tuple[str, Any], ...] = ()

    def get(self, name: str, default: Any = None) -> Any:
        for k, v in self.extra:
            if k == name:
                return v
        return default


@dataclass(frozen=True)
class SourceMark:
    """One source that listed a track."""
    source: str
    basis: Basis
    first_mono: float
    last_mono: float
    count: int


@dataclass(frozen=True)
class Association:
    """A George list entry -> one track. Never merges tracks; `candidates` keeps every track it could be."""
    list_index: int                      # 1-based, as on the wheel
    name: str
    strength: str                        # UNIQUE / BEST_OF_N / CLASS_ONLY / NONE
    track_id: Optional[str]
    candidates: Tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self):
        if self.strength not in STRENGTHS:
            raise ValueError(f"unknown association strength {self.strength!r}")
        if self.strength == NONE and self.track_id is not None:
            raise ValueError("an unassociated entry has no track")
        if self.strength in (UNIQUE, BEST_OF_N) and self.track_id is None:
            raise ValueError(f"a {self.strength} association needs its track")
        if self.track_id is not None and self.candidates and self.track_id not in self.candidates:
            raise ValueError("the chosen track must be one of the candidates")


@dataclass(frozen=True)
class Track:
    track_id: str                        # "T0042": stable for the generation, never reused
    key: str                             # "dcs:<object id>"
    generation: int
    category: Category
    affiliation: Affiliation
    type_name: Optional[str]
    display: Optional[str]
    position: Optional[Position]
    velocity: Optional[Velocity]
    first_seen_mono: float
    last_seen_mono: float
    last_update_mono: float
    sources: Tuple[SourceMark, ...]
    exist_conf: Conf
    ident_conf: Conf
    status: Status
    status_reason: str
    george: Optional[Association] = None
    tags: frozenset = frozenset()        # "jtac_target", "training", "outcast_target", "air_defence"
    flags: frozenset = frozenset()       # "age_unknown", "coalition_unreported", "no_altitude", "list_capped"

    def basis_of_position(self) -> Optional[Basis]:
        return self.position.basis if self.position else None


@dataclass(frozen=True)
class Evidence:
    fact: str
    basis: Basis
    source: str


@dataclass(frozen=True)
class ThreatAssessment:
    track_id: str
    category: str                        # "radar SAM", "IR SAM", "AAA", "MANPADS", "fighter", "armour" ...
    weight: int
    ring_m: Optional[float]
    ring_basis: Optional[Basis]
    range_m: Optional[float]
    inside_ring: Optional[bool]
    priority: int                        # sort key only, never spoken
    evidence: Tuple[Evidence, ...]
    freshness: Status

    def __post_init__(self):
        if not self.evidence:
            raise ValueError("every threat assessment needs its evidence")
        if self.ring_m is not None and self.ring_basis is None:
            raise ValueError("a ring needs to say where it came from")


@dataclass(frozen=True)
class SourceHealth:
    """How one source is doing, for the Status page and for coverage decisions (design sections 7, 11)."""
    source: str
    up: bool
    rows: int = 0
    age_s: Optional[float] = None        # None: unknown (old Export.lua) or never seen
    range_m: Optional[float] = None      # the list's range window (Export.lua's filter)
    cap: Optional[int] = None            # the list's maximum length
    full: bool = False                   # rows >= cap: farther units may be cut off
    farthest_m: Optional[float] = None   # farthest listed row from you (coverage edge when full)
    duplicates: int = 0
    bad_rows: int = 0
    note: str = ""


@dataclass(frozen=True)
class PictureEvent:
    kind: str                            # "new", "gone", "destroyed", "lost", "reappeared", "stale", ...
    track_id: Optional[str]
    text: str
    mono: float
    generation: int


@dataclass(frozen=True)
class PictureSnapshot:
    generation: int
    built_mono: float
    scan_age: Optional[float]
    tracks: Tuple[Track, ...]
    george_unassociated: Tuple[Association, ...] = ()   # George's entries with no track (class only, none)
    sources: Tuple[SourceHealth, ...] = ()
    threats: Tuple[ThreatAssessment, ...] = ()          # most important first
    version: int = 0
    george: Tuple[Association, ...] = ()                # every George list entry, in his order

    def track(self, track_id: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.track_id == track_id), None)


# ------------------------------------------------------------------ confidence rules (design section 5)
def existence_conf(status: Status, basis: Basis = Basis.WORLD) -> Conf:
    """How sure we are the entity exists now."""
    if basis == Basis.SENSOR:
        return Conf(Level.LOW, "on George's list, no position for it")
    if status == Status.ACTIVE:
        return Conf(Level.HIGH, "in a fresh DCS world scan")
    if status == Status.STALE:
        return Conf(Level.MEDIUM, "the picture is stale")
    if status == Status.OUT_OF_COVERAGE:
        return Conf(Level.MEDIUM, "beyond what the list can show")
    if status == Status.LOST:
        return Conf(Level.LOW, "out of the picture too long")
    return Conf(Level.LOW, "no longer in the DCS world")


def identity_conf(basis: Basis, strength: Optional[str] = None) -> Conf:
    """How sure we are what it is. World tracks carry DCS's own type name; George's names depend on
    how his entry was associated."""
    if basis == Basis.WORLD:
        return Conf(Level.HIGH, "DCS type name")
    if basis == Basis.SENSOR:
        if strength == UNIQUE:
            return Conf(Level.HIGH, "George named the type, one unit fits")
        if strength == BEST_OF_N:
            return Conf(Level.MEDIUM, "George named the type, several units fit")
        return Conf(Level.LOW, "George gives only a class")
    if basis == Basis.REPORTED:
        return Conf(Level.MEDIUM, "reported, not seen")
    return Conf(Level.LOW, "inferred")
