"""Instructor Mode Phase 2: adaptive levels and weaknesses, trend feedback, the approach and fuel
exercises (simulated flights through the real session), the kneeboard view and briefing section.
No tests.fakes import: nothing here touches config (main.py wiring: tests/test_instructor_main.py)."""
import math
import unittest

from instructor import adaptive, catalog, feedback, geometry, scoring
from instructor.history import History
from instructor.session import ARMED, IDLE, RUNNING, SELECTED, Instructor, TAG_OF, VERSIONS
from tests.instructor_sim import Sim, ll_of


def row(ex="hover.stationary", level=1, score=80.0, version=None, outcome="scored", conf="medium", lost=None,
        when="2026-10-30T23:00:00"):
    ex_obj = catalog.get(ex, level)
    crit = [{"id": c.id, "points": c.points, "judged": True, "lost": (lost or {}).get(c.id, 0.0)} for c in ex_obj.criteria]
    return {"exercise_id": ex, "exercise_version": VERSIONS[ex] if version is None else version, "level": level,
            "score": score, "outcome": outcome, "confidence": conf, "criteria": crit, "end_wall": when}


class TestLevels(unittest.TestCase):
    def nl(self, rows, default=1):
        return adaptive.next_level(adaptive.valid(rows, "hover.stationary", VERSIONS), default)[0]

    def test_first_and_settling(self):
        self.assertEqual(self.nl([]), 1)
        self.assertEqual(self.nl([], default=3), 3)
        self.assertEqual(self.nl([row(score=99), row(score=99)]), 1)            # only 2 at level 1: hold

    def test_promote_needs_median_85_and_no_score_below_70(self):
        self.assertEqual(self.nl([row(score=90), row(score=86), row(score=95)]), 2)
        self.assertEqual(self.nl([row(score=95), row(score=69), row(score=95)]), 1)   # one below 70
        self.assertEqual(self.nl([row(level=5, score=99)] * 3), 5)                # top stays

    def test_demote(self):
        self.assertEqual(self.nl([row(level=3, score=50), row(level=3, score=54), row(level=3, score=90)]), 2)
        self.assertEqual(self.nl([row(level=3, score=59), row(level=3, score=58), row(level=3, score=59)]), 2)
        self.assertEqual(self.nl([row(level=1, score=10)] * 3), 1)                # floor

    def test_one_bad_attempt_never_moves(self):
        self.assertEqual(self.nl([row(level=2, score=80), row(level=2, score=75), row(level=2, score=5)]), 2)

    def test_only_the_run_at_the_current_level_counts(self):
        rows = [row(level=1, score=95)] * 3 + [row(level=2, score=95), row(level=2, score=96)]
        self.assertEqual(self.nl(rows), 2)                                         # 2 at level 2: hold

    def test_invalid_and_old_versions_ignored(self):
        rows = [row(score=95)] * 2 + [row(score=95, outcome="partial"), row(score=95, conf="low"),
                                        row(score=95, version=1)]
        self.assertEqual(len(adaptive.valid(rows, "hover.stationary", VERSIONS)), 2)


class TestWeaknessAndSuggest(unittest.TestCase):
    def test_recurring_needs_3_of_last_5(self):
        drift = {"hover.radius_p95": 15.0}                                         # 50 % of 30 points
        rows = [row(lost=drift), row(), row(lost=drift), row(), row(lost=drift)]
        self.assertEqual(adaptive.weaknesses(rows, TAG_OF), {"hover.drift": 3})
        self.assertEqual(adaptive.weaknesses(rows[1:] + [row()], TAG_OF), {})      # fell out of the window

    def test_suggest_prefers_a_recurring_weakness(self):
        sink = {"approach.sink_low": 10.0}
        rows = ([row("hover.stationary", score=95)] * 3 +
                [row("approach.pad", score=60, lost=sink)] * 3 + [row("fuel.recovery", score=40)])
        ex, why = adaptive.suggest(rows, list(catalog.EXERCISES), TAG_OF, now=1793400000.0)
        self.assertEqual((ex, why), ("approach.pad", "recurring sink rate"))

    def test_suggest_not_flown_first_and_deterministic(self):
        rows = [row("hover.stationary", score=95)] * 3
        a = adaptive.suggest(rows, list(catalog.EXERCISES), TAG_OF, now=1793400000.0)
        b = adaptive.suggest(list(rows), list(reversed(list(catalog.EXERCISES))), TAG_OF, now=1793400000.0)
        self.assertEqual(a, b)
        self.assertEqual(a[1], "not flown yet")


