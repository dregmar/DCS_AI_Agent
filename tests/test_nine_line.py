"""5 Nov: Darkside's CAS 9-line (common/nine_line.py). Andre's choices: no readback by default
(NINE_LINE_READBACK to require one); IP = a mission waypoint named IP or Alpha, else computed; to you by
default (NINE_LINE_AI_TO_AI briefs the Hog flights too). Export.lua sends the magnetic variation, the comms hook
your route's waypoint names."""
import os
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from common import nine_line, olympus_orders
from common.darkside import Darkside
from common.jtac import Jtac
from tactical import link
from tests.test_darkside_laser import KM, FakeHook, FakeOly, ground
from tests.test_jtac import ME, snap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class World(unittest.TestCase):
    def setUp(self):
        was = link._get()
        link.set_provider(None)
        self.addCleanup(link.set_provider, was)
        for name in ("READBACK", "AI_TO_AI"):
            self.addCleanup(setattr, nine_line, name, getattr(nine_line, name))
        self.s = snap()
        self.s.update(pos=(0.0, 0.0, 300.0), friendly_units=[], enemy_lists_age=0.5)
        self.oly, self.hook = FakeOly(), FakeHook()
        self.ex = olympus_orders.Executor(self.oly, lambda: self.s)
        self.j = Jtac(lambda: self.s, oly=self.oly, laser=self.ex)
        self.j.sight = self.hook.svc
        self.spoken, self.ai = [], []
        self.d = Darkside(self.j, lambda: self.s, self.spoken.append, callsign=lambda: "Springfield 1-1",
                          oly=self.oly, say_ai=lambda t, who: self.ai.append((who, t)))
        self.j.extra_wanted = self.d.nine_wanted
        self.addCleanup(self.ex.lase_stop, [{"id": 700}])
        self.hook.jtacs()                                         # the hook's list: nobody yet


class Lines(World):
    def test_nine_line_from_a_fresh_target(self):
        said = self.d.handle("request nine line")
        self.assertTrue(said.startswith("Springfield 1-1, Darkside, nine-line. One, IP "), said)
        for n in ("One,", "Two,", "Three,", "Four,", "Five,", "Six,", "Seven,", "Eight,", "Nine,"):
            self.assertIn(n, said)
        tgt = self.j.current
        self.assertEqual(tgt["id"], 2)                             # the Shilka, as the talk-on would pick
        self.assertIn("Four, 1,040 feet.", said)
        self.assertIn("Five, single Shilka.", said)
        self.assertIn("Six, Grid 40 Romeo, ", said)               # the A-10 profile: MGRS, as its talk-on
        self.assertIn("Seven, red smoke.", said)                  # no JTAC sees it: smoke (Olympus up)
        self.assertIn("Eight, no friendlies within 3 kilometres.", said)
        self.assertIn(" true.", said)                             # no magnetic variation from Export
        self.assertIn("Remarks: Threats: SA-8", said)
        self.assertTrue(said.endswith("Call IN with direction."))

    def test_magnetic_named_ip_laser_friendlies(self):
        self.d.handle("request target")
        t = self.j.current
        north_m = (t["lat"] - ME[0]) * 111_320.0 - 6000.0         # 6 km south of the target
        self.s["route_names"] = [(3, north_m, 0.0, "IP")]
        self.s["magvar"] = 6.0
        self.s["friendly_units"] = [("M-1 Abrams", 9, t["lat"] - 2 * KM, t["lon"], 100.0, "g")]
        self.hook.jtacs(ground("JTAC Hammer-1", t["lat"] - 3 * KM, t["lon"]))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        said = self.d.handle("nine-line")
        self.assertIn("One, IP, waypoint 3.", said)
        self.assertIn("Two, 354 magnetic.", said)                 # north (000 true) - 6 east
        self.assertIn("Three, 3.2 miles.", said)
        self.assertIn("Seven, laser, code 1 6 8 8, from JTAC Hammer-1.", said)
        self.assertIn("Eight, friendly M-1 Abrams, south, 2000 metres.", said)
        self.assertIn("Nine, egress", said)
        self.s["friendly_units"] = [("M-1 Abrams", 9, t["lat"] - 0.5 * KM, t["lon"], 100.0, "g")]
        self.assertIn("Remarks: Danger close.", self.d.handle("nine-line"))

    def test_far_named_ip_not_used(self):
        self.d.handle("request target")
        self.s["route_names"] = [(1, 900_000.0, 0.0, "Alpha")]
        said = self.d.handle("nine line")
        self.assertIn("One, IP ", said)
        self.assertIn("4.9 miles south west of the target, Grid 40 Romeo", said)   # computed (9 km), with its grid


