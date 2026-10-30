"""Wingmen, step 1: flight line-up, who's alive, and the wingman menu map (no order ever sent)."""
import os
import tempfile
import unittest

from tests.fakes import wait_until
from tests.test_extras import FakeHook
from common import comms_menu, wingmen


class WingHook(FakeHook):
    TREE = {"": ["Wingman", "Flight", "ATC", "Other"],
            "Wingman": ["Navigation", "Engage", "Rejoin formation", "Return to base"],
            "Wingman/Navigation": ["Anchor here", "Return to base"],
            "Wingman/Engage": ["My target", "Ground targets", "Air defenses"],
            "Flight": ["Engage", "Rejoin formation"],
            "Flight/Engage": ["My target", "Ground targets"],
            "ATC": ["Kobuleti"], "ATC/Kobuleti": ["Request Startup"]}


def snap(alive=("Gunfighter-2",), age=1.0):
    return {"flight_group": "Gunfighter",
            "flight_units": [(1, "Gunfighter-1", "Player", True), (2, "Gunfighter-2", "High", False),
                             (3, "Gunfighter-3", "Excellent", False)],
            "flight_alive": list(alive), "flight_alive_age": age, "aircraft": "AH-64D_BLK_II"}


class TestFlight(unittest.TestCase):
    def test_parse(self):
        g, u = comms_menu.parse_flight("FLIGHT Gunfighter\n1\tGunfighter-1\tPlayer\t1\n2\tGunfighter-2\tHigh\t0")
        self.assertEqual((g, u), ("Gunfighter", [(1, "Gunfighter-1", "Player", True), (2, "Gunfighter-2", "High", False)]))

    def test_check(self):
        self.assertEqual(wingmen.check_report(snap()), "Two, AI, up. Three, AI, down.")
        self.assertEqual(wingmen.check_report(snap(age=99)), "Two, AI. Three, AI.")       # alive unknown
        self.assertEqual(wingmen.check_report({"flight_units": [(1, "Solo", "Player", True)]}),
                         "No wingmen, we're a single ship.")
        self.assertIn("No flight data", wingmen.check_report({}))


class TestMenuMap(unittest.TestCase):
    def setUp(self):
        self.menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.4)
        self.hook = WingHook(self.menu)
        wait_until(self.menu.live, 1)

    def test_maps_without_sending_orders(self):
        tree = self.menu.map_tree()
        lines = wingmen.menu_lines(tree)
        self.assertIn("    F1 My target", lines)
        self.assertIn("  F4 Return to base", lines)
        self.assertFalse(any("Kobuleti" in l for l in lines))            # ATC branch not walked
        self.assertEqual(self.hook.done, [])                             # nothing was ordered
        self.assertFalse(self.hook.open)                                 # menu closed again

    def test_voice_command_saves_files(self):
        said = []
        with tempfile.TemporaryDirectory() as d:
            old = wingmen.map_menu.__defaults__
            wingmen.map_menu.__defaults__ = (d, "wingman")
            try:
                out = wingmen.handle("George, map the wingman menu", self.menu, snap(), said.append)
            finally:
                wingmen.map_menu.__defaults__ = old
            files = sorted(os.listdir(d))
        self.assertEqual(said, ["Mapping the wingman menu, stand by."])
        self.assertTrue(out.startswith("Wingman menu mapped"))
        self.assertEqual(len(files), 2)
        self.assertTrue(files[0].startswith("wingman_menu_AH-64D_BLK_II_3ship_"))


class CarrierHook(FakeHook):
    TREE = {"": ["Wingman", "Flight", "ATC", "Other"],
            "Wingman": ["Engage"], "Wingman/Engage": ["My target"],
            "ATC": ["Kobuleti", "CVN-73 George Washington"],
            "ATC/Kobuleti": ["Request Startup"],
            "ATC/CVN-73 George Washington": ["Inbound", "Request Startup", "Ready for departure"]}


class TestCarrierMenuMap(unittest.TestCase):
    def setUp(self):
        self.menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.4)
        self.hook = CarrierHook(self.menu)
        wait_until(self.menu.live, 1)

    def test_map_carrier_menu(self):
        said = []
        with tempfile.TemporaryDirectory() as d:
            old = wingmen.map_menu.__defaults__
            wingmen.map_menu.__defaults__ = (d, "wingman")
            try:
                out = wingmen.handle("map carrier menu", self.menu, dict(snap(), aircraft="FA-18C_hornet"),
                                     said.append)
            finally:
                wingmen.map_menu.__defaults__ = old
            files = sorted(os.listdir(d))
            with open(os.path.join(d, files[1]), encoding="utf-8") as f:
                text = f.read()
        self.assertEqual(said, ["Mapping the carrier and ATC menu, stand by. Hands off the radio menu."])
        self.assertTrue(out.startswith("Carrier and ATC menu mapped"), out)
        self.assertTrue(files[0].startswith("carrier_menu_FA-18C_hornet_"))
        self.assertIn("F1 Inbound", text)
        self.assertNotIn("My target", text)                               # wingman branch not walked
        self.assertEqual(self.hook.done, [])                             # nothing was ordered


if __name__ == "__main__":
    unittest.main()


ENGAGE = ["My Target", "My Enemy", "Bandits", "Air Defences", "Ground Targets", "Mission and Rejoin", "Mission and RTB"]
WING = ["Engage", "Go Pincer", "Go To", "Cover Me", "Reconnaissance", "Hold Position", "Rejoin Formation",
        None, None, "Jettison Weapons"]


