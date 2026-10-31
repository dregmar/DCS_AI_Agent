"""Instructor Mode Phase 1 (instructor/): hover metrics, the quality gate, scoring rules, determinism,
the session flow, history on disk, voice phrases, and that the package can't send anything anywhere.
No tests.fakes import: the instructor package never imports config (instructor.session.build reads it
only when main.py calls it)."""
import gzip
import json
import math
import os
import random
import re
import tempfile
import time
import unittest

from instructor import catalog, feedback, scoring, voice
from instructor.history import History
from instructor.models import Sample
from instructor.quality import assess
from instructor.session import ARMED, IDLE, RUNNING, SELECTED, Instructor, SkidLearner, sample_from_snapshot

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def S(t, n=0.0, e=0.0, agl=15.0, hdg=90.0, vel=(0.2, 0.1, 0.0), vs=20.0, ac="UH-1H", pos=True, ias=0.0):
    return Sample(t, ac, (1000.0 + n, 2000.0 + e, 100.0) if pos else None, vel, agl, vs, hdg, ias)


def series(dur=45.0, hz=4.0, t0=100.0, **f):
    """Samples from t0 to t0+dur; f maps a field name to a function of the elapsed time."""
    out, k = [], 0
    while k / hz <= dur + 1e-9:
        x = k / hz
        kw = {name: fn(x) for name, fn in f.items()}
        out.append(S(t0 + x, **kw))
        k += 1
    return out


def ev_of(samples, level=3, t0=100.0, dur=None):   # level 3 = the proposal's worked example
    ex = catalog.hover(level)
    t1 = t0 + (dur if dur is not None else ex.duration_s)
    return scoring.evaluate(ex, samples, t0, t1, {"aircraft": "UH-1H", "mission": "M", "pilot": "p",
                                                  "start_wall": "w0", "end_wall": "w1"})


def row(ev, cid):
    return next(r for r in ev.criteria if r["id"] == cid)


class TestHoverScoring(unittest.TestCase):
    def test_perfect_hover_scores_100(self):
        ev = ev_of(series(hz=10))
        self.assertEqual((ev.outcome, ev.score, ev.confidence), ("scored", 100.0, "high"))
        self.assertEqual(ev.deductions, ())
        self.assertIn("Inside every tolerance", feedback.result_line(ev))

    def test_drift_circle_deducts_linearly(self):
        # 2.5 m circle round the start point after the first second: p95 2.5 m, L3 good 2 / limit 5
        ev = ev_of(series(hz=10, n=lambda x: 0 if x < 1 else 2.5 * math.cos(x), e=lambda x: 0 if x < 1 else 2.5 * math.sin(x)))
        r = row(ev, "hover.radius_p95")
        self.assertAlmostEqual(r["value"], 2.5, places=2)
        self.assertAlmostEqual(r["lost"], 5.0, places=1)             # 30 x (2.5 - 2) / 3
        self.assertEqual(row(ev, "hover.in_box")["lost"], 0.0)        # inside the 3 m box all the time
        d = ev.deductions[0]
        self.assertEqual(d.criterion, "hover.radius_p95")
        self.assertIn("95% of the time within 2.5 m", d.evidence)
        self.assertEqual(ev.weakness_tags, ())                        # 5 of 30 points: not a weakness
        self.assertAlmostEqual(ev.score, 95.0, places=1)

    def test_big_drift_tags_a_weakness(self):
        ev = ev_of(series(hz=10, n=lambda x: 0 if x < 1 else 4.5 * math.cos(x), e=lambda x: 0 if x < 1 else 4.5 * math.sin(x)))
        self.assertEqual(ev.weakness_tags, ("hover.drift",))
        self.assertIn("Biggest loss: drift, 4.5 meters at the 95th percentile", feedback.result_line(ev))

    def test_height_oscillation(self):
        # +/-2 ft square wave after the first second: RMS 2 ft (L3 good 1, limit 3), max 2 ft (good 3)
        ev = ev_of(series(hz=10, agl=lambda x: 15.0 if x < 1 else (17.0 if int(x) % 2 else 13.0)))
        self.assertAlmostEqual(row(ev, "hover.height_rms")["value"], 2.0, delta=0.05)
        self.assertAlmostEqual(row(ev, "hover.height_rms")["lost"], 12.5, delta=0.7)
        self.assertEqual(row(ev, "hover.height_max")["lost"], 0.0)

    def test_heading_wraps_through_north(self):
        ev = ev_of(series(hz=10, hdg=lambda x: 359.0 if int(x) % 2 else 1.0))
        self.assertLessEqual(row(ev, "hover.heading_p95")["value"], 2.0)

    def test_lost_points_shapes(self):
        c = catalog.hover(3).criteria
        radius, box = c[0], c[4]
        self.assertEqual(scoring.lost_points(radius, 1.0), 0.0)
        self.assertEqual(scoring.lost_points(radius, 3.5), 15.0)
        self.assertEqual(scoring.lost_points(radius, 99.0), 30.0)
        self.assertEqual(scoring.lost_points(box, 95.0), 0.0)        # higher is better
        self.assertEqual(scoring.lost_points(box, 75.0), 7.5)
        self.assertEqual(scoring.lost_points(box, 10.0), 15.0)

    def test_flown_hovers_30_oct(self):
        """The two UH-1H hovers flown on 30 Oct (tests/fixtures): 6.8 and 15.2 at the old level 2, which is
        today's level 3 (the same criteria), and a usable score at the new beginner level 1."""
        with open(os.path.join(HERE, "tests", "fixtures", "instructor_hovers_20261030.json"), encoding="utf-8") as f:
            hovers = json.load(f)["hovers"]
        want = {"first": {3: 6.8, 2: 23.2, 1: 71.5}, "second": {3: 15.2, 2: 60.4, 1: 98.0}}
        for key, h in hovers.items():
            s = [Sample(x["t"], x["aircraft"], tuple(x["pos"]), tuple(x["vel"]), x["agl_ft"], x["vs_fpm"], x["hdg"],
                        x["ias_kt"]) for x in h["samples"]]
            for level, score in want[key].items():
                ex = catalog.hover(level)
                ev = scoring.evaluate(ex, s, h["start_model"], h["start_model"] + ex.duration_s, {})
                self.assertEqual((ev.score, ev.confidence), (score, "medium"), f"{key} level {level}")

    def test_levels_tighten(self):
        goods = [catalog.hover(l).criteria[0].good for l in catalog.LEVELS]
        self.assertEqual(goods, sorted(goods, reverse=True))
        self.assertEqual(catalog.hover(9).level, 5)
        self.assertEqual(catalog.hover(0).level, 1)


