"""
instructor/session.py - one exercise at a time: select -> armed -> running -> evaluated.

    Instructor(snapshot, speak, history, ...)
        .wanted(text) / .handle(text)   "Instructor, ..." (instructor/voice.py); returns what to say
        .start()                        the observer thread (reads state_store.snapshot() 10x a second,
                                        keeps each new telemetry packet once, by its tel_seq)
        .tick(sample)                   one new packet (the observer calls it; tests call it directly)
        .on_mission_loaded()            a mission load cancels a running attempt
        .link()                         the kneeboard Status row

Hover flow (decision 6: explicit start only):
    "Instructor, hover exercise"  -> brief ("... say begin when ready")
    "Instructor, begin"           -> armed; the clock starts after STABLE_S of steady hover
                                     (< 5 kt, |vs| < 300 fpm, at least 3 ft above the learned skid height,
                                     or 15 ft AGL before one is learned). The start point, height and
                                     heading are what you hold at that moment.
    after duration_s              -> evaluated on a worker thread, saved, the result spoken
    leaving the hover (> 50 m or 50 ft from the start), a mission load, an aircraft change or
    "Instructor, stop"            -> aborted: stored, never scored
Nothing here sends anything to DCS: the only outputs are speak(), the history files and link().
"""
import math
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from instructor import catalog, feedback, scoring, voice
from instructor.history import History
from instructor.models import Evaluation, Exercise, Sample

KT_PER_MS = 1.94384
STABLE_S = 3.0
HOVER_GS_KT, HOVER_VS_FPM = 5.0, 300.0      # huey/talkdown.py's hover rule
MIN_HOVER_FT = 3.0                          # above the learned skid height
NO_SKID_MIN_AGL_FT = 15.0                   # no skid height yet (air start): clearly off the ground
MAX_HOVER_FT = 100.0
LEAVE_M, LEAVE_FT = 50.0, 50.0              # this far from the start point: the hover was broken off
TELEMETRY_STALE_S = 3.0
POLL_S = 0.1
MAX_SAMPLES = 10000

IDLE, SELECTED, ARMED, RUNNING, EVALUATING = "idle", "selected", "armed", "running", "evaluating"


def sample_from_snapshot(snap: Dict[str, Any]) -> Optional[Sample]:
    """A Sample from state_store.snapshot(), or None without model time / fresh telemetry."""
    t = snap.get("t_model")
    if not isinstance(t, (int, float)) or (snap.get("telemetry_age") or 0) > TELEMETRY_STALE_S:
        return None

    def num(v, lo=None):
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and (lo is None or v >= lo) else None

    def vec(v):
        return tuple(float(x) for x in v) if isinstance(v, (list, tuple)) and len(v) == 3 else None

    return Sample(float(t), str(snap.get("aircraft") or ""), vec(snap.get("pos")), vec(snap.get("vel")),
                  num(snap.get("agl_ft"), 0.0), num(snap.get("vs_fpm")), num(snap.get("hdg"), 0.0),
                  num(snap.get("speed_kts")))


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
        gs = math.hypot(s.vel[0], s.vel[1]) * KT_PER_MS
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


def hovering(s: Sample, skid_ft: Optional[float]) -> bool:
    if s.vel is None or s.vs_fpm is None or s.agl_ft is None:
        return False
    gs = math.hypot(s.vel[0], s.vel[1]) * KT_PER_MS
    if gs >= HOVER_GS_KT or abs(s.vs_fpm) >= HOVER_VS_FPM:
        return False
    if skid_ft is None:
        return NO_SKID_MIN_AGL_FT <= s.agl_ft <= MAX_HOVER_FT
    return MIN_HOVER_FT <= s.agl_ft - skid_ft <= MAX_HOVER_FT


