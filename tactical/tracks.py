"""
tactical/tracks.py - the track table: one track per DCS object, its lifecycle, and the one rule for
"is it gone?" (design sections 6 and 7).

    table = TrackTable(Thresholds())
    transitions = table.ingest(reports, health, scan_time, now_mono, me_ll, hints)
    picture = table.snapshot(now_mono)

Correlation is tier 1 only: reports with the same key ("dcs:<object id>") in the same generation are the
same entity, different keys are different entities however close they are. Nothing is merged by
geometry. (George's list - tier 2 - comes in T4.)

States (models.Status):
    ACTIVE           listed in the latest fresh scan
    STALE            the picture itself is stale (scan older than stale_s): we can't currently tell
    OUT_OF_COVERAGE  fresh scan, not listed, but beyond what every list it was in can show
    LOST             out of coverage longer than lost_s (per class: ground / air / own side)
    GONE             absent from gone_scans fresh scans in a row while inside coverage

"Destroyed" needs evidence (KillHints): a DCS event, an AI flight or Outcast attacking it, or a shot by
you / George within 90 s while it was your target. Without it the reason is "no longer in the picture".
With ages unknown (old Export.lua) nothing ever becomes GONE.

Track ids ("T0042") are allocated when a key is first seen in a generation and kept for the whole
generation, also after the track is dropped, so a unit that comes back keeps its number. A mission load
(new generation) starts a new table and a new counter.

Pure and single-owner (the tactical service thread): no locks, no I/O. snapshot() returns immutable
records any thread may read.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from tactical import clock as clk
from tactical.models import (Affiliation, Basis, Category, Conf, Level, PictureSnapshot, Position, Relation,
                             Report, SourceHealth, SourceMark, Status, Track, Velocity, existence_conf,
                             identity_conf)
from tactical.sources import coverage_edge_m, dist_m

SELF_SHOOTERS = frozenset({"George", "you"})


@dataclass(frozen=True)
class Thresholds:
    stale_s: float = 5.0                 # scan age past which the whole picture is stale (ENEMY_LIST_STALE_SEC)
    gone_scans: int = 3                  # fresh scans in a row a covered unit must be missing
    lost_ground_s: float = 120.0         # out of coverage this long -> LOST
    lost_air_s: float = 30.0
    lost_own_s: float = 30.0
    retain_ground_s: float = 600.0       # LOST kept this long, then dropped (the id stays reserved)
    retain_air_s: float = 120.0
    retain_own_s: float = 120.0
    retain_gone_s: float = 600.0         # GONE kept this long ("what changed", debrief)
    history_max: int = 500               # most non-ACTIVE tracks kept
    shot_window_s: float = 90.0          # a shot this soon before GONE counts as evidence
    velocity_min_dt: float = 0.5         # two fixes this far apart (model s) give a velocity ...
    velocity_max_dt: float = 10.0        # ... but not farther

    @classmethod
    def from_config(cls, cfg) -> "Thresholds":
        """config.py may override any of these as TACTICAL_<NAME IN CAPITALS> (all optional)."""
        kw = {}
        for name in cls.__dataclass_fields__:
            v = getattr(cfg, "TACTICAL_" + name.upper(), None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                kw[name] = type(getattr(cls, name))(v)
        return cls(**kw)

    def lost_s(self, cls_: str) -> float:
        return {"air": self.lost_air_s, "own": self.lost_own_s}.get(cls_, self.lost_ground_s)

    def retain_s(self, cls_: str) -> float:
        return {"air": self.retain_air_s, "own": self.retain_own_s}.get(cls_, self.retain_ground_s)


@dataclass(frozen=True)
class KillHints:
    """Evidence that a unit that vanished was destroyed. Built by the tactical service from the
    engagement monitor, Outcasts, Overlord's flights and (I-T4) DCS events."""
    last_shot_mono: Optional[float] = None                     # your / George's last release (ammo dropped)
    targeted: Mapping[str, str] = field(default_factory=dict)   # key -> who was attacking it
    dcs_events: Mapping[str, str] = field(default_factory=dict)  # key -> what DCS reported (I-T4)


