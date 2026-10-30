"""DCS Olympus: client login, the orders the radio menu can't give, target choice, waypoints."""
import base64
import hashlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests import fakes  # noqa: F401
from common import geo, olympus, olympus_orders, targets, wingmen

PW = "thor"


class FakeOlympus(BaseHTTPRequestHandler):
    """core.dll as far as we use it: Basic auth with sha256(password), GET /olympus/mission with
    spots, PUT /olympus {command: params}."""
    commands = []
    spots = {}
    up = True

    def _auth_ok(self):
        want = "Basic " + base64.b64encode(f"George:{hashlib.sha256(PW.encode()).hexdigest()}".encode()).decode()
        return self.headers.get("Authorization") == want

    def _reply(self, code, obj=None):
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth_ok():
            return self._reply(401)
        if self.path == "/olympus/mission":
            return self._reply(200, {"mission": {"theatre": "Caucasus"}, "spots": FakeOlympus.spots})
        self._reply(404)

    def do_PUT(self):
        if not self._auth_ok():
            return self._reply(401)
        if self.path != "/olympus":
            return self._reply(404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOlympus.commands.append(body)
        if "fireLaser" in body:
            FakeOlympus.spots[str(len(FakeOlympus.spots) + 1)] = {
                "type": "laser", "sourceUnitID": body["fireLaser"]["ID"], "active": True}
        self._reply(200, {})

    def log_message(self, *a):
        pass


# 2.0 km north of you a ZSU (2 o'clock-ish), trucks, a tank on the nose
def snap():
    s = {"hdg": 0.0, "pos": (0.0, 0.0, 100.0), "my_ll": (42.0, 42.0), "self_id": 100,
         "telemetry_age": 0.1, "my_group": "Rotary-2", "my_unit": "Rotary-2-1",
         "flight_group": "Rotary-2", "flight_alive_age": 1.0,
         "flight_units": [(1, "Rotary-2-1", "Player", True), (2, "Rotary-2-2", "High", False),
                          (3, "Rotary-2-3", "High", False), (4, "Rotary-2-4", "High", False)],
         "flight_alive": ["Rotary-2-2", "Rotary-2-3", "Rotary-2-4"],
         "wing": [("Rotary-2-2", 101, 42.0005, 42.0005), ("Rotary-2-3", 102, 42.0, 41.999),
                  ("Rotary-2-4", 103, 41.999, 42.0)],
         "route": [(0, 0.0, 0.0), (1, 5000.0, 0.0), (2, 10000.0, 3000.0)],
         "contacts": [contact(7, "ZSU-23-4 Shilka", 2000, 60), contact(8, "T-72B", 3000, 2),
                      contact(9, "Ural-375", 1500, 300), contact(10, "KAMAZ Truck", 1700, 310)]}
    return s


def ll(x, z):
    """The test world's map -> lat/lon (linear: 1e-5 deg/m north, 1.3e-5 deg/m east)."""
    return round(42.0 + x * 1e-5, 6), round(42.0 + z * 1.3e-5, 6)


def contact(cid, typ, dist, brg):
    import math
    lat, lon = ll(dist * math.cos(math.radians(brg)), dist * math.sin(math.radians(brg)))
    return {"id": cid, "type": typ, "dist_m": dist, "brg": brg, "lat": lat, "lon": lon}


class OlympusCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeOlympus)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeOlympus.commands.clear()
        FakeOlympus.spots.clear()
        self.snap = snap()
        geo.FIT = geo.LocalFit()
        # map coordinates <-> lat/lon for the waypoint tests: 1e-5 deg per metre, linear
        for x, z in ((0, 0), (4000, 0), (0, 4000), (4000, 4000)):
            geo.FIT.add(x, z, *ll(x, z))
        self.oly = olympus.Olympus(f"http://127.0.0.1:{self.srv.server_port}", "George", PW)
        wingmen.EXECUTOR = olympus_orders.Executor(self.oly, lambda: self.snap)
        olympus_orders.SPOT_UPDATE_SEC = 0.1

    def tearDown(self):
        wingmen.EXECUTOR = None

    def say(self, text):
        return wingmen.handle(text, None, self.snap, lambda t: None)

    def cmds(self):
        return [next(iter(c.items())) for c in FakeOlympus.commands]


