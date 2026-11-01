"""22 Oct features: named waypoints, airfields, pop-up warnings, target N, debrief."""
import os
import tempfile
import unittest
from unittest import mock

from common import airfields as af, debrief as db, overlord as ov, user_waypoints as uw, position
from common.olympus_orders import Executor, parse

ME = (41.9033, 43.4590)
SNAP = {"my_ll": ME, "telemetry_age": 0.1, "hdg": 0.0, "vel": (40.0, 0.0, 0.0), "pos": (0.0, 0.0, 0.0),
        "route": [(0, 0.0, 0.0), (3, 8000.0, 2000.0)], "coalition": 2}


def north(km, base=ME):
    return (base[0] + km * 1000 / 111_320.0, base[1])


class TestWaypoints(unittest.TestCase):
    def setUp(self):
        self.w = uw.UserWaypoints(os.path.join(tempfile.mkdtemp(), "wp.json"))

    def test_mark_grid_objective_and_nav(self):
        self.assertEqual(self.w.handle("Mark our current position as waypoint Alpha", SNAP),
                         "Waypoint Alpha marked. Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8.")
        said = self.w.handle("Set grid location 38T LM 7218 4520 as waypoint Bravo", SNAP)
        self.assertTrue(said.startswith("Waypoint Bravo set, grid 38 Tango, Lima Mike, 7 2 1 8, 4 5 2 0."), said)
        self.assertIn("steer 359, 2.7 miles", self.w.handle("where is Bravo?", SNAP))   # 5 km north
        self.assertIn("Waypoint Charlie set", self.w.handle("set grid 721 452 as Charlie", SNAP))
        self.assertIn("No objective set", self.w.handle("how far to the objective", SNAP))
        self.assertIn("Objective is waypoint 3", self.w.handle("the objective is waypoint 3", SNAP))
        self.assertIn("Objective, waypoint 3, 12 o'clock, steer 014", self.w.handle("bearing to the objective", SNAP))
        self.assertEqual(self.w.handle("list waypoints", SNAP), "Waypoints: Alpha, Bravo, Charlie, Objective.")
        self.assertEqual(self.w.handle("delete waypoint Alpha", SNAP), "Waypoint Alpha deleted.")
        self.assertIsNone(self.w.handle("where is waypoint 2", SNAP))           # numbered: nav_commands
        self.assertIsNone(self.w.handle("what is our grid", SNAP))
        again = uw.UserWaypoints(self.w.store_file)                              # remembered
        self.assertEqual(again.names(), ["Bravo", "Charlie", "Objective"])

    def test_spoken_grid(self):
        said = self.w.handle("set grid thirty eight tango lima mike seven two one eight four five two zero "
                             "as waypoint Delta", SNAP)
        self.assertTrue(said.startswith("Waypoint Delta set, grid 38 Tango, Lima Mike, 7 2 1 8, 4 5 2 0."), said)
        self.assertEqual(self.w.handle("set grid as waypoint Echo", SNAP), "Say again the grid for Echo.")

    def test_overlord_and_outcast_use_them(self):
        self.w.set("Alpha", {"ll": list(north(5)), "grid": ""})
        with mock.patch.object(uw, "USER_WAYPOINTS", self.w):
            point, where = ov.parse_where("cas on waypoint alpha", SNAP)
            self.assertEqual((point, where), (north(5), "waypoint Alpha"))
            self.assertEqual(ov.parse_where("cas on waypoint bravo", SNAP)[1], "there's no waypoint Bravo")
            self.assertEqual(parse("go to waypoint alpha"), ("goto_named", {"name": "Alpha"}))
            sent = []
            oly = type("O", (), {"command": lambda self, n, p: (sent.append((n, p)), (True, ""))[1]})()
            ex = Executor(oly, lambda: SNAP)
            self.assertEqual(ex.run("goto_named", {"name": "Alpha"}, [{"n": 2, "id": 9}], "Outcast 2"), "")
            self.assertEqual(sent[0], ("setPath", {"ID": 9, "path": [{"lat": north(5)[0], "lng": ME[1]}]}))


class TestAirfields(unittest.TestCase):
    MSG = ("AIRBASES\nKutaisi\t2\t0\t42.1764\t42.4826\nSenaki-Kolkhi\t1\t0\t42.2386\t42.0612\n"
           "FARP London\t2\t1\t41.95\t43.50\nStennis\t2\t2\t41.5\t41.0\nNeutral Field\t0\t0\t41.91\t43.47\n")

    def setUp(self):
        self.a = af.Airfields()
        self.a.on_airbases_message(self.MSG)

    def test_nearest(self):
        said = self.a.handle("nearest friendly airfield", SNAP)
        self.assertTrue(said.startswith("Nearest friendly FARP, FARP London") or
                        said.startswith("Nearest friendly field"), said)
        self.assertTrue(self.a.handle("nearest FARP", SNAP).startswith("Nearest friendly FARP, FARP London, "))
        self.assertTrue(self.a.handle("nearest friendly ship", SNAP).startswith("Nearest friendly ship, Stennis"))
        red = dict(SNAP, coalition=1)
        self.assertTrue(self.a.handle("divert", red).startswith("Nearest friendly field, Senaki-Kolkhi"))
        self.assertIsNone(self.a.handle("what's our fuel", SNAP))
        self.assertIn("no airfield list", af.Airfields().handle("divert", SNAP))

    def test_overlord(self):
        with mock.patch.object(af, "AIRFIELDS", self.a):
            o = ov.Overlord(None, lambda: SNAP, lambda s: None)
            self.assertTrue(o.handle("Overlord, request vector to nearest FARP").startswith(
                "Overlord, nearest friendly FARP, FARP London"))


