"""
instructor/models.py - the instructor's data: exercises, samples, observations, evaluations.

Everything is a frozen dataclass with to_json(), so an evaluation saved today can be read back and
explained later. SCHEMA goes up whenever a saved field changes meaning.
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

SCHEMA = 3          # 2 (Phase 2): Evaluation.context; Sample.ll / fuel_lbs. 3 (Phase 3): pitch / roll / g / gmax


@dataclass(frozen=True)
class Criterion:
    """One scored line of an exercise. Linear deduction: nothing lost at or below `good`, all
    `points` lost at or above `limit`, a straight line between."""
    id: str                         # "hover.radius_p95"
    label: str                      # "Position drift (95% of the time within)"
    points: float
    observation: str                # Observation.id it is judged on
    good: float
    limit: float
    units: str
    signals: Tuple[str, ...]        # signals that must be good enough to judge it
    tag: str                        # weakness tag when it loses >= 40 % of its points
    core: bool = False              # the attempt can't be "scored" without the core criteria
    higher_is_better: bool = False  # e.g. time in the box: good 90 %, limit 60 %


@dataclass(frozen=True)
class Exercise:
    id: str                         # "hover.stationary"
    version: int                    # bump when criteria change; history keeps both
    area: str                       # hover | approach | sling | weapons | fuel
    name: str                       # spoken name: "hover"
    aircraft: Tuple[str, ...]       # DCS type-name prefixes: ("UH-1H", "AH-64")
    level: int                      # difficulty 1..5
    duration_s: float
    criteria: Tuple[Criterion, ...]
    box: Dict[str, float] = field(default_factory=dict)    # tolerance box for "time in the box"
    min_rate_hz: float = 3.5        # below this nothing is judged
    good_rate_hz: float = 5.0       # below this (but >= min) judged with confidence capped at medium

    def fits(self, aircraft: str) -> bool:
        return any((aircraft or "").upper().startswith(p.upper()) for p in self.aircraft)


@dataclass(frozen=True)
class Sample:
    """One telemetry packet as the observer saw it. None = not sent / unknown."""
    t: float                                    # DCS model time (Export "t"), seconds
    aircraft: str
    pos: Optional[Tuple[float, float, float]]   # north, east (m, map frame), alt (m ASL)
    vel: Optional[Tuple[float, float, float]]   # north, east, up (m/s over the ground)
    agl_ft: Optional[float]
    vs_fpm: Optional[float]
    hdg: Optional[float]                        # true heading, deg
    ias_kt: Optional[float]
    ll: Optional[Tuple[float, float]] = None    # lat, lon (deg): distances / bearings to waypoints, true north
    fuel_lbs: Optional[float] = None            # core.fuel.current_lbs (DCS fuel first)
    pitch: Optional[float] = None               # deg, nose up +
    roll: Optional[float] = None                # deg, right wing down +
    g: Optional[float] = None                   # normal acceleration now (LoGetAccelerationUnits().y)
    gmax: Optional[float] = None                # peak g since the previous 10 Hz packet (read every frame)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Sample":
        vec = lambda v, n: tuple(v) if isinstance(v, (list, tuple)) and len(v) == n else None
        return Sample(d["t"], d.get("aircraft", ""), vec(d.get("pos"), 3), vec(d.get("vel"), 3), d.get("agl_ft"),
                      d.get("vs_fpm"), d.get("hdg"), d.get("ias_kt"), vec(d.get("ll"), 2), d.get("fuel_lbs"),
                      d.get("pitch"), d.get("roll"), d.get("g"), d.get("gmax"))


@dataclass(frozen=True)
class SignalQuality:
    signal: str
    status: str                     # "good" | "degraded" | "insufficient"
    rate_hz: float
    gap_max_s: float
    coverage: float                 # share of the window with valid samples
    reason: str                     # "" when good

    def judgeable(self) -> bool:
        return self.status in ("good", "degraded")


@dataclass(frozen=True)
class Observation:
    id: str                         # "hover.radius_p95"
    value: Optional[float]
    units: str
    kind: str                       # "measured" | "derived" | "inferred"
    signals: Tuple[str, ...]
    evidence: Dict[str, Any]        # e.g. {"worst": 6.8, "t_worst": 412.3, "n": 180}


@dataclass(frozen=True)
class Deduction:
    criterion: str
    points: float                   # points lost
    observation: str
    evidence: str                   # "95 % of the time within 4.6 m (good 2.0, limit 5.0); worst 6.8 m at 06:52"


@dataclass(frozen=True)
class Evaluation:
    schema: int
    exercise_id: str
    exercise_version: int
    exercise_name: str
    level: int
    aircraft: str
    mission: str
    pilot: str
    start_model: float
    end_model: float
    start_wall: str                 # ISO local time
    end_wall: str
    outcome: str                    # "scored" | "partial" | "not_scored" | "aborted"
    score: Optional[float]          # 0..100; None unless scored / partial
    points_judged: float
    points_total: float
    criteria: Tuple[Dict[str, Any], ...]     # every criterion: judged or not, value, lost, reason
    deductions: Tuple[Deduction, ...]
    observations: Tuple[Observation, ...]
    quality: Tuple[SignalQuality, ...]
    confidence: str                 # "high" | "medium" | "low" | "none"
    weakness_tags: Tuple[str, ...]
    note: str = ""                  # why it was aborted / not scored, in words
    context: Dict[str, Any] = field(default_factory=dict)   # the attempt's setup: target, bingo, touchdown time...

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def valid_for_skills(self) -> bool:
        """Only these move levels or count towards weaknesses (Phase 2)."""
        return self.outcome == "scored" and self.confidence in ("high", "medium")
