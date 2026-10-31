"""Feature 8 (approved 30 Oct 2026): "Overlord, spawn ..." through the step 7 gate. Parsing (fixed rules
only), the Olympus spawn order, the reply, the group appearing in Export.lua's enemy list, tasking
(weapons hold / live, moving, helicopter height), failed to appear, cleared before it appeared, kills
with times in the debrief, and the main.py wiring. The real Olympus client runs over a fake HTTP
layer, so the allow-list and the guard decide exactly as on the PC."""
import json
import math
import os
import tempfile
import threading
import time
import unittest

from tests import fakes  # noqa: F401
from common import olympus, training_targets as tt
from common.training import Training, TRAINING_COMMANDS
from common.debrief import Debrief

ME = (42.0, 42.0)
HELO, JET = "AH-64D_BLK_II", "FA-18C_hornet"


def north_km(km):
    return (ME[0] + km * 1000 / 111_320.0, ME[1])


class World:
    """Export.lua's view: the enemy-side Olympus list. A spawn adds its group after `delay`."""
    def __init__(self, delay=0.05, appear=True):
        self.lock = threading.Lock()
        self.olyx, self.sent, self.n, self.delay, self.appear = [], [], 0, delay, appear
        self.snap = {"coalition": 2, "my_ll": ME, "hdg": 0.0, "aircraft": HELO, "alt_ft": 1000.0,
                     "agl_ft": 200.0, "enemy_lists_age": 0.5, "route": [], "self_id": 100}

    def snapshot(self):
        with self.lock:
            return dict(self.snap, olympus_enemy=list(self.olyx))

    def __call__(self, req, timeout):                    # the Olympus HTTP layer
        if req.get_method() == "PUT":
            body = json.loads(req.data)
            self.sent.append(body)
            name = next(iter(body))
            if name.startswith("spawn") and self.appear:
                units = body[name]["units"]
                threading.Timer(self.delay, self._add, args=(len(units) if "SAM Battery" not in
                                                           units[0]["unitType"] else 11,)).start()

        class R:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()

    def _add(self, n):
        with self.lock:
            self.n += 1
            g = f"Olympus-{self.n + 6}"
            self.olyx += [(g, f"{g}-{i + 1}", 5000 + 100 * self.n + i, *north_km(5), 300) for i in range(n)]

    def kill(self, uid):
        with self.lock:
            self.olyx = [u for u in self.olyx if u[2] != uid]

    def commands(self):
        return [next(iter(c)) for c in self.sent]


class _Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.said = []
        self.dir = tempfile.TemporaryDirectory()
        self.tr = Training(self.w.snapshot, lambda guard: olympus.Olympus(
            "http://localhost:4512", "George", "pw", opener=self.w, allowed=TRAINING_COMMANDS, guard=guard),
            enabled=True, path=None, templates={"SA-6 SAM Battery": 11}, printer=lambda s: None)
        self.tr.note_players("PLAYERS 1 sp")
        self.db = Debrief(log_dir=self.dir.name)
        self.sp = tt.Spawner(self.tr, self.w.snapshot, self.said.append, debrief=self.db,
                             appear_s=1.0, poll_s=0.02, kill_poll_s=0.02)

    def tearDown(self):
        self.dir.cleanup()

    def wait(self, cond, t=3.0):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    def parse(self, text, **snap):
        return tt.parse_spawn(text, dict(self.w.snapshot(), **snap), self.sp.units)


class TestUnitList(unittest.TestCase):
    def test_file_is_valid_and_names_dont_shadow(self):
        u = tt.UnitList()
        self.assertGreaterEqual(len(u.entries), 20)
        for e in u.entries:
            self.assertIn(e["cat"], tt.COMMAND)
            for s in e["say"]:
                got = u.find(tt._norm(s))
                self.assertIsNotNone(got, s)
                self.assertEqual(got[0]["type"], e["type"], s)
            if e["cat"] in ("air", "heli"):
                self.assertTrue(e.get("live_loadout"), e["type"])


