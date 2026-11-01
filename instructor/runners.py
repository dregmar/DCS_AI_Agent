"""
instructor/runners.py - when each exercise's clock starts and stops, and what goes in its context.

A runner sees every new sample while its exercise is armed or running (instructor/session.py holds
the lock and does the common checks: model time going back, another aircraft). It never speaks or
sends; it returns text for the session to say.

    begin(s, skid, fuel)   -> (refusal or None, line to say or None, start now?)   on "Instructor, begin"
    armed(s, skid)         -> line to say when the clock starts, else None
    running(s, skid)       -> None (carry on) | ("done", None) | ("abort", why)
    window()               -> (t0, t1)
    context()              -> the attempt's setup, saved in the evaluation
    live(s)                -> rounded numbers for the kneeboard (sent only when they change)
"""
import math
from typing import Optional, Tuple

from instructor import catalog, geometry
from instructor.models import Exercise, Sample

KT_PER_MS = 1.94384
STABLE_S = 3.0
HOVER_GS_KT, HOVER_VS_FPM = 5.0, 300.0      # huey/talkdown.py's hover rule
MIN_HOVER_FT = 3.0
NO_SKID_MIN_AGL_FT = 15.0
MAX_HOVER_FT = 100.0
LEAVE_M, LEAVE_FT = 50.0, 50.0
TD_SETTLE_S = 2.0


def gs_kt(s: Sample) -> Optional[float]:
    return math.hypot(s.vel[0], s.vel[1]) * KT_PER_MS if s.vel else None


def height(s: Sample, skid: Optional[float]) -> Optional[float]:
    if s.agl_ft is None:
        return None
    return s.agl_ft - (skid or 0.0)


def hovering(s: Sample, skid_ft: Optional[float]) -> bool:
    g = gs_kt(s)
    if g is None or s.vs_fpm is None or s.agl_ft is None or g >= HOVER_GS_KT or abs(s.vs_fpm) >= HOVER_VS_FPM:
        return False
    if skid_ft is None:
        return NO_SKID_MIN_AGL_FT <= s.agl_ft <= MAX_HOVER_FT
    return MIN_HOVER_FT <= s.agl_ft - skid_ft <= MAX_HOVER_FT


def on_ground(s: Sample, skid: Optional[float]) -> bool:
    g, h = gs_kt(s), height(s, skid)
    if g is None or h is None or s.vs_fpm is None:
        return False
    low = h <= 1.5 if skid is not None else s.agl_ft <= 6.0
    return low and abs(s.vs_fpm) < 100 and g < 2.0


class Touchdown:
    """Inferred touchdown: on_ground() for TD_SETTLE_S; the time is the first settled sample."""

    def __init__(self):
        self.since: Optional[float] = None
        self.t: Optional[float] = None

    def update(self, s: Sample, skid: Optional[float]) -> Optional[float]:
        if self.t is not None:
            return self.t
        if on_ground(s, skid):
            self.since = s.t if self.since is None else self.since
            if s.t - self.since >= TD_SETTLE_S:
                self.t = self.since
        else:
            self.since = None
        return self.t


class HoverRunner:
    def __init__(self, ex: Exercise):
        self.ex = ex
        self.t0: Optional[float] = None
        self.t_last: Optional[float] = None
        self._stable: Optional[float] = None
        self._ref = None

    def begin(self, s, skid, fuel):
        return None, None, False

    def armed(self, s: Sample, skid) -> Optional[str]:
        if not hovering(s, skid):
            self._stable = None
            return None
        self._stable = s.t if self._stable is None or s.t < self._stable else self._stable
        if s.t - self._stable < STABLE_S or s.pos is None or s.agl_ft is None:
            return None
        self.t0 = self.t_last = s.t
        self._ref = (s.pos[0], s.pos[1], s.agl_ft)
        return f"Instructor, clock running. Hold it for {self.ex.duration_s:.0f} seconds."

    def running(self, s: Sample, skid):
        self.t_last = s.t
        if s.pos is not None and math.hypot(s.pos[0] - self._ref[0], s.pos[1] - self._ref[1]) > LEAVE_M:
            return "abort", f"you moved more than {LEAVE_M:.0f} meters from the start point"
        if s.agl_ft is not None and abs(s.agl_ft - self._ref[2]) > LEAVE_FT:
            return "abort", f"you moved more than {LEAVE_FT:.0f} feet from the start height"
        if s.t - self.t0 >= self.ex.duration_s:
            return "done", None
        return None

    def window(self) -> Tuple[float, float]:
        end = self.t0 + self.ex.duration_s
        return self.t0, (end if (self.t_last or self.t0) >= end else (self.t_last or self.t0))

    def context(self) -> dict:
        return {}

    def left_s(self) -> Optional[float]:
        return None if self.t0 is None else max(0.0, self.ex.duration_s - ((self.t_last or self.t0) - self.t0))

    def live(self, s: Optional[Sample]) -> dict:
        if s is None or self._ref is None or s.pos is None:
            return {}
        b = self.ex.box
        r = math.hypot(s.pos[0] - self._ref[0], s.pos[1] - self._ref[1])
        out = {"Drift": [f"{round(r * 2) / 2:.1f} m", r <= b["radius_m"]]}
        if s.agl_ft is not None:
            dh = s.agl_ft - self._ref[2]
            out["Height"] = [f"{dh:+.0f} ft", abs(dh) <= b["height_ft"]]
        return out


