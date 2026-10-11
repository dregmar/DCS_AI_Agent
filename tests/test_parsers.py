"""Parsers and pure functions, checked against text captured from DCS."""
import os
import unittest

import tests.fakes  # noqa: F401  (config overrides first)
from core.george_ui import parse_indication
from core.wpn_page_listener import parse_wpn_page
from core.state import clock_position
from intelligence.threat_engine import _threat_weight

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class GeorgeInterfaceText(unittest.TestCase):
    def test_main_ring_from_dcs(self):
        ui = parse_indication(fixture("ui_main.txt"))
        self.assertTrue(ui["visible"])
        self.assertIsNone(ui["list"])
        self.assertEqual((ui["weapon"], ui["roe"], ui["tads"]), ("NO_WPN", "HOLD", "FIXED"))
        self.assertEqual(ui["labels"]["center"], "POINT / AREA")

    def test_target_list_from_dcs(self):
        ui = parse_indication(fixture("ui_list.txt"))
        self.assertEqual(ui["list"]["kind"], "TARGET_LIST")
        self.assertEqual(ui["list"]["window"], ["", "", "LARC-V", "LARC-V", "LARC-V"])

    def test_hidden(self):
        self.assertEqual(parse_indication("-" * 41 + "\n#1#\n\nchildren are {\n}\n"), {"visible": False})


class WpnPage(unittest.TestCase):
    def test_gun_boxed_from_dcs(self):
        self.assertEqual(parse_wpn_page(fixture("wpn11.txt")), ("GUN", "HMD"))

    def test_nothing_boxed(self):
        self.assertEqual(parse_wpn_page(fixture("wpn11.txt").replace("PB17_13_b", "PB17_13_x")), ("NONE", "HMD"))

    def test_not_a_wpn_page(self):
        self.assertEqual(parse_wpn_page("-" * 41 + "\nfoo\nbar\n"), (None, None))


class Geometry(unittest.TestCase):
    def test_clock_positions(self):
        self.assertEqual(clock_position(0, 0), 12)
        self.assertEqual(clock_position(90, 0), 3)
        self.assertEqual(clock_position(180, 90), 3)
        self.assertEqual(clock_position(350, 10), 11)
        self.assertIsNone(clock_position(10, -1))


class ThreatWeights(unittest.TestCase):
    def test_ranking(self):
        w = _threat_weight
        self.assertGreater(w("ZSU-23-4 Shilka"), w("T-72B3"))
        self.assertGreater(w("Igla manpad"), w("BMP-3"))
        self.assertGreater(w("T-72B3"), w("BTR-82A"))
        self.assertGreater(w("Soldier RPG"), w("LARC-V"))
        self.assertEqual(w("AAA"), 90)          # George's classification labels
        self.assertEqual(w("ARMORED"), 50)


if __name__ == "__main__":
    unittest.main()
