"""Phase 2 refinement R6 (approved 3 Nov 2026): wider checks.
Warlord: MANPADS / AAA (and short-range IR SAMs) within WARLORD_CAUTION_M of a CAS target give an approval
with a caution - "CAS approved. Caution, ZSU-23 and Igla near the target." - never a denial.
Darkside: danger close also counts you, your wingmen and your Outcasts within 1000 m of the target and below
DANGER_CLOSE_AGL_M; a jet passing high overhead doesn't hold the run."""
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import exchange, rules
from tactical import link
from tactical.models import PictureSnapshot
from tests.tactical_fixtures import track
from tests.test_command_roles import north, warlord
from tests.test_refine_r4 import Stub, threat


def tgt(km, tid=701):
    lat, lon = north(km)
    return {"type": "T-72B", "id": tid, "lat": lat, "lon": lon, "src": "enemy_ground"}


class WithPicture(unittest.TestCase):
    def setUp(self):
        was = link._get()
        self.addCleanup(link.set_provider, was)
        tracks = (track("T0701", "T-72B", -8), track("T0801", "ZSU-23-4 Shilka", -10),
                  track("T0802", "SA-18 Igla manpad", -11), track("T0803", "ZSU-23-4 Shilka", -20),
                  track("T0804", "Strela-10M3", -9))
        self.pic = PictureSnapshot(0, 1000.0, 0.4, tracks, threats=(
            threat("T0801", "AAA", 90), threat("T0802", "MANPADS", 95), threat("T0803", "AAA", 90),
            threat("T0804", "IR SAM", 95)))
        link.set_provider(Stub(self.pic))


class WarlordCaution(WithPicture):
    def test_caution_not_denial(self):
        w = warlord()
        d = w.review("CAS", north(-8), [tgt(-8)], {})
        self.assertEqual((d.verdict, d.rule), ("approve", "caution"))
        self.assertEqual(d.advice, "Caution, SA-13, Shilka and SA-18 Igla manpad near the target.")   # nearest first
        self.assertEqual(d.advice.count("Shilka"), 1)                       # the one 12 km away isn't named
        with mock.patch.object(exchange, "short", lambda: False):
            self.assertEqual(w.advice_text("CAS", d), "Enfield 1-1, Warlord, CAS approved. " + d.advice)
        with mock.patch.object(exchange, "short", lambda: True):
            self.assertTrue(w.advice_text("CAS", d).startswith("Warlord, caution, "))   # short form: no callsign

    def test_names_and_distance(self):
        w = warlord()
        w.caution_m = 2500.0                                  # only the Shilka (2 km) and the SA-13 (1 km)
        d = w.review("CAS", north(-8), [tgt(-8)], {})
        self.assertEqual(d.advice, "Caution, SA-13 and Shilka near the target.")
        w.caution_m = 500.0
        self.assertEqual(w.review("CAS", north(-8), [tgt(-8)], {}).advice, "")
        self.assertEqual(w.review("SEAD", north(-8), [tgt(-8)], {}).advice, "")     # CAS only

    def test_with_another_note(self):
        w = warlord()
        d = w.review("CAS", north(-30), [tgt(-30)], {})       # outside Objective Bravo: the priority note too
        self.assertTrue(d.advice.startswith("this is outside the priority areas"))   # no threats near there
        link.set_provider(Stub(PictureSnapshot(0, 1000.0, 0.4, (track("T0901", "ZSU-23-4 Shilka", -31),),
                                               threats=(threat("T0901", "AAA", 90),))))
        d = w.review("CAS", north(-30), [tgt(-30)], {})
        self.assertTrue(d.advice.startswith("Caution, Shilka near the target. Also, this is outside the priority"), d.advice)


class OwnAircraftLow(unittest.TestCase):
    def setUp(self):
        was = link._get()
        self.addCleanup(link.set_provider, was)
        link.set_provider(None)

    def snap(self, me_km, agl_ft, **kw):
        s = {"my_ll": north(me_km), "agl_ft": agl_ft, "self_id": 5, "friendly_units": [], "enemy_lists_age": 0.5,
             "telemetry_age": 0.1, "scan_t": 1.0,
             "enemy_ground": [("T-72B", 701, *north(-8), 300.0)]}
        s.update(kw)
        return s

    def test_you_wingmen_outcasts(self):
        p = north(-8)
        self.assertEqual([(x["who"], round(x["d"] / 100)) for x in rules.own_low_near(self.snap(-7.2, 200), *p)],
                         [("you", 8)])
        self.assertEqual(rules.own_low_near(self.snap(-7.2, 20000), *p), [])          # jet high overhead
        s = self.snap(-20, 150, wing=[("Rotary-1-2", 6, *north(-7.5)), ("Rotary-1-1", 5, *north(-7.6))])
        self.assertEqual([x["who"] for x in rules.own_low_near(s, *p)], ["your wingman"])   # not yourself twice
        s = self.snap(-20, 150, olympus_units=[("Olympus-3", "Olympus-3-1", 900, *north(-8.4), 500.0),
                                                ("Olympus-2", "Olympus-2-1", 901, *north(-8.1), 400.0),
                                                ("Olympus-4", "Olympus-4-1", 902, *north(-8.2), 900.0)])
        got = rules.own_low_near(s, *p, outcasts={900: "Outcast 2", 902: "Outcast 3"}, target_alt=300.0)
        self.assertEqual([x["who"] for x in got], ["Outcast 2"])     # Overlord's Hog isn't yours; Outcast 3 is high

    def test_darkside_holds_the_run(self):
        from common.darkside import Darkside
        s = self.snap(-7.2, 200)
        d = Darkside(jtac=None, snapshot=lambda: s, speak=lambda t: None)
        m = type("M", (), {"task_id": "K1"})()
        ok, why = d.clearance(m, tgt(-8), s)
        self.assertEqual((ok, why), (False, "you're 800 metres from the target"))
        s["agl_ft"] = 20000
        self.assertEqual(d.clearance(m, tgt(-8), s), (True, ""))
        s.update(agl_ft=200, my_ll=north(-20), olympus_units=[("Olympus-3", "Olympus-3-1", 900, *north(-8.3), 400.0)])
        d.outcasts = lambda: {900: "Outcast 2"}
        self.assertEqual(d.clearance(m, tgt(-8), s), (False, "Outcast 2 is 300 metres from the target"))


class MainWiring(unittest.TestCase):
    def test_settings(self):
        import main
        if main.WARLORD is None or main.DARKSIDE is None:
            self.skipTest("command roles off")
        self.assertEqual(main.WARLORD.caution_m, 5000.0)
        self.assertEqual(main.DARKSIDE.agl_m, 300.0)
        self.assertEqual(main._outcast_units({"olympus_units": []}), {})


if __name__ == "__main__":
    unittest.main()