class TurnRunner(HoverRunner):
    """hover.turn: the clock starts like the hover's (3 s steady); done STOP_S after the 360."""

    def __init__(self, ex: Exercise):
        super().__init__(ex)
        self._prev_hdg: Optional[float] = None
        self.turned = 0.0                    # signed degrees so far (+ right)
        self.t_360: Optional[float] = None

    def begin(self, s, skid, fuel):
        return None, f"Instructor, hover turn, level {self.ex.level}. Clock starts when you're steady.", False

    def armed(self, s: Sample, skid) -> Optional[str]:
        line = super().armed(s, skid)
        if line is None:
            return None
        self._prev_hdg = s.hdg
        return "Instructor, clock running. Turn 360, left or right, and stop on this heading."

    def running(self, s: Sample, skid):
        self.t_last = s.t
        if s.pos is not None and math.hypot(s.pos[0] - self._ref[0], s.pos[1] - self._ref[1]) > LEAVE_M:
            return "abort", f"you moved more than {LEAVE_M:.0f} meters from the start point"
        if s.agl_ft is not None and abs(s.agl_ft - self._ref[2]) > LEAVE_FT:
            return "abort", f"you moved more than {LEAVE_FT:.0f} feet from the start height"
        if s.hdg is not None:
            if self._prev_hdg is not None:
                self.turned += (s.hdg - self._prev_hdg + 180.0) % 360.0 - 180.0
            self._prev_hdg = s.hdg
        if self.t_360 is None and abs(self.turned) >= 360.0:
            self.t_360 = s.t
        if self.t_360 is not None and s.t - self.t_360 >= catalog.TURN_STOP_S:
            return "done", None
        if self.t_360 is None and s.t - self.t0 > self.ex.duration_s:
            return "abort", f"no 360 within {self.ex.duration_s:.0f} seconds"
        return None

    def window(self) -> Tuple[float, float]:
        if self.t_360 is not None and (self.t_last or 0) >= self.t_360 + catalog.TURN_STOP_S:
            return self.t0, self.t_360 + catalog.TURN_STOP_S
        return self.t0, (self.t_last or self.t0)

    def left_s(self) -> Optional[float]:
        return None

    def live(self, s: Optional[Sample]) -> dict:
        out = super().live(s)
        if self._ref is not None:
            out["Turned"] = [f"{abs(round(self.turned / 5) * 5):.0f} deg" + (" right" if self.turned > 0 else
                                                                                " left" if self.turned < 0 else ""),
                             None]
        return out


class ApproachRunner:
    def __init__(self, ex: Exercise, target: dict):
        self.ex, self.target = ex, dict(target)
        self.tgt = tuple(target["ll"])
        self.t0: Optional[float] = None
        self.t_last: Optional[float] = None
        self.out = False
        self.td = Touchdown()
        self.skid: Optional[float] = None

    def begin(self, s, skid, fuel):
        if s is None or s.ll is None:
            return "Instructor, unable, I don't have your position.", None, False
        d = geometry.dist_m(s.ll, self.tgt)
        self.out = d >= catalog.APPROACH_ARM_OUT_M
        if not self.out:
            return None, (f"Instructor, approach to {self.target['name']}, {d / 1000:.1f} kilometers. Fly out beyond "
                          f"{catalog.APPROACH_ARM_OUT_M:.0f} meters; the clock starts at {catalog.APPROACH_START_M:.0f}."), False
        return None, (f"Instructor, approach to {self.target['name']}, {d / 1000:.1f} kilometers, "
                      f"{geometry.bearing_deg(s.ll, self.tgt):03.0f}. Clock starts at {catalog.APPROACH_START_M:.0f} meters."), False

    def armed(self, s: Sample, skid) -> Optional[str]:
        if s.ll is None:
            return None
        d = geometry.dist_m(s.ll, self.tgt)
        if d >= catalog.APPROACH_ARM_OUT_M:
            self.out = True
        h = height(s, skid)
        if self.out and d <= catalog.APPROACH_START_M and h is not None and h > 30:
            self.t0 = self.t_last = s.t
            self.skid = skid
            return "Instructor, approach clock running."
        return None

    def running(self, s: Sample, skid):
        self.t_last = s.t
        if s.ll is not None and geometry.dist_m(s.ll, self.tgt) > 1500.0:
            return "abort", "you flew more than 1500 meters away from the point"
        if s.t - self.t0 > self.ex.duration_s:
            return "abort", f"no landing within {self.ex.duration_s / 60:.0f} minutes"
        if self.td.update(s, self.skid if self.skid is not None else skid) is not None and s.t >= self.td.t + TD_SETTLE_S:
            return "done", None
        return None

    def window(self) -> Tuple[float, float]:
        return self.t0, (self.td.t + TD_SETTLE_S) if self.td.t is not None else (self.t_last or self.t0)

    def context(self) -> dict:
        return {"target_name": self.target["name"], "target_ll": list(self.tgt), "target_kind": self.target.get("kind", ""),
                "t_touchdown": self.td.t, "skid_ft": self.skid, "inferred_window": True}

    def left_s(self) -> Optional[float]:
        return None

    def live(self, s: Optional[Sample]) -> dict:
        if s is None or s.ll is None:
            return {}
        d = geometry.dist_m(s.ll, self.tgt)
        out = {"To the point": [f"{round(d / 10) * 10:.0f} m", None]}
        g = gs_kt(s)
        if g is not None:
            out["Ground speed"] = [f"{round(g):.0f} kt", None]
        if s.vs_fpm is not None:
            out["Sink"] = [f"{round(-s.vs_fpm / 50) * 50:.0f} fpm", -s.vs_fpm < 800]
        return out


