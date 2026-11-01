"""Tactical picture step T2: the track table and its lifecycle (design sections 6, 7 and 12), driven
through the real clock and source adapters from snapshot-shaped dicts."""
import types
import unittest

from tactical import sources
from tactical.clock import PictureClock
from tactical.events import EventLog
from tactical.models import Affiliation, Basis, Category, Level, Relation, Report, Status
from tactical.tracks import KillHints, Thresholds, TrackTable

ME = (42.0, 42.0)
KM_LAT = 1000 / 111_320.0


def north(km):
    return 42.0 + km * KM_LAT


class World:
    """What Export.lua would list: {snapshot list key: {object id: row}}."""
    def __init__(self):
        self.t = 100.0
        self.me = ME
        self.lists = {k: {} for k in ("contacts", "enemy_ground", "air_defence", "enemy_air", "olympus_units",
                                      "olympus_enemy", "wing")}

    def gnd(self, oid, typ, km_north, alt=300.0):
        self.lists["enemy_ground"][oid] = (typ, oid, north(km_north), 42.0, alt)

    def snap(self, age=0.3, old_export=False):
        s = {"my_ll": self.me, "telemetry_age": 0.05,
             "t_model": None if old_export else self.t, "scan_t": None if old_export else self.t - age,
             "enemy_lists_age": None if old_export else age}
        for k, rows in self.lists.items():
            s[k] = list(rows.values())
        return s


class Rig:
    def __init__(self, world=None, th=None):
        self.w = world or World()
        self.clock = PictureClock()
        self.table = TrackTable(th or Thresholds())
        self.log = EventLog()
        self.now = 1000.0
        self.events = []
        self.jtac = None

    def step(self, dt=1.0, age=0.3, hints=None, same_scan=False, old_export=False):
        self.now += dt
        if not same_scan:
            self.w.t += dt
        snap = self.w.snap(age, old_export)
        st = self.clock.scan_time(snap, self.now)
        reports, health = sources.world_reports(snap, st)
        j = sources.jtac_report(self.jtac, self.now, st.generation)
        if j:
            reports.append(j)
        tr = self.table.ingest(reports, health, st, self.now, self.w.me, hints)
        pic = self.table.snapshot(self.now)
        self.events += self.log.add(tr, {t.track_id: t.display for t in pic.tracks})
        return tr

    def pic(self):
        return self.table.snapshot(self.now)

    def track(self, oid):
        tid = self.table.track_id_for(f"dcs:{oid}")
        return self.pic().track(tid) if tid else None

    def kinds(self):
        return [e.kind for e in self.events]