class TestParse(_Base):
    def test_design_examples(self):
        r = self.parse("training, spawn two T-72s 5 km north of me")
        self.assertEqual((r.unit["type"], r.count, r.live, r.moving, r.difficulty), ("T-72B", 2, False, False, "average"))
        self.assertAlmostEqual(r.point[0], north_km(5)[0], places=4)
        self.assertEqual(r.where, "5 kilometres north of you")
        r = self.parse("spawn a pair of Mi-24s at 20 miles, live")
        self.assertEqual((r.unit["type"], r.count, r.live), ("Mi-24V", 2, True))
        self.assertEqual(r.where, "20 miles ahead of you")
        self.assertAlmostEqual(r.dist_m, 20 * 1609.34, delta=5)
        r = self.parse("spawn an SA-6 site 10 km east")
        self.assertEqual((r.unit["type"], r.count), ("SA-6 SAM Battery", 1))

    def test_flown_phrase(self):
        """30 Oct flight: 'Overlord spawn 1, MiG-21, 10 kilometers, north.'"""
        r = self.parse("spawn 1, MiG-21, 10 kilometers, north.")
        self.assertEqual((r.unit["type"], r.count, r.where), ("MiG-21Bis", 1, "10 kilometres north of you"))

    def test_type_digits_are_not_counts(self):
        self.assertEqual(self.parse("spawn mig 21").count, 1)
        self.assertEqual(self.parse("spawn a zsu 23 4").count, 1)
        self.assertEqual(self.parse("spawn four BMPs 6 km north").count, 4)

    def test_default_place_ahead(self):
        self.assertEqual(self.parse("spawn a T-72").where, "5 kilometres ahead of you")
        r = self.parse("spawn a MiG-29", aircraft=JET, hdg=90.0)
        self.assertEqual(r.where, "20 miles ahead of you")
        self.assertAlmostEqual(r.dist_m, 37040, delta=50)
        self.assertGreater(r.point[1], ME[1])                 # east, along your heading

    def test_options(self):
        r = self.parse("spawn two BTRs 3 km north moving hard")
        self.assertEqual((r.moving, r.difficulty), (True, "hard"))
        self.assertEqual(self.parse("spawn a truck 3 km north easy").difficulty, "easy")
        self.assertFalse(self.parse("spawn a tank 3 km north weapons hold").live)

    def test_refusals(self):
        self.assertEqual(self.parse("spawn a battleship 5 km north"), "I can't spawn that")
        self.assertEqual(self.parse("spawn"), "say what to spawn")
        self.assertEqual(self.parse("spawn ten tanks 5 km north"), "at most 8 at a time")
        self.assertEqual(self.parse("spawn a tank on my position"), "too close to you, at least 1 kilometre")
        self.assertIn("waypoint 7", self.parse("spawn a tank at waypoint 7"))


class TestOrder(_Base):
    def test_ground(self):
        cmd, p = tt.build_order(self.parse("spawn two T-72s 5 km north of me"), self.w.snapshot())
        self.assertEqual(cmd, "spawnGroundUnits")
        self.assertEqual((p["coalition"], p["country"], p["immediate"]), ("red", "", True))
        self.assertNotIn("airbaseName", p)
        a, b = p["units"]
        self.assertEqual((a["unitType"], a["skill"]), ("T-72B", "Good"))
        gap = math.hypot((a["location"]["lat"] - b["location"]["lat"]) * 111_320,
                         (a["location"]["lng"] - b["location"]["lng"]) * 111_320 * math.cos(math.radians(42)))
        self.assertAlmostEqual(gap, 30, delta=1)
        self.assertAlmostEqual(a["heading"], math.pi, places=2)          # facing you (south)

    def test_air_and_heli(self):
        cmd, p = tt.build_order(self.parse("spawn a MiG-21 10 km north"), self.w.snapshot())
        self.assertEqual((cmd, p["airbaseName"], p["units"][0]["altitude"], p["units"][0]["loadout"]),
                         ("spawnAircrafts", "", tt.AIR_ALT_M, ""))           # weapons hold: clean
        cmd, p = tt.build_order(self.parse("spawn a MiG-21 10 km north live"), self.w.snapshot())
        self.assertEqual(p["units"][0]["loadout"], "Patrol, short range")
        cmd, p = tt.build_order(self.parse("spawn a Mi-24 10 km north"), self.w.snapshot())
        ground = (1000 - 200) / 3.28084
        self.assertEqual(cmd, "spawnHelicopters")
        self.assertAlmostEqual(p["units"][0]["altitude"], ground + tt.HELI_SPAWN_ABOVE_M, places=3)

    def test_red_pilot_gets_blue_targets(self):
        cmd, p = tt.build_order(self.parse("spawn a tank 5 km north"), dict(self.w.snapshot(), coalition=1))
        self.assertEqual(p["coalition"], "blue")

    def test_describe(self):
        r = self.parse("spawn two T-72s 5 km north of me")
        self.assertEqual(tt.describe(r, 2), "two T-72B, 5 kilometres north of you, weapons hold, static.")
        r = self.parse("spawn a pair of Mi-24s at 20 miles, live")
        self.assertEqual(tt.describe(r, 2), "two Mi-24V, 20 miles ahead of you, live. They will engage.")
        r = self.parse("spawn an SA-6 site 10 km east")
        self.assertEqual(tt.describe(r, 11), "SA-6 site, 11 units, 10 kilometres east of you, weapons hold, static.")


