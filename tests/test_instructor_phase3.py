"""Instructor Mode Phase 3: g (I1) and the 10 Hz flight block (I5) from Export.lua to the state store and
the observer; approach v3 (airspeed at 300 m, touchdown g); the hover turn exercise."""
import math
import os
import tempfile
import unittest
from unittest import mock

from instructor import catalog, feedback, scoring, voice
from instructor.history import History
from instructor.metrics import turn as turn_metrics
from instructor.models import Sample
from instructor.session import ARMED, IDLE, RUNNING, SELECTED, Instructor
from tests.instructor_sim import Sim, ll_of

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def full_packet(t, **kw):
    d = {"ac": "UH-1H", "t": t, "alt_ft": 400, "agl_ft": 15, "vs_fpm": 0, "speed_kts": 0, "pitch": 1.0, "roll": 0.5,
         "yaw": 0, "hdg": 90.0, "pos": [10.0, 20.0, 120.0], "vel": [0.0, 0.0, 0.0], "ll": [42.0, 41.0], "g": 1.01,
         "contacts": [{"id": 1, "type": "T-72B", "dist_m": 3000, "brg": 10, "lat": 42.0, "lon": 41.0}],
         "route": [[1, 0.0, 0.0]], "wp": 1, "cargo": None}
    d.update(kw)
    return d


def fast_packet(t, **kw):
    d = {"k": "f", "t": t, "pos": [10.5, 20.0, 120.0], "vel": [0.1, 0.0, 0.0], "agl_ft": 15.3, "vs_fpm": -12,
         "speed_kts": 3.2, "hdg": 91.5, "pitch": 1.2, "roll": 0.4, "g": 1.02, "gmax": 1.07, "ll": [42.00001, 41.0],
         "cargo": None}
    d.update(kw)
    return d


class TestStateStore(unittest.TestCase):
    def setUp(self):
        from tests import fakes  # noqa: F401  (config)
        from core.state import ApacheStateStore
        self.st = ApacheStateStore()

    def test_fast_packet_keeps_everything_else(self):
        self.st.update_telemetry(full_packet(10.0))
        self.st.update_telemetry(fast_packet(10.1))
        snap = self.st.snapshot()
        self.assertEqual(len(snap["contacts"]), 1)                   # the flight block carries no contacts
        self.assertEqual(snap["route"], [(1, 0.0, 0.0)])
        self.assertEqual(snap["aircraft"], "UH-1H")
        self.assertEqual((snap["agl_ft"], snap["hdg"], snap["t_model"], snap["g"]), (15.3, 91.5, 10.1, 1.02))
        self.assertEqual(snap["tel_seq"], 2)

    def test_flight_since_every_packet_once(self):
        for i in range(12):
            t = 10.0 + 0.1 * i
            self.st.update_telemetry(full_packet(t) if i % 3 == 0 else fast_packet(t))
        recs = self.st.flight_since(0)
        self.assertEqual([r["seq"] for r in recs], list(range(1, 13)))
        self.assertEqual([r["fast"] for r in recs[:3]], [False, True, True])
        self.assertEqual((recs[1]["gmax"], recs[0]["gmax"], recs[0]["g"]), (1.07, None, 1.01))
        self.assertEqual([r["seq"] for r in self.st.flight_since(10)], [11, 12])
        self.assertEqual(recs[0]["aircraft"], "UH-1H")

    def test_null_fields_are_none(self):
        self.st.update_telemetry(full_packet(10.0))
        self.st.update_telemetry(fast_packet(10.1, agl_ft=None, g=None, gmax=None))
        r = self.st.flight_since(1)[0]
        self.assertEqual((r["agl_ft"], r["g"], r["gmax"]), (None, None, None))
        self.assertEqual(self.st.snapshot()["agl_ft"], 15.0)           # kept from the full packet

    def test_ring_is_bounded(self):
        from core import state
        for i in range(state.FLIGHT_RING + 50):
            self.st.update_telemetry(fast_packet(float(i)))
        self.assertEqual(len(self.st.flight_since(0)), state.FLIGHT_RING)