class Correlation(unittest.TestCase):
    def test_one_unit_in_three_lists_is_one_track(self):
        r = Rig()
        r.w.lists["contacts"][11] = {"id": 11, "type": "Osa 9A33 ln", "dist_m": 5000, "brg": 0,
                                     "lat": north(5), "lon": 42.0}
        r.w.gnd(11, "Osa 9A33 ln", 5, alt=250.0)
        r.w.lists["air_defence"][11] = ("Osa 9A33 ln", 11, north(5), 42.0, 250.0)
        r.step()
        p = r.pic()
        self.assertEqual(len(p.tracks), 1)
        t = p.tracks[0]
        self.assertEqual(sorted(m.source for m in t.sources), ["world.ad", "world.contacts", "world.gnd"])
        self.assertEqual(t.category, Category.GROUND)                 # gnd is more specific than contacts
        self.assertAlmostEqual(t.position.alt_m, 250.0)
        self.assertIn("air_defence", t.tags)
        self.assertEqual((t.exist_conf.level, t.ident_conf.level), (Level.HIGH, Level.HIGH))
        self.assertEqual(t.position.basis, Basis.WORLD)

    def test_nearby_units_are_never_merged(self):
        r = Rig()
        r.w.lists["enemy_ground"][31] = ("T-72B", 31, 42.0, 42.0, 300.0)
        r.w.lists["enemy_ground"][32] = ("T-72B", 32, 42.00001, 42.0, 300.0)      # about 1 m apart
        r.step()
        self.assertEqual(len(r.pic().tracks), 2)

    def test_newer_position_wins_and_gives_a_velocity(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.step()
        self.assertIsNone(r.track(12).velocity)                       # one fix: no velocity yet
        r.w.gnd(12, "BMP-2", 3.010)                                    # 10 m north in one model second
        r.step()
        t = r.track(12)
        self.assertAlmostEqual(t.position.lat, north(3.010))
        self.assertAlmostEqual(t.velocity.north_mps, 10.0, delta=0.2)
        self.assertAlmostEqual(t.velocity.east_mps, 0.0, delta=0.1)
        self.assertEqual(t.velocity.basis, Basis.INFERRED)

    def test_older_scan_never_moves_a_track_back(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.step()
        r.w.gnd(12, "BMP-2", 3.5)
        r.step()
        r.w.gnd(12, "BMP-2", 3.0)                                      # an out-of-order packet: a quarter
        r.step(dt=-0.25, age=1.3)                                      # second back, carrying the scan before
        self.assertAlmostEqual(r.track(12).position.lat, north(3.5))
        self.assertEqual(r.table.older_ignored, 1)

    def test_same_scan_twice_counts_nothing(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.step()
        del r.w.lists["enemy_ground"][12]
        for _ in range(5):
            r.step(dt=0.25, same_scan=True)                            # four packets per scan
        self.assertEqual(r.track(12).status, Status.ACTIVE)            # same scan: no absence counted

    def test_affiliations(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.w.lists["olympus_units"][21] = ("Olympus-3", "Olympus-3-1", 21, north(1), 42.0, 3000.0)
        r.w.lists["olympus_enemy"][22] = ("Olympus-7", "Olympus-7-1", 22, north(2), 42.0, 200.0)
        r.w.lists["wing"][23] = ("Ford 2", 23, north(0.1), 42.0)
        r.step()
        gnd, oly, olyx, wing = r.track(12), r.track(21), r.track(22), r.track(23)
        self.assertEqual(gnd.affiliation, Affiliation.HOSTILE)
        self.assertIn("coalition_unreported", gnd.flags)              # enemy or neutral, until I-T2
        self.assertEqual((oly.affiliation, oly.display), (Affiliation.FRIENDLY, "Olympus-3-1"))
        self.assertIn("olympus_flight", oly.tags)
        self.assertEqual(olyx.affiliation, Affiliation.HOSTILE)
        self.assertIn("training", olyx.tags)
        self.assertEqual((wing.affiliation, wing.category), (Affiliation.FRIENDLY, Category.AIR))
        self.assertEqual(oly.ident_conf.level, Level.LOW)              # Olympus lists don't say the type

    def test_neutral_when_coalition_reported(self):
        from tactical.clock import ScanTime
        from tactical.models import Position
        st = ScanTime("new", 10.0, 100.0, 0.2, True, 0, False)
        rep = Report("world.gnd", Basis.WORLD, "dcs:5", Relation.OTHER, Category.GROUND, "Ural-375", None, 0,
                     Position(42.0, 42.0, 10.0, Basis.WORLD, 10.0, 100.0), 10.0, 100.0, True, 0)
        table = TrackTable()
        table.ingest([rep], {}, st, 100.0, ME)
        t = table.snapshot(100.0).tracks[0]
        self.assertEqual(t.affiliation, Affiliation.NEUTRAL)
        self.assertNotIn("coalition_unreported", t.flags)

    def test_jtac_tag_follows_the_unit(self):
        r = Rig()
        r.w.gnd(41, "BMP-1", 6.0)
        r.jtac = {"type": "BMP-1", "id": 41, "lat": north(6.0), "lon": 42.0, "src": "enemy_ground"}
        r.step()
        r.w.gnd(41, "BMP-1", 6.2)                                      # it drives off
        r.step()
        t = r.track(41)
        self.assertIn("jtac_target", t.tags)
        self.assertAlmostEqual(t.position.lat, north(6.2))             # the world's position, not the frozen copy
        r.jtac = None
        r.step()
        self.assertNotIn("jtac_target", r.track(41).tags)


class Lifecycle(unittest.TestCase):
    def test_gone_after_three_fresh_scans(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.step()
        del r.w.lists["enemy_ground"][12]
        r.step()
        r.step()
        self.assertEqual(r.track(12).status, Status.ACTIVE)            # one missing row is not a kill
        r.step()
        t = r.track(12)
        self.assertEqual(t.status, Status.GONE)
        self.assertEqual(t.status_reason, "no longer in the picture, cause unknown")
        self.assertEqual(r.events[-1].text, "BMP-2 (T0001) no longer in the picture.")
        self.assertNotIn("destroyed", r.events[-1].text)

    def _vanish(self, hints):
        r = Rig()
        r.w.gnd(12, "T-72B", 3.0)
        r.step()
        del r.w.lists["enemy_ground"][12]
        for _ in range(3):
            r.step(hints=hints(r))
        return r

    def test_destroyed_by_george_needs_a_recent_shot(self):
        r = self._vanish(lambda r: KillHints(last_shot_mono=r.now - 10, targeted={"dcs:12": "George"}))
        self.assertEqual(r.track(12).status_reason, "destroyed by George")
        self.assertEqual(r.events[-1].kind, "destroyed")
        self.assertEqual(r.events[-1].text, "T-72B (T0001) destroyed by George.")
        r = self._vanish(lambda r: KillHints(last_shot_mono=r.now - 120, targeted={"dcs:12": "George"}))
        self.assertEqual(r.events[-1].kind, "gone")                    # shot too long ago
        r = self._vanish(lambda r: KillHints(last_shot_mono=r.now - 10))
        self.assertEqual(r.events[-1].kind, "gone")                    # a shot, but not at this unit

    def test_destroyed_by_an_ai_flight_or_dcs(self):
        r = self._vanish(lambda r: KillHints(targeted={"dcs:12": "Outcast 2"}))
        self.assertEqual(r.track(12).status_reason, "destroyed by Outcast 2")
        r = self._vanish(lambda r: KillHints(dcs_events={"dcs:12": "unit dead"}))
        self.assertEqual(r.track(12).status_reason, "destroyed: DCS reports unit dead")

    def test_out_of_coverage_lost_and_dropped_keeps_its_id(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 39.5)                                     # inside 40 km, beyond 95 % (38 km)
        r.step()
        tid = r.track(12).track_id
        del r.w.lists["enemy_ground"][12]
        for _ in range(5):
            r.step()
        self.assertEqual(r.track(12).status, Status.OUT_OF_COVERAGE)   # not gone: it may just be out of range
        self.assertEqual(r.track(12).exist_conf.level, Level.MEDIUM)
        r.step(dt=120.0)
        self.assertEqual(r.track(12).status, Status.LOST)
        self.assertEqual(r.events[-1].kind, "lost")
        r.step(dt=601.0)
        self.assertIsNone(r.track(12))                                 # dropped after 10 min
        r.w.gnd(12, "BMP-2", 30.0)
        r.step()
        self.assertEqual(r.track(12).track_id, tid)                    # same number when it comes back
        self.assertEqual(r.track(12).status, Status.ACTIVE)

    def test_reappears_from_lost(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 39.5)
        r.step()
        del r.w.lists["enemy_ground"][12]
        r.step()
        r.step(dt=121.0)
        r.w.gnd(12, "BMP-2", 37.0)
        r.step()
        self.assertEqual(r.events[-1].text, "BMP-2 (T0001) back in the picture.")

    def test_full_list_cuts_off_far_units(self):
        r = Rig()
        for i in range(40):
            r.w.gnd(100 + i, "Ural-375", 1 + i * 0.2)                  # 1 .. 8.8 km
        r.w.gnd(99, "T-72B", 9.5)
        r.step()
        del r.w.lists["enemy_ground"][99]                              # Export would have cut it (41 > 40)
        for _ in range(4):
            r.step()
        self.assertEqual(r.track(99).status, Status.OUT_OF_COVERAGE)

    def test_air_is_lost_sooner(self):
        r = Rig()
        r.w.lists["enemy_air"][14] = ("Su-25T", 14, north(119), 42.0, 1500.0)
        r.step()
        del r.w.lists["enemy_air"][14]
        r.step()
        r.step(dt=31.0)
        self.assertEqual(r.track(14).status, Status.LOST)

    def test_stale_picture(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.w.gnd(13, "T-72B", 4.0)
        r.step()
        del r.w.lists["enemy_ground"][12]
        for _ in range(5):
            r.step(age=6.0)                                            # world scan failing
        self.assertTrue(all(t.status == Status.STALE for t in r.pic().tracks))   # nothing called gone
        self.assertEqual(r.kinds().count("stale"), 1)                  # one line for the picture
        self.assertEqual(r.track(13).status_reason, "picture is 6 seconds old")
        self.assertEqual(r.track(13).exist_conf.level, Level.MEDIUM)
        r.step()
        self.assertEqual(r.kinds()[-1], "fresh")
        self.assertEqual(r.track(13).status, Status.ACTIVE)
        self.assertEqual(r.track(12).status, Status.STALE)
        r.step()
        r.step()
        self.assertEqual(r.track(12).status, Status.GONE)              # three fresh scans without it

    def test_old_export_lua_never_declares_gone(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.step(old_export=True)
        self.assertIn("age_unknown", r.track(12).flags)
        del r.w.lists["enemy_ground"][12]
        for _ in range(10):
            r.step(old_export=True)
        t = r.track(12)
        self.assertEqual(t.status, Status.OUT_OF_COVERAGE)
        self.assertIn("ages unknown", t.status_reason)

    def test_no_scan_yet(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        snap = r.w.snap()
        snap.update(scan_t=-1.0, enemy_lists_age=float("inf"))
        st = r.clock.scan_time(snap, r.now)
        rep, health = sources.world_reports(snap, st)
        self.assertEqual(r.table.ingest(rep, health, st, r.now, ME), [])
        self.assertEqual(r.pic().tracks, ())


class Ids(unittest.TestCase):
    def test_stable_over_a_thousand_updates(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.w.gnd(13, "T-72B", 4.0)
        r.step()
        ids = (r.track(12).track_id, r.track(13).track_id)
        for i in range(1000):
            r.w.gnd(12, "BMP-2", 3.0 + (i % 7) * 0.001)
            r.step()
        self.assertEqual((r.track(12).track_id, r.track(13).track_id), ids)
        r.w.gnd(14, "ZU-23", 5.0)
        r.step()
        self.assertEqual(r.track(14).track_id, "T0003")

    def test_new_mission_resets(self):
        r = Rig()
        r.w.gnd(12, "BMP-2", 3.0)
        r.w.gnd(13, "T-72B", 4.0)
        r.step()
        r.w.t = 1.0                                                    # mission restarted: model time back to 0
        r.w.lists["enemy_ground"] = {}
        r.w.gnd(7, "Ural-375", 2.0)
        r.step()
        self.assertEqual([t.track_id for t in r.pic().tracks], ["T0001"])
        self.assertEqual(r.track(7).generation, 1)
        self.assertIn("new_picture", r.kinds())


class Settings(unittest.TestCase):
    def test_from_config(self):
        cfg = types.SimpleNamespace(TACTICAL_STALE_S=8, TACTICAL_GONE_SCANS=5.0, TACTICAL_LOST_AIR_S="x")
        th = Thresholds.from_config(cfg)
        self.assertEqual((th.stale_s, th.gone_scans, th.lost_air_s), (8.0, 5, 30.0))
        self.assertIsInstance(th.gone_scans, int)

    def test_history_is_bounded(self):
        r = Rig(th=Thresholds(history_max=5))
        for i in range(10):
            r.w.gnd(200 + i, "Ural-375", 39.5)                         # out of coverage once removed
        r.step()
        r.w.lists["enemy_ground"] = {}
        r.step()
        self.assertEqual(len(r.pic().tracks), 5)


if __name__ == "__main__":
    unittest.main()