class Instructor:
    def __init__(self, snapshot: Callable[[], dict], speak: Callable[[str], None], history: History,
                 mission_name: Callable[[], str] = lambda: "", level: int = 1,
                 printer: Callable[[str], None] = print, run_async: bool = True,
                 wall: Callable[[], float] = time.time):
        self.snapshot, self.speak, self.history = snapshot, speak, history
        self.mission_name, self.level, self.printer = mission_name, int(level), printer
        self.run_async, self.wall = run_async, wall
        self._lock = threading.Lock()
        self.state = IDLE
        self.exercise: Optional[Exercise] = None
        self.skid = SkidLearner()
        self._samples: List[Sample] = []
        self._t0: Optional[float] = None
        self._t0_wall = ""
        self._stable_since: Optional[float] = None
        self._ref = None                     # (north, east, agl) at the start, for "left the hover"
        self._last_t: Optional[float] = None
        self._aircraft = ""
        self._recent_t: deque = deque(maxlen=41)   # model times of the last packets: live sample rate
        self.last: Optional[Evaluation] = None
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
        snap = self.snapshot()
        seq = snap.get("tel_seq")
        if seq is None or seq == self._seq:
            return
        self._seq = seq
        s = sample_from_snapshot(snap)
        if s is not None:
            self.tick(s)

    def tick(self, s: Sample) -> None:
        say: List[str] = []
        finish = None
        with self._lock:
            if self._last_t is not None and s.t < self._last_t:
                self._recent_t.clear()
            self._recent_t.append(s.t)
            self.skid.update(s)
            if self.state == ARMED:
                self._tick_armed(s, say)
            elif self.state == RUNNING:
                finish = self._tick_running(s, say)
            self._last_t = s.t
        for line in say:
            self.speak(line)
        if finish is not None:
            self._evaluate(*finish)

    def _tick_armed(self, s: Sample, say: List[str]) -> None:
        if not self.exercise.fits(s.aircraft):
            return
        if not hovering(s, self.skid.skid_ft):
            self._stable_since = None
            return
        if self._stable_since is None or s.t < self._stable_since:
            self._stable_since = s.t
        if s.t - self._stable_since < STABLE_S or s.pos is None or s.agl_ft is None:
            return
        self.state, self._t0, self._t0_wall = RUNNING, s.t, self._now_iso()
        self._samples, self._aircraft = [s], s.aircraft
        self._ref = (s.pos[0], s.pos[1], s.agl_ft)
        self.printer(f"[INSTRUCTOR] {self.exercise.id} level {self.exercise.level} started at "
                     f"{scoring.mission_time(s.t)}, {s.agl_ft:.0f} ft AGL, heading {s.hdg if s.hdg is not None else -1:.0f}.")
        say.append(f"Instructor, clock running. Hold it for {self.exercise.duration_s:.0f} seconds.")

    def _tick_running(self, s: Sample, say: List[str]):
        why = None
        if s.t < self._last_t:
            why = "the mission restarted"
        elif s.aircraft != self._aircraft:
            why = "the aircraft changed"
        elif s.pos is not None and math.hypot(s.pos[0] - self._ref[0], s.pos[1] - self._ref[1]) > LEAVE_M:
            why = f"you moved more than {LEAVE_M:.0f} meters from the start point"
        elif s.agl_ft is not None and abs(s.agl_ft - self._ref[2]) > LEAVE_FT:
            why = f"you moved more than {LEAVE_FT:.0f} feet from the start height"
        if why:
            return self._end_locked(why)
        if len(self._samples) < MAX_SAMPLES:
            self._samples.append(s)
        if s.t - self._t0 >= self.exercise.duration_s:
            return self._end_locked(None)
        return None

    def _end_locked(self, aborted: Optional[str]):
        """Lock held: hand the attempt to the evaluator. Returns the arguments for _evaluate."""
        ex, samples, t0 = self.exercise, list(self._samples), self._t0
        t1 = samples[-1].t if samples else t0
        if aborted is None:
            t1 = t0 + ex.duration_s if samples and samples[-1].t >= t0 + ex.duration_s else t1
        self.state = EVALUATING
        self._samples, self._stable_since = [], None
        return ex, samples, t0, t1, self._t0_wall, aborted

    def _evaluate(self, ex: Exercise, samples: List[Sample], t0: float, t1: float, t0_wall: str,
                  aborted: Optional[str]) -> None:
        def work():
            try:
                meta = {"aircraft": samples[0].aircraft if samples else "", "mission": self.mission_name() or "",
                        "pilot": self.history.pilot, "start_wall": t0_wall, "end_wall": self._now_iso()}
                ev = scoring.evaluate(ex, samples, t0, t1, meta, aborted=aborted)
                self.history.append(ev.to_json(), None if aborted else [x.to_json() for x in samples])
                with self._lock:
                    self.last = ev
                    if self.state == EVALUATING:
                        self.state = SELECTED
                self.printer(f"[INSTRUCTOR] {ev.exercise_id}: {ev.outcome}, score {ev.score}, {ev.confidence} "
                             f"confidence; " + "; ".join(d.evidence for d in ev.deductions[:3]))
                self.speak(feedback.result_line(ev))
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
        cmd, ex_id = voice.parse(rest)
        snap = self.snapshot()
        aircraft = str(snap.get("aircraft") or "")
        finish = None
        with self._lock:
            if cmd in ("select", "brief"):
                said = self._select_locked(ex_id, aircraft)
            elif cmd == "begin":
                said = self._begin_locked(ex_id, aircraft)
            elif cmd in ("stop", "off"):
                if self.state in (RUNNING,):
                    finish = self._end_locked("stopped by the pilot")
                    said = ""                                   # the result line says it
                elif self.state == ARMED:
                    self.state = SELECTED if cmd == "stop" else IDLE
                    said = "Instructor, cancelled."
                else:
                    said = "Instructor, nothing running." if cmd == "stop" else "Instructor, standing by."
                if cmd == "off" and self.state != EVALUATING:
                    self.state = IDLE
            elif cmd == "result":
                said = feedback.details(self.last) if self.last else "Instructor, no results yet this session."
            elif cmd == "status":
                said = self._status_locked()
            else:
                said = "Instructor, say again. I can run a hover exercise: say hover exercise, then begin."
        if finish is not None:
            self._evaluate(*finish)
        return said

    def _select_locked(self, ex_id: Optional[str], aircraft: str) -> str:
        if self.state in (RUNNING, EVALUATING):
            return "Instructor, an exercise is running. Say stop first."
        ex = catalog.get(ex_id or catalog.HOVER_ID, self.level)
        if aircraft and not ex.fits(aircraft):
            return "Instructor, the hover exercise is for the Huey and the Apache."
        self.exercise, self.state = ex, SELECTED
        return catalog.brief(ex)

    def _begin_locked(self, ex_id: Optional[str], aircraft: str) -> str:
        if self.state == RUNNING:
            left = self.exercise.duration_s - ((self._last_t or self._t0) - self._t0)
            return f"Instructor, already running, {max(0, left):.0f} seconds to go."
        if self.state == EVALUATING:
            return "Instructor, still scoring the last one."
        if self.exercise is None or (ex_id and ex_id != self.exercise.id):
            ex = catalog.get(ex_id or catalog.HOVER_ID, self.level)
            if aircraft and not ex.fits(aircraft):
                return "Instructor, the hover exercise is for the Huey and the Apache."
            self.exercise = ex
        elif aircraft and not self.exercise.fits(aircraft):
            return "Instructor, the hover exercise is for the Huey and the Apache."
        self.state, self._stable_since = ARMED, None
        return (f"Instructor, {self.exercise.name}, level {self.exercise.level}, {self.exercise.duration_s:.0f} seconds. "
                f"Clock starts when you're steady.")

    def _status_locked(self) -> str:
        if self.state == RUNNING:
            left = self.exercise.duration_s - ((self._last_t or self._t0) - self._t0)
            return f"Instructor, {self.exercise.name} running, {max(0, left):.0f} seconds to go."
        if self.state == ARMED:
            return f"Instructor, {self.exercise.name} armed, waiting for a steady hover."
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
            self._last_t = None
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
            left = (ex.duration_s - ((self._last_t or self._t0) - self._t0)) if st == RUNNING else None
        parts = [st if ex is None else f"{st} {ex.id} L{ex.level}"]
        if left is not None:
            parts.append(f"{max(0, left):.0f} s to go")
        if rate is not None:
            parts.append(f"{rate:.1f} samples/s" + (" (medium confidence below 5)" if rate < 5.0 else ""))
        parts.append(f"skid {skid:.0f} ft" if skid is not None else "skid height not learned")
        if last is not None:
            parts.append(f"last: {last.exercise_name} {last.outcome}"
                         + (f" {last.score:.0f} ({last.confidence})" if last.score is not None else ""))
        return {"name": "Instructor", "ok": None, "detail": "; ".join(parts)}

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.wall()))


