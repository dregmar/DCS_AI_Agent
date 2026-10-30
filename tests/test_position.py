"""Grid / position requests: MGRS math against published points, and who answers what."""
import unittest

from common import position as pos

ME = {"my_ll": (41.9033, 43.4590), "telemetry_age": 0.2}


class TestMgrs(unittest.TestCase):
    def test_known_points(self):
        # Washington Monument, published as 18S UJ 23487 06483 (rounded; MGRS truncates to 23486)
        gzd, sq, e, n = pos.to_mgrs(38.8895, -77.0352, 5)
        self.assertEqual((gzd, sq, n), ("18S", "UJ", "06483"))
        self.assertIn(e, ("23486", "23487"))
        self.assertEqual(pos.mgrs_text(0.0, 0.0, 5), "31N AA 66021 00000")
        self.assertEqual(pos.to_mgrs(41.9033, 43.4590)[:2], ("38T", "LM"))     # Caucasus, near Kutaisi
        self.assertEqual(pos.to_mgrs(-33.8568, 151.2153)[0], "56H")            # southern hemisphere
        self.assertEqual(pos.utm_zone(60.0, 5.0), 32)                          # Norway exception

    def test_spoken(self):
        self.assertEqual(pos.say_mgrs(41.9033, 43.4590), "Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8.")
        self.assertEqual(pos.say_latlon(41.9033, 43.4590), "North 41 degrees 54.2, East 43 degrees 27.5.")
        self.assertEqual(pos.bullseye_call((41.9033, 43.459), (42.0, 43.0)), "Bullseye 106 for 21.")


class TestRequests(unittest.TestCase):
    def test_what_counts(self):
        for q in ("What is our current grid location?", "where are we?", "say bullseye", "grid",
                  "give me a ten digit grid", "what's our position in lat long", "request our grid"):
            self.assertTrue(pos.wanted(q), q)
        for q in ("two, smoke my position", "hold position", "store my position", "what's the frequency",
                  "requesting CAS on my position", "land at my position"):
            self.assertFalse(pos.wanted(q), q)

    def test_answers(self):
        self.assertEqual(pos.handle("What is our current grid location?", ME),
                         "Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8.")
        self.assertEqual(pos.handle("six digit grid", ME), None)              # not a request on its own
        self.assertEqual(pos.handle("give me a six digit grid", ME), "Grid 38 Tango, Lima Mike, 7 2 1, 4 0 1.")
        self.assertTrue(pos.handle("say bullseye", ME).startswith("Unable, no bullseye"))
        self.assertEqual(pos.handle("say bullseye", ME, lambda: (42.0, 43.0)), "Bullseye 106 for 21.")
        self.assertEqual(pos.handle("where are we", {"my_ll": None}), "Unable, no position.")

    def test_olympus_bullseye(self):
        class Oly:
            def connect(self):
                return True

            def get(self, path):
                return {"bullseyes": {"0": {"latitude": 1, "longitude": 2, "coalition": "neutral"},
                                      "1": {"latitude": 3, "longitude": 4, "coalition": "red"},
                                      "2": {"latitude": 42.0, "longitude": 43.0, "coalition": "blue"}}}
        self.assertEqual(pos.olympus_bullseye(Oly()), (42.0, 43.0))


class TestRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_pipeline
        cls.tp, cls.main = test_pipeline, test_pipeline.main

    def setUp(self):
        self.tp.SPOKEN.clear()
        self.tp.OVERLORD_SPOKEN.clear()

    def test_copilot_and_overlord(self):
        from unittest import mock
        snap = dict(self.main.state_store.snapshot(), my_ll=(41.9033, 43.459), telemetry_age=0.1)
        with mock.patch.object(self.main.state_store, "snapshot", lambda: snap), \
                mock.patch.object(self.main, "OVERLORD", None):
            self.assertTrue(self.main.handle_shared("What is our current grid location?"))
            self.assertTrue(self.main.handle_shared("Overlord, request our grid"))
        self.assertEqual(self.tp.SPOKEN, ["Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8."])
        self.assertEqual(self.tp.OVERLORD_SPOKEN, ["Overlord, Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8."])

    def test_other_aircraft_has_no_george(self):
        from unittest import mock
        with mock.patch.object(self.main.state_store, "profile", lambda: "other"):
            self.main.handle_command("lock target 3")
        self.assertEqual(self.tp.SPOKEN, [self.main.OTHER_AIRCRAFT_SAY])