class TestQualityGate(unittest.TestCase):
    def test_four_hz_is_degraded_medium_confidence(self):
        ev = ev_of(series(hz=4))
        self.assertEqual((ev.outcome, ev.confidence), ("scored", "medium"))
        self.assertTrue(all(q.status == "degraded" for q in ev.quality))
        self.assertIn("medium because telemetry is 4 samples a second", feedback.details(ev))

    def test_two_hz_is_not_judged_at_all(self):
        ev = ev_of(series(hz=2))
        self.assertEqual((ev.outcome, ev.score, ev.confidence), ("not_scored", None, "none"))
        self.assertIn("telemetry too slow", ev.note)
        self.assertEqual(ev.deductions, ())                           # never a deduction on bad data

    def test_missing_heading_is_not_judged_not_deducted(self):
        ev = ev_of(series(hz=10, hdg=lambda x: None))
        h = row(ev, "hover.heading_p95")
        self.assertFalse(h["judged"])
        self.assertIn("no heading data", h["reason"])
        self.assertEqual(ev.points_judged, 80.0)
        self.assertEqual((ev.outcome, ev.confidence), ("scored", "medium"))     # 80 % judged
        self.assertIn("Not judged: heading holding", feedback.result_line(ev))
        self.assertEqual(row(ev, "hover.in_box")["judged"], True)             # box without the heading axis

    def test_missing_position_is_partial_at_best(self):
        ev = ev_of(series(hz=10, pos=lambda x: False))
        self.assertFalse(row(ev, "hover.radius_p95")["judged"])
        self.assertEqual((ev.outcome, ev.confidence), ("partial", "low"))      # 55 of 100 points, core missing
        self.assertIn("partial score", feedback.result_line(ev))

    def test_gap_makes_signal_insufficient(self):
        s = [x for x in series(hz=10) if not (120.0 < x.t < 122.5)]
        ev = ev_of(s)
        self.assertEqual(ev.outcome, "not_scored")
        self.assertIn("position dropped out for 2.5 seconds", ev.note)

    def test_whole_feet_note(self):
        q = assess(series(hz=10), "agl_ft", 100.0, 145.0, 3.5, 5.0)
        self.assertEqual(q.status, "good")
        self.assertIn("whole feet", q.reason)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_json(self):
        s = series(hz=4, n=lambda x: math.sin(x), agl=lambda x: 15 + round(math.sin(x / 2)))
        a = json.dumps(ev_of(s).to_json(), sort_keys=True)
        b = json.dumps(ev_of(list(s)).to_json(), sort_keys=True)
        shuffled = list(s)
        random.Random(4).shuffle(shuffled)
        c = json.dumps(ev_of(shuffled).to_json(), sort_keys=True)
        self.assertEqual(a, b)
        self.assertEqual(a, c)


