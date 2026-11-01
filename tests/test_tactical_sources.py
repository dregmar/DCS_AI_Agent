"""Tactical picture step T1: Export.lua's world lists, George's Target List and the JTAC target as
Reports, with source health and coverage (design sections 2, 3 and 12)."""
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from core import state as st_mod
from tactical import sources as s
from tactical.clock import PictureClock
from tactical.models import Basis, Category, Relation

ME = (42.0, 42.0)
NORTH_1KM = (42.0 + 1000 / 111_320.0, 42.0)


def world(**over):
    snap = {
        "my_ll": ME, "t_model": 100.0, "scan_t": 99.5, "enemy_lists_age": 0.5, "telemetry_age": 0.05,
        "contacts": [{"id": 11, "type": "T-72B", "dist_m": 1000, "brg": 359, "lat": NORTH_1KM[0], "lon": NORTH_1KM[1]}],
        "enemy_ground": [("T-72B", 11, NORTH_1KM[0], NORTH_1KM[1], 310.0), ("BMP-2", 12, 42.05, 42.05)],
        "air_defence": [("Osa 9A33 ln", 13, 42.1, 42.1, 250.0)],
        "enemy_air": [("Su-25T", 14, 42.3, 42.3, 1500.0)],
        "olympus_units": [("Olympus-3", "Olympus-3-1", 21, 41.9, 41.9, 3000.0)],
        "olympus_enemy": [], "wing": [("Ford 2", 22, 42.001, 42.0)],
        "george_list": [], "george_list_age": float("inf"),
    }
    snap.update(over)
    return snap


def scan(snap, now=1000.0):
    return PictureClock().scan_time(snap, now)


class WorldLists(unittest.TestCase):
    def test_every_list(self):
        snap = world()
        reports, health = s.world_reports(snap, scan(snap))
        by_src = {}
        for r in reports:
            by_src.setdefault(r.source, []).append(r)
        self.assertEqual(sorted(by_src), ["world.ad", "world.air", "world.contacts", "world.gnd", "world.oly",
                                          "world.wing"])
        gnd = by_src["world.gnd"][0]
        self.assertEqual((gnd.key, gnd.type_name, gnd.category, gnd.basis), ("dcs:11", "T-72B", Category.GROUND,
                                                                               Basis.WORLD))
        self.assertAlmostEqual(gnd.position.alt_m, 310.0)
        self.assertEqual(gnd.seen_model, 99.5)
        self.assertAlmostEqual(gnd.seen_mono, 999.5)
        self.assertIn("coalition_unreported", gnd.flags)          # enemy or neutral, can't tell (I-T2)
        self.assertEqual(gnd.relation, Relation.OTHER)
        self.assertIn("no_altitude", by_src["world.gnd"][1].flags)
        self.assertIn("air_defence_by_name", by_src["world.ad"][0].flags)
        oly = by_src["world.oly"][0]
        self.assertEqual((oly.relation, oly.get("group"), oly.get("unit")), (Relation.OWN, "Olympus-3", "Olympus-3-1"))
        self.assertEqual(by_src["world.wing"][0].relation, Relation.OWN)
        self.assertNotIn("coalition_unreported", by_src["world.wing"][0].flags)
        self.assertTrue(health["world.olyx"].up)                   # sent, just empty
        self.assertEqual(health["world.olyx"].rows, 0)

    def test_same_object_in_several_lists_shares_a_key(self):
        snap = world()
        reports, _ = s.world_reports(snap, scan(snap))
        groups = s.by_key(reports)
        self.assertEqual(sorted(r.source for r in groups["dcs:11"]), ["world.contacts", "world.gnd"])

    def test_nearby_units_stay_apart(self):
        snap = world(enemy_ground=[("T-72B", 31, 42.0, 42.0), ("T-72B", 32, 42.00001, 42.0)])   # ~1 m apart
        reports, _ = s.world_reports(snap, scan(snap))
        self.assertEqual(len(s.by_key(r for r in reports if r.source == "world.gnd")), 2)

    def test_contacts_bearings(self):
        snap = world()
        reports, _ = s.world_reports(snap, scan(snap))
        c = next(r for r in reports if r.source == "world.contacts")
        self.assertEqual(c.category, Category.SURFACE)              # ground or naval: the list doesn't say
        self.assertEqual(c.get("brg_grid"), 359.0)                  # as Export.lua sent it (grid)
        self.assertAlmostEqual(c.get("brg_true"), 0.0, delta=0.1)   # from lat / lon (true)
        self.assertEqual(c.get("dist_m"), 1000.0)

    def test_bad_and_duplicate_rows_are_counted(self):
        snap = world(enemy_ground=[("T-72B", 11, 42.01, 42.0), ("T-72B", 11, 42.01, 42.0), ("x",), "junk",
                                   ("ZU-23", "abc", 42.02, 42.0), ("Ural", 15, 0, 0)])
        reports, health = s.world_reports(snap, scan(snap))
        h = health["world.gnd"]
        self.assertEqual((h.duplicates, h.bad_rows, h.rows), (1, 2, 3))
        gnd = [r for r in reports if r.source == "world.gnd"]
        self.assertIn("no_id", gnd[1].flags)
        self.assertIsNone(gnd[1].key)
        self.assertIn("no_position", gnd[2].flags)                  # 0, 0: no LatLongAlt
        self.assertIsNone(gnd[2].position)

    def test_coverage(self):
        far = [("T-72B", 100 + i, 42.0 + i * 0.002, 42.0) for i in range(40)]     # full list, farthest ~8.7 km
        snap = world(enemy_ground=far)
        _, health = s.world_reports(snap, scan(snap))
        h = health["world.gnd"]
        self.assertTrue(h.full)
        self.assertAlmostEqual(h.farthest_m, 39 * 0.002 * 111_320.0, delta=5)
        self.assertAlmostEqual(s.coverage_edge_m(h), h.farthest_m)  # full: the farthest row is the edge
        _, health = s.world_reports(world(), scan(world()))
        self.assertAlmostEqual(s.coverage_edge_m(health["world.gnd"]), 38_000.0)   # 95 % of 40 km
        self.assertEqual(s.coverage_edge_m(health["world.wing"]), float("inf"))     # no range, not full
        snap = world(enemy_air=[("Su-25T", 200 + i, 42.0, 42.0, 100.0) for i in range(20)], my_ll=None)
        _, health = s.world_reports(snap, scan(snap))
        self.assertIsNone(s.coverage_edge_m(health["world.air"]))  # full, no distances: can't judge
        del snap["enemy_air"]
        _, health = s.world_reports(snap, scan(snap))
        self.assertIsNone(s.coverage_edge_m(health["world.air"]))  # list down

    def test_no_scan_yet(self):
        snap = world(scan_t=-1.0, enemy_lists_age=float("inf"))
        reports, health = s.world_reports(snap, scan(snap))
        self.assertEqual(reports, [])
        self.assertFalse(health["world.gnd"].up)
        self.assertEqual(health["world.gnd"].note, "no world scan yet")

    def test_old_export_lua(self):
        snap = world(t_model=None, scan_t=None, enemy_lists_age=None, telemetry_age=0.2)
        reports, health = s.world_reports(snap, scan(snap))
        self.assertTrue(reports)
        self.assertTrue(all("age_unknown" in r.flags and not r.age_known for r in reports))
        self.assertAlmostEqual(reports[0].seen_mono, 999.8)          # receive time
        self.assertIn("old Export.lua", health["world.gnd"].note)

    def test_list_not_sent(self):
        snap = world()
        del snap["enemy_air"]
        _, health = s.world_reports(snap, scan(snap))
        self.assertFalse(health["world.air"].up)

    def test_no_own_position(self):
        snap = world(my_ll=None)
        reports, health = s.world_reports(snap, scan(snap))
        self.assertTrue(reports)
        self.assertIsNone(health["world.gnd"].farthest_m)


