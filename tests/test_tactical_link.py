"""Tactical picture step T7: existing features read the picture through tactical/link.py - kill / BDA
(target_state), George's "Target destroyed", Overlord's "closing", the JTAC's moving target - and fall
back to their old rules whenever the picture can't say."""
import types
import unittest
from dataclasses import replace

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from tactical import link
from tactical.models import Category, Status
from tests.tactical_fixtures import ME, track
from tests.test_kneeboard_tactical import picture


class Stub:
    def __init__(self, pic):
        self.pic = pic

    def picture(self):
        return self.pic


class WithPicture(unittest.TestCase):
    def setUp(self):
        self.was = link._get()
        self.addCleanup(link.set_provider, self.was)
        pic = picture()
        extra = (track("T0060", "T-72B", 5, status=Status.GONE, reason="destroyed by George"),
                 track("T0061", "MiG-29S", 20, cat=Category.AIR, vel=(-250.0, 0.0), alt=3000.0),
                 track("T0062", "MiG-29S", 20, km_east=3, cat=Category.AIR, vel=(250.0, 0.0), alt=3000.0))
        self.pic = replace(pic, tracks=pic.tracks + extra)
        link.set_provider(Stub(self.pic))


class Link(WithPicture):
    def test_status(self):
        self.assertEqual(link.status("dcs:0042"), None)            # keys are "dcs:<id>" without zeros
        self.assertEqual(link.status("dcs:42"), "alive")
        self.assertEqual(link.status("dcs:60"), "dead")
        self.assertEqual(link.status("dcs:12"), "unknown")         # out of coverage: never dead
        self.assertIsNone(link.status("dcs:999"))                  # not in the picture: caller decides
        self.assertIsNone(link.status(None))

    def test_position_and_closure(self):
        lat, lon, alt = link.position("dcs:42")
        self.assertAlmostEqual(alt, 300.0)
        self.assertLess(link.closure("dcs:61", ME), -200)          # coming south at 250 m/s
        self.assertGreater(link.closure("dcs:62", ME), 200)
        self.assertIsNone(link.closure("dcs:42", ME))              # no measured velocity
        self.assertAlmostEqual(link.closure("dcs:61", ME, (-250.0, 0.0)), 0.0, places=3)   # flying with it

    def test_no_picture(self):
        link.set_provider(None)
        self.assertFalse(link.active())
        self.assertIsNone(link.status("dcs:42"))
        link.set_provider(Stub(None))
        self.assertFalse(link.active())

    def test_service_link_freshness(self):
        svc = types.SimpleNamespace(last_tick_mono=None, picture=lambda: self.pic)
        clock = [100.0]
        sl = link.ServiceLink(svc, max_age=5.0, clock=lambda: clock[0])
        self.assertIsNone(sl.picture())                             # never updated
        svc.last_tick_mono = 99.0
        self.assertIs(sl.picture(), self.pic)
        clock[0] = 105.5
        self.assertIsNone(sl.picture())                             # stopped updating


class TargetState(WithPicture):
    def test_picture_decides(self):
        from common.overlord import target_state
        snap = {"enemy_ground": [], "my_ll": ME, "enemy_lists_age": 0.5}   # absent from the lists ...
        self.assertEqual(target_state(snap, {"id": 42, "lat": 42.0, "lon": 42.0, "src": "enemy_ground"}),
                         "alive")                                   # ... but the picture still has it
        self.assertEqual(target_state(snap, {"id": 60, "lat": 42.0, "lon": 42.0, "src": "enemy_ground"}), "dead")
        self.assertEqual(target_state(snap, {"id": 12, "lat": 42.0, "lon": 42.0, "src": "enemy_ground"}),
                         "unknown")

    def test_old_rule_without_picture(self):
        from common.overlord import target_state
        link.set_provider(None)
        snap = {"enemy_ground": [], "my_ll": ME, "enemy_lists_age": 0.5}
        self.assertEqual(target_state(snap, {"id": 42, "lat": 42.01, "lon": 42.0, "src": "enemy_ground"}), "dead")


class Closing(WithPicture):
    def test_measured_only(self):
        from common.overlord import AirWarning
        c = AirWarning._closing
        snap = {"vel": None}
        self.assertEqual(c({"id": 61}, ME, snap), ", closing")
        self.assertEqual(c({"id": 62}, ME, snap), ", opening")
        self.assertEqual(c({"id": 42}, ME, snap), "")              # no measured velocity: nothing claimed
        self.assertEqual(c({"id": 61}, ME, {"vel": (-250.0, 0.0, 0.0)}), "")   # same speed and way as you
        link.set_provider(None)
        self.assertEqual(c({"id": 61}, ME, snap), ", closing")     # no picture: the old wording

    def test_in_the_warning(self):
        from common.overlord import AirWarning
        spoken = []
        from tests.tactical_fixtures import KM_LAT
        snap = {"my_ll": ME, "telemetry_age": 0.1, "vel": None, "aircraft": "AH-64D_BLK_II",
                "enemy_air": [("MiG-29S", 61, 42.0 + 20 * KM_LAT, 42.0, 3000.0)]}
        w = AirWarning(lambda: snap, spoken.append)
        w.check(now=100.0)                                          # 20 km: the pop-up call
        snap["enemy_air"] = [("MiG-29S", 61, 42.0 + 10 * KM_LAT, 42.0, 3000.0)]   # 10 km: inside the 15 km call
        said = w.check(now=104.0)
        self.assertTrue(any(s.endswith("of you, closing.") for s in said), said)


class JtacLive(WithPicture):
    def test_moving_target(self):
        from common.jtac import Jtac
        sent = []
        oly = types.SimpleNamespace(connect=lambda: True, command=lambda n, p: sent.append((n, p)) or (True, ""))
        j = Jtac(lambda: {"my_ll": ME, "aircraft": "FA-18C_hornet", "enemy_ground": [], "air_defence": []}, oly=oly)
        j.current = {"type": "T-72B", "id": 42, "lat": 1.0, "lon": 1.0, "src": "enemy_ground"}   # assigned earlier
        live = self.pic.track("T0042").position
        self.assertEqual(j.mark("mark target"), "Overlord, red smoke on the target.")
        self.assertAlmostEqual(sent[0][1]["location"]["lat"], live.lat)                        # where it is now
        mt = j.my_target()
        self.assertAlmostEqual((mt["lat"], mt["lon"]), (live.lat, live.lon))
        self.assertTrue(mt["jtac"])
        link.set_provider(None)
        self.assertEqual(j.my_target()["lat"], 1.0)               # no picture: the assigned copy


class EngagementKill(WithPicture):
    def monitor(self):
        from intelligence.engagement_monitor import EngagementMonitor
        said = []
        m = EngagementMonitor(said.append, clock=lambda: 1000.0)
        return m, said

    def snap(self, cid):
        return {"contacts": [], "engagement": [{"index": 1, "type": "T-72B", "contact_id": cid}],
                "engagement_total": 1}

    def test_gone_after_a_shot(self):
        m, said = self.monitor()
        m._last_shot = 990.0
        m._check_destroyed(self.snap(60), 1000.0)
        self.assertEqual(said, ["Target destroyed."])

    def test_no_shot_no_call(self):
        m, said = self.monitor()
        m._check_destroyed(self.snap(60), 1000.0)
        self.assertEqual(said, [])

    def test_still_in_the_picture(self):
        m, said = self.monitor()
        m._last_shot = 990.0
        m._last_seen[42] = 3000.0
        for _ in range(5):                                         # missing from contacts, alive in the picture
            m._check_destroyed(self.snap(42), 1000.0)
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