class FakeClock:
    def __init__(self):
        self.t = 1793400000.0

    def __call__(self):
        return self.t


class TestSession(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.snap = {"aircraft": "UH-1H"}
        self.hist = History(None)
        self.ins = Instructor(lambda: self.snap, self.said.append, self.hist, mission_name=lambda: "Training",
                              run_async=False, printer=lambda *a: None, wall=FakeClock())
        self.t = 0.0

    def feed(self, count, **kw):
        for _ in range(count):
            self.ins.tick(S(self.t, **kw))
            self.t += 0.25

    def ground(self):
        self.feed(16, agl=5.0, vel=(0, 0, 0), vs=0.0)

    def test_full_flow(self):
        self.ground()
        self.assertEqual(self.ins.skid.skid_ft, 5.0)
        self.assertIn("level 1", self.ins.handle("Instructor, hover exercise"))
        self.assertEqual(self.ins.state, SELECTED)
        self.ins.handle("instructor, begin")
        self.assertEqual(self.ins.state, ARMED)
        self.feed(4, agl=15.0, vel=(4.0, 0, 0))                       # 7.8 kt: not steady yet
        self.assertEqual(self.ins.state, ARMED)
        self.feed(12, agl=15.0)                                         # 2.75 s steady: not yet
        self.assertEqual(self.ins.state, ARMED)
        self.feed(1, agl=15.0)                                          # 3 s steady -> running
        self.assertEqual(self.ins.state, RUNNING)
        self.assertEqual(self.said[-1], "Instructor, clock running. Hold it for 30 seconds.")
        self.feed(200, agl=15.0)
        self.assertEqual(self.ins.state, SELECTED)
        self.assertRegex(self.said[-1], r"^Instructor, hover, 100, medium confidence\.")
        rec = self.hist.recent()
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["outcome"], rec[0]["mission"], rec[0]["aircraft"]), ("scored", "Training", "UH-1H"))
        self.assertAlmostEqual(rec[0]["end_model"] - rec[0]["start_model"], 30.0)

    def test_on_the_ground_never_starts(self):
        self.ground()
        self.ins.handle("Instructor, begin")
        self.feed(40, agl=5.0, vel=(0, 0, 0), vs=0.0)
        self.feed(40, agl=7.0)                                          # 2 ft above skids: not a hover
        self.assertEqual(self.ins.state, ARMED)

    def test_no_skid_needs_fifteen_feet(self):
        self.ins.handle("Instructor, begin")
        self.feed(20, agl=10.0)
        self.assertEqual(self.ins.state, ARMED)
        self.feed(13, agl=16.0)
        self.assertEqual(self.ins.state, RUNNING)

    def test_leaving_the_hover_aborts(self):
        self.ins.handle("Instructor, begin")
        self.feed(13, agl=20.0)
        self.feed(4, agl=20.0, n=60.0)
        self.assertEqual(self.ins.state, SELECTED)
        self.assertIn("not scored", self.said[-1])
        self.assertIn("50 meters", self.said[-1])
        self.assertEqual(self.hist.recent()[0]["outcome"], "aborted")

    def test_stop_and_mission_reload(self):
        self.ins.handle("Instructor, begin")
        self.feed(13, agl=20.0)
        self.ins.handle("Instructor, stop")
        self.assertEqual(self.hist.recent()[-1]["note"], "stopped by the pilot")
        self.ins.handle("Instructor, begin")
        self.feed(13, agl=20.0)
        self.ins.on_mission_loaded()
        self.assertEqual(self.hist.recent()[-1]["note"], "the mission was reloaded")
        self.assertIsNone(self.ins.skid.skid_ft)
        self.ins.handle("Instructor, begin")
        self.ins.on_mission_loaded()
        self.assertEqual(self.said[-1], "Instructor, mission reloaded, exercise cancelled.")

    def test_model_time_restart_aborts(self):
        self.ins.handle("Instructor, begin")
        self.feed(13, agl=20.0)
        self.t = 0.0
        self.feed(1, agl=20.0)
        self.assertEqual(self.hist.recent()[-1]["note"], "the mission restarted")

    def test_jets_are_refused(self):
        self.snap["aircraft"] = "FA-18C_hornet"
        self.assertIn("for the Huey and the Apache", self.ins.handle("Instructor, hover exercise"))
        self.assertIn("for the Huey and the Apache", self.ins.handle("Instructor, begin"))
        self.assertEqual(self.ins.state, IDLE)

    def test_apache_is_allowed(self):
        self.snap["aircraft"] = "AH-64D_BLK_II"
        self.assertIn("level 1", self.ins.handle("Instructor, hover exercise"))

    def test_answers(self):
        self.assertEqual(self.ins.handle("Instructor, how did I do"), "Instructor, no results yet this session.")
        self.assertEqual(self.ins.handle("Instructor, status"), "Instructor, standing by.")
        self.assertIn("say again", self.ins.handle("Instructor, sing me a song"))
        self.assertEqual(self.ins.handle("George, status"), "")
        self.ins.handle("Instructor, begin")
        self.assertIn("waiting for a steady hover", self.ins.handle("Instructor, status"))
        self.feed(13, agl=20.0)
        self.assertIn("seconds to go", self.ins.handle("Instructor, status"))
        self.assertIn("already running", self.ins.handle("Instructor, begin"))
        self.assertIn("Say stop first", self.ins.handle("Instructor, hover exercise"))

    def test_link_row(self):
        self.ground()
        d = self.ins.link()["detail"]
        self.assertIn("idle", d)
        self.assertIn("4.0 samples/s (medium confidence below 5)", d)
        self.assertIn("skid 5 ft", d)

    def test_skid_learner_ignores_low_steady_hover(self):
        sk = SkidLearner()
        for i in range(16):
            sk.update(S(i * 0.25, agl=5.0, vel=(0, 0, 0), vs=0.0))
        for i in range(40):
            sk.update(S(10 + i * 0.25, agl=11.0, vel=(0.1, 0.1, 0), vs=0.0))
        self.assertEqual(sk.skid_ft, 5.0)
        sk.update(S(30.0, agl=5.0, ac="AH-64D_BLK_II"))
        self.assertIsNone(sk.skid_ft)                                    # another aircraft: learn again


