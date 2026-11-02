"""
instructor/session.py - one exercise at a time: select -> armed -> running -> evaluated.

    Instructor(snapshot, speak, history, ...)
        .wanted(text) / .handle(text)   "Instructor, ..." (instructor/voice.py); returns what to say
        .start()                        the observer thread (10x a second: every new telemetry packet once,
                                        from state_store.flight_since(seq), else the snapshot's latest)
        .tick(sample)                   one new packet (the observer calls it; tests call it directly)
        .on_mission_loaded()            a mission load cancels a running attempt
        .busy()                         armed or running (the crew chief's talk-down holds its hover calls)
        .link() / .view() / .briefing_section()   the kneeboard Status row, Instructor tab, Briefing section

Exercises (instructor/catalog.py; when their clocks start and stop: instructor/runners.py):
    hover     "Instructor, hover exercise" / "begin": clock after 3 s of steady hover; 30-90 s by level
    approach  "Instructor, approach to Alpha" (a named waypoint, else the nearest helipad / FARP) / "begin":
              fly out beyond 800 m, the clock starts inside 600 m and stops at touchdown
    fuel      "Instructor, fuel exercise" / "begin" on the ground: a bingo is given, scored on the turn for
              home, the fuel used after bingo, the direct return and landing back at the start
Levels come from the history (instructor/adaptive.py) unless "easier" / "harder" / "level 3" set the
next one. Aborts (stored, never scored): a mission load, model time going back, another aircraft,
"Instructor, stop", and each exercise's own (leaving the hover, flying away from the point, ...).
Nothing here sends anything to DCS: the only outputs are speak(), the history files and the views.
"""
import math
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from instructor import adaptive, catalog, feedback, runners, scoring, voice
from instructor.history import History
from instructor.models import Evaluation, Exercise, Sample
from instructor.runners import hovering  # noqa: F401  (kept for callers of the Phase 1 name)

TELEMETRY_STALE_S = 3.0
POLL_S = 0.1
MAX_SAMPLES = 30000            # 50 min at 10 Hz (the fuel exercise runs up to 45 min)

IDLE, SELECTED, ARMED, RUNNING, EVALUATING = "idle", "selected", "armed", "running", "evaluating"
# 1 Nov (one training mode): these need training mode on; results / status / suggest always answer
GATED = ("select", "brief", "begin", "easier", "harder", "level")
TRAINING_OFF = "Instructor, training mode is off. Say training mode on."
VERSIONS = {ex_id: catalog.get(ex_id, 1).version for ex_id in catalog.EXERCISES}
TAG_OF = {c.id: c.tag for ex_id in catalog.EXERCISES for c in catalog.get(ex_id, 1).criteria}


def sample_from_record(r: Dict[str, Any], snap: Dict[str, Any], lbs: Optional[float]) -> Optional[Sample]:
    """A Sample from one state_store flight record (core/state.py), with the snapshot's aircraft."""
    t = r.get("t")
    if not isinstance(t, (int, float)):
        return None

    def num(v, lo=None):
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and (lo is None or v >= lo) else None

    def vec(v, n=3):
        return tuple(float(x) for x in v) if isinstance(v, (list, tuple)) and len(v) == n else None
    return Sample(float(t), str(r.get("aircraft") or snap.get("aircraft") or ""), vec(r.get("pos")), vec(r.get("vel")),
                  num(r.get("agl_ft"), 0.0), num(r.get("vs_fpm")), num(r.get("hdg"), 0.0), num(r.get("speed_kts")),
                  vec(r.get("ll"), 2), lbs, num(r.get("pitch")), num(r.get("roll")), num(r.get("g")), num(r.get("gmax")))


