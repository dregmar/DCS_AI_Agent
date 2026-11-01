"""Tactical picture step T1: the typed records and the confidence rules (design section 5)."""
import dataclasses
import unittest

from tactical import models as m
from tactical.models import Basis, Level, Status


def pos(lat=42.0, lon=42.0):
    return m.Position(lat, lon, 300.0, Basis.WORLD, 100.0, 5.0)


class Keys(unittest.TestCase):
    def test_dcs_key(self):
        self.assertEqual(m.dcs_key(16777472), "dcs:16777472")
        self.assertEqual(m.dcs_key(12.0), "dcs:12")
        self.assertEqual(m.dcs_key("7"), "dcs:7")
        for bad in (-1, None, "x", 1.5, True, float("nan"), float("inf")):
            self.assertIsNone(m.dcs_key(bad), bad)


class Records(unittest.TestCase):
    def test_frozen(self):
        p = pos()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.lat = 1.0

    def test_position_rejects_missing_latlong(self):
        for lat, lon in ((0, 0), (float("nan"), 1), (91, 0), (None, 3)):
            with self.assertRaises(ValueError):
                m.Position(lat, lon, None, Basis.WORLD, None, 0.0)
        self.assertTrue(m.valid_ll(0.0, 41.5))         # on the equator is fine; only 0, 0 is "missing"

    def test_report_extra(self):
        r = m.Report("world.contacts", Basis.WORLD, "dcs:1", m.Relation.OTHER, m.Category.SURFACE, "T-72B",
                     None, None, pos(), 100.0, 5.0, True, 0, extra=(("brg_grid", 45.0), ("dist_m", 900.0)))
        self.assertEqual(r.get("dist_m"), 900.0)
        self.assertIsNone(r.get("nothing"))
        self.assertEqual(r.get("nothing", 3), 3)

    def test_conf_needs_reason(self):
        with self.assertRaises(ValueError):
            m.Conf(Level.HIGH, " ")
        with self.assertRaises(TypeError):
            m.Conf("high", "x")

    def test_association_rules(self):
        m.Association(1, "T-72B", m.UNIQUE, "T0001", ("T0001",))
        m.Association(2, "T-72B", m.BEST_OF_N, "T0002", ("T0002", "T0003"))
        m.Association(3, "ARMORED", m.CLASS_ONLY, None, ("T0004", "T0005"))
        m.Association(4, "BMP-2", m.NONE, None)
        with self.assertRaises(ValueError):
            m.Association(1, "T-72B", m.UNIQUE, None)           # unique needs its track
        with self.assertRaises(ValueError):
            m.Association(1, "BMP-2", m.NONE, "T0001")          # unassociated has none
        with self.assertRaises(ValueError):
            m.Association(1, "T-72B", m.BEST_OF_N, "T0009", ("T0002", "T0003"))
        with self.assertRaises(ValueError):
            m.Association(1, "T-72B", "maybe", None)

    def test_threat_needs_evidence(self):
        ev = (m.Evidence("DCS attribute 'MR SAM'", Basis.WORLD, "unit_types"),)
        m.ThreatAssessment("T0001", "radar SAM", 100, 25_000.0, Basis.INFERRED, 9000.0, True, 1, ev, Status.ACTIVE)
        with self.assertRaises(ValueError):
            m.ThreatAssessment("T0001", "radar SAM", 100, None, None, 9000.0, None, 1, (), Status.ACTIVE)
        with self.assertRaises(ValueError):                     # a ring says where it came from
            m.ThreatAssessment("T0001", "radar SAM", 100, 25_000.0, None, 9000.0, True, 1, ev, Status.ACTIVE)

    def test_snapshot_lookup(self):
        t = m.Track("T0001", "dcs:1", 0, m.Category.GROUND, m.Affiliation.HOSTILE, "T-72B", "T-72B", pos(), None,
                    1.0, 5.0, 5.0, (m.SourceMark("world.gnd", Basis.WORLD, 1.0, 5.0, 5),),
                    m.existence_conf(Status.ACTIVE), m.identity_conf(Basis.WORLD), Status.ACTIVE, "in the scan")
        snap = m.PictureSnapshot(0, 5.0, 0.5, (t,))
        self.assertIs(snap.track("T0001"), t)
        self.assertIsNone(snap.track("T0002"))
        self.assertEqual(t.basis_of_position(), Basis.WORLD)


class Confidence(unittest.TestCase):
    """Each row of the design's confidence table."""
    def test_existence(self):
        self.assertEqual(m.existence_conf(Status.ACTIVE).level, Level.HIGH)
        self.assertEqual(m.existence_conf(Status.STALE).level, Level.MEDIUM)
        self.assertEqual(m.existence_conf(Status.OUT_OF_COVERAGE).level, Level.MEDIUM)
        self.assertEqual(m.existence_conf(Status.LOST).level, Level.LOW)
        self.assertEqual(m.existence_conf(Status.GONE).level, Level.LOW)
        self.assertEqual(m.existence_conf(Status.ACTIVE, Basis.SENSOR).level, Level.LOW)   # George, no track

    def test_identity(self):
        self.assertEqual(m.identity_conf(Basis.WORLD).level, Level.HIGH)
        self.assertEqual(m.identity_conf(Basis.SENSOR, m.UNIQUE).level, Level.HIGH)
        self.assertEqual(m.identity_conf(Basis.SENSOR, m.BEST_OF_N).level, Level.MEDIUM)
        self.assertEqual(m.identity_conf(Basis.SENSOR, m.CLASS_ONLY).level, Level.LOW)

    def test_every_conf_has_a_reason(self):
        for s in Status:
            for b in Basis:
                self.assertTrue(m.existence_conf(s, b).reason)
        for b in Basis:
            for st in m.STRENGTHS + (None,):
                self.assertTrue(m.identity_conf(b, st).reason)


if __name__ == "__main__":
    unittest.main()
