"""Phase 2 refinement R4 (approved 3 Nov 2026): the roles' checks read the tactical picture - big SAMs from
its threat list, friendlies from its FRIENDLY tracks, JTAC targets from hostile ACTIVE tracks. Stale, lost
and out-of-coverage units are treated the same everywhere: a unit the picture can't vouch for is never
called alive or clear. With no current picture, Export's raw lists as before."""
import unittest
from dataclasses import replace

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import rules
from tactical import link
from tactical.models import (Affiliation, Basis, Category, Evidence, PictureSnapshot, Status, ThreatAssessment)
from tests.tactical_fixtures import track
from tests.test_command_roles import north, quiet_tasks, warlord


class Stub:
    def __init__(self, pic):
        self.pic = pic

    def picture(self):
        return self.pic


def threat(tid, cat="radar SAM", w=100):
    return ThreatAssessment(tid, cat, w, 20_000.0, Basis.INFERRED, 9000.0, False, 1,
                            (Evidence("DCS lists it as SAM TR", Basis.WORLD, "DCS unit attributes"),), Status.ACTIVE)


def pic():
    tracks = (
        track("T0501", "Buk 9S18 sr", -4),                                           # big SAM, a threat
        track("T0502", "Buk 9S18 sr", -6, status=Status.GONE, reason="killed"),       # destroyed: not a threat
        track("T0503", "ZSU-23-4 Shilka", -8.5),                                      # AAA near the CAS point
        track("T0701", "T-72B", -8),                                                  # ACTIVE hostile
        track("T0702", "BMP-2", -9, status=Status.STALE, reason="lists stale"),       # can't vouch: not offered
        track("T0703", "Ural-375", -7, aff=Affiliation.NEUTRAL),                      # neutral: never a target
        track("T0704", "MiG-29S", -8, cat=Category.AIR),                              # air: not ground
        track("T0006", "M-1 Abrams", -7.4, aff=Affiliation.FRIENDLY, status=Status.LOST,
              reason="out of coverage too long"),                                     # last known 600 m away
        track("T0007", "M-1 Abrams", -30, aff=Affiliation.FRIENDLY),
        track("T0008", "M-1 Abrams", -8.1, aff=Affiliation.FRIENDLY, status=Status.GONE),   # gone: not counted
        track("T0009", "AH-64D", -8, aff=Affiliation.FRIENDLY, cat=Category.AIR),    # air: R6's job
    )
    return PictureSnapshot(0, 1000.0, 0.4, tracks, threats=(threat("T0501"), threat("T0503", "AAA", 90)))


class WithPicture(unittest.TestCase):
    def setUp(self):
        was = link._get()
        self.addCleanup(link.set_provider, was)
        link.set_provider(Stub(pic()))
        self.snap = {"friendly_units": [], "enemy_ground": [], "air_defence": [], "my_ll": north(0),
                     "aircraft": "A-10C_2", "enemy_lists_age": 0.5, "telemetry_age": 0.1}


class Lookups(WithPicture):
    def test_friendlies_threats_targets(self):
        p = north(-8)
        self.assertEqual([(r["id"], r["status"]) for r in link.friendlies_near(*p, 1000)], [(6, "lost")])
        self.assertEqual({r["id"]: r["category"] for r in link.threats_near(*p, 15_000)}, {501: "radar SAM", 503: "AAA"})
        self.assertEqual([r["id"] for r in link.hostile_ground_near(*p, 3000)], [701, 503])   # nearest first
        link.set_provider(Stub(None))
        self.assertIsNone(link.friendlies_near(*p, 1000))
        self.assertIsNone(link.threats_near(*p, 1000))
        self.assertIsNone(link.hostile_ground_near(*p, 1000))