def sample_from_snapshot(snap: Dict[str, Any], fuel: Optional[Callable[[dict], Optional[float]]] = None) -> Optional[Sample]:
    """A Sample from state_store.snapshot(), or None without model time / fresh telemetry.
    fuel: core.fuel.current_lbs (main.py passes it; the package doesn't import core)."""
    t = snap.get("t_model")
    if not isinstance(t, (int, float)) or (snap.get("telemetry_age") or 0) > TELEMETRY_STALE_S:
        return None

    def num(v, lo=None):
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and (lo is None or v >= lo) else None

    def vec(v, n=3):
        return tuple(float(x) for x in v) if isinstance(v, (list, tuple)) and len(v) == n else None

    lbs = None
    if fuel is not None:
        try:
            lbs = num(fuel(snap), 0.0)
        except Exception:
            lbs = None
    return Sample(float(t), str(snap.get("aircraft") or ""), vec(snap.get("pos")), vec(snap.get("vel")),
                  num(snap.get("agl_ft"), 0.0), num(snap.get("vs_fpm")), num(snap.get("hdg"), 0.0),
                  num(snap.get("speed_kts")), vec(snap.get("my_ll"), 2), lbs, num(snap.get("pitch")),
                  num(snap.get("roll")), num(snap.get("g")))


class SkidLearner:
    """huey/talkdown.py's rule: still (< 1 kt, |vs| < 30 fpm) below 12 ft for 3 s = the ground reading;
    the learned value only ever goes down (a steady low hover must not become 'the ground')."""

    def __init__(self):
        self.skid_ft: Optional[float] = None
        self._since: Optional[float] = None
        self._vals: List[float] = []
        self.aircraft = ""

    def update(self, s: Sample) -> None:
        if s.aircraft != self.aircraft:                 # another aircraft: learn again
            self.aircraft, self.skid_ft, self._since, self._vals = s.aircraft, None, None, []
        if s.vel is None or s.vs_fpm is None or s.agl_ft is None:
            return
        gs = math.hypot(s.vel[0], s.vel[1]) * runners.KT_PER_MS
        if not (s.agl_ft < 12 and abs(s.vs_fpm) < 30 and gs < 1.0):
            self._since, self._vals = None, []
            return
        if self._since is None or s.t < self._since:
            self._since, self._vals = s.t, []
        self._vals.append(s.agl_ft)
        if s.t - self._since >= 3.0:
            m = statistics.median(self._vals)
            if self.skid_ft is None or m < self.skid_ft:
                self.skid_ft = m