def build(config, snapshot: Callable[[], dict], speak: Callable[[str], None],
          mission_name: Callable[[], str] = lambda: "") -> Instructor:
    """The instructor from config.py. Every key is optional (defaults in brackets):
        INSTRUCTOR_DATA_DIR    [<project>/data/instructor]   where results are kept; None = memory only (tests)
        INSTRUCTOR_PILOT       ["default"]                   one history per pilot name
        INSTRUCTOR_HOVER_LEVEL [1]                           until Phase 2 picks levels itself
        INSTRUCTOR_KEEP_RAW    [True]                        raw samples per attempt ...
        INSTRUCTOR_RAW_DAYS    [30]                          ... kept this many days (decision 8)
    """
    import os
    base = getattr(config, "INSTRUCTOR_DATA_DIR", "default")
    if base == "default":
        base = os.path.join(getattr(config, "BASE_DIR", os.getcwd()), "data", "instructor")
    hist = History(base, getattr(config, "INSTRUCTOR_PILOT", "default"),
                   keep_raw=getattr(config, "INSTRUCTOR_KEEP_RAW", True),
                   raw_days=getattr(config, "INSTRUCTOR_RAW_DAYS", 30))
    return Instructor(snapshot, speak, hist, mission_name=mission_name,
                      level=getattr(config, "INSTRUCTOR_HOVER_LEVEL", 1))
