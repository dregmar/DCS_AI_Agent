"""
instructor_probe.py - Phase 0 of Instructor Mode: what the instructor could measure today.
Run it INSTEAD of main.py (it listens on the same ports). It only listens: nothing is sent to DCS,
DCS-BIOS, Olympus or the comms hook.

    python instructor_probe.py                 Export.lua (UDP 5005) + comms hook (UDP 5011)
    python instructor_probe.py --axes          ... and your joystick axes (item I2, needs pygame)
    python instructor_probe.py --seconds 600   stop by itself (default: Ctrl+C)
    python instructor_probe.py --fast          ask Export.lua for the 10 Hz flight block (Phase 3, I5) the
                                               way an exercise does (the flag file), and check it and g (I1)

A useful session, in the UH-1H and again in the AH-64D:
  1. Sit on the ground, still, for 10 s           (learns the skid height)
  2. Lift into a hover and hold it for 60 s       (sample spacing while hovering)
  3. Fly a short circuit and land                 (IAS vs ground speed, heading vs track)
  4. Huey with a sling load: hover over the load, then measure the same distance with the F10
     map ruler and compare it with the "cargo" line (the 13 Oct 3x question)
  5. --axes: move the cyclic, collective and pedals end to end

Every 10 s it prints a short status; at the end it prints and saves the verdict for each
instrumentation item of the proposal (I1-I5) to logs/instructor_probe_<date>_<time>.txt.
"""
import argparse
import json
import math
import os
import socket
import statistics
import time
from typing import Any, Dict, List, Optional

KT_PER_MS = 1.94384
FT_PER_M = 3.28084
HOVER_RATE_HZ = 5.0          # the proposal's minimum for hover / sling scoring
GROUND_STILL_S = 3.0         # same rule as huey/talkdown.py: 3 s still on the ground
HOVER_GS_KT, HOVER_VS_FPM = 5.0, 300.0     # talkdown's hover rule
SEGMENT_GAP_S = 5.0          # model time jumping more than this (pause, reload) starts a new segment

# Export.lua fields the instructor would use, and how a "good" value looks
FIELDS = ("ac", "t", "pos", "vel", "agl_ft", "vs_fpm", "speed_kts", "alt_ft",
          "pitch", "roll", "yaw", "hdg", "cargo", "fuel", "ll", "scan_t", "g")
NOT_SENT = ("control positions", "touchdown / weight on wheels",
            "shot / hit / kill / land events", "runway geometry")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _vec(v, n=3) -> bool:
    return isinstance(v, list) and len(v) == n and all(_num(x) for x in v)


def valid(name: str, v: Any) -> bool:
    """Is this field present with a usable value? (-1 is Export.lua's 'unknown' for hdg / agl)."""
    if name in ("pos", "vel", "cargo"):
        return _vec(v)
    if name == "fuel":
        return _vec(v, 2)
    if name == "ll":
        return _vec(v, 2)
    if name == "ac":
        return isinstance(v, str) and v != ""
    if name == "hdg":
        return _num(v) and 0 <= v <= 360
    if name == "agl_ft":
        return _num(v) and v >= 0
    if name == "scan_t":
        return _num(v) and v >= 0
    return _num(v)