class TestObserver(unittest.TestCase):
    def test_each_packet_once_and_stale_ignored(self):
        ticks = []
        snap = {"tel_seq": 1, "t_model": 10.0, "telemetry_age": 0.1, "aircraft": "UH-1H", "pos": [1, 2, 3],
                "vel": [0, 0, 0], "agl_ft": 5, "vs_fpm": 0, "hdg": 90, "speed_kts": 0}
        ins = Instructor(lambda: snap, lambda t: None, History(None), run_async=False, printer=lambda *a: None)
        ins.tick = ticks.append
        ins.poll()
        ins.poll()
        self.assertEqual(len(ticks), 1)
        snap.update(tel_seq=2, telemetry_age=9.0)
        ins.poll()
        self.assertEqual(len(ticks), 1)

    def test_sample_from_snapshot_unknowns(self):
        s = sample_from_snapshot({"t_model": 1.0, "telemetry_age": 0.0, "aircraft": "UH-1H", "pos": None,
                                  "vel": [0, 0, 0], "agl_ft": -1.0, "vs_fpm": 0, "hdg": -1.0, "speed_kts": 0})
        self.assertEqual((s.pos, s.agl_ft, s.hdg), (None, None, None))
        self.assertIsNone(sample_from_snapshot({"t_model": None, "telemetry_age": 0.0}))


class TestVoice(unittest.TestCase):
    def test_wake_and_sound_alikes(self):
        for t in ("Instructor, begin", "instructor begin", "In structure, begin.", "Constructor, begin"):
            self.assertEqual(voice.addressed(t), "begin", t)
        self.assertIsNone(voice.addressed("George, begin"))
        self.assertIsNone(voice.addressed("instructors"))

    def test_commands(self):
        cases = {"hover exercise": ("select", "hover.stationary"), "hover practice": ("select", "hover.stationary"),
                 "begin": ("begin", None), "begin hover exercise": ("begin", "hover.stationary"),
                 "start the hover": ("begin", "hover.stationary"), "stop": ("stop", None),
                 "abort the hover": ("stop", None), "how did I do?": ("result", None), "results": ("result", None),
                 "status": ("status", None), "off": ("off", None), "explain the hover": ("brief", "hover.stationary"),
                 "what's for dinner": ("unknown", None)}
        for said, want in cases.items():
            self.assertEqual(voice.parse(said), want, said)


