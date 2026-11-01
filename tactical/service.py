"""
tactical/service.py - the tactical picture, kept up to date on its own daemon thread (design sections 4
and 11).

    TACTICAL = TacticalService(state_store.snapshot, hints=..., jtac=lambda: JTAC.current)
    TACTICAL.start()
    pic = TACTICAL.picture()            # immutable PictureSnapshot, any thread, never blocks the builder
    TACTICAL.events(since_mono)         # recent picture changes (tactical/events.py)
    TACTICAL.status_link()              # the Status page row
    TACTICAL.mission_loaded()           # comms hook "LOADED": a new picture (any thread)
    TACTICAL.note_command(name, params) # Olympus commands as they're sent (who attacked what)

Every POLL_S the thread reads state_store.snapshot() (it never holds the store's lock itself), runs the
clock, the source adapters and the track table, and swaps in a new PictureSnapshot. `version` goes up
only when something a reader would show changed (a status, a position from a new scan, tags, flags,
a list's health), so the kneeboard can send only on change.

Readers take the published object under a lock held for one assignment; nothing here does I/O, HTTP or
speech. A failing step is logged once per distinct error, counted, shown on the Status page, and the
previous picture stays published.

Each update also associates George's Target List with the tracks (tactical/fusion.py), assesses the
threats (tactical/threats.py) and works out George's own list ranking once (george_evaluation(), the
same result intelligence/threat_engine.py gives today), so that later every reader shares one answer.

Nothing else reads the picture yet (design steps T3-T4): it runs beside the existing features and
changes none of them.
"""
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from dataclasses import replace

from tactical import sources
from tactical.clock import AGE_UNKNOWN, OLDER, SAME, PictureClock
from tactical.events import EventLog
from tactical.fusion import associate, previous_of
from tactical.models import PictureEvent, PictureSnapshot, Status, dcs_key
from tactical.threats import ThreatFns, assess, default_fns
from tactical.tracks import KillHints, Thresholds, TrackTable

POLL_S = 0.25
FNS_REFRESH_S = 30.0         # threat look-ups are cached per type name this long
ATTACK_KEEP_S = 600.0        # an attack order counts as evidence this long
ATTACK_MAX = 200
DCS_EVENT_KEEP_S = 600.0     # DCS's "unit dead / killed" (I-T4) counts as evidence this long
DCS_EVENT_MAX = 500