class TestClient(OlympusCase):
    def test_login_with_hashed_password(self):
        self.assertTrue(self.oly.connect(force=True))
        self.assertEqual(self.oly._auth_name, "sha256")
        self.assertEqual(self.oly.command("smoke", {"color": "green", "location": {"lat": 1, "lng": 2}}), (True, ""))
        self.assertEqual(self.oly.command("explosion", {"ID": 1})[0], False)     # not allowed
        self.assertEqual(self.cmds(), [("smoke", {"color": "green", "location": {"lat": 1, "lng": 2}})])

    def test_wrong_password(self):
        bad = olympus.Olympus(self.oly.url, "George", "nope")
        self.assertFalse(bad.connect(force=True))
        self.assertIn("refused", bad.last_error)


class TestOrders(OlympusCase):
    def test_attack(self):
        self.assertEqual(self.say("two, attack my target"), "")              # the tank on the nose
        self.assertEqual(self.say("three, take out the ZSU at two o'clock"), "")
        self.assertEqual(self.say("flight, hit the trucks"), "")
        self.assertEqual(self.cmds(), [
            ("attackUnit", {"ID": 101, "targetID": 8}),
            ("attackUnit", {"ID": 102, "targetID": 7}),
            ("attackUnit", {"ID": 101, "targetID": 9}), ("attackUnit", {"ID": 102, "targetID": 10}),
            ("attackUnit", {"ID": 103, "targetID": 9})])

    def test_my_target_is_georges_priority(self):
        wingmen.EXECUTOR.george_top = lambda s: {"type": "ZSU-23-4 Shilka", "dist_km": 2.0}
        self.assertEqual(self.say("two, engage my target"), "")
        self.assertEqual(self.cmds(), [("attackUnit", {"ID": 101, "targetID": 7})])

    def test_follow_waypoint_land_roe(self):
        self.assertEqual(self.say("three, stay on my left at 200 metres"), "")
        self.assertEqual(self.say("two, follow me"), "")
        self.assertEqual(self.say("two, go to waypoint 2"), "")
        self.assertEqual(self.say("four, land next to me"), "")
        self.assertEqual(self.say("flight, weapons hold"), "")
        c = self.cmds()
        self.assertEqual(c[0], ("followUnit", {"ID": 102, "targetID": 100, "offsetX": -60.0, "offsetY": 0, "offsetZ": -200.0}))
        self.assertEqual(c[1], ("followUnit", {"ID": 101, "targetID": 100, "offsetX": -60, "offsetY": 0, "offsetZ": 60}))
        self.assertEqual(c[2][0], "setPath")
        p = c[2][1]["path"][0]
        self.assertAlmostEqual(p["lat"], 42.1, places=4)                   # x 10000 m
        self.assertAlmostEqual(p["lng"], 42.039, places=4)                 # z 3000 m
        self.assertEqual(c[3][0], "landAtPoint")
        self.assertEqual(c[4:], [("setROE", {"ID": i, "ROE": 4}) for i in (101, 102, 103)])

    def test_lase_and_stop(self):
        self.assertEqual(self.say("two, lase the tank, code 1 6 8 8"), "")
        tank = self.snap["contacts"][1]
        self.assertEqual(self.cmds()[0], ("fireLaser", {"ID": 101, "code": 1688,
                                                        "location": {"lat": tank["lat"], "lng": tank["lon"]}}))
        self.snap["contacts"][1] = dict(tank, lat=tank["lat"] + 0.001)        # the tank moves
        time.sleep(0.4)
        self.assertIn(("moveSpot", {"spotID": 1, "location": {"lat": tank["lat"] + 0.001, "lng": tank["lon"]}}),
                      self.cmds())
        self.assertEqual(self.say("two, stop lasing"), "")
        self.assertEqual(self.cmds()[-1], ("deleteSpot", {"spotID": 1}))

    def test_smoke(self):
        self.assertEqual(self.say("two, mark my target with smoke"), "")
        self.assertEqual(self.say("flight, pop smoke"), "")
        tank = self.snap["contacts"][1]
        self.assertEqual(self.cmds(), [("smoke", {"color": "red", "location": {"lat": tank["lat"], "lng": tank["lon"]}}),
                                       ("smoke", {"color": "green", "location": {"lat": 42.0, "lng": 42.0}})])

    def test_checks_still_apply(self):
        self.snap["flight_alive"] = ["Rotary-2-2"]
        self.assertEqual(self.say("three, follow me"), "Three is down.")
        self.snap["wing"] = []
        self.assertEqual(self.say("two, follow me"), "Unable, I can't see two.")
        self.assertEqual(self.cmds(), [])

    def test_olympus_down(self):
        wingmen.EXECUTOR.oly = olympus.Olympus("http://127.0.0.1:1", "George", PW, timeout=0.3)

        class Menu:
            last_items = []
            def select_path(self, path):
                Menu.path = path
                return True, ""
        self.assertEqual(wingmen.handle("two, follow me", Menu(), self.snap, lambda t: None),
                         "Unable, Olympus isn't running.")
        # "attack my target" falls back to the radio menu's Engage > My Target
        self.assertEqual(wingmen.handle("two, attack my target", Menu(), self.snap, lambda t: None), "")
        self.assertEqual(Menu.path, ["Wingman 2", "Engage", "My Target"])