@dataclass(frozen=True)
class Transition:
    """A change in the table. kind: "status" (track_id, before -> after), "new_picture",
    "picture_stale", "picture_fresh"."""
    kind: str
    mono: float
    generation: int
    track_id: Optional[str] = None
    key: Optional[str] = None
    before: Optional[Status] = None
    after: Optional[Status] = None
    reason: str = ""
    by: Optional[str] = None                   # who destroyed it, when known
    destroyed: bool = False


# ------------------------------------------------------------------ internal record
_CAT_RANK = {Category.UNKNOWN: 0, Category.SURFACE: 1, Category.AIR: 2, Category.GROUND: 2, Category.NAVAL: 2,
             Category.STRUCTURE: 2, Category.AIR_FIXED: 3, Category.AIR_ROTARY: 3}
_TAGS = {"world.ad": "air_defence", "world.olyx": "training", "world.oly": "olympus_flight", "world.wing": "wingman"}


@dataclass
class _Rec:
    track_id: str
    key: str
    first_mono: float
    last_seen_mono: float
    last_update_mono: float
    category: Category = Category.UNKNOWN
    type_name: Optional[str] = None
    display: Optional[str] = None
    relation: Optional[Relation] = None
    coalition: Optional[int] = None
    position: Optional[Position] = None
    velocity: Optional[Velocity] = None
    marks: Dict[str, List[float]] = field(default_factory=dict)     # source -> [first, last, count]
    lists: Set[str] = field(default_factory=set)
    status: Status = Status.ACTIVE
    reason: str = ""
    missing: int = 0
    since: float = 0.0                       # when the current status began
    tags: Set[str] = field(default_factory=set)
    flags: Set[str] = field(default_factory=set)
    by: Optional[str] = None

    def affiliation(self) -> Affiliation:
        if self.relation == Relation.OWN:
            return Affiliation.FRIENDLY
        if self.relation == Relation.OTHER:
            return Affiliation.NEUTRAL if self.coalition == 0 else Affiliation.HOSTILE
        return Affiliation.UNKNOWN

    def cls(self) -> str:
        if self.relation == Relation.OWN:
            return "own"
        return "air" if self.category in (Category.AIR, Category.AIR_FIXED, Category.AIR_ROTARY) else "ground"