class InCall(World):
    def test_in_lays_the_mark_and_clears_hot(self):
        self.assertIn("no nine-line on this target", self.d.handle("in from the south"))
        self.d.handle("request target")
        t = self.j.current
        self.hook.jtacs(ground("JTAC Hammer-1", t["lat"] - 3 * KM, t["lon"]))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        self.d.handle("request nine line")
        said = self.d.handle("Springfield 1-1, in from the south.")
        self.assertEqual(said, "Springfield 1-1, Darkside, laser on. Cleared hot.")
        self.assertIn("fireLaser", self.oly.names())

    def test_smoke_then_abort_when_dead(self):
        self.d.handle("request nine line")
        self.assertEqual(self.d.handle("in hot"), "Springfield 1-1, Darkside, smoke out. Cleared hot.")
        self.assertIn("smoke", self.oly.names())
        self.s["enemy_ground"] = [r for r in self.s["enemy_ground"] if r[1] != 2]
        self.assertIn("abort, the Shilka is destroyed", self.d.handle("in from the north"))

    def test_no_strike_zone(self):
        self.d.handle("request target")
        t = self.j.current
        self.d.warlord = type("W", (), {"block": {"no_strike": [{"name": "Gudauta town", "lat": t["lat"],
                                                                  "lon": t["lon"], "radius_m": 1500.0}]}})()
        said = self.d.handle("nine line")
        self.assertIn("Target is inside the Gudauta town no-strike zone, not cleared.", said)
        self.assertEqual(self.d.handle("in from the south"),
                         "Springfield 1-1, Darkside, abort, the target is inside the Gudauta town no-strike zone.")


class Readback(World):
    def test_required_when_set(self):
        nine_line.READBACK = True
        said = self.d.handle("request nine line")
        self.assertTrue(said.endswith("Read back lines 4, 6 and restrictions."))
        self.assertIn("read back lines 4, 6 and restrictions first", self.d.handle("in from the south"))
        self.assertIn("negative, line 4 is 1,040 feet", self.d.handle("readback 1200 feet"))
        nine = self.d._nine[1]
        good = f"readback {nine.elevation_ft} feet, {' '.join(nine.location_digits)}"
        self.assertEqual(self.d.handle(good), "Springfield 1-1, Darkside, readback correct. Call IN with direction.")
        self.assertIn("Cleared hot", self.d.handle("in from the south"))

    def test_off_by_default(self):
        self.assertFalse(nine_line.READBACK)
        self.d.handle("request nine line")
        self.assertFalse(self.d.nine_wanted("readback 1040"))     # no readback due: not his


class AiToAi(World):
    def test_hog_briefed_only_when_set(self):
        self.d.handle("request target")
        t = dict(self.j.current)
        m = type("M", (), {"cs": "Hog", "kind": type("K", (), {"desc": "two A-10s"})(), "targets": [t],
                           "ip": (t["lat"] - 9 * KM, t["lon"]), "task_id": "K1"})()
        self.d.check_in(m)
        self.assertEqual(self.ai, [])
        nine_line.AI_TO_AI = True
        self.d.check_in(m)
        self.assertEqual([w for w, _ in self.ai], ["darkside", "george"])
        self.assertTrue(self.ai[0][1].startswith("Hog 1, Darkside, nine-line. One, IP 4.9 miles south"), self.ai[0][1])
        self.assertTrue(self.ai[1][1].startswith("Darkside, Hog 1, 1,040 feet, Grid 40 Romeo"))


