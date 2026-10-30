"""apache_probe.py and flight 18 Oct 2026: power levers, tail wheel, mission route, sound-alikes."""
import re
import unittest

from tests import fakes  # noqa: F401
from apache.bios_map import POWER_LEVERS, power_lever
from common.comms_menu import parse_route
from core.state import ApacheStateStore


def lever(raw):
    return power_lever({POWER_LEVERS["pl1"]: raw}, "pl1")


class TestPowerLevers(unittest.TestCase):
    def test_measured_positions(self):
        self.assertEqual(lever(58981), "FLY")       # FLY detent (90%)
        self.assertEqual(lever(57619), "FLY")       # 88%
        self.assertEqual(lever(53595), "MID")       # 82%: used to read IDLE
        self.assertEqual(lever(44791), "MID")
        self.assertEqual(lever(35391), "IDLE")      # back at the IDLE stop (54%)
        self.assertEqual(lever(0), "OFF")


class TestMissionRoute(unittest.TestCase):
    def setUp(self):
        self.s = ApacheStateStore()

    def test_parse(self):
        self.assertEqual(parse_route("ROUTE 0,1000,2000;1,5000,6000"), [(0, 1000.0, 2000.0), (1, 5000.0, 6000.0)])
        self.assertEqual(parse_route("ROUTE "), [])

    def test_fallback_and_advance(self):
        self.s.update_telemetry({"pos": [1000, 2000, 50], "route": [], "wp": -1})
        self.s.set_mission_route([(0, 1000, 2000), (1, 5000, 2000), (2, 9000, 2000)])
        snap = self.s.snapshot()
        self.assertEqual((len(snap["route"]), snap["wp_next"], snap["route_source"]), (3, 1, "mission"))
        self.s.update_telemetry({"pos": [4500, 2100, 50], "route": [], "wp": -1})   # 500 m from wp 1
        self.assertEqual(self.s.snapshot()["wp_next"], 2)
        self.s.update_telemetry({"pos": [9000, 2000, 50], "route": [], "wp": -1})
        self.assertEqual(self.s.snapshot()["wp_next"], 2)                          # last one stays

    def test_logetroute_wins(self):
        self.s.set_mission_route([(0, 1, 2), (1, 3, 4)])
        self.s.update_telemetry({"pos": [0, 0, 0], "route": [[1, 10, 20], [2, 30, 40]], "wp": 2})
        snap = self.s.snapshot()
        self.assertEqual((snap["route"][0], snap["wp_next"], snap["route_source"]), ((1, 10.0, 20.0), 2, "LoGetRoute"))


class TestSoundalikes(unittest.TestCase):
    def test_fuel(self):
        import sys
        sys.modules.setdefault("sounddevice", type(sys)("sounddevice"))
        try:
            import main
        except Exception as e:
            self.skipTest(f"main.py not importable here: {e}")
        for heard in ("You will.", "See you well!", "A few will"):
            self.assertEqual(main._SOUNDALIKES.get(re.sub(r"[^a-z ]", "", heard.lower()).strip()), "fuel")


if __name__ == "__main__":
    unittest.main()
