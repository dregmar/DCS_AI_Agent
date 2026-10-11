"""The George driver against a simulated George (FakeGeorge). Never presses real keys."""
import time
import unittest

from tests.fakes import (FakeGeorge, FakeKeyboard, wait_until, send_telemetry, DEFAULT_CONTACTS,
                         state_store)
from core.george_interface import GeorgeInterface


class DriverCase(unittest.TestCase):
    WEAPONS = ("NO WPN", "GUN", "MSL", "RKT")

    def setUp(self):
        state_store.update_george_ui({"visible": False})
        state_store.set_george_list([], False)
        state_store.set_engagement([])
        send_telemetry(DEFAULT_CONTACTS)
        self.fg = FakeGeorge(weapons=self.WEAPONS)
        self.kb = FakeKeyboard(self.fg)
        self.events = []
        self.focus = True
        self.g = GeorgeInterface(self.kb, on_event=lambda e, d: self.events.append((e, d)),
                                 ui_source=state_store.george_ui_snapshot,
                                 list_sink=state_store.set_george_list,
                                 focus_check=lambda: self.focus)
        time.sleep(0.4)                          # first frames from the fake

    def tearDown(self):
        self.g.shutdown()
        self.fg.stop()
        time.sleep(0.4)

    # helpers
    def said(self):
        return [d.get("text") or d.get("reason") for e, d in self.events if e in ("say", "rejected")]

    def run_action(self, *a, wait_for=None, timeout=6.0, **k):
        n = len(self.events)
        ok, reason = self.g.run_action(*a, **k)
        if ok and wait_for:
            wait_until(lambda: any(e == wait_for for e, _ in self.events[n:]), timeout)
        return ok, reason

    def open_wheel(self):
        self.g.run_action("WHEEL_SHOW")
        self.assertTrue(wait_until(lambda: self.fg.visible and self.g.snapshot()["wheel_open"]))
        time.sleep(0.4)

    def search_and_scan(self):
        self.run_action("PHS_SEARCH")
        self.assertTrue(wait_until(lambda: any(e == "list_opened" for e, _ in self.events), 8))


class Settings(DriverCase):
    def test_weapons_free_is_confirmed_from_the_wheel(self):
        self.run_action("GEORGE_SET", setting="roe", value="FREE", speech="Weapons free.", wait_for="say")
        self.assertEqual(self.fg.roe, "FREE FIRE")
        self.assertIn("Weapons free.", self.said())

    def test_burst_value(self):
        self.run_action("GEORGE_SET", setting="weapon", value="GUN", wait_for="say")
        self.run_action("GEORGE_SET", setting="gun_burst", value="50", speech="Burst 50.", wait_for="say")
        self.assertEqual(self.fg.burst, "50")
        self.assertEqual(self.said()[-1], "Burst 50.")

    def test_unresponsive_wheel_is_reported_not_claimed(self):
        self.run_action("GEORGE_SET", setting="weapon", value="GUN", wait_for="say")
        self.fg.burst_responds = False
        self.run_action("GEORGE_SET", setting="gun_burst", value="10", speech="Burst 10.", wait_for="rejected")
        self.assertTrue(self.said()[-1].startswith("Unable"))

    def test_focus_lost_sends_nothing(self):
        self.focus = False
        ok, reason = self.g.run_action("GEORGE_SET", setting="roe", value="FREE")
        self.assertFalse(ok)
        self.assertEqual(reason, "DCS not in focus.")
        self.assertEqual(self.kb.log, [])


class NoRockets(DriverCase):
    WEAPONS = ("NO WPN", "GUN", "MSL")              # rockets not loaded: the cycle skips RKT

    def test_weapon_reached_by_reading_the_wheel(self):
        self.fg.weapon = "MSL"
        self.fg.publish(0)
        time.sleep(0.4)
        self.run_action("GEORGE_SET", setting="weapon", value="GUN", wait_for="say")
        self.assertEqual(self.fg.weapon, "GUN")       # MSL -> NO WPN -> GUN

    def test_list_command_without_a_list_is_refused(self):
        self.open_wheel()
        weapon = self.fg.weapon
        ok, reason = self.g.run_action("LIST_EXIT")
        self.assertEqual((ok, reason), (False, "No list up."))
        time.sleep(0.3)
        self.assertEqual(self.fg.weapon, weapon)


class TargetList(DriverCase):
    def test_scan_reads_the_whole_list_and_returns_to_top(self):
        self.search_and_scan()
        self.assertEqual(self.g.list_names(), self.fg.targets)
        self.assertEqual(self.fg.cursor, 0)

    def test_lock_target_beyond_the_window(self):
        self.search_and_scan()
        self.run_action("SELECT_TARGET_INDEX", target_index=5, wait_for="tracking")
        self.assertEqual(self.fg.events[-1], "TRACK LARC-V")
        self.assertEqual([d["index"] for e, d in self.events if e == "tracking"], [5])

    def test_too_high_a_number_is_refused(self):
        self.search_and_scan()
        ok, reason = self.g.run_action("SELECT_TARGET_INDEX", target_index=9)
        self.assertEqual((ok, reason), (False, "Negative, only 6 targets."))

    def test_engage_several_targets(self):
        self.search_and_scan()
        self.run_action("ENGAGE_TARGETS", targets=[1, 4], wait_for="engaging")
        self.assertEqual(self.fg.events[-1], "ENGAGE ZSU-23-4,T-72B")


class NamedSearches(DriverCase):
    def test_area_search_drops_weapon_first(self):
        self.fg.weapon = "GUN"
        self.open_wheel()
        self.run_action("AREA_SEARCH", value="FWD AREA", wait_for="say", timeout=10)
        self.assertEqual(self.fg.events[-1], "AREA SEARCH FWD AREA")

    def test_point_search_changes_filter_when_needed(self):
        self.open_wheel()
        self.run_action("POINT_SEARCH_AT", value="W03", wait_for="say", timeout=12)
        self.assertEqual(self.fg.events[-2:], ["SLAVE W03", "POINT SEARCH W03"])

    def test_unknown_point(self):
        self.open_wheel()
        self.run_action("POINT_SEARCH_AT", value="T99", wait_for="rejected", timeout=12)
        self.assertEqual(self.said()[-1], "Unable, no point tango nine nine.")


if __name__ == "__main__":
    unittest.main()