class _Store:
    """state_store's snapshot / flight_since for the observer."""
    def __init__(self):
        from tests import fakes  # noqa: F401
        from core.state import ApacheStateStore
        self.st = ApacheStateStore()


class TestObserver(unittest.TestCase):
    def setUp(self):
        self.s = _Store().st
        self.flags = []
        self.ticks = []
        self.ins = Instructor(self.s.snapshot, lambda t: None, History(None), run_async=False, printer=lambda *a: None,
                              flight=self.s.flight_since, fast=self.flags.append)
        self.ins.tick = self.ticks.append

    def test_every_packet_reaches_tick_at_10hz(self):
        self.s.update_telemetry(full_packet(10.0))
        self.ins.poll()                                   # first poll: only the latest
        for i in range(1, 6):                             # five packets between two polls
            self.s.update_telemetry(fast_packet(10.0 + 0.1 * i))
        self.ins.poll()
        self.assertEqual([round(x.t, 1) for x in self.ticks], [10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
        last = self.ticks[-1]
        self.assertEqual((last.gmax, last.g, last.pitch, last.roll, last.aircraft), (1.07, 1.02, 1.2, 0.4, "UH-1H"))
        self.ins.poll()                                   # nothing new
        self.assertEqual(len(self.ticks), 6)

    def test_fast_flag_follows_the_exercise(self):
        self.s.update_telemetry(full_packet(10.0))
        self.ins.poll()
        self.assertEqual(self.flags, [False])             # cleared at start (a stale flag from a crash)
        self.ins.handle("Instructor, hover exercise")
        self.ins.poll()
        self.assertEqual(self.flags, [False, True])
        self.ins.handle("Instructor, off")
        self.ins.poll()
        self.assertEqual(self.flags, [False, True, False])
        self.ins.handle("Instructor, fuel exercise")      # 45 min at 4 Hz is fine: no fast block
        self.ins.poll()
        self.assertEqual(self.flags, [False, True, False])


class TestFastFlagFile(unittest.TestCase):
    def test_created_and_removed(self):
        from tests import fakes  # noqa: F401
        import config
        import main
        path = os.path.join(tempfile.mkdtemp(), "george_fast_telemetry.flag")
        with mock.patch.object(config, "INSTRUCTOR_FAST_FLAG", path, create=True):
            main._instructor_fast(True)
            self.assertTrue(os.path.exists(path))
            main._instructor_fast(False)
            self.assertFalse(os.path.exists(path))
            main._instructor_fast(False)                  # already gone: fine


class TestExportLua(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(HERE, "dcs_scripts", "Export.lua"), encoding="utf-8").read()

    def test_flag_checked_once_a_second_and_fast_block_at_10hz(self):
        self.assertIn('Scripts\\george_fast_telemetry.flag', self.src)
        self.assertIn("now - george_fast_check >= 1.0", self.src)
        self.assertIn("george_fast_on and now - george_fast_last >= 0.1", self.src)
        self.assertIn("now - george_tel_last >= 0.25", self.src)              # the full packet stays at 4 Hz
        self.assertIn('{"k":"f","t":%.3f', self.src)
        self.assertIn('"g":%s,"gmax":%s', self.src)

    def test_full_packet_has_g(self):
        self.assertIn('"olyx":%s,"g":%s,', self.src)                     # T8 adds neu / fr after it
        self.assertIn("LoGetAccelerationUnits", self.src)

    def test_g_read_every_frame_only_while_fast(self):
        i = self.src.index("function LuaExportBeforeNextFrame")
        body = self.src[i:self.src.index("function LuaExportStop")]
        self.assertIn("if george_fast_on then\n        local g = george_g()", body)


# ------------------------------------------------------------------ hover turn
def tick_turn(ins, t0, seconds, rate, hz=10, drift=0.0, agl=15.0, hdg0=90.0, after_s=0.0, overshoot_deg=0.0, hold_s=4.0):
    """Hover still for hold_s, turn at rate deg/s for seconds, then (optionally) overshoot and come back.
    Returns (t, heading) at the end."""
    t, h = t0, hdg0
    dt = 1.0 / hz

    def put(t, h, n=0.0):
        ins.tick(Sample(t, "UH-1H", (n, 0.0, 100.0), (0.0, 0.0, 0.0), agl, 0.0, h % 360.0, 0.0, ll_of(n, 0.0), 1000.0))
    for _ in range(int(hold_s * hz)):
        put(t, h)
        t += dt
    for i in range(int(seconds * hz)):
        h += rate * dt
        put(t, h, drift * i / (seconds * hz))
        t += dt
    for i in range(int(after_s * hz)):
        f = i / max(1, int(after_s * hz) - 1)
        put(t, h + math.copysign(overshoot_deg, rate) * math.sin(math.pi * f), drift)
        t += dt
    return t, h


class TestTurn(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.ins = Instructor(lambda: {"aircraft": "UH-1H", "t_model": 0.0, "telemetry_age": 0.0},
                              self.said.append, History(None), run_async=False, printer=lambda *a: None,
                              wall=lambda: 1793400000.0)

    def test_voice(self):
        for t in ("hover turn exercise", "pedal turn", "360 turn", "three sixty", "turn exercise", "begin pedal turn"):
            self.assertEqual(voice.parse(t)[1], "hover.turn", t)
        self.assertEqual(voice.parse("hover exercise")[1], "hover.stationary")

    def test_brief_and_begin(self):
        self.assertIn("turn 360 degrees on the spot", self.ins.handle("Instructor, hover turn exercise"))
        self.assertEqual(self.ins.handle("Instructor, begin"), "Instructor, hover turn, level 1. Clock starts when you're steady.")
        self.assertEqual(self.ins.state, ARMED)

    def test_good_turn_right_scores_high_at_10hz(self):
        self.ins.handle("Instructor, begin pedal turn")
        tick_turn(self.ins, 0.0, 30.0, 12.5, after_s=4.5)       # 375 deg: past 360 by 15 then back
        ev = self.ins.last
        self.assertEqual((ev.exercise_id, ev.outcome, ev.confidence), ("hover.turn", "scored", "high"))
        self.assertGreaterEqual(ev.score, 95.0)
        rows = {r["id"]: r for r in ev.criteria}
        self.assertAlmostEqual(rows["turn.time"]["value"], 28.0, delta=0.15)   # from the first 10 degrees
        self.assertLess(rows["turn.rate_sd"]["value"], 0.5)
        self.assertIn("Turn 360, left or right", " ".join(self.said))

    def test_jerky_slow_turn_with_overshoot(self):
        self.ins.handle("Instructor, hover turn exercise")
        self.assertEqual(self.ins.handle("Instructor, level 3"), "Instructor, hover turn level 3. Say begin.")
        self.ins.handle("Instructor, begin")
        # left, irregular: slow then fast, 25 degrees past the start heading before coming back
        t, h = tick_turn(self.ins, 0.0, 20.0, -6.0)             # 120 deg in 20 s
        tick_turn(self.ins, t, 16.0, -15.0, hdg0=h, hold_s=0.0, after_s=4.5, overshoot_deg=25.0)   # 240 more in 16 s
        ev = self.ins.last
        self.assertEqual(ev.level, 3)
        rows = {r["id"]: r for r in ev.criteria}
        self.assertGreater(rows["turn.rate_sd"]["lost"], 0)
        self.assertGreater(rows["turn.overshoot"]["lost"], 0)
        self.assertIn("left", " ".join(d.evidence for d in ev.deductions))

    def test_no_turn_aborts(self):
        self.ins.handle("Instructor, begin 360")
        tick_turn(self.ins, 0.0, 160.0, 0.0)
        self.assertEqual(self.ins.last.outcome, "aborted")
        self.assertIn("no 360 within 150 seconds", self.ins.last.note)

    def test_metrics_unwrap_across_north(self):
        s = [Sample(float(i), "UH-1H", (0, 0, 0), None, 15.0, 0.0, (350.0 + 10.0 * i) % 360.0, 0.0) for i in range(40)]
        p = turn_metrics.progress(s)
        self.assertEqual((p["sign"], p["t_start"], p["t_360"]), (1, 1.0, 36.0))


# ------------------------------------------------------------------ approach v3: touchdown g
class TestTouchdownG(unittest.TestCase):
    TARGET = {"name": "waypoint Alpha", "ll": ll_of(0.0, 0.0), "kind": "waypoint"}

    def run_approach(self, g_peak):
        said, h = [], [None]
        ins = Instructor(lambda: h[0].snapshot(), said.append, History(None), run_async=False,
                         printer=lambda *a: None, targets=lambda text, snap: self.TARGET,
                         fuel=lambda snap: h[0].fuel, wall=lambda: 1793400000.0)
        sim = h[0] = Sim(ins)
        real_tick = ins.tick
        contact = {"seen": False}

        def tick(s):                                  # the sim's samples with g: a peak at first contact
            on = s.agl_ft is not None and s.agl_ft <= sim.skid + 0.5
            g = 1.0
            if on and not contact["seen"]:
                contact["seen"], g = True, g_peak
            real_tick(Sample(s.t, s.aircraft, s.pos, s.vel, s.agl_ft, s.vs_fpm, s.hdg, s.ias_kt, s.ll, s.fuel_lbs,
                             0.0, 0.0, g, g))
        sim.sit()
        contact["seen"] = True                        # the first sit is not the touchdown
        ins.tick = tick
        sim.n, sim.agl = 1200.0, 300.0
        ins.handle("Instructor, approach to Alpha")
        ins.handle("Instructor, begin")
        contact["seen"] = False
        sim.good_approach()
        return ins.last

    def test_soft_touchdown_keeps_the_points(self):
        ev = self.run_approach(1.2)
        rows = {r["id"]: r for r in ev.criteria}
        self.assertTrue(rows["approach.td_g"]["judged"])
        self.assertEqual(rows["approach.td_g"]["lost"], 0.0)

    def test_hard_touchdown_is_a_safety_call(self):
        ev = self.run_approach(2.7)
        rows = {r["id"]: r for r in ev.criteria}
        self.assertEqual(rows["approach.td_g"]["lost"], 10.0)
        self.assertIn("Safety: touchdown g, 2.7 g.", feedback.result_line(ev))
        self.assertIn("peak 2.70 g", " ".join(d.evidence for d in ev.deductions))


class TestCatalog(unittest.TestCase):
    def test_versions_and_names(self):
        self.assertEqual((catalog.APPROACH_VERSION, catalog.TURN_VERSION), (3, 1))
        self.assertEqual(catalog.NAMES[catalog.TURN_ID], "hover turn")
        for lv in range(1, 6):
            ex = catalog.turn(lv)
            self.assertEqual(sum(c.points for c in ex.criteria), 100)
            self.assertEqual(ex.duration_s, catalog.TURN_LEVELS[lv]["time"][1] + 30.0)
        self.assertEqual([c.signals for c in catalog.approach(1).criteria if c.id == "approach.gate300"][0],
                         ("ll", "ias_kt"))


class TestSuggestTurn(unittest.TestCase):
    def test_turn_only_after_a_good_hover(self):
        from instructor import adaptive
        from instructor.session import TAG_OF, VERSIONS
        cands = [catalog.HOVER_ID, catalog.TURN_ID]

        def row(ex, score):
            return {"exercise_id": ex, "exercise_version": VERSIONS[ex], "level": 1, "score": score, "outcome": "scored",
                    "confidence": "medium", "end_wall": "2026-10-30T23:00:00",
                    "criteria": [{"id": c.id, "points": c.points, "judged": True, "lost": 0.0}
                                 for c in catalog.get(ex, 1).criteria]}
        now = 1793400000.0
        self.assertEqual(adaptive.suggest([], cands, TAG_OF, now=now)[0], catalog.HOVER_ID)
        self.assertEqual(adaptive.suggest([row(catalog.HOVER_ID, 60)], cands, TAG_OF, now=now)[0], catalog.HOVER_ID)
        self.assertEqual(adaptive.suggest([row(catalog.HOVER_ID, 90)], cands, TAG_OF, now=now),
                         (catalog.TURN_ID, "not flown yet"))
        self.assertTrue(all(t in adaptive.TRAINS for t in {c.tag for c in catalog.turn(1).criteria}))


if __name__ == "__main__":
    unittest.main()
