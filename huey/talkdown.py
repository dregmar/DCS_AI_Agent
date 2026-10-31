"""
huey/talkdown.py
The crew chief talking you down and onto a sling load, from Export.lua's height above ground,
vertical speed, ground velocity and the nearest cargo object.

Landing / hover:
  * height on the way down: "Twenty feet." "Ten." "Five." "Three." then "Skids down."
  * drift while slow and low: "Drifting left." "Drifting back and right." ... "Steady."
  * "Easy, slow your descent!" when sinking fast close to the ground
  * "Skids up." on lift-off
Sling load (the cargo hook hangs about under the helicopter's centre):
  * far: "Load, 12 o'clock, 40 meters."
  * close: "Forward ten, left five." / "Down five." / "Steady, hold." (feet, where to move)
  * "Load's off the ground." once the cargo lifts with you
Heights are skid heights: the program learns what the height reads while you sit on the ground
(different per aircraft and terrain) and subtracts it.
"""
import json
import math
import os
import time
from typing import Callable, Optional

from common.nav import body_velocity, ground_speed_kt, relative

FT_PER_M = 3.28084
HEIGHT_CALLS = (20, 10, 5, 3)
DRIFT_MS = 0.6              # ~1.2 kt in any direction counts as drifting
STEADY_MS = 0.3
DRIFT_REPEAT_SEC = 3.0
TALKON_REPEAT_SEC = 3.0
SAME_CALL_SEC = 6.0         # don't repeat an identical talk-on call sooner than this
MANUAL_HOOK_M = 5.0         # manual 11.1.2: landed within 5 m of the load, hook it by command
SKIDS_UP_QUIET_SEC = 5.0    # no drift calls right after lift-off
CARGO_TALK_M = 60.0         # start talking you onto a load closer than this
HOOK_WINDOW_M = (2.5, 7.0)  # manual 11.1.2: hover 2.5 .. 7 m above the cargo, within 2 m
HOOK_BOX_M = 2.0
CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "huey_calibration.json")