class Rules(WithPicture):
    def test_friendlies_from_the_picture(self):
        got = rules.friendlies_near(self.snap, *north(-8), 1000)
        self.assertEqual([(f["type"], round(f["d"] / 100)) for f in got], [("M-1 Abrams", 6)])
        self.snap["friendly_units"] = None                                   # old Export: nobody can tell
        self.assertIsNone(rules.friendlies_near(self.snap, *north(-8), 1000))

    def test_raw_lists_without_a_picture(self):
        link.set_provider(Stub(None))
        self.snap["friendly_units"] = [("M-1 Abrams", 9, *north(-8.2), 100.0, "g")]
        self.assertEqual([f["id"] for f in rules.friendlies_near(self.snap, *north(-8), 1000)], [9])

    def test_big_sams(self):
        from common.overlord import big_sams_near
        self.snap["air_defence"] = [("Buk 9S18 sr", 502, *north(-6), 300.0)]          # raw list still shows the dead one
        self.assertEqual([s["id"] for s in big_sams_near(self.snap, north(-8))], [501])
        link.set_provider(Stub(None))
        self.assertEqual([s["id"] for s in big_sams_near(self.snap, north(-8))], [502])


class Warlord(WithPicture):
    def test_sam_and_lost_friendly(self):
        w = warlord()
        t = {"type": "T-72B", "id": 701, "lat": north(-12)[0], "lon": north(-12)[1]}
        d = w.review("CAS", north(-12), [t], self.snap)                      # SA-11 4 km south of me, from the picture
        self.assertEqual((d.verdict, d.rule), ("deny", "sam"))
        tasks = quiet_tasks()
        s = tasks.new("SEAD", "OVERLORD", "Weasel flight")
        tasks.set_state(s.task_id, "approved")
        w = warlord(tasks)
        t = {"type": "T-72B", "id": 701, "lat": north(-8)[0], "lon": north(-8)[1]}
        d = w.review("CAS", north(-8), [t], self.snap)
        self.assertEqual((d.verdict, d.rule, d.hard), ("deny", "danger_close", True))      # the LOST Abrams counts
        self.assertIn("M-1 Abrams", d.reason)


class Darkside(WithPicture):
    def test_lost_friendly_withholds(self):
        from common.darkside import Darkside
        d = Darkside(jtac=None, snapshot=lambda: self.snap, speak=lambda t: None)
        m = type("M", (), {"task_id": "K1"})()
        ok, why = d.clearance(m, {"type": "T-72B", "id": 701, "lat": north(-8)[0], "lon": north(-8)[1]}, self.snap)
        self.assertFalse(ok)
        self.assertIn("friendly M-1 Abrams", why)
        ok, why = d.clearance(m, {"type": "BMP-2", "id": 702, "lat": north(-9)[0], "lon": north(-9)[1]}, self.snap)
        self.assertEqual((ok, why), (False, "the BMP-2 isn't confirmed in the picture"))  # stale: never "alive"


class Jtac(WithPicture):
    def test_targets_and_threats_from_the_picture(self):
        from common.jtac import Jtac
        self.snap["enemy_ground"] = [("BMP-2", 702, *north(-9), 300.0)]            # raw list: the stale one only
        j = Jtac(lambda: self.snap)
        ids = [c["id"] for c in j.candidates(self.snap)]
        self.assertIn(701, ids)
        self.assertIn(503, ids)                                              # AAA is a CAS target (not a big SAM)
        for never in (501, 702, 703, 704):
            self.assertNotIn(never, ids)
        said = j.handle("request target, armour")
        self.assertTrue(said.startswith("Overlord, target T-72B."), said)
        self.assertIn("Threats: ZSU-23-4", said.replace("Shilka", "ZSU-23-4"))

    def test_no_picture_raw_list(self):
        from common.jtac import Jtac
        link.set_provider(Stub(None))
        self.snap["enemy_ground"] = [("BMP-2", 702, *north(-9), 300.0)]
        self.assertEqual([c["id"] for c in Jtac(lambda: self.snap).candidates(self.snap)], [702])


if __name__ == "__main__":
    unittest.main()