class Instructor:
    def __init__(self, snapshot: Callable[[], dict], speak: Callable[[str], None], history: History,
                 mission_name: Callable[[], str] = lambda: "", level: int = 1,
                 printer: Callable[[str], None] = print, run_async: bool = True,
                 wall: Callable[[], float] = time.time,
                 targets: Optional[Callable[[str, dict], Any]] = None,
                 fuel: Optional[Callable[[dict], Optional[float]]] = None,
                 fuel_allowance_lbs: float = catalog.FUEL_ALLOWANCE_LBS,
                 fuel_after_share: float = catalog.FUEL_AFTER_SHARE,
                 reserve: Optional[Callable[[dict], Optional[float]]] = None,
                 flight: Optional[Callable[[int], List[dict]]] = None,
                 fast: Optional[Callable[[bool], None]] = None,
                 enabled: Optional[Callable[[], bool]] = None):
        """targets(text, snap) -> {"name", "ll", "kind"} or a refusal string (main.py: named waypoint,
        else the nearest helipad / FARP). fuel(snap) -> pounds or None (core.fuel.current_lbs).
        reserve(snap) -> the landing reserve in pounds for this aircraft (the fuel exercise checks it).
        flight(seq) -> the flight records newer than seq (state_store.flight_since): every packet once.
        fast(on) -> ask Export.lua for the 10 Hz flight block (I5) while an exercise is selected or running.
        enabled() -> training mode is on (1 Nov: one training mode). Exercises can only be selected or
        begun while it is; results, status and the suggestion always answer. None = always on (tests)."""
        self.snapshot, self.speak, self.history = snapshot, speak, history
        self.mission_name, self.level, self.printer = mission_name, int(level), printer
        self.run_async, self.wall = run_async, wall
        self.targets, self.fuel = targets, fuel
        self.fuel_allowance, self.fuel_share = float(fuel_allowance_lbs), float(fuel_after_share)
        self.reserve = reserve
        self.flight, self.fast = flight, fast
        self.enabled = enabled
        self._fast_on: Optional[bool] = None
        self._lock = threading.Lock()
        self.state = IDLE
        self.exercise: Optional[Exercise] = None
        self.runner = None
        self._ctx: Dict[str, Any] = {}               # what select resolved (approach target)
        self.skid = SkidLearner()
        self._samples: List[Sample] = []
        self._t0_wall = ""
        self._last_t: Optional[float] = None
        self._last_sample: Optional[Sample] = None
        self._aircraft = ""
        self._recent_t: deque = deque(maxlen=41)   # model times of the last packets: live sample rate
        self.last: Optional[Evaluation] = None
        self.overrides: Dict[str, int] = {}          # exercise id -> level for its next attempt
        self._summary_key = None
        self._summary: Dict[str, Any] = {}
        self._seq = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ observer
    def start(self) -> "Instructor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="instructor-observer")
            self._thread.start()
        return self

    def stop_thread(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self.poll()
            except Exception as e:                      # never take the thread down
                self.printer(f"[INSTRUCTOR] {type(e).__name__}: {e}")

    def poll(self) -> None:
        self._update_fast()
        snap = self.snapshot()
        seq = snap.get("tel_seq")
        if seq is None or seq == self._seq:
            return
        if self.flight is None or (snap.get("telemetry_age") or 0) > TELEMETRY_STALE_S:
            self._seq = seq
            s = sample_from_snapshot(snap, self.fuel)
            if s is not None:
                self.tick(s)
            return
        recs = self.flight(self._seq if self._seq is not None and self._seq < seq else seq - 1)
        self._seq = seq
        lbs = None
        if self.fuel is not None:
            try:
                v = self.fuel(snap)
                lbs = float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 else None
            except Exception:
                lbs = None
        for r in recs:
            s = sample_from_record(r, snap, lbs)
            if s is not None:
                self.tick(s)

    def wants_fast(self) -> bool:
        """Hover, turn and approach want 10 Hz; the fuel exercise (up to 45 min) is fine at 4 Hz."""
        return self.state in (SELECTED, ARMED, RUNNING) and self.exercise is not None and self.exercise.area != "fuel"

    def _update_fast(self) -> None:
        """The 10 Hz flight block on while an exercise is selected, armed or running; off otherwise."""
        if self.fast is None:
            return
        want = self.wants_fast()
        if want != self._fast_on:
            self._fast_on = want
            try:
                self.fast(want)
            except Exception as e:
                self.printer(f"[INSTRUCTOR] fast telemetry {'on' if want else 'off'} failed: {type(e).__name__}: {e}")

    def tick(self, s: Sample) -> None:
        say: List[str] = []
        finish = None
        with self._lock:
            restarted = self._last_t is not None and s.t < self._last_t
            if restarted:
                self._recent_t.clear()
            self._recent_t.append(s.t)
            self.skid.update(s)
            if self.state == ARMED and self.exercise.fits(s.aircraft):
                line = self.runner.armed(s, self.skid.skid_ft)
                if line:
                    self._start_locked(s)
                    say.append(line)
            elif self.state == RUNNING:
                finish = self._tick_running(s, restarted)
            self._last_t, self._last_sample = s.t, s
        for line in say:
            self.speak(line)
        if finish is not None:
            self._evaluate(*finish)

    def _start_locked(self, s: Sample) -> None:
        self.state, self._t0_wall = RUNNING, self._now_iso()
        self._samples, self._aircraft = [s], s.aircraft
        self.printer(f"[INSTRUCTOR] {self.exercise.id} level {self.exercise.level} started at "
                     f"{scoring.mission_time(s.t)}, {s.agl_ft if s.agl_ft is not None else -1:.0f} ft AGL.")

    def _tick_running(self, s: Sample, restarted: bool):
        if restarted:
            return self._end_locked("the mission restarted")
        if s.aircraft != self._aircraft:
            return self._end_locked("the aircraft changed")
        got = self.runner.running(s, self.skid.skid_ft)
        if got is not None and got[0] == "abort":
            return self._end_locked(got[1])
        if len(self._samples) < MAX_SAMPLES:
            self._samples.append(s)
        if got is not None and got[0] == "done":
            return self._end_locked(None)
        return None

    def _end_locked(self, aborted: Optional[str]):
        """Lock held: hand the attempt to the evaluator. Returns the arguments for _evaluate."""
        ex, runner, samples = self.exercise, self.runner, list(self._samples)
        t0, t1 = runner.window() if runner.t0 is not None else (samples[0].t if samples else 0.0,) * 2
        self.state = EVALUATING
        self._samples = []
        return ex, samples, t0, t1, self._t0_wall, aborted, runner.context()

    def _evaluate(self, ex: Exercise, samples: List[Sample], t0: float, t1: float, t0_wall: str,
                  aborted: Optional[str], context: dict) -> None:
        def work():
            try:
                meta = {"aircraft": samples[0].aircraft if samples else "", "mission": self.mission_name() or "",
                        "pilot": self.history.pilot, "start_wall": t0_wall, "end_wall": self._now_iso()}
                ev = scoring.evaluate(ex, samples, t0, t1, meta, aborted=aborted, context=context)
                self.history.append(ev.to_json(), None if aborted else [x.to_json() for x in samples])
                nxt = None
                if ev.valid_for_skills():
                    lv, _why = self._next_level(ex.id)
                    nxt = lv if lv != ev.level else None
                with self._lock:
                    self.last = ev
                    if self.state == EVALUATING:
                        self.state = SELECTED
                        if nxt is not None:                    # the next attempt at the new level
                            self.exercise = catalog.get(ex.id, nxt)
                self.printer(f"[INSTRUCTOR] {ev.exercise_id}: {ev.outcome}, score {ev.score}, {ev.confidence} "
                             f"confidence; " + "; ".join(d.evidence for d in ev.deductions[:3]))
                line = feedback.result_line(ev)
                if nxt is not None:
                    line += f" {'Moving up' if nxt > ev.level else 'Next one'} to level {nxt}."
                self.speak(line)
            except Exception as e:
                with self._lock:
                    if self.state == EVALUATING:
                        self.state = SELECTED
                self.printer(f"[INSTRUCTOR] Evaluation failed: {type(e).__name__}: {e}")
                self.speak("Instructor, unable to score that one, system fault.")
        if self.run_async:
            threading.Thread(target=work, daemon=True, name="instructor-eval").start()
        else:
            work()

    # ------------------------------------------------------------------ levels
    def _valid_rows(self, exercise_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return adaptive.valid(self.history.recent(n=200), exercise_id, VERSIONS)

    def _next_level(self, exercise_id: str):
        if exercise_id in self.overrides:
            return self.overrides[exercise_id], "your choice"
        return adaptive.next_level(self._valid_rows(exercise_id), self.level)

    def busy(self) -> bool:
        """An exercise is armed or running (the crew chief's talk-down holds its hover calls)."""
        return self.state in (ARMED, RUNNING)

    # ------------------------------------------------------------------ voice
    @staticmethod
    def wanted(text: str) -> bool:
        return voice.addressed(text) is not None

    def handle(self, text: str) -> str:
        rest = voice.addressed(text)
        if rest is None:
            return ""
        cmd, ex_id, arg = voice.parse(rest)
        if cmd in GATED and not self.training_on():
            return TRAINING_OFF
        if cmd == "on" and not self.training_on():
            return TRAINING_OFF
        snap = self.snapshot()
        aircraft = str(snap.get("aircraft") or "")
        if cmd in ("select", "brief", "begin"):           # no exercise named: the selected one, else the hover
            ex_id = ex_id or (self.exercise.id if self.exercise else catalog.HOVER_ID)
        target = None
        if cmd in ("select", "brief", "begin") and ex_id == catalog.APPROACH_ID and (
                self.exercise is None or self.exercise.id != ex_id or voice.names_target(rest)):
            target = self._resolve_target(rest, snap)
            if isinstance(target, str):
                return target
        sample = sample_from_snapshot(snap, self.fuel) if cmd == "begin" else None
        finish = None
        with self._lock:
            if cmd in ("select", "brief"):
                said = self._select_locked(ex_id, aircraft, target)
            elif cmd == "begin":
                said = self._begin_locked(ex_id, aircraft, target, sample)
            elif cmd in ("stop", "off"):
                if self.state == RUNNING:
                    finish = self._end_locked("stopped by the pilot")
                    said = ""                                   # the result line says it
                elif self.state == ARMED:
                    self.state = SELECTED
                    said = "Instructor, cancelled."
                else:
                    said = "Instructor, nothing running." if cmd == "stop" else "Instructor, standing by."
                if cmd == "off" and self.state != EVALUATING:
                    self.state = IDLE
            elif cmd == "on":
                said = "Instructor, standing by. Say hover, hover turn, approach or fuel exercise."
            elif cmd == "result":
                said = feedback.details(self.last) if self.last else "Instructor, no results yet this session."
            elif cmd == "status":
                said = self._status_locked()
            elif cmd in ("easier", "harder", "level"):
                said = self._set_level_locked(cmd, arg)
            elif cmd == "suggest":
                said = None
            else:
                said = "Instructor, say again. Hover, hover turn, approach or fuel exercise, then begin."
        if cmd == "suggest":
            said = self._suggest_text(aircraft)
        if finish is not None:
            self._evaluate(*finish)
        return said

    def training_on(self) -> bool:
        if self.enabled is None:
            return True
        try:
            return bool(self.enabled())
        except Exception:
            return False

    def stand_down(self, why: str = "training mode off") -> str:
        """Training mode went off: a running attempt ends (stored as aborted, never scored), an armed
        or selected one is put away. Returns what to say first ('' when the result line says it)."""
        finish, said = None, ""
        with self._lock:
            if self.state == RUNNING:
                finish = self._end_locked(why)
            elif self.state == ARMED:
                said = "Instructor, exercise cancelled."
            if self.state != EVALUATING:
                self.state = IDLE
        if finish is not None:
            self._evaluate(*finish)
        return said

    def record_kill(self, what: str, seconds: float) -> None:
        """A training target destroyed (common/training_targets.py): kept in the score log."""
        snap = self.snapshot()
        self.history.add_kill({"what": str(what), "seconds": round(float(seconds), 1),
                               "aircraft": str(snap.get("aircraft") or ""), "mission": self.mission_name() or "",
                               "when": self._now_iso()})

    def _resolve_target(self, rest: str, snap: dict):
        if self.targets is None:
            return "Instructor, unable, I have no waypoints or FARPs."
        try:
            got = self.targets(rest, snap)
        except Exception as e:
            self.printer(f"[INSTRUCTOR] target lookup: {type(e).__name__}: {e}")
            got = None
        if not got:
            return "Instructor, no landing point. Name a waypoint, or fly near a helipad or FARP."
        return got

    def _make_runner(self, ex: Exercise, target):
        if ex.id == catalog.APPROACH_ID:
            return runners.ApproachRunner(ex, target)
        if ex.id == catalog.FUEL_ID:
            res = 0.0
            if self.reserve is not None:
                try:
                    res = float(self.reserve(self.snapshot()) or 0.0)
                except Exception:
                    res = 0.0
            return runners.FuelRunner(ex, self.fuel_allowance, self.fuel_share, res)
        if ex.id == catalog.TURN_ID:
            return runners.TurnRunner(ex)
        return runners.HoverRunner(ex)

    def _fresh_exercise(self, ex_id: str) -> Exercise:
        level, _ = self._next_level(ex_id)
        self.overrides.pop(ex_id, None)               # an override is for one attempt
        return catalog.get(ex_id, level)

    def _select_locked(self, ex_id: str, aircraft: str, target) -> str:
        if self.state in (RUNNING, EVALUATING):
            return "Instructor, an exercise is running. Say stop first."
        ex = self._fresh_exercise(ex_id)
        if aircraft and not ex.fits(aircraft):
            return f"Instructor, the {ex.name} exercise is for the Huey and the Apache."
        self.exercise, self.state = ex, SELECTED
        if target is not None:
            self._ctx = {"target": target}
        return catalog.brief(ex, {"target_name": (self._ctx.get("target") or {}).get("name")}
                             if ex.id == catalog.APPROACH_ID else None)

    def _begin_locked(self, ex_id: str, aircraft: str, target, sample: Optional[Sample]) -> str:
        if self.state == RUNNING:
            left = self.runner.left_s()
            return (f"Instructor, already running, {left:.0f} seconds to go." if left is not None
                    else f"Instructor, {self.exercise.name} already running.")
        if self.state == EVALUATING:
            return "Instructor, still scoring the last one."
        if self.exercise is None or ex_id != self.exercise.id or self.state == IDLE:
            self.exercise = self._fresh_exercise(ex_id)
        if aircraft and not self.exercise.fits(aircraft):
            return f"Instructor, the {self.exercise.name} exercise is for the Huey and the Apache."
        if target is not None:
            self._ctx = {"target": target}
        if self.exercise.id == catalog.APPROACH_ID and not self._ctx.get("target"):
            return "Instructor, no landing point. Name a waypoint, or fly near a helipad or FARP."
        runner = self._make_runner(self.exercise, self._ctx.get("target"))
        refusal, line, start_now = runner.begin(sample, self.skid.skid_ft, sample.fuel_lbs if sample else None)
        if refusal:
            return refusal
        self.runner = runner
        if start_now:
            self._start_locked(sample)
            self._last_t = sample.t
            return line
        self.state = ARMED
        return line or (f"Instructor, {self.exercise.name}, level {self.exercise.level}, "
                        f"{self.exercise.duration_s:.0f} seconds. Clock starts when you're steady.")

    def _set_level_locked(self, cmd: str, arg: Optional[int]) -> str:
        ex_id = self.exercise.id if self.exercise else catalog.HOVER_ID
        cur, _ = self._next_level(ex_id)
        if self.exercise is not None and self.exercise.id == ex_id and self.state in (SELECTED, ARMED):
            cur = self.exercise.level
        lv = cur - 1 if cmd == "easier" else cur + 1 if cmd == "harder" else int(arg or cur)
        lv = max(adaptive.MIN_LEVEL, min(adaptive.MAX_LEVEL, lv))
        name = catalog.NAMES[ex_id]
        if self.state == RUNNING:
            self.overrides[ex_id] = lv
            return f"Instructor, next {name} at level {lv}."
        self.overrides.pop(ex_id, None)
        self.exercise = catalog.get(ex_id, lv)
        if self.state == IDLE:
            self.state = SELECTED
        if self.state == ARMED:
            self.runner = self._make_runner(self.exercise, self._ctx.get("target"))
            if self.exercise.id == catalog.APPROACH_ID or self.exercise.id == catalog.FUEL_ID:
                self.state = SELECTED                   # their begin() checks position again
        return f"Instructor, {name} level {lv}." + (" Say begin." if self.state == SELECTED else "")

    def _suggest_text(self, aircraft: str) -> str:
        cands = [i for i in catalog.EXERCISES if not aircraft or catalog.get(i, 1).fits(aircraft)]
        ex_id, why = adaptive.suggest(self._valid_rows(), cands, TAG_OF, now=self.wall())
        if ex_id is None:
            return "Instructor, no exercises for this aircraft."
        return f"Instructor, I suggest the {catalog.NAMES[ex_id]} exercise: {why}. Say {catalog.NAMES[ex_id]} exercise."

    def status_line(self) -> str:
        """The exercise part of "training status", without the "Instructor," ('' when standing by)."""
        with self._lock:
            if self.state == IDLE:
                return ""
            said = self._status_locked()
        said = said[len("Instructor, "):] if said.startswith("Instructor, ") else said
        return said[:1].upper() + said[1:]

    def _status_locked(self) -> str:
        if self.state == RUNNING:
            left = self.runner.left_s()
            if self.exercise.id == catalog.FUEL_ID:
                return (f"Instructor, fuel exercise running, bingo {self.runner.bingo:.0f} pounds"
                        + (", passed." if self.runner.t_bingo is not None else "."))
            return (f"Instructor, {self.exercise.name} running" +
                    (f", {left:.0f} seconds to go." if left is not None else "."))
        if self.state == ARMED:
            what = {"hover.stationary": "waiting for a steady hover",
                    "approach.pad": f"waiting for you inside {catalog.APPROACH_START_M:.0f} meters"}
            return f"Instructor, {self.exercise.name} armed, {what.get(self.exercise.id, 'waiting')}."
        if self.state == EVALUATING:
            return "Instructor, scoring."
        if self.state == SELECTED:
            return f"Instructor, {self.exercise.name} level {self.exercise.level} selected. Say begin."
        return "Instructor, standing by."

    # ------------------------------------------------------------------ mission / kneeboard
    def on_mission_loaded(self) -> None:
        finish, said = None, ""
        with self._lock:
            if self.state == RUNNING:
                finish = self._end_locked("the mission was reloaded")
            elif self.state == ARMED:
                self.state, said = SELECTED, "Instructor, mission reloaded, exercise cancelled."
            self._recent_t.clear()
            self._last_t, self._last_sample = None, None
            self._ctx = {}                             # waypoints / FARPs belong to the old mission
            self.skid = SkidLearner()
        if said:
            self.speak(said)
        if finish is not None:
            self._evaluate(*finish)

    def live_rate_hz(self) -> Optional[float]:
        with self._lock:
            ts = list(self._recent_t)
        if len(ts) < 2 or ts[-1] <= ts[0]:
            return None
        return (len(ts) - 1) / (ts[-1] - ts[0])

    def link(self) -> dict:
        """The kneeboard Status page row."""
        rate = self.live_rate_hz()
        with self._lock:
            st, ex, last, skid = self.state, self.exercise, self.last, self.skid.skid_ft
            left = self.runner.left_s() if st == RUNNING and self.runner else None
        parts = [st if ex is None else f"{st} {ex.id} L{ex.level}"]
        if left is not None:
            parts.append(f"{left:.0f} s to go")
        if rate is not None:
            parts.append(f"{rate:.1f} samples/s" + (" (medium confidence below 5)" if rate < 5.0 else ""))
        parts.append(f"skid {skid:.0f} ft" if skid is not None else "skid height not learned")
        if last is not None:
            parts.append(f"last: {last.exercise_name} {last.outcome}"
                         + (f" {last.score:.0f} ({last.confidence})" if last.score is not None else ""))
        return {"name": "Instructor", "ok": None, "detail": "; ".join(parts)}

    def _progress(self, aircraft: str) -> Dict[str, Any]:
        """Levels, medians, weaknesses and the suggestion: recomputed only when the history changes."""
        rows = self.history.recent(n=200)
        key = (len(rows), rows[-1].get("end_wall") if rows else None, tuple(sorted(self.overrides.items())), aircraft)
        if key == self._summary_key:
            return self._summary
        val = adaptive.valid(rows, None, VERSIONS)
        levels = []
        for ex_id in catalog.EXERCISES:
            mine = [r for r in val if r.get("exercise_id") == ex_id]
            lv, why = self._next_level(ex_id)
            med = adaptive.median_score(mine)
            levels.append({"exercise": catalog.NAMES[ex_id], "level": lv, "why": why, "attempts": len(mine),
                           "median": None if med is None else round(med)})
        weak = [adaptive.TAG_WORDS.get(t, t) for t in adaptive.weaknesses(val, TAG_OF)]
        cands = [i for i in catalog.EXERCISES if not aircraft or catalog.get(i, 1).fits(aircraft)]
        sug, why = adaptive.suggest(val, cands, TAG_OF, now=self.wall())
        self._summary_key = key
        self._summary = {"levels": levels, "weak": weak,
                         "suggest": {"exercise": catalog.NAMES.get(sug, ""), "why": why} if sug else None}
        return self._summary

    def view(self) -> dict:
        """The kneeboard Instructor tab (values rounded, so the feed only changes when they do)."""
        snap_ac = (self._last_sample.aircraft if self._last_sample else "") or ""
        with self._lock:
            st, ex, runner, last, s = self.state, self.exercise, self.runner, self.last, self._last_sample
            live = runner.live(s) if st == RUNNING and runner else {}
            left = runner.left_s() if st == RUNNING and runner else None
            ctx_target = (self._ctx.get("target") or {}).get("name")
        out: Dict[str, Any] = {"state": st, "exercise": None, "live": live,
                               "left_s": None if left is None else int(left)}
        if ex is not None:
            out["exercise"] = {"name": ex.name, "level": ex.level, "target": ctx_target if ex.id == catalog.APPROACH_ID else None}
        if last is not None:
            out["last"] = {"exercise": last.exercise_name, "level": last.level, "outcome": last.outcome,
                           "score": None if last.score is None else round(last.score),
                           "confidence": last.confidence, "line": feedback.result_line(last),
                           "deductions": [{"points": round(d.points, 1), "evidence": d.evidence} for d in last.deductions],
                           "not_judged": [f"{r['label']}: {r.get('reason', '')}" for r in last.criteria if not r.get("judged")],
                           "when": last.end_wall[11:16]}
        out.update(self._progress(snap_ac))
        out["kills"] = [{"what": k.get("what", ""), "seconds": k.get("seconds"),
                         "when": str(k.get("when", ""))[5:16].replace("T", " ")} for k in self.history.kills(8)]
        out["training_on"] = self.training_on()
        return out

    def briefing_section(self) -> Optional[dict]:
        """The selected exercise for the kneeboard Briefing page, or None."""
        with self._lock:
            ex, st, target = self.exercise, self.state, (self._ctx.get("target") or {}).get("name")
            bingo = getattr(self.runner, "bingo", None) if st == RUNNING else None
        if ex is None or st == IDLE:
            return None
        ctx = {"target_name": target} if ex.id == catalog.APPROACH_ID else None
        rows = catalog.brief_lines(ex, ctx)
        if bingo is not None:
            rows.insert(1, ("Bingo", f"{bingo:.0f} pounds; after bingo {self.runner.after:.0f} pounds to land back at the start."))
        return {"key": "instructor", "title": f"Instructor: {ex.name}, level {ex.level} ({st})",
                "entries": [{"id": f"instr-{i}", "title": t, "text": x} for i, (t, x) in enumerate(rows)]}

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.wall()))