class TalkDown:
    def __init__(self, speak: Callable[[str], None], calibration_file: Optional[str] = CALIBRATION_FILE):
        self.speak = speak
        # Where the cargo hook sits relative to the position DCS reports for the helicopter
        # (forward, right metres), learned with "load is under me". Saved between sessions.
        self.calibration_file = calibration_file
        self.hook_offset = (0.0, 0.0)
        self._load_calibration()
        self._debug_t = -1e9
        self.ground_offset_ft: Optional[float] = None
        self._ground_since: Optional[float] = None
        self._on_ground: Optional[bool] = None     # unknown until the first good reading
        self._called = set()             # heights already called this approach
        self._drift_said = ""
        self._drift_t = -1e9
        self._descent_warned = False
        self._talkon_t = -1e9
        self._cargo_rest_alt: Optional[float] = None
        self._cargo_lifted = False
        self._cargo_last = None
        self.talkon = False              # talk-on runs only when asked ("talk me onto the load")
        self._talkon_said = ""
        self._talkon_said_t = -1e9
        self._skids_up_t = -1e9
        self._in_range_said = False
        # True while an instructor exercise is armed or running (main.py sets it): the height countdown
        # and drift / "Steady" calls are held so the graded hover isn't coached and the instructor
        # isn't queued behind them (30 Oct flight). "Easy, slow your descent!", skids and talk-on still play.
        self.quiet: Callable[[], bool] = lambda: False

    # ------------------------------------------------------------ geometry
    def _load_calibration(self) -> None:
        if not self.calibration_file:
            return
        try:
            with open(self.calibration_file, encoding="utf-8") as f:
                d = json.load(f)
            self.hook_offset = (float(d["hook_fwd_m"]), float(d["hook_right_m"]))
            print(f"[TALKDOWN] Cargo hook offset loaded: {self.hook_offset[0]:+.1f} m forward, "
                  f"{self.hook_offset[1]:+.1f} m right.")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def cargo_rel(self, snap: dict) -> Optional[dict]:
        """Load position from the cargo hook: distance, clock, fwd / right metres (calibrated)."""
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not cargo or not pos:
            return None
        r = relative(pos, cargo[0], cargo[1], snap.get("hdg"))
        if r is None or r["fwd"] is None:
            return r
        fwd, right = r["fwd"] - self.hook_offset[0], r["right"] - self.hook_offset[1]
        rel = math.degrees(math.atan2(right, fwd)) % 360
        h = int(round(rel / 30.0)) % 12
        out = dict(r, fwd=fwd, right=right, dist_m=math.hypot(fwd, right), clock=12 if h == 0 else h)
        now = time.monotonic()
        if now - self._debug_t > 2.0:
            self._debug_t = now
            print(f"[CARGO] heli N{pos[0]:.1f} E{pos[1]:.1f} alt {pos[2]:.1f} | load N{cargo[0]:.1f} "
                  f"E{cargo[1]:.1f} alt {cargo[2]:.1f} | hdg {snap.get('hdg', -1):.0f} | from hook: "
                  f"fwd {fwd:+.1f} m, right {right:+.1f} m, {out['dist_m']:.1f} m")
        return out

    def calibrate(self, snap: dict) -> str:
        """Pilot says the load is right under the hook: whatever offset we read is the hook's."""
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not cargo or not pos:
            return "I don't see a load nearby."
        r = relative(pos, cargo[0], cargo[1], snap.get("hdg"))
        if r is None or r["fwd"] is None:
            return "No heading, can't calibrate."
        if r["dist_m"] > 15:
            return f"That load reads {r['dist_m']:.0f} meters away. Calibrate right over it."
        self.hook_offset = (r["fwd"], r["right"])
        if self.calibration_file:
            try:
                with open(self.calibration_file, "w", encoding="utf-8") as f:
                    json.dump({"hook_fwd_m": r["fwd"], "hook_right_m": r["right"]}, f)
            except OSError as e:
                print(f"[TALKDOWN] Could not save the calibration: {e}")
        print(f"[TALKDOWN] Cargo hook offset set: {r['fwd']:+.1f} m forward, {r['right']:+.1f} m right.")
        return (f"Calibrated. It read {_feet(r['fwd'])} feet {'ahead' if r['fwd'] >= 0 else 'behind'}, "
                f"{_feet(r['right'])} {'right' if r['right'] >= 0 else 'left'}. I'll correct for that.")

    def reset_calibration(self) -> str:
        self.hook_offset = (0.0, 0.0)
        if self.calibration_file and os.path.exists(self.calibration_file):
            try:
                os.remove(self.calibration_file)
            except OSError:
                pass
        return "Load calibration cleared."

    # ------------------------------------------------------------ voice
    def start_talkon(self, snap: dict) -> str:
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not cargo or not pos:
            return "I don't see a load nearby."
        self.talkon, self._talkon_said, self._in_range_said = True, "", False
        self._cargo_lifted = False
        return "Talking you on. " + self.load_report(snap)

    def stop_talkon(self) -> str:
        self.talkon = False
        return "Talk-on stopped."

    def load_report(self, snap: dict) -> str:
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not cargo or not pos:
            return "No load within 500 meters."
        if self._cargo_lifted:
            return "Load's on the hook."
        r = self.cargo_rel(snap)
        if r is None or r["clock"] is None:
            return "Load's nearby, no heading to give you a clock position."
        if r["dist_m"] < 5:
            return "Load's right under you."
        dist = f"{round(r['dist_m'], -1):.0f} meters" if r["dist_m"] >= 15 else f"{_feet(r['dist_m'])} feet"
        return f"Load, {r['clock']} o'clock, {dist}."

    # ------------------------------------------------------------ helpers
    def skid_height(self, agl_ft: float) -> float:
        return agl_ft - (self.ground_offset_ft or 0.0)

    def _learn_ground(self, agl_ft, vs, gs, now) -> None:
        """Sitting still and level near the ground for 3 s: that height reading is 'on the ground'."""
        still = agl_ft < 12 and abs(vs) < 30 and (gs is not None and gs < 1.0)
        if not still:
            self._ground_since = None
            return
        if self._ground_since is None:
            self._ground_since = now
        elif now - self._ground_since >= 3.0:
            if self.ground_offset_ft is None or agl_ft < self.ground_offset_ft:
                self.ground_offset_ft = agl_ft
                print(f"[TALKDOWN] Ground height learned: {agl_ft:.1f} ft reads as skids down.")

    # ------------------------------------------------------------ main
    def check(self, snap: dict, now: float) -> None:
        agl = snap.get("agl_ft", -1)
        if agl is None or agl < 0:
            return
        vs = snap.get("vs_fpm", 0) or 0
        vel, hdg = snap.get("vel"), snap.get("hdg")
        gs = ground_speed_kt(vel)
        self._learn_ground(agl, vs, gs, now)
        h = self.skid_height(agl)

        # --- where are we to start with (no "skids up" just because the program started)
        if self._on_ground is None:
            if self.ground_offset_ft is not None:
                self._on_ground = h < 2.0
            elif agl > 20:
                self._on_ground = False
            return
        # --- lift-off / touchdown
        if self._on_ground and h > 2.0:
            self._on_ground = False
            self._skids_up_t = now
            self.speak("Skids up.")
        elif not self._on_ground and h < 0.7 and abs(vs) < 60:
            self._on_ground = True
            self._called.clear()
            self._descent_warned = False
            self._drift_said = ""
            self.speak("Skids down.")
        if self._on_ground:
            self._landed_near_load(snap)
            return
        if h > 40:                                   # new approach next time we come down
            self._called.clear()
            self._descent_warned = False

        self._watch_lift(snap)
        talking = self._talk_on(snap, h, gs, now)

        slow = gs is not None and gs < 30
        quiet = self._quiet()
        if quiet:
            self._drift_said = ""                   # no stale "Steady." when the exercise ends
        # --- heights on the way down (the talk-on calls take over over a load)
        if slow and vs < -50 and not talking and not quiet:
            for mark in HEIGHT_CALLS:
                if h <= mark and mark not in self._called:
                    self._called.update(m for m in HEIGHT_CALLS if m >= mark)
                    self.speak("Twenty feet." if mark == 20 else f"{_word(mark).capitalize()}.")
                    break
        # --- too fast a descent near the ground
        if h < 15 and vs < -400 and not self._descent_warned:
            self._descent_warned = True
            self.speak("Easy, slow your descent!")

        # --- drift in the hover: not while talking you onto a load, not just after lift-off,
        # not while climbing or descending briskly
        if (not talking and not quiet and h < 30 and gs is not None and gs < 5 and abs(vs) < 300
                and now - self._skids_up_t > SKIDS_UP_QUIET_SEC):
            self._drift(vel, hdg, now)

    def _quiet(self) -> bool:
        try:
            return bool(self.quiet())
        except Exception:                           # never let the hook stop the crew chief
            return False

    def _drift(self, vel, hdg, now) -> None:
        bv = body_velocity(vel, hdg)
        if bv is None:
            return
        fwd, right = bv
        words = []
        if abs(fwd) > DRIFT_MS:
            words.append("forward" if fwd > 0 else "back")
        if abs(right) > DRIFT_MS:
            words.append("right" if right > 0 else "left")
        said = "Drifting " + " and ".join(words) + "." if words else ""
        if said:
            if said != self._drift_said or now - self._drift_t >= DRIFT_REPEAT_SEC:
                self._drift_said, self._drift_t = said, now
                self.speak(said)
        elif self._drift_said and math.hypot(fwd, right) < STEADY_MS:
            self._drift_said = ""
            self.speak("Steady.")

    # ------------------------------------------------------------ sling load
    def _watch_lift(self, snap: dict) -> None:
        """Notices the load coming up with you (said only if it's yours: close or talked on)."""
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not cargo or not pos:
            self._cargo_rest_alt, self._cargo_lifted = None, False
            return
        if self._cargo_last is None or math.hypot(cargo[0] - self._cargo_last[0],
                                                  cargo[1] - self._cargo_last[1]) > 50:
            self._cargo_rest_alt, self._cargo_lifted = cargo[2], False     # a different load
        self._cargo_last = cargo
        if self._cargo_rest_alt is None or cargo[2] < self._cargo_rest_alt:
            self._cargo_rest_alt = cargo[2]
        if not self._cargo_lifted and cargo[2] - self._cargo_rest_alt > 1.5:
            self._cargo_lifted = True
            mine = self.talkon or math.hypot(cargo[0] - pos[0], cargo[1] - pos[1]) < 15
            self.talkon = False
            if mine:
                self.speak("Load's off the ground.")

    def _landed_near_load(self, snap: dict) -> None:
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not self.talkon or not cargo or not pos or self._in_range_said:
            return
        r = self.cargo_rel(snap)
        if r is not None and r["dist_m"] <= MANUAL_HOOK_M:
            self._in_range_said = True
            self.speak("You're in range. Say hook the load.")

    def _say_talkon(self, text: str, now: float) -> None:
        if text == self._talkon_said and now - self._talkon_said_t < SAME_CALL_SEC:
            return
        self._talkon_said, self._talkon_said_t = text, now
        self.speak(text)

    def _talk_on(self, snap: dict, h: float, gs, now) -> bool:
        """Corrections onto the load, only after "talk me onto the load". True while talking."""
        cargo, pos = snap.get("cargo"), snap.get("pos")
        if not self.talkon or not cargo or not pos or self._cargo_lifted:
            return False
        r = self.cargo_rel(snap)
        if r is None or r["fwd"] is None or r["dist_m"] > CARGO_TALK_M or h > 100 or (gs or 0) > 20:
            return False
        if now - self._talkon_t < TALKON_REPEAT_SEC:
            return True
        self._talkon_t = now
        if r["dist_m"] > 15:
            self._say_talkon(f"Load, {r['clock']} o'clock, {round(r['dist_m'], -1):.0f} meters.", now)
            return True
        above = pos[2] - cargo[2]
        moves = []
        if abs(r["fwd"]) > HOOK_BOX_M:
            moves.append(f"{'forward' if r['fwd'] > 0 else 'back'} {_feet(r['fwd'])}")
        if abs(r["right"]) > HOOK_BOX_M:
            moves.append(f"{'right' if r['right'] > 0 else 'left'} {_feet(r['right'])}")
        if above > HOOK_WINDOW_M[1]:
            moves.append(f"down {_feet(above - HOOK_WINDOW_M[1] + 1)}")
        elif above < HOOK_WINDOW_M[0]:
            moves.append(f"up {_feet(HOOK_WINDOW_M[0] - above + 1)}")
        self._say_talkon((", ".join(moves).capitalize() + ".") if moves else "Steady, hold.", now)
        return True


def _feet(m: float) -> str:
    ft = abs(m) * FT_PER_M
    return _word(max(5, int(round(ft / 5.0)) * 5) if ft >= 7 else max(1, int(round(ft))))


_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 10: "ten", 15: "fifteen",
          20: "twenty", 25: "twenty-five", 30: "thirty"}


def _word(n: int) -> str:
    return _WORDS.get(n, str(n))