def instructor(sim_holder, said, targets=None, level=1):
    return Instructor(lambda: sim_holder[0].snapshot(), said.append, History(None), run_async=False,
                      printer=lambda *a: None, targets=targets, fuel=lambda snap: sim_holder[0].fuel,
                      wall=lambda: 1793400000.0, level=level)


class TestAdaptiveSession(unittest.TestCase):
    def setUp(self):
        self.said, self.h = [], [None]
        self.ins = instructor(self.h, self.said)
        self.sim = self.h[0] = Sim(self.ins)
        self.sim.sit()

    def hover(self):
        self.ins.handle("Instructor, begin hover")
        self.sim.agl = 15.0
        self.sim.step(count=13)                    # 3 s steady: the clock starts
        while self.ins.state == RUNNING:           # 30 s at level 1, longer higher up
            self.sim.step()

    def test_three_good_hovers_move_up_a_level(self):
        for _ in range(3):
            self.hover()
            self.sim.sit()
        self.assertTrue(self.said[-1].endswith("Moving up to level 2."), self.said[-1])
        self.assertIn("level 2", self.ins.handle("Instructor, hover exercise"))

    def test_easier_harder_and_level(self):
        self.assertEqual(self.ins.handle("Instructor, harder"), "Instructor, hover level 2. Say begin.")
        self.assertIn("level 2", self.ins.handle("Instructor, status"))
        self.assertEqual(self.ins.handle("Instructor, level five"), "Instructor, hover level 5. Say begin.")
        self.assertEqual(self.ins.handle("Instructor, easier"), "Instructor, hover level 4. Say begin.")
        self.hover()
        self.assertEqual((self.ins.last.level, self.ins.last.end_model - self.ins.last.start_model), (4, 60.0))

    def test_override_while_running_applies_next_time(self):
        self.ins.handle("Instructor, begin hover")
        self.sim.agl = 15.0
        self.sim.step(count=13)
        self.assertEqual(self.ins.state, RUNNING)
        self.assertEqual(self.ins.handle("Instructor, level 3"), "Instructor, next hover at level 3.")
        self.sim.step(count=121)
        self.assertTrue(self.said[-1].endswith("Moving up to level 3."), self.said[-1])
        self.assertIn("level 3", self.ins.handle("Instructor, hover exercise"))

    def test_suggest_and_on(self):
        self.assertIn("I suggest the", self.ins.handle("Instructor, what should I practise"))
        self.assertIn("hover, hover turn, approach or fuel", self.ins.handle("Instructor mode on"))
        self.assertEqual(self.ins.handle("Instructor mode off"), "Instructor, standing by.")


class TestTrend(unittest.TestCase):
    def ev(self, n=lambda x: 0.0, agl=lambda x: 15.0, dur=30.0):
        from tests.test_instructor import series
        s = series(dur=dur, hz=4, n=n, agl=agl)
        ex = catalog.hover(1)
        return scoring.evaluate(ex, s, 100.0, 100.0 + ex.duration_s, {})

    def test_steady_drift_south(self):
        ev = self.ev(n=lambda x: -0.6 * x)                  # 18 m south by the end
        self.assertIn("a steady drift south, 17 meters by the end", feedback.result_line(ev))

    def test_wandering(self):
        ev = self.ev(n=lambda x: 12.0 * math.sin(x / 2.0) if x < 25 else 0.0)
        self.assertIn("wandering about the point", feedback.result_line(ev))

    def test_steady_climb(self):
        ev = self.ev(agl=lambda x: 15.0 + round(min(12.0, x * 0.5)))
        self.assertIn("a steady climb of 12 feet over 30 seconds", feedback.result_line(ev))