class RealMenuHook(FakeHook):
    """The AH-64D 4-ship menu mapped on 19 Oct 2026, with DCS's gaps (hidden items keep their F-key)."""
    TREE = {"": ["Flight", "Wingman 2", "Wingman 3", None, "ATC"],
            "Flight": ["Engage", "Go Pincer", "Go To", "Cover Me", "Go Formation", "Hold Position",
                       "Rejoin Formation", None, None, "Jettison Weapons"],
            "Flight/Go Formation": [None, "Echelon", "Spread", "Trail", "Overwatch"],
            "ATC": ["Kobuleti"], "ATC/Kobuleti": ["Request Startup"]}
    for _w in ("Flight", "Wingman 2", "Wingman 3"):
        TREE[_w + "/Engage"] = ENGAGE
        TREE[_w + "/Go Pincer"] = ["Right", "Left"]
        TREE[_w + "/Go To"] = ["Return to Base", "Route"]
    for _w in ("Wingman 2", "Wingman 3"):
        TREE[_w] = WING
        TREE[_w + "/Reconnaissance"] = ["1 km", "2 km", "3 km", "5 km", "8 km", "10 km"]

    def items(self):
        names = self.TREE.get("/".join(self.path), [])
        out = {i + 1: n + ("..." if "/".join(self.path + [n]) in self.TREE else "")
               for i, n in enumerate(names) if n}
        if self.path:
            out[11] = "Previous Menu"
        out[12] = "Exit"
        return out


def flight(alive=("Rotary-2-2", "Rotary-2-3", "Rotary-2-4")):
    return {"flight_group": "Rotary-2",
            "flight_units": [(1, "Rotary-2-1", "Player", True), (2, "Rotary-2-2", "High", False),
                             (3, "Rotary-2-3", "High", False), (4, "Rotary-2-4", "High", False)],
            "flight_alive": list(alive), "flight_alive_age": 1.0, "aircraft": "AH-64D_BLK_II"}


class TestOrders(unittest.TestCase):
    def setUp(self):
        self.menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.4)
        self.hook = RealMenuHook(self.menu)
        wait_until(self.menu.live, 1)

    def say(self, text, snap=None):
        return wingmen.handle(text, self.menu, snap or flight(), lambda t: None)

    def test_orders_go_through_quietly(self):
        self.assertEqual(self.say("Two, return to base"), "")
        self.assertEqual(self.say("flight, engage my target"), "")
        self.assertEqual(self.say("three, take out the SAMs"), "")
        self.assertEqual(self.say("flight go trail"), "")
        self.assertEqual(self.say("three, recon 5 kilometers"), "")
        self.assertEqual(self.hook.done, ["Wingman 2/Go To/Return to Base", "Flight/Engage/My Target",
                                          "Wingman 3/Engage/Air Defences", "Flight/Go Formation/Trail",
                                          "Wingman 3/Reconnaissance/5 km"])
        self.assertFalse(self.hook.open)

    def test_checks_before_the_menu(self):
        self.assertEqual(self.say("three, cover me", flight(alive=("Rotary-2-2",))), "Three is down.")
        two_ship = dict(flight(), flight_units=flight()["flight_units"][:2])
        self.assertEqual(self.say("three, cover me", two_ship), "Negative, we're a two ship, there's no three.")
        self.assertEqual(self.say("wingman, rejoin", flight()), "Which wingman?")
        self.assertEqual(self.say("wingman, rejoin", two_ship), "")
        solo = dict(flight(), flight_units=flight()["flight_units"][:1])
        self.assertEqual(self.say("two, rejoin", solo), "Negative, no wingmen.")
        self.assertEqual(self.hook.done, ["Wingman 2/Rejoin Formation"])

    def test_not_in_this_menu(self):
        # "Wingman 4" is hidden in this menu (F4 had no text): George says so, menu closed
        self.assertEqual(self.say("four, hold position"), "Four isn't in the radio menu. Tell flight, two or three.")
        self.assertFalse(self.hook.open)
        self.assertEqual(self.hook.done, [])

    def test_old_hook_without_line_up(self):
        snap = {"flight_alive": ["Rotary-2-2"], "flight_alive_age": 1.0}
        self.assertEqual(self.say("two, rejoin", snap), "")
        self.assertEqual(self.hook.done, ["Wingman 2/Rejoin Formation"])

    def test_wingman_not_listed(self):
        # 3-ship UH-1H, 19 Oct: the menu had only "Wingman 2" although three was flying
        huey = RealMenuHook.TREE.copy()
        self.hook.TREE = dict(huey, **{"": ["Flight", "Wingman 2", None, None, "ATC"]})
        three_ship = dict(flight(), flight_units=flight()["flight_units"][:3])
        self.assertEqual(self.say("three, cover me", three_ship), "Three isn't in the radio menu. Tell flight or two.")
        self.assertEqual(self.say("four, whole position", flight()), "Four isn't in the radio menu. Tell flight or two.")
        self.assertEqual(self.hook.done, [])

    def test_line_up_of_another_group_is_ignored(self):
        # Apache flight, 19 Oct: the hook reported the Huey group (the mission's "Player" unit)
        snap = dict(flight(alive=("Gunfighter-2",)), my_group="Gunfighter", my_unit="Gunfighter-1")
        self.assertEqual(self.say("two, rejoin", snap), "")                 # from what Export.lua sees
        self.assertEqual(self.hook.done, ["Wingman 2/Rejoin Formation"])

    def test_not_orders(self):
        for text in ("all gunners only return fire if fired upon", "everybody stop shooting",
                     "waypoint two", "two", "to the left of the road"):
            with self.subTest(text=text):
                self.assertIsNone(self.say(text))
        # addressed to the wingmen but not an order: never handed to George (flight, 19 Oct)
        self.assertEqual(self.say("flight, sing a song"), "Say again the order.")
        self.assertEqual(self.say("flight status"), "Two, AI, up. Three, AI, up. Four, AI, up.")
