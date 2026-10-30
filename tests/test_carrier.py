"""Supercarrier by voice (common/carrier.py), against a fake radio menu that changes the way DCS's did
on 28 Oct: Inbound first (under ATC), then the main page IS the carrier page."""
import threading
import unittest

from common import comms_menu
from common.carrier import CarrierCalls, QuietWindow, call_of
from tests.fakes import wait_until

ARRIVAL = "Arrival. Carrier approach CASE 2 and 3"


class SuperHook:
    """george_comms_hook.lua + Supercarrier's menu."""
    def __init__(self, menu):
        self.menu, self.path, self.open, self.done = menu, [], False, []
        self.stage = 0                          # 0 before Inbound, 1 marshal, 2 after commencing
        menu._send = self.on_command
        self.publish()

    def page(self):
        if self.stage and not self.path:
            items = {0: [], 1: ["Abort Inbound", "ESTABLISHED", "COMMENCING", "Request Azimuth"],
                     2: ["Abort Inbound", "CHECK IN", "Platform", "Request Azimuth"]}[self.stage]
            return ARRIVAL, {i + 1: n for i, n in enumerate(items)}, set()
        tree = {"": ["Wingman...", "Flight...", "ATC...", "Other..."],
                "ATC": ["CVN-71 Theodore Roosevelt (127.5 MHz AM)...", "Fujairah Intl (124.6 MHz AM)..."],
                "ATC/CVN-71 Theodore Roosevelt (127.5 MHz AM)": ["Inbound", "Request Azimuth"],
                "ATC/Fujairah Intl (124.6 MHz AM)": ["Inbound", "Request Azimuth"]}
        names = tree.get("/".join(self.path), [])
        items = {i + 1: n for i, n in enumerate(names)}
        return "Main. " + ". ".join(self.path), items, {k for k, v in items.items() if v.endswith("...")}

    def publish(self):
        if not self.open:
            text = "MENU 0\n"
        else:
            cap, items, _ = self.page()
            text = f"MENU 1\nCAPTION {cap}\n" + "".join(f"ITEM {k} F{k}. {v}\n" for k, v in items.items())
        threading.Timer(0.02, lambda: self.menu.update(comms_menu.parse_menu(text))).start()

    def on_command(self, cmd):
        if cmd == "OPEN":
            self.open, self.path = True, []
        elif cmd == "CLOSE":
            self.open = False
        elif cmd == "BACK" and self.path:
            self.path.pop()
        elif cmd.startswith("SELECT"):
            n = int(cmd.split()[1])
            _, items, subs = self.page()
            name = items.get(n, "")
            if n in subs:
                self.path.append(name.replace("...", ""))
            elif name:
                self.done.append(name)
                self.open = False
                if name == "Inbound" and "CVN" in "/".join(self.path):
                    self.stage = 1
                elif name == "COMMENCING":
                    self.stage = 2
                elif name == "Abort Inbound":
                    self.stage = 0
        self.publish()


class TestCalls(unittest.TestCase):
    def test_what_was_said(self):
        self.assertEqual(call_of("Marshal, 401, inbound"), "inbound")
        self.assertEqual(call_of("abort inbound"), "abort inbound")
        self.assertEqual(call_of("401 commencing"), "commencing")
        self.assertEqual(call_of("Hornet ball, 4.5"), "ball")
        self.assertEqual(call_of("checking in"), "check in")
        self.assertIsNone(call_of("say bingo"))


class TestCarrier(unittest.TestCase):
    def setUp(self):
        self.menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.4)
        self.hook = SuperHook(self.menu)
        wait_until(self.menu.live, 1)
        self.now = [0.0]
        self.quiet = QuietWindow(clock=lambda: self.now[0])
        self.jet, self.ships = [True], [True]
        self.c = CarrierCalls(self.menu, self.quiet, lambda: self.jet[0], lambda: self.ships[0])

    def test_recovery_by_voice(self):
        self.assertEqual(self.c.handle("Marshal, 401, inbound"), "")
        self.assertEqual(self.hook.done, ["Inbound"])                   # the carrier's, not Fujairah's
        self.assertTrue(self.quiet.active())
        self.assertEqual(self.c.freq, "127.5 AM")
        self.assertEqual(self.c.handle("established"), "")
        self.assertEqual(self.c.handle("401, commencing"), "")
        self.assertEqual(self.c.handle("platform"), "")
        self.assertEqual(self.c.handle("check in"), "")
        self.assertEqual(self.hook.done, ["Inbound", "ESTABLISHED", "COMMENCING", "Platform", "CHECK IN"])

    def test_call_not_on_the_page(self):
        self.c.handle("inbound")
        said = self.c.handle("Hornet ball, 4.5")
        self.assertEqual(said, "No ball call now. Carrier menu has: abort inbound, established, commencing, "
                               "request azimuth.")
        self.assertEqual(self.hook.done, ["Inbound"])                   # nothing else selected
        self.assertFalse(self.hook.open)

    def test_abort_ends_quiet(self):
        self.c.handle("inbound")
        self.c.handle("abort inbound")
        self.assertFalse(self.quiet.active())

    def test_only_in_jets_with_a_ship(self):
        self.jet[0] = False
        self.assertIsNone(self.c.handle("Marshal, inbound"))
        self.jet[0], self.ships[0] = True, False
        self.assertIsNone(self.c.handle("Marshal, inbound"))
        self.ships[0] = True
        self.assertIsNone(self.c.handle("Marshal, say bingo"))          # addressed, but not a carrier call
        self.assertEqual(self.hook.done, [])


class TestQuiet(unittest.TestCase):
    def test_hold_then_flush(self):
        now = [0.0]
        q = QuietWindow(clock=lambda: now[0])
        out = []
        say = q.gate(out.append)
        say("before")
        q.start()
        say("Overlord, pop-up group")
        now[0] = 30
        say("Outcast 2, splash one")
        now[0] = 80
        q.tick({"telemetry_age": 0.1, "speed_kts": 140.0, "alt_ft": 600.0})   # still flying
        self.assertEqual(out, ["before"])
        q.tick({"telemetry_age": 0.1, "speed_kts": 5.0, "alt_ft": 70.0})      # stopped on deck
        self.assertEqual(out, ["before", "Outcast 2, splash one"])            # the 80 s old one dropped
        self.assertFalse(q.active())