class TrackTable:
    def __init__(self, thresholds: Optional[Thresholds] = None):
        self.th = thresholds or Thresholds()
        self.generation: Optional[int] = None
        self._reset()

    def _reset(self) -> None:
        self._ids: Dict[str, str] = {}
        self._counter = 0
        self._recs: Dict[str, _Rec] = {}
        self._health: Dict[str, SourceHealth] = {}
        self._jtac_key: Optional[str] = None
        self._stale: Optional[bool] = None
        self.stale_reason = "no picture from DCS"
        self.scan_age: Optional[float] = None
        self.older_ignored = 0
        self.scans = 0

    @property
    def stale(self) -> bool:
        """The whole picture is stale (or there is none yet)."""
        return self._stale is not False

    # ------------------------------------------------------------ ids
    def track_id_for(self, key: str) -> Optional[str]:
        """The id a key has (or had) in this generation, or None if never seen."""
        return self._ids.get(key)

    def _new_id(self, key: str) -> str:
        tid = self._ids.get(key)
        if tid is None:
            self._counter += 1
            tid = f"T{self._counter:04d}"
            self._ids[key] = tid
        return tid

    # ------------------------------------------------------------ ingest
    def ingest(self, reports: Iterable[Report], health: Mapping[str, SourceHealth], st: clk.ScanTime,
               now_mono: float, me_ll: Optional[Tuple[float, float]] = None,
               hints: Optional[KillHints] = None) -> List[Transition]:
        hints = hints or KillHints()
        out: List[Transition] = []
        if self.generation != st.generation:
            first = self.generation is None
            self.generation = st.generation
            self._reset()
            if not first:
                out.append(Transition("new_picture", now_mono, st.generation, reason="mission loaded"))
        gen = st.generation
        reports = list(reports)
        self._health = dict(health)
        self.scan_age = st.age_s
        jt = next((r for r in reports if r.source == "jtac"), None)
        self._jtac_key = jt.key if jt else None

        stale = self._is_stale(st, now_mono)
        self.stale_reason = self._stale_reason(st, now_mono)
        if self._stale is not None and stale != self._stale:
            out.append(Transition("picture_stale" if stale else "picture_fresh", now_mono, gen,
                                  reason=self.stale_reason))
        self._stale = stale

        if st.kind == clk.OLDER:
            self.older_ignored += 1
        elif st.kind in (clk.NEW, clk.AGE_UNKNOWN) and not stale:
            self.scans += 1
            self._presence([r for r in reports if r.basis == Basis.WORLD], st, now_mono, me_ll, hints, out)

        self._timers(st, now_mono, stale, out)
        self._trim(now_mono)
        return out

    def _is_stale(self, st: clk.ScanTime, now: float) -> bool:
        if st.kind == clk.NO_SCAN:
            return True
        if st.age_known:
            return st.age_s is None or st.age_s > self.th.stale_s
        return st.seen_mono is None or now - st.seen_mono > self.th.stale_s

    def _stale_reason(self, st: clk.ScanTime, now: float) -> str:
        if st.kind == clk.NO_SCAN:
            return "no picture from DCS"
        age = st.age_s if st.age_known else (None if st.seen_mono is None else now - st.seen_mono)
        return "picture is current" if age is not None and age <= self.th.stale_s else (
            f"picture is {int(age)} seconds old" if age is not None else "no picture from DCS")

    def _set(self, rec: _Rec, status: Status, reason: str, now: float, out: List[Transition],
             by: Optional[str] = None, destroyed: bool = False) -> None:
        before = rec.status
        if before == status and rec.reason == reason:
            return
        rec.status, rec.reason, rec.by = status, reason, by
        rec.last_update_mono = now
        if before != status:
            rec.since = now
            out.append(Transition("status", now, self.generation, rec.track_id, rec.key, before, status,
                                  reason, by, destroyed))

    # ------------------------------------------------------------ presence (fresh scans only)
    def _presence(self, world: List[Report], st: clk.ScanTime, now: float, me_ll, hints: KillHints,
                  out: List[Transition]) -> None:
        groups: Dict[str, List[Report]] = {}
        for r in world:
            if r.key is not None:
                groups.setdefault(r.key, []).append(r)
        for key, rs in groups.items():
            rec = self._recs.get(key)
            if rec is None:
                seen = min((r.seen_mono for r in rs if r.seen_mono is not None), default=now)
                rec = _Rec(self._new_id(key), key, seen, seen, now)
                rec.status, rec.since = Status.ACTIVE, now
                self._recs[key] = rec
                self._merge(rec, rs, now)
                rec.reason = "in the DCS world scan"
                out.append(Transition("status", now, self.generation, rec.track_id, key, None, Status.ACTIVE,
                                      rec.reason))
                continue
            self._merge(rec, rs, now)
            rec.missing = 0
            back = rec.status in (Status.GONE, Status.LOST)
            self._set(rec, Status.ACTIVE, "back in the DCS world scan" if back else "in the DCS world scan", now, out)

        for key, rec in self._recs.items():
            if key in groups or rec.status == Status.GONE:
                continue
            if not st.age_known:
                if rec.status != Status.LOST:
                    self._set(rec, Status.OUT_OF_COVERAGE,
                              "not listed; ages unknown (old Export.lua), so gone can't be told", now, out)
                continue
            if self._covered(rec, me_ll):
                rec.missing += 1
                if rec.missing >= self.th.gone_scans:
                    reason, by, destroyed = self._gone_reason(rec, now, hints)
                    self._set(rec, Status.GONE, reason, now, out, by, destroyed)
                elif rec.status == Status.STALE:
                    # the picture is fresh again but it isn't in it: counting towards GONE
                    rec.reason, rec.last_update_mono = "not in the latest scan", now
                # ACTIVE stays ACTIVE (at its last scan's position) and OUT_OF_COVERAGE / LOST stay as they
                # are for the gone_scans - 1 scans before GONE
            else:
                rec.missing = 0
                if rec.status not in (Status.OUT_OF_COVERAGE, Status.LOST):
                    self._set(rec, Status.OUT_OF_COVERAGE, "beyond what the lists can show", now, out)

    def _covered(self, rec: _Rec, me_ll) -> bool:
        """Would a list it was in have shown it now? Only then is its absence evidence."""
        for src in rec.lists:
            h = self._health.get(src)
            edge = coverage_edge_m(h) if h is not None else None
            if edge is None:
                continue
            if math.isinf(edge):
                return True
            if me_ll is not None and rec.position is not None:
                if dist_m((me_ll[0], me_ll[1]), (rec.position.lat, rec.position.lon)) < edge:
                    return True
        return False

    def _gone_reason(self, rec: _Rec, now: float, hints: KillHints) -> Tuple[str, Optional[str], bool]:
        ev = hints.dcs_events.get(rec.key)
        who = hints.targeted.get(rec.key)
        if ev:
            return f"destroyed: DCS reports {ev}", who, True
        if who and who not in SELF_SHOOTERS:
            return f"destroyed by {who}", who, True
        if who in SELF_SHOOTERS and hints.last_shot_mono is not None \
                and 0.0 <= now - hints.last_shot_mono <= self.th.shot_window_s:
            return f"destroyed by {who}", who, True
        return "no longer in the picture, cause unknown", None, False

    # ------------------------------------------------------------ merge one key's reports
    def _merge(self, rec: _Rec, rs: List[Report], now: float) -> None:
        for r in rs:
            m = rec.marks.get(r.source)
            seen = r.seen_mono if r.seen_mono is not None else now
            if m is None:
                rec.marks[r.source] = [seen, seen, 1]
            else:
                m[1], m[2] = max(m[1], seen), m[2] + 1
            rec.lists.add(r.source)
            if r.source in _TAGS:
                rec.tags.add(_TAGS[r.source])
            if r.relation is not None:
                rec.relation = r.relation
            if r.coalition is not None:
                rec.coalition = r.coalition
            if _CAT_RANK[r.category] > _CAT_RANK[rec.category]:
                rec.category = r.category
            if r.type_name and not rec.type_name:
                rec.type_name = r.type_name
            if not rec.display:
                rec.display = r.type_name or r.get("unit")
            if r.seen_mono is not None:
                rec.last_seen_mono = max(rec.last_seen_mono, r.seen_mono)
            if "age_unknown" in r.flags:
                rec.flags.add("age_unknown")
            else:
                rec.flags.discard("age_unknown")
        cands = [r.position for r in rs if r.position is not None]
        if cands:
            newest = max(cands, key=lambda p: (p.seen_model if p.seen_model is not None else -math.inf,
                                               p.alt_m is not None))
            if newest.alt_m is None:
                alt = next((p.alt_m for p in cands if p.alt_m is not None and p.seen_model == newest.seen_model), None)
                if alt is not None:
                    newest = Position(newest.lat, newest.lon, alt, newest.basis, newest.seen_model, newest.seen_mono)
            old = rec.position
            if old is None or old.seen_model is None or newest.seen_model is None or newest.seen_model > old.seen_model:
                rec.velocity = self._velocity(old, newest)      # None after a gap: no stale velocity
                rec.position = newest
        rec.last_update_mono = now
        rec.flags.discard("no_position")
        if rec.position is None:
            rec.flags.add("no_position")
        if rec.position is not None and rec.position.alt_m is None:
            rec.flags.add("no_altitude")
        else:
            rec.flags.discard("no_altitude")
        if rec.relation == Relation.OTHER and rec.coalition is None:
            rec.flags.add("coalition_unreported")
        else:
            rec.flags.discard("coalition_unreported")

    def _velocity(self, a: Optional[Position], b: Position) -> Optional[Velocity]:
        if a is None or a.seen_model is None or b.seen_model is None:
            return None
        dt = b.seen_model - a.seen_model
        if not (self.th.velocity_min_dt <= dt <= self.th.velocity_max_dt):
            return None
        n = (b.lat - a.lat) * 111_320.0
        e = (b.lon - a.lon) * 111_320.0 * math.cos(math.radians((a.lat + b.lat) / 2))
        up = (b.alt_m - a.alt_m) / dt if a.alt_m is not None and b.alt_m is not None else None
        return Velocity(n / dt, e / dt, up, Basis.INFERRED, a.seen_model, b.seen_model)

    # ------------------------------------------------------------ time-based changes
    def _timers(self, st: clk.ScanTime, now: float, stale: bool, out: List[Transition]) -> None:
        why = self._stale_reason(st, now)
        for rec in self._recs.values():
            if stale and rec.status in (Status.ACTIVE, Status.OUT_OF_COVERAGE):
                self._set(rec, Status.STALE, why, now, out)
            elif rec.status == Status.STALE and stale and rec.reason != why:
                rec.reason = why                         # the age moves on; no transition
            elif rec.status == Status.OUT_OF_COVERAGE and now - rec.since > self.th.lost_s(rec.cls()):
                self._set(rec, Status.LOST, "out of the picture too long", now, out)

    def _trim(self, now: float) -> None:
        """Drop LOST / GONE tracks past their retention, and the oldest non-ACTIVE ones past history_max.
        Their ids stay reserved (self._ids) for the whole generation."""
        drop = [key for key, rec in self._recs.items()
                if (rec.status == Status.LOST and now - rec.since > self.th.retain_s(rec.cls()))
                or (rec.status == Status.GONE and now - rec.since > self.th.retain_gone_s)]
        for key in drop:
            del self._recs[key]
        old = sorted((r for r in self._recs.values() if r.status != Status.ACTIVE), key=lambda r: r.last_update_mono)
        for rec in old[:max(0, len(old) - self.th.history_max)]:
            del self._recs[rec.key]

    # ------------------------------------------------------------ snapshot
    def snapshot(self, now_mono: float, version: int = 0, george_unassociated=(), threats=()) -> PictureSnapshot:
        tracks = []
        for rec in sorted(self._recs.values(), key=lambda r: r.track_id):
            tags = set(rec.tags)
            if rec.key == self._jtac_key:
                tags.add("jtac_target")
            ident = identity_conf(Basis.WORLD) if rec.type_name else Conf(Level.LOW, "type not reported")
            marks = tuple(SourceMark(s, Basis.WORLD, m[0], m[1], int(m[2])) for s, m in sorted(rec.marks.items()))
            tracks.append(Track(
                track_id=rec.track_id, key=rec.key, generation=self.generation or 0, category=rec.category,
                affiliation=rec.affiliation(), type_name=rec.type_name, display=rec.display or rec.type_name,
                position=rec.position, velocity=rec.velocity, first_seen_mono=rec.first_mono,
                last_seen_mono=rec.last_seen_mono, last_update_mono=rec.last_update_mono, sources=marks,
                exist_conf=existence_conf(rec.status), ident_conf=ident, status=rec.status,
                status_reason=rec.reason, george=None, tags=frozenset(tags), flags=frozenset(rec.flags)))
        return PictureSnapshot(generation=self.generation or 0, built_mono=now_mono, scan_age=self.scan_age,
                               tracks=tuple(tracks), george_unassociated=tuple(george_unassociated),
                               sources=tuple(self._health[k] for k in sorted(self._health)),
                               threats=tuple(threats), version=version)

    def destroyed_by(self, key: str) -> Optional[str]:
        rec = self._recs.get(key)
        return rec.by if rec else None