class TestSpawner(_Base):
    def test_full_flow_ground(self):
        said = self.sp.handle("training, spawn two T-72s 5 km north of me")
        self.assertEqual(said, "Overlord, two T-72B, 5 kilometres north of you, weapons hold, static.")
        self.assertTrue(self.wait(lambda: "Overlord, training group is up." in self.said), self.said)
        self.assertEqual(self.w.commands(), ["spawnGroundUnits", "setROE"])
        roe = self.w.sent[1]["setROE"]
        self.assertEqual(roe["ROE"], 4)                                     # weapons hold
        self.assertEqual(self.tr.counts(), (1, 2))
        for u in list(self.w.olyx):
            self.w.kill(u[2])
        self.assertTrue(self.wait(lambda: len(self.db.training[0]["kills"]) == 2))
        self.assertIn("Training: two T-72B, both destroyed (", self.db.summary())

    def test_live_moving_heli(self):
        self.sp.handle("spawn a Mi-24 8 km north, live, moving")
        self.assertTrue(self.wait(lambda: "Overlord, training group is up." in self.said))
        self.assertEqual(self.w.commands(), ["spawnHelicopters", "setROE", "setAltitudeType", "setAltitude", "setPath"])
        self.assertEqual(self.w.sent[1]["setROE"]["ROE"], 0)
        self.assertEqual(self.w.sent[3]["setAltitude"]["altitude"], tt.HELI_AGL_M)
        self.assertEqual(len(self.w.sent[4]["setPath"]["path"]), 2)

    def test_static_aircraft_held_at_the_spawn_point(self):
        """30 Oct flight: a static MiG-21 flew straight on; now its path is its own spawn point."""
        self.sp.handle("spawn a MiG-21 10 km north")
        self.assertTrue(self.wait(lambda: "Overlord, training group is up." in self.said))
        self.assertEqual(self.w.commands(), ["spawnAircrafts", "setROE", "setPath"])
        path = self.w.sent[2]["setPath"]["path"]
        self.assertEqual(len(path), 1)
        self.assertAlmostEqual(path[0]["lat"], north_km(10)[0], places=4)

    def test_failed_to_appear_frees_the_limit(self):
        self.w.appear = False
        self.sp.handle("spawn two T-72s 5 km north")
        self.assertEqual(self.tr.counts()[1], 2)                           # counted while pending
        self.assertTrue(self.wait(lambda: any("failed to appear" in s for s in self.said)))
        self.assertEqual(self.tr.counts(), (0, 0))

    def test_cleared_before_it_appeared(self):
        self.w.delay = 0.4
        self.sp.handle("spawn a tank 5 km north")
        self.assertEqual(self.tr.handle("clear training targets"), "Overlord, 1 training group removed.")
        self.assertTrue(self.wait(lambda: "deleteUnit" in self.w.commands()))
        self.assertFalse(any("group is up" in s for s in self.said))
        self.assertFalse(self.tr.has_groups())

    def test_gate_reasons_spoken(self):
        self.tr.on = False
        self.assertEqual(self.sp.handle("spawn a tank 5 km north"), "Overlord, unable, training mode is off.")
        self.tr.on = True
        self.assertEqual(self.sp.handle("spawn a tank 60 km north"),
                         "Overlord, unable, spawn point more than 50 km from you.")
        self.tr.note_players("PLAYERS 3 mp")
        self.assertEqual(self.sp.handle("spawn a tank 5 km north"), "Overlord, unable, other players are in the mission.")
        self.assertEqual(self.w.sent, [])
        self.assertEqual(self.sp.handle("spawn a submarine"), "Overlord, I can't spawn that.")

    def test_limits_count_sites(self):
        for _ in range(2):
            self.sp.handle("spawn an SA-6 site 10 km north")
        self.assertTrue(self.wait(lambda: self.said.count("Overlord, training group is up.") == 2))
        self.assertIn("limit of 24", self.sp.handle("spawn an SA-6 site 10 km north"))


class TestMainWiring(unittest.TestCase):
    def test_overlord_spawn_reaches_the_spawner(self):
        main = fakes.pipeline_module().main
        said, who, _ = main.shared_reply("Overlord, spawn two T-72s 5 km north of me")
        self.assertEqual((said, who), ("Overlord, unable, training mode is off.", "overlord"))
        self.assertIs(main.SPAWNER.training, main.TRAINING)


if __name__ == "__main__":
    unittest.main()