class GeorgeList(unittest.TestCase):
    def test_names_and_classes(self):
        snap = world(george_list=["T-72B", "ARMORED", "", "ZSU-23-4 Shilka", "Unknown"], george_list_age=3.0,
                     george_list_complete=True)
        reports, health = s.george_reports(snap, 1000.0, 0)
        self.assertEqual([r.get("list_index") for r in reports], [1, 2, 3, 4, 5])
        self.assertEqual([r.type_name for r in reports], ["T-72B", None, None, "ZSU-23-4 Shilka", None])
        self.assertEqual([r.klass for r in reports], [None, "ARMORED", None, None, None])
        self.assertTrue(all(r.key is None and r.position is None and r.basis == Basis.SENSOR for r in reports))
        self.assertAlmostEqual(reports[0].seen_mono, 997.0)
        self.assertTrue(health.up)
        self.assertEqual(health.rows, 5)

    def test_not_read_yet(self):
        reports, health = s.george_reports(world(), 1000.0, 0)
        self.assertEqual(reports, [])
        self.assertFalse(health.up)

    def test_classes_cover_the_state_store(self):
        self.assertTrue(st_mod._GENERIC_NAMES - {""} <= s.GEORGE_CLASSES)

    def test_store_snapshot_carries_the_list(self):
        store = st_mod.ApacheStateStore()
        store.update_telemetry({"t": 10.0, "scan_t": 9.5})
        store.set_george_list(["BMP-2", "GROUND"], complete=False)
        snap = store.snapshot()
        self.assertEqual(snap["scan_t"], 9.5)
        self.assertEqual(snap["george_list"], ["BMP-2", "GROUND"])
        self.assertLess(snap["george_list_age"], 1.0)
        reports, health = s.george_reports(snap, 1000.0, 0)
        self.assertEqual([r.type_name for r in reports], ["BMP-2", None])
        self.assertEqual(health.note, "end of list not reached")

    def test_store_without_scan_times(self):
        snap = st_mod.ApacheStateStore().snapshot()
        self.assertIsNone(snap["scan_t"])
        self.assertEqual(snap["george_list"], [])


class Jtac(unittest.TestCase):
    def test_pointer_not_a_sighting(self):
        r = s.jtac_report({"type": "BMP-1", "id": 41, "lat": 42.2, "lon": 42.3, "src": "enemy_ground"}, 50.0, 2)
        self.assertEqual((r.key, r.basis, r.generation), ("dcs:41", Basis.REPORTED, 2))
        self.assertIsNone(r.position)                              # frozen copy, not used as a position
        self.assertEqual((r.get("tag"), r.get("assigned_lat")), ("jtac_target", 42.2))

    def test_none(self):
        self.assertIsNone(s.jtac_report(None, 0.0, 0))
        self.assertIsNone(s.jtac_report({"type": "x", "id": -1}, 0.0, 0))


class Geometry(unittest.TestCase):
    def test_flat_earth(self):
        self.assertAlmostEqual(s.dist_m(ME, NORTH_1KM), 1000.0, delta=0.5)
        self.assertAlmostEqual(s.true_bearing(ME, NORTH_1KM), 0.0, delta=0.01)
        east = (42.0, 42.0 + 1000 / (111_320.0 * 0.7431))
        self.assertAlmostEqual(s.true_bearing(ME, east), 90.0, delta=0.1)
        self.assertAlmostEqual(s.dist_m(ME, east), 1000.0, delta=2)


if __name__ == "__main__":
    unittest.main()
