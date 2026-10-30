"""VR kneeboard (kneeboard/): data model, comms log, web server and SSE."""
import json
import time
import unittest
import urllib.request

from kneeboard.comms_log import CommsLog
from kneeboard.model import build_map, ring_m
from kneeboard.server import Kneeboard

ME = (42.0, 42.0)


class Ctx:
    def xz_to_ll(self, x, z):
        return (ME[0] + x / 111_320.0, ME[1] + z / 82_700.0)

    def is_air_defence(self, t):
        return "sam" in t.lower()

    def imperial(self, snap):
        return snap.get("aircraft", "").startswith("FA-18")

    def named_waypoints(self, snap):
        return [{"name": "Alpha", "lat": 42.05, "lon": 42.0}]

    def outcasts(self, snap):
        return [{"n": 2, "lat": 42.001, "lon": 42.001}]

    def flights(self, snap):
        return [{"cs": "Hog", "kind": "CAS", "state": "engaging", "pts": [[42.1, 42.1]]}]

    def jtac(self, snap):
        return {"type": "ZSU-23-4 Shilka", "lat": 42.06, "lon": 42.02}


SNAP = {"my_ll": ME, "hdg": 45.0, "alt_ft": 1200.0, "speed_kts": 250.0, "aircraft": "FA-18C_hornet",
        "route": [(0, 0.0, 0.0), (1, 10_000.0, 5_000.0)], "wp_next": 1,
        "air_defence": [("Kub 1S91 str", 7, 42.2, 42.1, 300.0)],
        "enemy_ground": [("ZSU-23-4 Shilka", 5, 42.06, 42.02, 310.0), ("Ural-375", 6, 42.07, 42.03, 305.0),
                         ("Kub 1S91 str", 7, 42.2, 42.1, 300.0)],
        "enemy_air": [("Mi-24V", 9, 42.3, 42.0, 450.0)]}


class TestModel(unittest.TestCase):
    def test_rings(self):
        self.assertEqual(ring_m("Kub 1S91 str"), 25_000)
        self.assertEqual(ring_m("ZSU-23-4 Shilka"), 2_500)
        self.assertEqual(ring_m("Osa 9A33 ln"), 10_000)
        self.assertIsNone(ring_m("Ural-375"))
        self.assertEqual(ring_m("Some new SAM", lambda t: True), 5_000)

    def test_build(self):
        m = build_map(SNAP, Ctx())
        self.assertEqual(m["units"], "nm")
        self.assertEqual(m["me"]["hdg"], 45.0)
        self.assertEqual([p["n"] for p in m["route"]], [0, 1])
        self.assertEqual(len(m["ground"]), 3)                        # SA-6 once, though in both lists
        sa6 = next(g for g in m["ground"] if g["id"] == 7)
        self.assertTrue(sa6["ad"])
        self.assertEqual(sa6["ring_m"], 25_000)
        self.assertEqual(m["air"][0]["alt_ft"], 1476)
        self.assertEqual(m["jtac"]["type"], "ZSU-23-4 Shilka")
        json.dumps(m)                                                # JSON-ready

    def test_no_position(self):
        m = build_map({}, Ctx())
        self.assertIsNone(m["me"])
        self.assertEqual(m["ground"], [])


class TestCommsLog(unittest.TestCase):
    def test_since(self):
        log = CommsLog(maxlen=3)
        for i in range(5):
            log.add("pilot", f"line {i}")
        log.add("george", "  ")                                      # empty: ignored
        self.assertEqual([e["text"] for e in log.since(0)], ["line 2", "line 3", "line 4"])
        self.assertEqual([e["text"] for e in log.since(4)], ["line 4"])
        self.assertEqual(log.last_id(), 5)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.log = CommsLog()
        cls.log.add("overlord", "Overlord, target Shilka. Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8.")
        cls.kb = Kneeboard(lambda: build_map(SNAP, Ctx()), lambda: {"links": [{"name": "x", "ok": True}]},
                           cls.log, port=0).start()
        cls.base = f"http://127.0.0.1:{cls.kb.port}"

    @classmethod
    def tearDownClass(cls):
        cls.kb.stop()

    def test_page_and_state(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        self.assertIn('<canvas id="map">', html)
        st = json.loads(urllib.request.urlopen(self.base + "/state", timeout=3).read())
        self.assertEqual(st["map"]["units"], "nm")
        self.assertEqual(st["comms"][0]["who"], "overlord")
        self.assertEqual(urllib.request.urlopen(self.base + "/state", timeout=3).status, 200)

    def test_events(self):
        r = urllib.request.urlopen(self.base + "/events", timeout=3)
        got, t0 = "", time.time()
        while time.time() - t0 < 3 and not all(k in got for k in ("event: comms", "event: map", "event: status")):
            got += r.readline().decode()
        r.close()
        self.assertIn("event: comms", got)
        self.assertIn("event: map", got)
        self.assertIn("event: status", got)

    def test_bound_to_localhost(self):
        self.assertEqual(self.kb.httpd.server_address[0], "127.0.0.1")


class TestMainKneeboard(unittest.TestCase):
    def test_map_and_status(self):
        import main
        m = main.kneeboard_map()
        json.dumps(m)
        s = main.kneeboard_status()
        names = [l["name"] for l in s["links"]]
        self.assertIn("Comms hook", names)
        self.assertTrue(any(n.startswith("Ollama") for n in names))
        json.dumps(s)


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None
