"""Huey flight 18 Oct 2026: waypoint mis-hearings, 'turn off the X', questions, start-up false
alarms, contact call flood, menu readout."""
import unittest

from tests.fakes import send_telemetry
from tests.test_huey import HueyCase, needle
from core.state import state_store
from common.comms_menu import readout
from common.nav_commands import _norm, _NUMBERED
from huey import commands
from huey.crew_chief import CrewChief


def m(text):
    r = commands.match(text)
    return r and {k: v for k, v in r.items() if k not in ("speech", "source")}


class TestPhrases(unittest.TestCase):
    def test_waypoint_misheard(self):
        for heard, n in (("waypoint tool", "two"), ("way of point two", "two"), ("away point three", "three"),
                         ("way of 0.4", "4"), ("away point five", "five"), ("waypoint5", "5")):
            with self.subTest(heard=heard):
                hit = _NUMBERED.search(_norm(heard))
                self.assertIsNotNone(hit)
                self.assertEqual(hit.group(1), n)
        self.assertIsNone(_NUMBERED.search(_norm("point search T07")))

    def test_turn_off_the_x(self):
        self.assertEqual(m("turn off the navigation lights"), {"action": "SWITCH", "key": "nav_lts", "value": "OFF"})
        self.assertEqual(m("return off the navigation lights"), {"action": "SWITCH", "key": "nav_lts", "value": "OFF"})
        self.assertEqual(m("turn off, search light"), {"action": "SWITCH", "key": "search_light", "value": "OFF"})
        self.assertEqual(m("turn on the landing light"), {"action": "SWITCH", "key": "ldg_light", "value": "ON"})
        self.assertEqual(m("turn off lights"), {"action": "NONE"})          # which ones? George asks

    def test_questions_never_switch(self):
        r = commands.sanitize({"action": "SWITCH", "key": "master_arm", "value": "ARMED"}, "do we have rockets?")
        self.assertEqual(r["action"], "NONE")
        self.assertEqual(commands.sanitize({"action": "FUEL"}, "how much fuel do we have?")["action"], "FUEL")
        r = commands.sanitize({"action": "SWITCH", "key": "search_light", "value": "STOW"}, "hard enough lights on")
        self.assertEqual(r["action"], "NONE")                                 # said "on", model said stow

    def test_radio_frequency_in_other_fields(self):        # bench 19 Oct: mhz left out
        for res in ({"action": "RADIO", "key": "uhf", "value": "241"}, {"action": "RADIO", "frequency": 241},
                    {"action": "RADIO", "key": "uhf"}):
            r = commands.sanitize(res, "Set reader frequency 241")
            self.assertEqual((r["action"], r.get("key"), r.get("mhz")), ("RADIO", "uhf", 241.0))

    def test_menu_readout_spoken_clean(self):
        menu = {"caption": "ATC", "items": {1: "[N/A] Dallas (116 MHz)", 5: "Kutaisi (4.4 / 134 / 41 / 263 MHz)"}}
        self.assertEqual(readout(menu), "ATC: 1 Dallas, 5 Kutaisi.")


class TestCrewChiefOct18(HueyCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.chief = CrewChief(self.said.append, state_store.snapshot, lambda: True,
                               contact_range_m=1500, contact_gap_sec=0, talkdown=False)

    def gauges(self, **vals):
        for k, v in vals.items():
            arg, n = needle(k, v)
            self.game.gauges[arg] = n
        self.game.publish()

    def test_no_false_alarms_at_mission_start(self):
        # first packets after the mission loads look like flight, gauges still at zero
        send_telemetry()
        state_store.update_telemetry({"agl_ft": 400, "speed_kts": 0, "pos": [0, 0, 0]})
        self.gauges(rotor_rpm=0, engine_rpm=0, n1_pct=0)
        for t in (100, 101, 102, 103):
            self.chief.check(now=t)
        state_store.update_telemetry({"agl_ft": 0, "speed_kts": 0})
        self.chief.check(now=104)
        self.assertEqual(self.said, [])

    def test_contact_flood(self):
        convoy = [{"id": i, "type": t, "dist_m": 400 + 50 * i, "brg": 270}
                  for i, t in enumerate(["BTR-80", "Soldier AK", "BRDM-2", "MTLB", "Soldier AK", "BMP-2",
                                         "Ural-375", "KAMAZ Truck", "outpost_road"], start=1)]
        send_telemetry(contacts=convoy, hdg=0)
        for t in range(100, 160, 10):
            self.chief.check(now=t)
        calls = [s for s in self.said if s.startswith(("Contact", "Multiple"))]
        self.assertEqual(len([s for s in calls if s.startswith("Contact")]), 3)
        self.assertEqual(len([s for s in calls if s.startswith("Multiple")]), 1)
        self.assertFalse(any("Ural" in s or "KAMAZ" in s or "outpost" in s for s in calls))

    def test_air_defence_always_called(self):
        send_telemetry(contacts=[{"id": i, "type": "Soldier AK", "dist_m": 500, "brg": 90} for i in range(1, 6)], hdg=0)
        for t in range(100, 140, 10):
            self.chief.check(now=t)
        send_telemetry(contacts=[{"id": 99, "type": "ZU-23 Insurgent", "dist_m": 900, "brg": 90}], hdg=0)
        self.chief.check(now=145)
        self.assertIn("Contact, ZU-23 Insurgent, 3 o'clock, 900 meters.", self.said)


if __name__ == "__main__":
    unittest.main()
