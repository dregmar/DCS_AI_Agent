"""31 Oct flight: the approach bounced (2,800 fpm from 100 ft) and the fuel exercise began with 210 lb.

Approach v2 judges the touchdown on the 2 s before the skids first touched (v1 used the 2 s before the
settled touchdown, after the bounce, and missed it); a blown safety limit leads the spoken result;
the fuel exercise refuses to start when its bingo would take you below your reserve.
"""
import json
import os
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from instructor import catalog, feedback, scoring
from instructor.models import Sample
from instructor.runners import FuelRunner

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "instructor_approach_20261031.json")


def _ground(t=0.0, fuel=210.0):
    return Sample(t=t, aircraft="UH-1H", ias_kt=0.0, pos=(0.0, 0.0), agl_ft=4.0, vel=(0.0, 0.0), vs_fpm=0.0, hdg=0.0, ll=(42.0, 42.0),
                  fuel_lbs=fuel)


class TestFlownApproach(unittest.TestCase):
    def setUp(self):
        d = json.load(open(FIX))
        self.e = d["evaluation"]
        self.samples = [Sample.from_json(x) for x in d["samples"]]
        self.ev = scoring.evaluate(catalog.approach(self.e["level"]), self.samples, self.e["start_model"],
                                   self.e["end_model"], {}, context=self.e["context"])

    def test_version(self):
        # v2 judged the touchdown at first contact; v3 (Phase 3) adds IAS at 300 m and touchdown g.
        # This flight had no g in its packets: touchdown g is "not judged", the score is unchanged.
        self.assertEqual(catalog.APPROACH_VERSION, 3)
        self.assertEqual(self.ev.exercise_version, 3)
        rows = {r["id"]: r for r in self.ev.criteria}
        self.assertEqual((rows["approach.td_g"]["judged"], rows["approach.td_g"]["reason"]), (False, "no g data"))
        self.assertEqual(rows["approach.gate300"]["value"], 58.0)          # indicated airspeed now

    def test_rescored(self):
        self.assertEqual((self.ev.outcome, self.ev.confidence), ("scored", "medium"))
        self.assertAlmostEqual(self.ev.score, 41.6, places=1)
        rows = {r["id"]: r for r in self.ev.criteria}
        self.assertAlmostEqual(rows["approach.td_sink"]["value"], 2803.0, delta=1.0)
        self.assertEqual(rows["approach.td_sink"]["lost"], rows["approach.td_sink"]["points"])
        self.assertAlmostEqual(rows["approach.land_dist"]["value"], 46.07, delta=0.05)

    def test_contact_before_settled_touchdown(self):
        o = next(x for x in self.ev.observations if x.id == "approach.td_sink")
        self.assertLess(o.evidence["t_contact"], o.evidence["t_touchdown"])

    def test_safety_said_first(self):
        line = feedback.result_line(self.ev)
        self.assertIn("Safety: sink rate near the ground, 2803 feet a minute; touchdown, 2803 feet a minute.", line)
        self.assertIn("Also: landing position, 46 meters. Not judged: touchdown g.", line)
        self.assertNotIn("Biggest loss", line)

    def test_details_do_not_repeat(self):
        d = feedback.details(self.ev)
        self.assertEqual(d.count("landing position"), 1)
        self.assertEqual(d.count("touchdown,"), 1)
        self.assertIn("speed at 100 meters", d)


class TestFuelReserve(unittest.TestCase):
    def test_refused_below_reserve(self):
        r = FuelRunner(catalog.fuel(1), 80.0, 1.25, reserve_lbs=185.0)
        refusal, line, start = r.begin(_ground(fuel=210.0), 4.0, 210.0)
        self.assertEqual(refusal, "Instructor, not enough fuel for the exercise: 210 pounds on board, it needs 365, "
                                  "with your 185 pound reserve. Refuel first.")
        self.assertFalse(start)
        self.assertIsNone(r.t0)

    def test_starts_with_enough(self):
        r = FuelRunner(catalog.fuel(1), 80.0, 1.25, reserve_lbs=185.0)
        refusal, line, start = r.begin(_ground(fuel=600.0), 4.0, 600.0)
        self.assertIsNone(refusal)
        self.assertTrue(start)
        self.assertEqual(r.context()["reserve_lbs"], 185.0)

    def test_no_reserve_wording(self):
        r = FuelRunner(catalog.fuel(1), 80.0, 1.25)
        refusal, _, _ = r.begin(_ground(fuel=150.0), 4.0, 150.0)
        self.assertEqual(refusal, "Instructor, not enough fuel for the exercise: 150 pounds on board, it needs 180. "
                                  "Refuel first.")


class TestReserveWiring(unittest.TestCase):
    def test_huey_uses_config_reserve(self):
        import config
        import main
        with mock.patch.object(main.state_store, "profile", return_value="huey"), \
                mock.patch.object(config, "HUEY_FUEL_RESERVE_LBS", 185, create=True):
            self.assertEqual(main._instructor_reserve({}), 185.0)


if __name__ == "__main__":
    unittest.main()