class TestFlightOct20(OlympusCase):
    """Phrases from the 20 Oct Olympus flight."""

    def test_heard_as(self):
        self.assertEqual(self.say("tool, attack my target."), "")                 # "two"
        self.assertEqual(wingmen.parse_order("arm, wingman two, hold position"), (2, ["Hold Position"]))
        self.assertEqual(self.say("wingman 1, stop blazing"), "You're one. Your first wingman is two.")
        self.assertEqual(self.cmds()[0], ("attackUnit", {"ID": 101, "targetID": 8}))

    def test_positions(self):
        self.assertEqual(self.say("3, move in front, 1 kilometer"), "")
        self.assertEqual(self.say("4, move forward, 500 metres"), "")
        self.assertEqual(self.say("two, stay behind me"), "")
        c = self.cmds()
        self.assertEqual(c[0], ("followUnit", {"ID": 102, "targetID": 100, "offsetX": 1000.0, "offsetY": 0, "offsetZ": 0.0}))
        self.assertEqual(c[1][1]["offsetX"], 500.0)
        self.assertEqual(c[2][1]["offsetX"], -300.0)

    def test_stop_lasing_as_heard(self):
        self.assertEqual(self.say("four, lase my target"), "")
        time.sleep(0.3)
        for heard in ("stop lase in target", "stop losing", "stop blazing"):
            with self.subTest(heard=heard):
                self.assertEqual(olympus_orders.parse(heard)[0], "lase_stop")
        self.assertEqual(self.say("wingman, stop blazing"), "")             # whoever is lasing
        self.assertEqual(self.cmds()[-1], ("deleteSpot", {"spotID": 1}))
        self.assertEqual(self.say("wingman, stop lasing"), "Nobody's lasing.")


class TestParse(unittest.TestCase):
    def test_phrases(self):
        cases = {
            "attack my target": ("attack", "mine"), "take out the zsu at two o clock": ("attack", "described"),
            "hit the trucks": ("attack", "described"), "engage everything": ("attack", "all"),
            "follow me": ("follow", None), "on me": ("follow", None), "go to waypoint three": ("goto_wp", None),
            "land at waypoint 2": ("land_wp", None), "land next to me": ("land_me", None),
            "weapons hold": ("roe", None), "weapons free": ("roe", None), "return fire": ("roe", None),
            "lase my target": ("lase", None), "stop lasing": ("lase_stop", None),
            "pop smoke": ("smoke_me", None), "mark the target with red smoke": ("smoke_target", None),
        }
        for text, (kind, which) in cases.items():
            with self.subTest(text=text):
                r = olympus_orders.parse(text)
                self.assertEqual(r[0], kind)
                if which:
                    self.assertEqual(r[1]["which"], which)
        for text in ("return to base", "rejoin", "cover me", "engage ground targets", "hold position"):
            with self.subTest(text=text):
                self.assertIsNone(olympus_orders.parse(text))       # radio menu orders stay there


class TestGeo(unittest.TestCase):
    def test_fit_needs_spread_then_converts(self):
        f = geo.LocalFit()
        f.add(0, 0, 42.0, 42.0)
        f.add(100, 100, 42.001, 42.0013)
        self.assertIsNone(f.to_ll(5000, 5000))                         # too little ground covered
        for x, z in ((3000, 0), (0, 3000), (3000, 3000)):
            f.add(x, z, 42.0 + x * 1e-5, 42.0 + z * 1.3e-5)
        lat, lon = f.to_ll(20000, -10000)
        self.assertAlmostEqual(lat, 42.2, places=6)
        self.assertAlmostEqual(lon, 41.87, places=6)


if __name__ == "__main__":
    unittest.main()