def _pct(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def _angle_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


class AircraftStats:
    """Everything measured for one aircraft (stats restart when the aircraft changes)."""

    def __init__(self, ac: str):
        self.ac = ac
        self.n = 0
        self.wall: List[float] = []
        self.model: List[float] = []
        self.dup_t = 0
        self.present = {f: 0 for f in FIELDS}
        self.values: Dict[str, List[float]] = {f: [] for f in ("pitch", "roll", "yaw", "speed_kts", "hdg", "g")}
        self.full = 0                                   # 4 Hz full packets (field shares are of these)
        self.fast = 0                                   # 10 Hz flight-block packets ("k": "f")
        self.gmax: List[float] = []
        self.ias_minus_gs: List[float] = []
        self.crab: List[float] = []
        self.ground_since: Optional[float] = None
        self.ground_agl: List[float] = []
        self.skid_ft: Optional[float] = None
        self.hover_s = 0.0
        self.hover_dt: List[float] = []
        self._last_t: Optional[float] = None
        self.segments: List[List[float]] = []          # [first t, last t, distinct packets] per run of model time
        self.fraction_seen: Dict[str, bool] = {}       # False = only whole numbers arrived
        self._last_hover_t: Optional[float] = None
        self.cargo_last: Optional[Dict[str, float]] = None
        self.cargo_seen = 0

    # ------------------------------------------------------------------ one packet
    def add(self, p: Dict[str, Any], wall: float) -> None:
        self.n += 1
        self.wall.append(wall)
        if p.get("k") == "f":
            self.fast += 1
            if _num(p.get("gmax")):
                self.gmax.append(float(p["gmax"]))
        else:
            self.full += 1
            for f in FIELDS:
                if valid(f, p.get(f)):
                    self.present[f] += 1
        t = p.get("t")
        if _num(t):
            t = float(t)
            if self._last_t is not None and t == self._last_t:
                self.dup_t += 1
            else:
                # model time restarts with each mission (and jumps after a pause): a new segment, so
                # the rate isn't measured across it (30 Oct: a mission reload gave "22.68 Hz")
                if self._last_t is None or t < self._last_t or t - self._last_t > SEGMENT_GAP_S:
                    self.segments.append([t, t, 1])
                else:
                    seg = self.segments[-1]
                    seg[1], seg[2] = t, seg[2] + 1
                self.model.append(t)
            self._last_t = t
        for f in ("agl_ft", "speed_kts", "vs_fpm"):
            if valid(f, p.get(f)):
                v = float(p[f])
                self.fraction_seen[f] = self.fraction_seen.get(f, False) or (v != int(v))
        for f in self.values:
            if valid(f, p.get(f)):
                self.values[f].append(float(p[f]))
        vel, vs, agl = p.get("vel"), p.get("vs_fpm"), p.get("agl_ft")
        gs = math.hypot(vel[0], vel[1]) * KT_PER_MS if _vec(vel) else None
        if gs is not None and valid("speed_kts", p.get("speed_kts")) and gs > 40:
            self.ias_minus_gs.append(float(p["speed_kts"]) - gs)
        # forward flight only (IAS > 40 kt): at 20 kt ground speed a sling carry or sideways / backward
        # hover-taxi gave a 166 deg 95th percentile (30 Oct)
        ias = p.get("speed_kts")
        if gs is not None and gs > 20 and valid("speed_kts", ias) and ias > 40 and valid("hdg", p.get("hdg")):
            track = math.degrees(math.atan2(vel[1], vel[0])) % 360     # vel = north, east, up
            self.crab.append(_angle_diff(float(p["hdg"]), track))
        if _num(t) and gs is not None and _num(vs) and valid("agl_ft", agl):
            self._ground(float(t), gs, float(vs), float(agl))
            self._hover(float(t), gs, float(vs), float(agl))
        if _vec(p.get("cargo")) and _vec(p.get("pos")):
            c, me = p["cargo"], p["pos"]                                  # both north, east, alt (m)
            self.cargo_seen += 1
            self.cargo_last = {"horiz_m": math.hypot(c[0] - me[0], c[1] - me[1]), "below_m": me[2] - c[2],
                               "n": c[0] - me[0], "e": c[1] - me[1]}

    def _ground(self, t: float, gs: float, vs: float, agl: float) -> None:
        """huey/talkdown.py's rule exactly: below 12 ft, |vs| < 30 fpm, under 1 kt for 3 s, and the
        learned value only ever goes down (a steady 10 ft hover must not become 'the ground')."""
        still = agl < 12 and abs(vs) < 30 and gs < 1.0
        if not still:
            self.ground_since, self.ground_agl = None, []
            return
        if self.ground_since is None:
            self.ground_since = t
        self.ground_agl.append(agl)
        if t - self.ground_since >= GROUND_STILL_S:
            m = statistics.median(self.ground_agl)
            if self.skid_ft is None or m < self.skid_ft:
                self.skid_ft = m

    def _hover(self, t: float, gs: float, vs: float, agl: float) -> None:
        if self.skid_ft is None:                 # sitting on 9 ft skids would look like a hover
            self._last_hover_t = None
            return
        h = agl - self.skid_ft
        hovering = gs < HOVER_GS_KT and abs(vs) < HOVER_VS_FPM and 2.0 < h < 100.0
        if hovering and self._last_hover_t is not None and t > self._last_hover_t:
            dt = t - self._last_hover_t
            if dt < 2.0:
                self.hover_s += dt
                self.hover_dt.append(dt)
        self._last_hover_t = t if hovering else None

    # ------------------------------------------------------------------ results
    def rate_hz(self) -> Optional[float]:
        """Distinct packets per model second (what a scorer would actually get), over the
        segments of continuous model time only."""
        dur = sum(s[1] - s[0] for s in self.segments)
        steps = sum(s[2] - 1 for s in self.segments)
        if dur <= 0 or steps <= 0:
            return None
        return steps / dur

    def wall_gaps(self) -> List[float]:
        return [b - a for a, b in zip(self.wall, self.wall[1:])]

    def share(self, f: str) -> float:
        return self.present[f] / self.full if self.full else 0.0

    def varies(self, f: str, spread: float = 0.5) -> bool:
        xs = self.values.get(f) or []
        return len(xs) > 1 and (max(xs) - min(xs)) > spread

    def verdicts(self) -> List[Dict[str, str]]:
        """One row per instrumentation item: (item, verdict, why)."""
        out = []
        rate = self.rate_hz()
        if rate is None:
            out.append({"item": "I5 rate", "verdict": "unknown", "why": "no model time 't' in the packets"})
        else:
            need = rate < HOVER_RATE_HZ
            if self.fast:
                out.append({"item": "I5 rate", "verdict": "10 Hz block working" if not need else "10 Hz block too slow",
                             "why": f"{rate:.2f} distinct packets per model second, {self.fast} of {self.n} from the "
                                    f"flight block (hover scoring wants >= {HOVER_RATE_HZ:.0f})"})
            else:
                out.append({"item": "I5 rate", "verdict": "needed" if need else "not needed",
                            "why": f"{rate:.2f} distinct packets per model second (hover scoring wants >= "
                                   f"{HOVER_RATE_HZ:.0f}); no flight-block packets (run with --fast)"})
        hdg = self.share("hdg")
        out.append({"item": "I1 heading", "verdict": "present" if hdg >= 0.9 else "missing",
                    "why": f"valid in {hdg:.0%} of packets"})
        if self.ias_minus_gs:
            d = statistics.median(self.ias_minus_gs)
            out.append({"item": "I1 airspeed", "verdict": "present (IAS)",
                        "why": f"speed_kts - ground speed: median {d:+.0f} kt over {len(self.ias_minus_gs)} samples above 40 kt"})
        else:
            v = "present, not checked" if self.share("speed_kts") >= 0.9 else "missing"
            out.append({"item": "I1 airspeed", "verdict": v, "why": "no samples above 40 kt ground speed to compare"})
        att = [f for f in ("pitch", "roll") if self.varies(f)]
        out.append({"item": "I1 attitude", "verdict": "present" if len(att) == 2 else "check",
                    "why": "pitch and roll change" if len(att) == 2 else f"changing: {att or 'none'} (fly a turn)"})
        gs = self.values.get("g") or []
        if self.share("g") >= 0.9 and gs:
            why = f"valid in {self.share('g'):.0%} of full packets, {min(gs):.2f} to {max(gs):.2f} g"
            if self.gmax:
                why += f"; peak between 10 Hz packets up to {max(self.gmax):.2f} g"
            out.append({"item": "I1 g", "verdict": "present", "why": why})
        else:
            out.append({"item": "I1 g", "verdict": "missing",
                        "why": f"valid in {self.share('g'):.0%} of full packets (old Export.lua, or the module gives none)"})
        whole = [f for f, frac in sorted(self.fraction_seen.items()) if not frac]
        if whole:
            out.append({"item": "resolution", "verdict": "whole numbers",
                        "why": f"{', '.join(whole)} only arrive rounded (1 ft / 1 kt / 1 fpm steps)"})
        if self.crab:
            out.append({"item": "heading vs track", "verdict": "info",
                        "why": f"median {statistics.median(self.crab):+.1f} deg, 95th pct "
                               f"{_pct([abs(c) for c in self.crab], 95):.1f} deg (expected: small; large = wrong frame)"})
        out.append({"item": "skid height", "verdict": "learned" if self.skid_ft is not None else "not learned",
                    "why": (f"{self.skid_ft:.1f} ft" if self.skid_ft is not None else "sit still on the ground for 3 s")})
        if self.hover_dt:
            out.append({"item": "hover samples", "verdict": "info",
                        "why": f"{self.hover_s:.0f} s hovering, spacing median {statistics.median(self.hover_dt):.2f} s, "
                               f"max {max(self.hover_dt):.2f} s"})
        if self.cargo_last:
            c = self.cargo_last
            out.append({"item": "cargo (F10 check)", "verdict": "info",
                        "why": f"last: {c['horiz_m']:.1f} m horizontal ({c['horiz_m'] * FT_PER_M:.0f} ft), "
                               f"{c['below_m']:.1f} m below; compare with the F10 ruler"})
        return out

    def summary_lines(self) -> List[str]:
        gaps = self.wall_gaps()
        rate = self.rate_hz()
        lines = [f"== {self.ac or '(no aircraft name)'}: {self.n} packets ({self.fast} from the 10 Hz block), "
                 f"{('%.2f Hz' % rate) if rate else 'rate unknown'} (model time), "
                 f"{self.dup_t} repeated 't', {len(self.segments)} model-time segment(s)"]
        if gaps:
            lines.append(f"   arrival spacing: median {statistics.median(gaps) * 1000:.0f} ms, "
                         f"95th pct {_pct(gaps, 95) * 1000:.0f} ms, max {max(gaps) * 1000:.0f} ms")
        lines.append("   fields: " + ", ".join(f"{f} {self.share(f):.0%}" for f in FIELDS))
        lines.append("   not sent by Export.lua: " + "; ".join(NOT_SENT))
        for v in self.verdicts():
            lines.append(f"   {v['item']:<18} {v['verdict']:<20} {v['why']}")
        return lines


class HookStats:
    """Messages from george_comms_hook.lua (UDP 5011), counted by kind."""

    def __init__(self):
        self.kinds: Dict[str, List[float]] = {}
        self.airbases = 0
        self.last_fuel = ""

    def add(self, text: str, wall: float) -> None:
        first = (text or "").split("\n", 1)[0].strip()
        kind = first.split(" ", 1)[0] if first else "(empty)"
        self.kinds.setdefault(kind, []).append(wall)
        if kind == "AIRBASES":
            self.airbases = max(0, len([ln for ln in text.split("\n")[1:] if ln.strip()]))
        elif kind == "FUEL":
            self.last_fuel = first

    def summary_lines(self) -> List[str]:
        if not self.kinds:
            return ["== comms hook: nothing received (is george_comms_hook.lua in Saved Games\\DCS\\Scripts\\Hooks?)"]
        lines = ["== comms hook messages:"]
        for k, ts in sorted(self.kinds.items()):
            every = f", every {statistics.median([b - a for a, b in zip(ts, ts[1:])]):.1f} s" if len(ts) > 2 else ""
            lines.append(f"   {k:<10} {len(ts)}{every}")
        lines.append(f"   airbases listed: {self.airbases} (runway headings / lengths: not sent, item I4)")
        lines.append("   event lines (EVT, item I3): " + ("present" if "EVT" in self.kinds else "not sent yet"))
        if self.last_fuel:
            lines.append(f"   last fuel: {self.last_fuel}")
        return lines


class AxisStats:
    """Joystick axes (item I2): range each axis travelled."""

    def __init__(self):
        self.ranges: Dict[str, List[float]] = {}

    def add(self, device: str, axis: int, value: float) -> None:
        key = f"{device} axis {axis}"
        r = self.ranges.setdefault(key, [value, value])
        r[0], r[1] = min(r[0], value), max(r[1], value)

    def summary_lines(self) -> List[str]:
        if not self.ranges:
            return ["== joystick axes: not read (run with --axes; needs pygame)"]
        lines = ["== joystick axes (I2: moved end to end = usable for control-activity scoring):"]
        for k, (lo, hi) in sorted(self.ranges.items()):
            lines.append(f"   {k:<44} {lo:+.2f} .. {hi:+.2f}{'   moved' if hi - lo > 0.5 else ''}")
        return lines


class Probe:
    def __init__(self):
        self.aircraft: Dict[str, AircraftStats] = {}
        self.current: Optional[AircraftStats] = None
        self.hook = HookStats()
        self.axes = AxisStats()
        self.bad_packets = 0

    def telemetry(self, data: bytes, wall: float) -> None:
        try:
            p = json.loads(data.decode("utf-8"))
        except ValueError:
            self.bad_packets += 1
            return
        if not isinstance(p, dict):
            self.bad_packets += 1
            return
        ac = p.get("ac") if isinstance(p.get("ac"), str) else ""
        if self.current is None or (ac and ac != self.current.ac):
            self.current = self.aircraft.setdefault(ac, AircraftStats(ac))
        self.current.add(p, wall)

    def report(self) -> List[str]:
        lines = [f"instructor_probe {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        if not self.aircraft:
            lines.append("== Export.lua: nothing received on UDP 5005 (is DCS in a mission? main.py closed?)")
        for st in self.aircraft.values():
            lines += st.summary_lines()
        if self.bad_packets:
            lines.append(f"   bad telemetry packets dropped: {self.bad_packets}")
        lines += self.hook.summary_lines()
        lines += self.axes.summary_lines()
        return lines

    def status(self) -> str:
        st = self.current
        if st is None:
            return "waiting for Export.lua on UDP 5005..."
        rate = st.rate_hz()
        return (f"{st.ac}: {st.n} packets, {('%.2f Hz' % rate) if rate else '?'}; skid "
                f"{('%.1f ft' % st.skid_ft) if st.skid_ft is not None else 'not learned'}; hover {st.hover_s:.0f} s"
                + (f"; cargo {st.cargo_last['horiz_m']:.1f} m" if st.cargo_last else ""))


# ====================================================================== live part (not unit-tested)
def _udp(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.bind(("127.0.0.1", port))
    s.setblocking(False)
    return s


def _open_axes():
    try:
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame
        pygame.display.init()
        pygame.joystick.init()
        sticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        for j in sticks:
            j.init()
            print(f"  axes: {j.get_name()} ({j.get_numaxes()} axes)")
        return pygame, sticks
    except Exception as e:
        print(f"Can't read joystick axes: {type(e).__name__}: {e} (pip install pygame-ce)")
        return None, []


def fast_flag_path(config) -> Optional[str]:
    """Same file as main._fast_flag_path (Export.lua sends the 10 Hz block while it exists)."""
    p = getattr(config, "INSTRUCTOR_FAST_FLAG", None)
    if p:
        return p
    scripts = os.path.join(os.path.expanduser("~"), "Saved Games", "DCS", "Scripts")
    return os.path.join(scripts, "george_fast_telemetry.flag") if os.path.isdir(scripts) else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Instructor Mode Phase 0 probe (listen only)")
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = Ctrl+C)")
    ap.add_argument("--axes", action="store_true", help="also read joystick axes (item I2)")
    ap.add_argument("--no-hook", action="store_true", help="don't listen for the comms hook")
    ap.add_argument("--fast", action="store_true", help="ask Export.lua for the 10 Hz flight block (I5)")
    a = ap.parse_args()
    import config
    flag = fast_flag_path(config) if a.fast else None
    if a.fast:
        if flag is None:
            print("No Saved Games\\DCS\\Scripts folder: set INSTRUCTOR_FAST_FLAG in config.py.")
        else:
            with open(flag, "w", encoding="utf-8") as f:
                f.write("instructor_probe.py --fast\n")
            print(f"10 Hz flight block requested ({flag}); Export.lua sees it within a second.")
    print(__doc__)
    probe = Probe()
    try:
        tel = _udp(getattr(config, "UDP_TELEMETRY_PORT", 5005))
    except OSError as e:
        print(f"Can't listen on the telemetry port ({e}). Close main.py first.")
        return
    hook = None
    if not a.no_hook:
        try:
            hook = _udp(getattr(config, "COMMS_MENU_PORT", 5011))
        except OSError as e:
            print(f"Not listening for the comms hook ({e}).")
    pg, sticks = _open_axes() if a.axes else (None, [])
    start = last_status = time.monotonic()
    try:
        while not a.seconds or time.monotonic() - start < a.seconds:
            got = False
            for sock, fn in ((tel, probe.telemetry), (hook, None)):
                if sock is None:
                    continue
                try:
                    data, _ = sock.recvfrom(65535)
                except (BlockingIOError, OSError):
                    continue
                got = True
                now = time.monotonic()
                if fn:
                    fn(data, now)
                else:
                    probe.hook.add(data.decode("utf-8", "replace"), now)
            if pg is not None:
                pg.event.pump()
                for j in sticks:
                    for i in range(j.get_numaxes()):
                        probe.axes.add(j.get_name(), i, j.get_axis(i))
            if time.monotonic() - last_status > 10.0:
                last_status = time.monotonic()
                print(time.strftime("%H:%M:%S"), probe.status())
            if not got:
                time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        if flag and os.path.exists(flag):
            os.remove(flag)
    lines = probe.report()
    print("\n" + "\n".join(lines))
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                        time.strftime("instructor_probe_%Y%m%d_%H%M.txt"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\nSaved {path}")
    except OSError as e:
        print(f"Couldn't save the report: {e}")


if __name__ == "__main__":
    main()