class TestApproach(unittest.TestCase):
    TARGET = {"name": "waypoint Alpha", "ll": ll_of(0.0, 0.0), "kind": "waypoint"}

    def setUp(self):
        self.said, self.h = [], [None]
        self.ins = instructor(self.h, self.said, targets=lambda text, snap: self.TARGET)
        self.sim = self.h[0] = Sim(self.ins)
        self.sim.sit()

    def test_good_approach_scores_well(self):
        self.sim.n, self.sim.agl = 1200.0, 300.0
        self.assertIn("to waypoint Alpha", self.ins.handle("Instructor, approach to Alpha"))
        self.assertIn("1.2 kilometers, 180", self.ins.handle("Instructor, begin"))
        self.sim.good_approach()
        ev = self.ins.last
        self.assertEqual((ev.exercise_id, ev.outcome, ev.confidence), ("approach.pad", "scored", "medium"))
        self.assertGreaterEqual(ev.score, 95.0)
        self.assertTrue(ev.context["inferred_window"])
        self.assertLess(ev.context["t_touchdown"], ev.end_model)
        self.assertEqual(self.said[-2], "Instructor, approach clock running.")

    def test_sloppy_approach_has_evidence(self):
        self.ins.handle("Instructor, approach exercise")
        self.sim.n, self.sim.e, self.sim.agl = 900.0, 300.0, 250.0
        self.ins.handle("Instructor, begin")
        while self.sim.n > 20:
            self.sim.step(vn=-30.0 if self.sim.n > 150 else -12.0, ve=-10.0 if self.sim.e > 0 else 0.0,
                          vs_fpm=-900.0 if self.sim.agl > 10 else -600.0)
        self.sim.sit(3.0)
        ev = self.ins.last
        ids = [d.criterion for d in ev.deductions]
        # since approach v2 the touchdown sink is the 2 s before the skids touch (here 600-900 fpm),
        # so both sink criteria lose points and lead the list
        self.assertEqual(set(ids[:2]), {"approach.sink_low", "approach.td_sink"})
        self.assertIn("touched down 18 m north of waypoint Alpha", " ".join(d.evidence for d in ev.deductions))
        self.assertLess(ev.score, 90.0)

    def test_inside_800m_must_fly_out_first(self):
        self.sim.n, self.sim.agl = 500.0, 200.0
        self.assertIn("Fly out beyond 800 meters", self.ins.handle("Instructor, begin approach"))
        self.sim.step(vn=-10.0, count=8)
        self.assertEqual(self.ins.state, ARMED)                 # never started inside 600 m
        self.sim.n = 900.0
        self.sim.step(vn=-25.0, count=40)                       # 900 -> 650 m: armed, out far enough
        self.assertEqual(self.ins.state, ARMED)
        self.sim.step(vn=-25.0, count=12)                       # inside 600 m
        self.assertEqual(self.ins.state, RUNNING)

    def test_flying_away_aborts(self):
        self.sim.n, self.sim.agl = 1000.0, 300.0
        self.ins.handle("Instructor, begin approach")
        self.sim.step(vn=-25.0, count=70)                       # 1000 -> 560 m
        self.assertEqual(self.ins.state, RUNNING)
        self.sim.step(vn=40.0, count=200)
        self.assertEqual(self.ins.last.outcome, "aborted")
        self.assertIn("1500 meters", self.ins.last.note)

    def test_no_target(self):
        ins = instructor([self.sim], [], targets=lambda text, snap: None)
        self.assertIn("no landing point", ins.handle("Instructor, approach exercise"))
        ins = instructor([self.sim], [], targets=lambda text, snap: "Instructor, waypoint Alpha isn't set.")
        self.assertEqual(ins.handle("Instructor, approach to Alpha"), "Instructor, waypoint Alpha isn't set.")