def build(config, snapshot: Callable[[], dict], speak: Callable[[str], None],
          mission_name: Callable[[], str] = lambda: "", targets=None, fuel=None, reserve=None,
          flight=None, fast=None, enabled=None) -> Instructor:
    """The instructor from config.py. Every key is optional (defaults in brackets):
        INSTRUCTOR_DATA_DIR    [<project>/data/instructor]   where results are kept; None = memory only (tests)
        INSTRUCTOR_PILOT       ["default"]                   one history per pilot name
        INSTRUCTOR_HOVER_LEVEL [1]                           the first level of every exercise (then adaptive)
        INSTRUCTOR_KEEP_RAW    [True]                        raw samples per attempt ...
        INSTRUCTOR_RAW_DAYS    [30]                          ... kept this many days (decision 8)
        INSTRUCTOR_FUEL_ALLOWANCE_LBS [80]                   fuel exercise: bingo = fuel at begin - this
    """
    import os
    base = getattr(config, "INSTRUCTOR_DATA_DIR", "default")
    if base == "default":
        base = os.path.join(getattr(config, "BASE_DIR", os.getcwd()), "data", "instructor")
    hist = History(base, getattr(config, "INSTRUCTOR_PILOT", "default"),
                   keep_raw=getattr(config, "INSTRUCTOR_KEEP_RAW", True),
                   raw_days=getattr(config, "INSTRUCTOR_RAW_DAYS", 30))
    return Instructor(snapshot, speak, hist, mission_name=mission_name,
                      level=getattr(config, "INSTRUCTOR_HOVER_LEVEL", 1), targets=targets, fuel=fuel, reserve=reserve,
                      flight=flight, fast=fast, enabled=enabled,
                      fuel_allowance_lbs=getattr(config, "INSTRUCTOR_FUEL_ALLOWANCE_LBS", catalog.FUEL_ALLOWANCE_LBS))