class TestAirWarning(unittest.TestCase):
    def test_popup_then_closing_once(self):
        snap = dict(SNAP, enemy_air=[("Mi-24V", 1, *north(30), 150), ("Mi-24V", 2, *north(31), 150)])
        said = []
        w = ov.AirWarning(lambda: snap, said.append)
        self.assertEqual(w.check(now=0.0), ["Overlord, pop-up group, 2 ship Mi-24V, 30 kilometres north of you, "
                                            "500 feet."])
        self.assertEqual(w.check(now=4.0), [])                                    # once
        snap["enemy_air"] = [("Mi-24V", 1, *north(12), 150), ("Mi-24V", 2, *north(31), 150)]
        self.assertEqual(w.check(now=8.0), ["Overlord, bandit, Mi-24V, 12 kilometres north of you, closing."])
        self.assertEqual(w.check(now=12.0), [])
        snap["enemy_air"] = []
        w.check(now=400.0)                                                        # gone long enough
        snap["enemy_air"] = [("Mi-24V", 1, *north(30), 150)]
        self.assertEqual(len(w.check(now=404.0)), 1)                              # called again

    def test_far_away_or_none(self):
        w = ov.AirWarning(lambda: dict(SNAP, enemy_air=[("MiG-29S", 3, *north(80), 7000)]), lambda s: None)
        self.assertEqual(w.check(now=0.0), [])


class TestTargetN(unittest.TestCase):
    def test_from_georges_list(self):
        self.assertEqual(parse("attack target 3"), ("attack", {"which": "list", "n": 3, "text": "attack target 3"}))
        snap = dict(SNAP, targets=[{"id": 1, "type": "ZBD-04", "contact_id": 11, "dist_m": 2000},
                                   {"id": 2, "type": "ZTZ-96", "contact_id": 12, "dist_m": 2500}],
                    contacts=[{"id": 11, "type": "ZBD04A", "dist_m": 2000, "lat": 1, "lon": 1, "brg": 0},
                              {"id": 12, "type": "ZTZ96B", "dist_m": 2500, "lat": 1, "lon": 1, "brg": 0}])
        sent = []
        oly = type("O", (), {"command": lambda self, n, p: (sent.append((n, p)), (True, ""))[1]})()
        ex = Executor(oly, lambda: snap)
        self.assertEqual(ex.run("attack", {"which": "list", "n": 2, "text": ""}, [{"n": 2, "id": 9}], "x"), "")
        self.assertEqual(sent[-1], ("attackUnit", {"ID": 9, "targetID": 12}))
        self.assertEqual(ex.run("attack", {"which": "list", "n": 5, "text": ""}, [{"n": 2, "id": 9}], "x"),
                         "No target 5 on George's list.")


class TestDebrief(unittest.TestCase):
    def test_summary_and_file(self):
        d = db.Debrief(tempfile.mkdtemp())
        self.assertIsNone(d.handle("fuel"))
        d.fuel("Fuel 2400 pounds.")
        d.kill("George", "ZBD-04")
        d.kill("George", "ZBD-04")
        d.kill("George", "T-72B")
        d.kill("Hog flight", "ZU-23")
        d.loss("Outcast 4")
        d.launch("Hog flight", "CAS")
        d.shot("missile", 3)
        d.fuel("Fuel 1500 pounds.")
        said = d.handle("debrief")
        self.assertEqual(said, "Mission summary. 4 kills: George 3, 2 ZBD-04, T-72B; Hog flight 1, ZU-23. "
                               "Lost: Outcast 4. Fired 3 missiles. Overlord sent 1 flight. Fuel 1500 pounds.")
        files = os.listdir(d.log_dir)
        self.assertEqual(len(files), 1)
        d.new_mission("next")
        self.assertEqual(d.summary(), "Mission summary. No kills recorded.")


class TestOct23(unittest.TestCase):
    def test_nameless_air_objects_are_not_bandits(self):
        snap = dict(SNAP, enemy_air=[("", 7, *north(0.1), 150), ("UNKNOWN", 8, *north(0.2), 50)])
        self.assertEqual(ov.AirWarning(lambda: snap, lambda s: None).check(now=0.0), [])

    def test_bare_mission_question(self):
        from common import briefing
        for q in ("current mission", "current objective", "our mission"):
            self.assertTrue(briefing.is_briefing_question(q), q)
        self.assertFalse(briefing.is_briefing_question("mission status"))


class TestMarkOct31(unittest.TestCase):
    """31 Oct: "Mark position as waypoint Zulu" heard as "...Zolo", "...A" or without "as"."""
    def setUp(self):
        self.w = uw.UserWaypoints(None)

    def test_sound_alikes_and_no_as(self):
        for t in ("Mark position as waypoint Zolo.", "Mark position as waypoint Sulu", "Mark position waypoint Zulu",
                  "Mark our position, waypoint Zulu", "Set current position as waypoint Zulu"):
            self.w = uw.UserWaypoints(None)
            self.assertTrue(self.w.handle(t, SNAP).startswith("Waypoint Zulu marked."), t)
            self.assertEqual(self.w.names(), ["Zulu"])

    def test_unknown_name_is_asked_again(self):
        self.assertEqual(self.w.handle("Mark position as waypoint A.", SNAP), "Say again the waypoint name, Alpha to Zulu.")
        self.assertEqual(self.w.names(), [])

    def test_others_untouched(self):
        for t in ("Outcast 2, hold position", "mark the target position", "set laser code"):
            self.assertIsNone(self.w.handle(t, SNAP), t)
