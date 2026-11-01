"""Tactical picture step T4: threat assessment with its evidence (design section 8; test plan row
"Threat assessment")."""
import unittest

from tactical import threats as th
from tactical.models import Affiliation, Association, Basis, Category, Status, UNIQUE
from tests.tactical_fixtures import ME, attrs, track

WEIGHTS = {"Osa 9A33 ln": 100, "ZSU-23-4 Shilka": 100, "T-72B": 60, "BMP-2": 50, "Ural-375": 10, "MiG-29S": 70,
           "Mystery-1": 5}
RINGS = {"Osa 9A33 ln": 10_000.0, "ZSU-23-4 Shilka": 2_500.0}
FNS = th.ThreatFns(weight=lambda n: WEIGHTS.get(n, 5), attrs=attrs, ring_m=lambda n: RINGS.get(n))


def facts(a):
    return [e.fact for e in a.evidence]


class Assess(unittest.TestCase):
    def test_order_and_who(self):
        tracks = [track("T0001", "Ural-375", 1),                       # a truck: not a threat
                  track("T0002", "T-72B", 2),
                  track("T0003", "Osa 9A33 ln", 12),                    # outside its 10 km ring
                  track("T0004", "ZSU-23-4 Shilka", 2),                 # inside its 2.5 km ring
                  track("T0005", "Osa 9A33 ln", 30, status=Status.STALE, reason="picture is 7 seconds old"),
                  track("T0006", "T-72B", 1, status=Status.GONE),
                  track("T0007", "T-72B", 1, status=Status.LOST),
                  track("T0008", "T-72B", 1, aff=Affiliation.FRIENDLY),
                  track("T0009", "T-72B", 1, aff=Affiliation.NEUTRAL)]
        out = th.assess(tracks, ME, None, FNS)
        self.assertEqual([a.track_id for a in out], ["T0004", "T0003", "T0002", "T0005"])
        self.assertEqual([a.priority for a in out], [1, 2, 3, 4])
        self.assertEqual((out[0].inside_ring, out[1].inside_ring), (True, False))
        self.assertEqual(out[3].freshness, Status.STALE)               # stale ranks after current ones
        self.assertIn("not current: picture is 7 seconds old", facts(out[3]))

    def test_capability_only_from_attributes_or_the_table(self):
        osa, = th.assess([track("T0001", "Osa 9A33 ln", 5)], ME, None, FNS)
        self.assertEqual(osa.category, "radar SAM")
        self.assertIn("DCS lists it as RADAR_BAND1_FOR_ARM, SAM TR, SR SAM", facts(osa))
        self.assertEqual(osa.evidence[0].basis, Basis.WORLD)
        self.assertIn("engages to about 10 km (approximate table)", facts(osa))
        self.assertEqual((osa.ring_m, osa.ring_basis), (10_000.0, Basis.INFERRED))
        self.assertIn("you are inside its ring", facts(osa))
        tank, = th.assess([track("T0002", "T-72B", 3)], ME, None, FNS)
        self.assertEqual(tank.category, "armour")
        self.assertIsNone(tank.ring_m)
        self.assertFalse(any("engages" in f for f in facts(tank)))      # no range claimed for a tank

    def test_unknown_type_says_so(self):
        fns = th.ThreatFns(weight=lambda n: 60)                         # name rules only, no attributes
        a, = th.assess([track("T0001", "T-62M", 3)], ME, None, fns)
        self.assertEqual(a.category, "armour")
        self.assertEqual(a.evidence[0].basis, Basis.INFERRED)
        self.assertIn("armour, by its name", facts(a))
        self.assertIn("capability unknown: DCS hasn't described this type", facts(a))

    def test_unknown_enemy_aircraft_is_still_a_threat(self):
        a, = th.assess([track("T0001", "Mystery-1", 40, cat=Category.AIR, alt=5000)], ME, None, FNS)
        self.assertEqual(a.category, "aircraft")
        self.assertIn("an aircraft, kind unknown", facts(a))
        self.assertEqual(th.assess([track("T0002", "Mystery-1", 4)], ME, None, FNS), ())   # on the ground: no

    def test_closing_only_from_a_measured_range_rate(self):
        coming = track("T0001", "MiG-29S", 40, cat=Category.AIR, vel=(-200.0, 0.0), alt=5000)
        going = track("T0002", "MiG-29S", 40, km_east=5, cat=Category.AIR, vel=(200.0, 0.0), alt=5000)
        unknown = track("T0003", "MiG-29S", 40, km_east=-5, cat=Category.AIR, alt=5000)
        out = {a.track_id: facts(a) for a in th.assess([coming, going, unknown], ME, None, FNS)}
        self.assertIn("closing at 200 m/s", out["T0001"])
        self.assertTrue(any(f.startswith("opening at") for f in out["T0002"]))
        self.assertFalse(any("closing" in f or "opening" in f for f in out["T0003"]))   # no velocity: nothing said
        a, = th.assess([coming], ME, (-200.0, 0.0), FNS)                 # flying alongside it: no closure
        self.assertFalse(any("closing" in f for f in facts(a)))

    def test_george_and_range_evidence(self):
        t = track("T0001", "T-72B", 3)
        from dataclasses import replace
        t = replace(t, george=Association(2, "T-72B", UNIQUE, "T0001", ("T0001",)))
        a, = th.assess([t], ME, None, FNS)
        self.assertIn("on George's list as #2", facts(a))
        self.assertIn("3.0 km from you", facts(a))
        self.assertIn("threat weight 60", facts(a))
        b, = th.assess([track("T0002", "T-72B", 3)], None, None, FNS)  # no own position: no range claims
        self.assertIsNone(b.range_m)
        self.assertFalse(any("km from you" in f for f in facts(b)))

    def test_every_assessment_has_evidence(self):
        tracks = [track(f"T{i:04d}", n, i) for i, n in enumerate(WEIGHTS, start=1)]
        for a in th.assess(tracks, ME, None, FNS):
            self.assertTrue(a.evidence)
            self.assertTrue(all(e.fact and e.source for e in a.evidence))

    def test_default_fns_use_the_program(self):
        from tests import fakes  # noqa: F401
        fns = th.default_fns()
        self.assertGreaterEqual(fns.weight("ZSU-23-4 Shilka"), 90)
        self.assertEqual(fns.ring_m("ZSU-23-4 Shilka"), 2500.0)


if __name__ == "__main__":
    unittest.main()