class TacticalService:
    def __init__(self, snapshot: Callable[[], dict],
                 hints: Optional[Callable[[dict, Dict[str, int]], KillHints]] = None,
                 jtac: Optional[Callable[[], Optional[dict]]] = None,
                 thresholds: Optional[Thresholds] = None, interval: float = POLL_S,
                 clock: Callable[[], float] = time.monotonic,
                 on_events: Optional[Callable[[List[PictureEvent]], None]] = None,
                 threat_fns: Optional[ThreatFns] = None,
                 george_eval: Optional[Callable[[dict], tuple]] = None):
        """snapshot: state_store.snapshot. hints(snap, attacks) -> KillHints: evidence for "destroyed",
        given attacks = {target key: attacker object id} from the Olympus commands seen. jtac: the JTAC's
        current target dict (common/jtac.py Jtac.current). on_events: called on the service thread, outside
        every lock, with each batch of new picture events. threat_fns: the threat lookups (default: the
        program's own, tactical/threats.py default_fns). george_eval(snap) -> (targets, top, constraints):
        George's Target List ranking (default: intelligence/threat_engine.py evaluate_tactical_environment),
        worked out once per update here so every reader can share it (step T7)."""
        self._snapshot, self._hints, self._jtac = snapshot, hints, jtac
        self._now, self.interval, self.on_events = clock, interval, on_events
        self._clock = PictureClock()
        self._table = TrackTable(thresholds)
        self._log = EventLog()
        self._lock = threading.Lock()                  # the published picture and the event log
        self._pic: Optional[PictureSnapshot] = None
        self._sig = None
        self._version = 0
        self._loads = 0
        self._loads_lock = threading.Lock()
        self._attacks: Deque[Tuple[str, int, float]] = deque(maxlen=ATTACK_MAX)   # (target key, attacker id, mono)
        self._attacks_lock = threading.Lock()
        self._dcs_events: Dict[str, Tuple[str, float]] = {}   # key -> (what DCS said, mono); _attacks_lock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.ticks = 0
        self.errors = 0
        self.last_error: Optional[str] = None
        self.last_error_mono: Optional[float] = None
        self.last_tick_mono: Optional[float] = None
        self.last_tick_ms: Optional[float] = None
        self._stale = True
        self._stale_reason = "no picture from DCS"
        self._age_unknown = False
        self._said_errors = set()
        self._threat_fns = threat_fns
        self._fns_cached: Optional[ThreatFns] = None
        self._fns_t = 0.0
        self._george_eval_fn = george_eval
        self._george_eval: Optional[tuple] = None          # (targets, top, constraints, mono)
        self._prev_assoc: Dict[Tuple[int, str], str] = {}
        self._world = None                                 # (generation, list health) of the last scan read

    # ------------------------------------------------------------ thread
    def start(self) -> "TacticalService":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="tactical-picture")
            self._thread.start()
            print("[TACTICAL] Picture service running (nothing reads it yet).")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()

    # ------------------------------------------------------------ inputs from other threads
    def mission_loaded(self) -> None:
        """A mission was loaded or restarted: the next tick starts a new picture."""
        with self._loads_lock:
            self._loads += 1

    def note_command(self, name: str, params: Any) -> None:
        """An Olympus command that was accepted (common/olympus.py ON_COMMAND). attackUnit {ID, targetID}
        is kept as evidence of who attacked what."""
        if name != "attackUnit" or not isinstance(params, dict):
            return
        key = dcs_key(params.get("targetID"))
        try:
            attacker = int(params.get("ID"))
        except (TypeError, ValueError):
            return
        if key is not None:
            with self._attacks_lock:
                self._attacks.append((key, attacker, self._now()))

    def note_dcs_events(self, text: str) -> None:
        """An "EVENTS" message from george_comms_hook.lua (comms_menu.ON_EVENTS, its listener thread):
        DCS's own word that units died, kept as evidence for "destroyed" (T8, I-T4)."""
        try:
            got = sources.dcs_events(text)
        except Exception as e:                          # evidence is optional
            self._error(f"dcs events: {type(e).__name__}: {e}")
            return
        if not got:
            return
        now = self._now()
        with self._attacks_lock:
            for k, said in got.items():
                old = self._dcs_events.get(k)
                if old is not None and old[0].startswith("a kill") and not said.startswith("a kill"):
                    continue                            # a later DEAD doesn't hide who killed it
                self._dcs_events[k] = (said, now)
            if len(self._dcs_events) > DCS_EVENT_MAX:
                for k, _ in sorted(self._dcs_events.items(), key=lambda kv: kv[1][1])[:-DCS_EVENT_MAX]:
                    del self._dcs_events[k]

    def dcs_events(self) -> Dict[str, str]:
        """{key: what DCS said} for unit deaths reported in the last DCS_EVENT_KEEP_S."""
        now = self._now()
        with self._attacks_lock:
            return {k: said for k, (said, t) in self._dcs_events.items() if now - t <= DCS_EVENT_KEEP_S}

    def attacks(self) -> Dict[str, int]:
        """{target key: attacker object id} for attacks ordered in the last ATTACK_KEEP_S (latest wins)."""
        now = self._now()
        with self._attacks_lock:
            rows = list(self._attacks)
        return {k: a for k, a, t in rows if now - t <= ATTACK_KEEP_S}

    # ------------------------------------------------------------ one step
    def tick(self) -> Optional[PictureSnapshot]:
        """Read, rebuild, publish. Returns the published picture (also for tests)."""
        t0 = time.perf_counter()
        try:
            with self._loads_lock:
                loads, self._loads = self._loads, 0
            now = self._now()
            if loads:
                self._clock.mission_loaded(now)
                with self._attacks_lock:
                    self._dcs_events.clear()                # unit ids start again
            snap = self._snapshot()
            st = self._clock.scan_time(snap, now)
            if st.kind in (SAME, OLDER) and self._world is not None and self._world[0] == st.generation:
                reports, health = [], dict(self._world[1])     # same scan as before: nothing new to read
            else:
                reports, health = sources.world_reports(snap, st)
                self._world = (st.generation, dict(health))
            george, health["george.list"] = sources.george_reports(snap, now, st.generation)
            j = sources.jtac_report(self._jtac() if self._jtac else None, now, st.generation)
            if j is not None:
                reports.append(j)
            hints = KillHints()
            if self._hints is not None:
                try:
                    hints = self._hints(snap, self.attacks()) or KillHints()
                except Exception as e:                  # evidence is optional; the picture isn't
                    self._error(f"hints: {type(e).__name__}: {e}")
            dcs = self.dcs_events()
            if dcs:
                hints = replace(hints, dcs_events={**dict(hints.dcs_events), **dcs})
            me = _pair(snap.get("my_ll"))
            transitions = self._table.ingest(reports, health, st, now, me, hints)
            pic = self._enrich(self._table.snapshot(now, 0), george, me, _pair(snap.get("vel"), 3))
            sig = _signature(pic)
            if sig != self._sig:
                self._sig = sig
                self._version += 1
            pic = replace(pic, version=self._version)
            self._evaluate_george(snap, now)
            names = {t.track_id: t.display or t.type_name or "unknown type" for t in pic.tracks}
            with self._lock:
                new = self._log.add(transitions, names)
                self._pic = pic
            self._stale, self._stale_reason = self._table.stale, self._table.stale_reason
            self._age_unknown = st.kind == AGE_UNKNOWN
            self.ticks += 1
            self.last_tick_mono = now
            self.last_tick_ms = (time.perf_counter() - t0) * 1000.0
        except Exception as e:
            self._error(f"{type(e).__name__}: {e}")
            return self.picture()
        if new and self.on_events is not None:
            try:
                self.on_events(new)
            except Exception as e:
                self._error(f"on_events: {type(e).__name__}: {e}")
        return pic

    def _fns(self, now: float) -> ThreatFns:
        """The threat lookups, memoised per type name (they're regex and attribute look-ups, the same for
        every unit of a type) and refreshed every FNS_REFRESH_S so types DCS describes later count."""
        if self._threat_fns is None:
            self._threat_fns = default_fns()
        if self._fns_cached is None or now - self._fns_t > FNS_REFRESH_S or now < self._fns_t:
            base = self._threat_fns
            self._fns_cached = ThreatFns(weight=lru_cache(maxsize=1024)(base.weight),
                                         attrs=lru_cache(maxsize=1024)(base.attrs),
                                         ring_m=lru_cache(maxsize=1024)(base.ring_m))
            self._fns_t = now
        return self._fns_cached

    def _enrich(self, pic: PictureSnapshot, george, me, vel) -> PictureSnapshot:
        """George's list associated with the tracks (tactical/fusion.py), then the threat assessment
        (tactical/threats.py): once per update, for every reader."""
        fns = self._fns(pic.built_mono)
        assocs = associate(george, pic.tracks, me, attrs=fns.attrs, previous=self._prev_assoc)
        self._prev_assoc = previous_of(assocs)
        by_track = {a.track_id: a for a in assocs if a.track_id is not None}
        tracks = tuple(replace(t, george=by_track[t.track_id]) if t.track_id in by_track else t for t in pic.tracks)
        threats = assess(tracks, me, vel[:2] if vel else None, fns)
        return replace(pic, tracks=tracks, george=tuple(assocs), threats=threats,
                       george_unassociated=tuple(a for a in assocs if a.track_id is None))

    def _evaluate_george(self, snap: dict, now: float) -> None:
        fn = self._george_eval_fn
        if fn is None:
            from intelligence.threat_engine import evaluate_tactical_environment
            fn = self._george_eval_fn = evaluate_tactical_environment
        try:
            targets, top, constraints = fn(snap)
        except Exception as e:
            self._error(f"George ranking: {type(e).__name__}: {e}")
            return
        with self._lock:
            self._george_eval = (targets, top, constraints, now)

    def george_evaluation(self) -> Optional[tuple]:
        """(evaluated targets, highest threat, constraints, when) from the last update, as
        intelligence/threat_engine.py evaluate_tactical_environment returns them (copies), or None."""
        with self._lock:
            ev = self._george_eval
        if ev is None:
            return None
        targets, top, constraints, when = ev
        return [dict(t) for t in targets], dict(top), dict(constraints), when

    def _error(self, text: str) -> None:
        self.errors += 1
        self.last_error, self.last_error_mono = text, self._now()
        if text not in self._said_errors and len(self._said_errors) < 50:
            self._said_errors.add(text)
            print(f"[TACTICAL ERROR] {text}")

    # ------------------------------------------------------------ readers (any thread)
    @property
    def stale(self) -> bool:
        """The whole picture is stale (or there is none yet)."""
        return self._stale

    @property
    def stale_reason(self) -> str:
        return self._stale_reason

    def picture(self) -> Optional[PictureSnapshot]:
        with self._lock:
            return self._pic

    def events(self, since_mono: float = float("-inf")) -> List[PictureEvent]:
        with self._lock:
            return self._log.recent(since_mono)

    def status_link(self) -> dict:
        """The Status page row: "Tactical picture"."""
        name = "Tactical picture"
        pic = self.picture()
        now = self._now()
        if self._thread is None and pic is None:
            return {"name": name, "ok": None, "detail": "not started"}
        if self.last_error and self.last_error_mono is not None and now - self.last_error_mono < 10.0:
            return {"name": name, "ok": False, "detail": f"error: {self.last_error}"}
        if pic is None or self.last_tick_mono is None:
            return {"name": name, "ok": None, "detail": "starting"}
        if now - self.last_tick_mono > 5.0:
            return {"name": name, "ok": False, "detail": f"not updated for {int(now - self.last_tick_mono)} s"}
        counts: Dict[Status, int] = {}
        for t in pic.tracks:
            counts[t.status] = counts.get(t.status, 0) + 1
        order = (Status.ACTIVE, Status.STALE, Status.OUT_OF_COVERAGE, Status.LOST, Status.GONE)
        said = ", ".join(f"{counts[s]} {s.value.replace('_', ' ')}" for s in order if counts.get(s))
        parts = [f"{len(pic.tracks)} tracks" + (f" ({said})" if said else "")]
        full = [h.source.split(".", 1)[1] for h in pic.sources if h.full and h.source.startswith("world.")]
        if full:
            parts.append("full lists: " + ", ".join(full))
        if self._age_unknown:
            parts.append("ages unknown: copy the new Export.lua")
        if self._stale:
            parts.append(self._stale_reason)
        parts.append(f"{self.last_tick_ms:.1f} ms per update" if self.last_tick_ms is not None else "")
        return {"name": name, "ok": not self._stale, "detail": "; ".join(p for p in parts if p)}


def _pair(v, n: int = 2):
    """A (lat, lon) / (north, east, up) tuple of floats, or None."""
    if isinstance(v, (list, tuple)) and len(v) == n:
        try:
            return tuple(float(x) for x in v)
        except (TypeError, ValueError):
            return None
    return None


def _signature(pic: PictureSnapshot):
    """What a reader would show: changes bump the picture's version; ages ticking on don't."""
    return (pic.generation,
            tuple((t.track_id, t.status,
                   t.position.lat if t.position else None, t.position.lon if t.position else None,
                   t.tags, t.flags, t.status_reason if t.status != Status.STALE else "") for t in pic.tracks),
            tuple((h.source, h.up, h.rows, h.full) for h in pic.sources),
            tuple((a.list_index, a.name, a.strength, a.track_id) for a in pic.george),
            tuple((a.track_id, a.inside_ring, a.category) for a in pic.threats))