class TestFuel(unittest.TestCase):
    def setUp(self):
        self.said, self.h = [], [None]
        self.ins = instructor(self.h, self.said)
        self.sim = self.h[0] = Sim(self.ins)
        self.sim.sit()

    def fly(self, late_s=60.0, out_after=True):
        self.assertIn("Bingo 1120 pounds", self.ins.handle("Instructor, begin fuel exercise"))
        bingo = self.ins.runner.bingo
        while self.sim.fuel > bingo:
            self.sim.step(ve=40.0, vs_fpm=500.0 if self.sim.agl < 500 else 0.0)
        self.sim.step(ve=40.0, count=int(late_s * 4))
        while self.sim.e > 100:
            self.sim.step(ve=-40.0, vs_fpm=-300.0 if self.sim.e < 3000 and self.sim.agl > 20 else 0.0)
        while self.sim.agl > 6:
            self.sim.step(vs_fpm=-300.0)
        self.sim.sit(3.0)
        return self.ins.last

    def test_full_exercise(self):
        ev = self.fly(late_s=60.0)
        self.assertEqual((ev.exercise_id, ev.outcome), ("fuel.recovery", "scored"))
        rows = {r["id"]: r for r in ev.criteria}
        self.assertAlmostEqual(rows["fuel.turn_delay"]["value"], 61.0, delta=2.0)
        self.assertLess(rows["fuel.home_dist"]["value"], 150.0)
        self.assertEqual(ev.context["bingo_lbs"], 1120.0)
        self.assertEqual(ev.context["after_lbs"], 100.0)

    def test_prompt_turn_scores_higher(self):
        late = self.fly(late_s=150.0).score
        self.sim.n = self.sim.e = 0.0
        self.sim.fuel = 1200.0
        self.sim.sit()
        prompt = self.fly(late_s=0.0).score
        self.assertGreater(prompt, late)

    def test_begin_in_the_air_refused(self):
        self.sim.agl = 50.0
        self.sim.step(ve=20.0, count=4)
        self.assertIn("say begin on the ground", self.ins.handle("Instructor, begin fuel exercise"))

    def test_not_far_enough_out_aborts(self):
        self.ins.handle("Instructor, begin fuel exercise")
        self.sim.agl = 100.0
        self.sim.step(ve=20.0, count=200)                         # 1 km out
        self.sim.step(ve=-20.0, count=200)
        while self.sim.agl > 6:
            self.sim.step(vs_fpm=-300.0)
        self.sim.sit(3.0)
        self.assertEqual(self.ins.last.outcome, "aborted")
        self.assertIn("only flew 1.0 kilometers out", self.ins.last.note)

    def test_status_reports_bingo(self):
        self.ins.handle("Instructor, begin fuel exercise")
        self.assertEqual(self.ins.handle("Instructor, status"), "Instructor, fuel exercise running, bingo 1120 pounds.")


class TestViews(unittest.TestCase):
    def setUp(self):
        self.said, self.h = [], [None]
        self.ins = instructor(self.h, self.said)
        self.sim = self.h[0] = Sim(self.ins)
        self.sim.sit()

    def test_view_while_running_and_after(self):
        self.assertIsNone(self.ins.view()["exercise"])
        self.ins.handle("Instructor, begin hover")
        self.sim.agl = 15.0
        self.sim.step(count=13)
        v = self.ins.view()
        self.assertEqual((v["state"], v["exercise"]["name"], v["exercise"]["level"]), ("running", "hover", 1))
        self.assertEqual(v["live"]["Drift"], ["0.0 m", True])
        self.assertEqual(self.ins.view(), v)                      # unchanged sample: identical feed
        self.sim.step(count=121)
        v = self.ins.view()
        self.assertEqual(v["last"]["score"], 100)
        self.assertEqual([l["exercise"] for l in v["levels"]], ["hover", "hover turn", "approach", "fuel"])
        self.assertIsNotNone(v["suggest"])

    def test_briefing_section(self):
        self.assertIsNone(self.ins.briefing_section())
        self.ins.handle("Instructor, hover exercise")
        sec = self.ins.briefing_section()
        self.assertEqual(sec["title"], "Instructor: hover, level 1 (selected)")
        titles = [e["title"] for e in sec["entries"]]
        self.assertEqual(titles[0], "What to do")
        self.assertIn("Position drift (30 points)", titles)


class TestGeometry(unittest.TestCase):
    def test_distance_bearing_compass(self):
        a, b = ll_of(0, 0), ll_of(300, 400)
        self.assertAlmostEqual(geometry.dist_m(a, b), 500.0, delta=0.5)
        self.assertAlmostEqual(geometry.bearing_deg(a, b), 53.13, delta=0.1)
        self.assertEqual(geometry.compass8(180.0), "south")
        self.assertEqual(geometry.compass8(350.0), "north")

    def test_tracks_need_a_second_and_movement(self):
        lls = [ll_of(i * 2.5, 0) for i in range(8)]
        tr = geometry.tracks(lls, [i * 0.25 for i in range(8)])
        self.assertEqual(tr[:4], [None] * 4)
        self.assertAlmostEqual(tr[-1], 0.0, delta=0.01)
        self.assertEqual(geometry.tracks([ll_of(0, 0)] * 8, [i * 0.25 for i in range(8)])[-1], None)


if __name__ == "__main__":
    unittest.main()