class TestHistory(unittest.TestCase):
    def test_append_reload_and_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            h = History(d, "Andre G.", printer=lambda *a: None)
            h.append({"exercise_id": "hover.stationary", "score": 80}, [{"t": 1.0}])
            with open(h.log_path, "a", encoding="utf-8") as f:
                f.write("{not json\n")
            h.append({"exercise_id": "hover.stationary", "score": 90}, None)
            h2 = History(d, "Andre G.", printer=lambda *a: None)
            self.assertEqual([r["score"] for r in h2.recent()], [80, 90])
            self.assertEqual(h2.bad_lines, 1)
            self.assertTrue(h.dir.endswith("Andre_G"))
            raw = os.listdir(os.path.join(h.dir, "sessions"))
            self.assertEqual(len(raw), 1)
            with gzip.open(os.path.join(h.dir, "sessions", raw[0]), "rt", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["samples"], [{"t": 1.0}])

    def test_raw_files_pruned_after_30_days(self):
        with tempfile.TemporaryDirectory() as d:
            h = History(d, "p", printer=lambda *a: None)
            h.append({"exercise_id": "x"}, [])
            raw_dir = os.path.join(h.dir, "sessions")
            old = os.path.join(raw_dir, os.listdir(raw_dir)[0])
            os.utime(old, (time.time() - 31 * 86400, time.time() - 31 * 86400))
            self.assertEqual(History(d, "p", printer=lambda *a: None).prune_raw(), 0)   # pruned at load
            self.assertEqual(os.listdir(raw_dir), [])
            self.assertTrue(os.path.exists(h.log_path))                                  # results stay


class TestReadOnly(unittest.TestCase):
    FORBIDDEN = re.compile(r"^\s*(?:from|import)\s+(?:common\.olympus|common\.comms_menu|core\.dcs_bios|"
                           r"core\.george_interface|pynput|socket|urllib|requests|http|audio|config)\b", re.M)

    def test_package_imports_nothing_that_can_send(self):
        pkg = os.path.join(HERE, "instructor")
        seen = 0
        for root, _, files in os.walk(pkg):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        src = f.read()
                    seen += 1
                    self.assertIsNone(self.FORBIDDEN.search(src), f"{name} imports something that can send")
        self.assertGreaterEqual(seen, 9)


if __name__ == "__main__":
    unittest.main()


class TestTalkDownQuiet(unittest.TestCase):
    """huey/talkdown.py holds its hover coaching while an exercise is armed or running (30 Oct flight:
    'Drifting forward' / 'Steady' coached the graded hover and queued the instructor 8-19 s late)."""

    def run_talkdown(self, quiet):
        from huey.talkdown import TalkDown
        said = []
        td = TalkDown(said.append, calibration_file=None)
        td.quiet = lambda: quiet
        now = 0.0

        def snap(agl, vel=(0.0, 0.0, 0.0), vs=0.0):
            return {"agl_ft": agl, "vs_fpm": vs, "vel": list(vel), "hdg": 0.0, "pos": [0, 0, 100], "cargo": None}
        for _ in range(16):                                     # on the ground: skid height 5 ft
            td.check(snap(5.0), now)
            now += 0.25
        td.check(snap(12.0, vs=200.0), now)                     # lift-off
        now += 10.0
        for _ in range(8):                                      # hover drifting forward 1 m/s
            td.check(snap(15.0, vel=(1.0, 0.0, 0.0)), now)
            now += 0.5
        for agl in (25.0, 18.0, 12.0, 8.0):                     # fast descent near the ground
            td.check(snap(agl, vs=-500.0), now)
            now += 0.5
        return said

    def test_quiet_holds_hover_calls_but_not_safety(self):
        loud = self.run_talkdown(False)
        quiet = self.run_talkdown(True)
        self.assertIn("Drifting forward.", loud)
        self.assertIn("Twenty feet.", loud)
        self.assertNotIn("Drifting forward.", quiet)
        self.assertFalse(any(s in quiet for s in ("Twenty feet.", "Ten.", "Steady.")))
        self.assertIn("Easy, slow your descent!", quiet)        # safety still plays
        self.assertIn("Skids up.", quiet)

    def test_broken_hook_never_stops_the_crew_chief(self):
        from huey.talkdown import TalkDown
        td = TalkDown(lambda t: None, calibration_file=None)
        td.quiet = lambda: 1 / 0
        self.assertFalse(td._quiet())

    def test_instructor_busy(self):
        ins = Instructor(lambda: {"aircraft": "UH-1H"}, lambda t: None, History(None), run_async=False,
                         printer=lambda *a: None)
        self.assertFalse(ins.busy())
        ins.handle("Instructor, begin")
        self.assertTrue(ins.busy())
        ins.handle("Instructor, stop")
        self.assertFalse(ins.busy())