class Plumbing(unittest.TestCase):
    def test_hook_export_state(self):
        from common.comms_menu import parse_wpnames
        self.assertEqual(parse_wpnames("WPNAMES\n3\t1000\t2000\tIP\nbad"), [(3, 1000.0, 2000.0, "IP")])
        self.assertEqual(parse_wpnames("WPNAMES"), [])
        from core.state import ApacheStateStore
        st = ApacheStateStore()
        st.set_route_names([(3, 1000.0, 2000.0, "IP")])
        self.assertEqual(st.snapshot()["route_names"], [(3, 1000.0, 2000.0, "IP")])
        with open(os.path.join(ROOT, "dcs_scripts", "george_comms_hook.lua"), encoding="utf-8") as f:
            hook = f.read()
        self.assertIn('gch.wpnames_text = "WPNAMES"', hook)
        self.assertIn('string.format("%d\\t%.0f\\t%.0f\\t%s", i - 1, p.x, p.y, nm)', hook)
        with open(os.path.join(ROOT, "dcs_scripts", "Export.lua"), encoding="utf-8") as f:
            exp = f.read()
        self.assertIn('"magvar":%s}', exp)
        self.assertIn('george_num(magvar, "%.1f"))', exp)

    def test_main(self):
        import main
        if main.DARKSIDE is None:
            self.skipTest("command roles off")
        self.assertTrue(main.JTAC.wanted("request nine line"))
        self.assertFalse(nine_line.READBACK)
        self.assertFalse(nine_line.AI_TO_AI)
        self.assertIn("nine-line", main.stt_prompt())


if __name__ == "__main__":
    unittest.main()


class KneeboardIp(World):
    """5 Nov: the 9-line's IP (with its run-in to the target) and the Hog flights' IPs on the map."""
    def test_points_and_labels(self):
        self.assertEqual(self.d.ip_points(), [])
        self.d.handle("nine line")
        pts = self.d.ip_points()
        self.assertEqual(len(pts), 1)
        p = pts[0]
        self.assertEqual((p["kind"], p["label"]), ("nine", "IP (computed)"))
        self.assertEqual((p["tlat"], p["tlon"]), (self.j.current["lat"], self.j.current["lon"]))
        t = self.j.current
        self.s["route_names"] = [(3, (t["lat"] - ME[0]) * 111_320.0 - 6000.0, 0.0, "IP")]
        self.d.handle("nine line")
        self.assertEqual(self.d.ip_points()[0]["label"], "IP · wp 3")
        self.s["route_names"] = [(4, (t["lat"] - ME[0]) * 111_320.0 - 6000.0, 0.0, "Alpha")]
        self.d.handle("nine line")
        self.assertEqual(self.d.ip_points()[0]["label"], "IP Alpha · wp 4")
        m = type("M", (), {"cs": "Hog", "task_id": "K1", "ip": (t["lat"] - 9 * KM, t["lon"]), "lead_id": None})()
        self.d.register(m)
        self.assertEqual([x["label"] for x in self.d.ip_points()], ["IP Alpha · wp 4", "Hog IP"])
        self.d.unregister(m)
        self.d.handle("request next target")                       # a new target: the old 9-line's IP goes
        self.assertEqual(self.d.ip_points(), [])

    def test_feed_and_page(self):
        from kneeboard import model
        from tests.test_kneeboard_tactical import SNAP, Ctx
        ctx = Ctx(None)
        ctx.ips = lambda snap: [{"lat": 42.0, "lon": 42.0, "label": "IP · wp 3", "kind": "nine",
                                 "tlat": 42.05, "tlon": 42.0}]
        self.assertEqual(model.build_map(SNAP, ctx)["ips"][0]["label"], "IP · wp 3")
        self.assertEqual(model.build_map(SNAP, Ctx(None))["ips"], [])            # a context without IPs
        with open(os.path.join(ROOT, "kneeboard", "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('["ip", "IP and run-in"]', html)
        self.assertIn("for (const p of (M.ips || []))", html)
        self.assertIn('<i>△</i>IP</span>', html)
        import main
        self.assertIsInstance(main._KneeboardContext.ips({}), list)