class FuelRunner:
    def __init__(self, ex: Exercise, allowance_lbs: float, after_share: float, reserve_lbs: float = 0.0):
        """reserve_lbs: what you must still have on landing (Huey: config.HUEY_FUEL_RESERVE_LBS); the
        exercise won't start if its bingo would take you below it (31 Oct: begun with 210 lb, bingo 130)."""
        self.ex = ex
        self.allowance, self.share = float(allowance_lbs), float(after_share)
        self.reserve = max(0.0, float(reserve_lbs or 0.0))
        self.t0: Optional[float] = None
        self.t_last: Optional[float] = None
        self.home = None
        self.bingo: Optional[float] = None
        self.after: Optional[float] = None
        self.t_bingo: Optional[float] = None
        self.out_max = 0.0
        self.airborne = False
        self.td = Touchdown()
        self.skid: Optional[float] = None

    def begin(self, s, skid, fuel):
        if s is None or s.ll is None:
            return "Instructor, unable, I don't have your position.", None, False
        if fuel is None:
            return "Instructor, unable, I can't read your fuel.", None, False
        if not on_ground(s, skid):
            return "Instructor, say begin on the ground, where you'll land at the end.", None, False
        need = self.allowance + round(self.allowance * self.share, -1) + self.reserve
        if fuel < need:
            return (f"Instructor, not enough fuel for the exercise: {fuel:.0f} pounds on board, it needs {need:.0f}"
                    + (f", with your {self.reserve:.0f} pound reserve" if self.reserve else "") + ". Refuel first."), None, False
        self.home, self.skid = s.ll, skid
        self.bingo = round(fuel - self.allowance, -1)
        self.after = round(self.allowance * self.share, -1)
        self.t0 = self.t_last = s.t
        return None, (f"Instructor, fuel exercise running. Bingo {self.bingo:.0f} pounds. After bingo you have "
                      f"{self.after:.0f} pounds to land back here."), True

    def armed(self, s, skid):
        return None

    def running(self, s: Sample, skid):
        self.t_last = s.t
        if s.ll is not None:
            self.out_max = max(self.out_max, geometry.dist_m(self.home, s.ll))
        if s.fuel_lbs is not None and self.t_bingo is None and s.fuel_lbs <= self.bingo:
            self.t_bingo = s.t
        if s.t - self.t0 > self.ex.duration_s:
            return "abort", f"no landing within {self.ex.duration_s / 60:.0f} minutes"
        if not self.airborne:
            self.airborne = self.out_max > 500.0
            return None
        if self.td.update(s, self.skid) is not None and s.t >= self.td.t + TD_SETTLE_S:
            if self.out_max < catalog.FUEL_MIN_OUT_M:
                return "abort", f"you only flew {self.out_max / 1000:.1f} kilometers out"
            return "done", None
        return None

    def window(self) -> Tuple[float, float]:
        return self.t0, (self.td.t + TD_SETTLE_S) if self.td.t is not None else (self.t_last or self.t0)

    def context(self) -> dict:
        return {"home_ll": list(self.home) if self.home else None, "bingo_lbs": self.bingo, "after_lbs": self.after,
                "allowance_lbs": self.allowance, "reserve_lbs": self.reserve, "t_touchdown": self.td.t, "inferred_window": True}

    def left_s(self) -> Optional[float]:
        return None

    def live(self, s: Optional[Sample]) -> dict:
        if s is None:
            return {}
        out = {}
        if s.fuel_lbs is not None:
            out["Fuel"] = [f"{round(s.fuel_lbs / 5) * 5:.0f} lb", s.fuel_lbs > (self.bingo or 0)]
        out["Bingo"] = [f"{self.bingo:.0f} lb" + (" (passed)" if self.t_bingo is not None else ""), None]
        if s.ll is not None and self.home:
            out["Home"] = [f"{geometry.dist_m(self.home, s.ll) / 1000:.1f} km", None]
        return out
