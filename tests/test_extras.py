"""Navigation, Huey talk-down / sling-load talk-on, radio menu, Apache checklists and EUFD callouts."""
import math
import threading
import time
import unittest

from tests import fakes  # noqa: F401  (fast timings first)
from tests.fakes import wait_until
from core.state import state_store
from common import nav, nav_commands, comms_menu
from huey.talkdown import TalkDown
from apache import bios_map as abm
from apache.crew import ApacheCrew
from apache.eufd import EufdMonitor, messages, speakable

ROUTE = [(1, 0.0, 0.0), (2, 9260.0, 0.0), (3, 9260.0, 9260.0)]      # 5 nm north, then 5 nm east


def snap(pos=(0.0, 0.0, 100.0), hdg=0.0, vel=(0.0, 0.0, 0.0), agl=500.0, vs=0.0, wp=2, cargo=None, **kw):
    s = {"telemetry_age": 0.1, "pos": pos, "hdg": hdg, "vel": vel, "agl_ft": agl, "vs_fpm": vs,
         "route": list(ROUTE), "wp_next": wp, "cargo": cargo, "speed_kts": 0}
    s.update(kw)
    return s


class TestNav(unittest.TestCase):
    def test_waypoints(self):
        s = snap(vel=(46.3, 0.0, 0.0))                        # 90 kt north
        self.assertEqual(nav.waypoint_report(s, "next"),
                         "Waypoint 2, 12 o'clock, steer 000, 5.0 miles, 3 minutes.")
        self.assertEqual(nav.waypoint_report(s, 3), "Waypoint 3, 2 o'clock, steer 045, 7.1 miles, 5 minutes.")
        s = snap(pos=(9260.0, 9260.0, 100.0), hdg=90.0)
        self.assertTrue(nav.waypoint_report(s, "home").startswith("Home, 4 o'clock, steer 225, 7.1 miles"))
        self.assertEqual(nav.waypoint_report(s, 9), "No waypoint 9.")
        self.assertEqual(nav.waypoint_report(snap(route=[]), "next"), "No route loaded. Home is where we started.")
        # no route: home is where the flight started
        s = snap(route=[], pos=(0.0, 1852.0, 100.0), start_pos=(0.0, 0.0, 100.0), hdg=0.0)
        self.assertTrue(nav.waypoint_report(s, "home").startswith("Start point, 9 o'clock, steer 270, 1.0 miles"))
        self.assertTrue(nav.fuel_to(s, 300, 555, 185).startswith("Start point is 1.0 miles"))

    def test_fuel_and_bingo(self):
        s = snap(pos=(9260.0, 0.0, 100.0))                    # 5 nm from home
        said = nav.fuel_to(s, 600, 555, 185)
        self.assertEqual(said, "Home is 5.0 miles, about 31 pounds at 90 knots. You'd land with 569.")
        self.assertFalse(nav.bingo(s, 600, 555, 185))
        self.assertTrue(nav.bingo(s, 200, 555, 185))
        self.assertIsNone(nav.bingo(s, None, 555, 185))

    def test_phrases(self):
        s = snap()
        self.assertTrue(nav_commands.handle("next waypoint", s).startswith("Waypoint 2"))
        self.assertTrue(nav_commands.handle("how far to waypoint three", s).startswith("Waypoint 3"))
        self.assertTrue(nav_commands.handle("where's home", s).startswith("Home"))
        self.assertTrue(nav_commands.handle("fuel to home", s, 500).startswith("Home is"))
        self.assertEqual(nav_commands.handle("bingo?", s), "No fuel reading.")
        self.assertIsNone(nav_commands.handle("point search tango 07", s))
        self.assertIsNone(nav_commands.handle("status", s))
        self.assertEqual(nav.body_velocity((0.0, 1.0, 0.0), 90.0)[0], 1.0)   # east, heading east = forward


class TestTalkDown(unittest.TestCase):
    def run_seq(self, td, seq, t0=0.0):
        t = t0
        for s in seq:
            td.check(s, t)
            t += 0.25
        return t

    def test_landing(self):
        said = []
        td = TalkDown(said.append, calibration_file=None)
        # sit on the ground (reads 4 ft) -> learns the offset; take off; approach and land
        t = self.run_seq(td, [snap(agl=4.0)] * 16)
        self.assertAlmostEqual(td.ground_offset_ft, 4.0)
        t = self.run_seq(td, [snap(agl=10.0, vs=300), snap(agl=60.0, vs=500)], t)
        self.assertEqual(said, ["Skids up."])
        heights = [40, 30, 22, 19, 12, 9, 6, 4.5, 4.4]
        seq = [snap(agl=h + 4.0, vs=-200, vel=(0.0, 0.0, -1.0)) for h in heights]
        t = self.run_seq(td, seq, t)
        self.assertEqual(said[1:], ["Twenty feet.", "Ten.", "Five."])
        t = self.run_seq(td, [snap(agl=6.5, vs=-100, vel=(0, -1.0, 0))], t + 5)   # 2.5 ft, drifting left
        self.assertIn("Three.", said)
        self.assertIn("Drifting left.", said)
        self.run_seq(td, [snap(agl=4.2, vs=-10, vel=(0, 0, 0))], t + 5)
        self.assertEqual(said[-1], "Skids down.")

    def test_hard_descent_and_no_calls_climbing(self):
        said = []
        td = TalkDown(said.append, calibration_file=None)
        td.ground_offset_ft = 0.0
        self.run_seq(td, [snap(agl=50.0), snap(agl=12.0, vs=-600, vel=(5, 0, 0))])
        self.assertIn("Easy, slow your descent!", said)
        said.clear()
        td2 = TalkDown(said.append, calibration_file=None)
        td2.ground_offset_ft = 0.0
        self.run_seq(td2, [snap(agl=1.0), snap(agl=8.0, vs=400), snap(agl=15.0, vs=400)])
        self.assertEqual(said, ["Skids up."])

    def test_sling_load(self):
        said = []
        td = TalkDown(said.append, calibration_file=None)
        td.ground_offset_ft = 0.0
        cargo = (100.0, 0.0, 10.0)                    # 100 m north, on the ground at 10 m
        td.check(snap(agl=60.0, pos=(0.0, 0.0, 40.0)), 0)
        td.check(snap(agl=60.0, pos=(60.0, 0.0, 40.0), cargo=cargo), 1)
        self.assertEqual(said, [])                    # not asked: no talk-on (6 loads in one mission)
        self.assertEqual(td.load_report(snap(pos=(60.0, 0.0, 40.0), cargo=cargo)), "Load, 12 o'clock, 40 meters.")
        self.assertEqual(td.start_talkon(snap(pos=(60.0, 0.0, 40.0), cargo=cargo)),
                         "Talking you on. Load, 12 o'clock, 40 meters.")
        td.check(snap(agl=60.0, pos=(60.0, 0.0, 40.0), cargo=cargo), 2)
        self.assertEqual(said[-1], "Load, 12 o'clock, 40 meters.")
        td.check(snap(agl=60.0, pos=(60.0, 0.0, 40.0), cargo=cargo), 5.5)
        self.assertEqual(len(said), 1)                # same call again within 6 s: not repeated
        td.check(snap(agl=30.0, pos=(95.0, 4.0, 25.0), cargo=cargo), 9)
        self.assertEqual(said[-1], "Forward fifteen, left fifteen, down thirty.")
        td.check(snap(agl=15.0, pos=(99.0, 0.5, 15.0), cargo=cargo), 13)
        self.assertEqual(said[-1], "Steady, hold.")
        td.check(snap(agl=20.0, pos=(99.0, 0.5, 20.0), cargo=(99.0, 0.5, 12.0)), 14)
        self.assertEqual(said[-1], "Load's off the ground.")
        self.assertFalse(td.talkon)
        self.assertEqual(td.load_report(snap(cargo=(99.0, 0.5, 12.0))), "Load's on the hook.")

    def test_calibration(self):
        said = []
        td = TalkDown(said.append, calibration_file=None)
        cargo = (4.0, 1.0, 0.0)                         # reads 4 m ahead, 1 m right when "under" us
        s = snap(pos=(0.0, 0.0, 5.0), cargo=cargo)
        self.assertTrue(td.calibrate(s).startswith("Calibrated. It read fifteen feet ahead, three right."))
        self.assertEqual(td.load_report(s), "Load's right under you.")
        s2 = snap(pos=(-20.0, 0.0, 5.0), cargo=cargo)   # 20 m further back: 20 m ahead of the hook
        self.assertEqual(td.load_report(s2), "Load, 12 o'clock, 20 meters.")
        self.assertIn("Calibrate right over it", td.calibrate(snap(pos=(-50.0, 0.0, 5.0), cargo=cargo)))

    def test_landed_next_to_load(self):
        said = []
        td = TalkDown(said.append, calibration_file=None)
        td.ground_offset_ft = 0.0
        cargo = (3.0, 0.0, 0.0)
        td.check(snap(agl=10.0, pos=(0.0, 0.0, 3.0)), 0)
        td.start_talkon(snap(pos=(0.0, 0.0, 3.0), cargo=cargo))
        td.check(snap(agl=0.2, pos=(0.0, 0.0, 1.0), cargo=cargo), 1)
        self.assertEqual(said[-2:], ["Skids down.", "You're in range. Say hook the load."])


class FakeHook:
    """Simulates george_comms_hook.lua + the DCS radio menu."""
    TREE = {"": ["Flight", "ATC", "Ground Crew", "Other"],
            "ATC": ["Kobuleti", "Batumi"],
            "ATC/Kobuleti": ["Request Startup", "Request Taxi to Runway", "Inbound"],
            "Ground Crew": ["Rearm & Refuel", "Repair"]}

    def __init__(self, menu: comms_menu.CommsMenu):
        self.menu, self.path, self.open, self.done = menu, [], False, []
        menu._send = self.on_command
        self.publish()

    def items(self):
        names = self.TREE.get("/".join(self.path), [])
        out = {i + 1: n + ("..." if "/".join(self.path + [n]) in self.TREE else "") for i, n in enumerate(names)}
        if self.path:
            out[11] = "Previous Menu"
        out[12] = "Exit"
        return out

    def page_text(self) -> str:
        if not self.open:
            return "MENU 0\n"
        return "MENU 1\nCAPTION Main. " + ". ".join(self.path) + "\n" + "".join(
            f"ITEM {k} F{k}. {v}\n" for k, v in self.items().items())

    def show(self, path):
        """Test set-up: open the menu at this page at once (no timer), newer than any pending publish."""
        self.open, self.path = True, list(path)
        self._seq = getattr(self, "_seq", 0) + 1
        self.menu.update(comms_menu.parse_menu(self.page_text()))

    def publish(self):
        if not self.open:
            text = "MENU 0\n"
        else:
            text = "MENU 1\nCAPTION Main. " + ". ".join(self.path) + "\n" + "".join(
                f"ITEM {k} F{k}. {v}\n" for k, v in self.items().items())
        # 29 Oct: two timers started together can fire in either order on Windows (15 ms timer
        # ticks), so an older page could land last. Only the newest publish is applied.
        self._seq = getattr(self, "_seq", 0) + 1
        seq = self._seq
        threading.Timer(0.02, lambda: seq == self._seq and self.menu.update(comms_menu.parse_menu(text))).start()

    def on_command(self, cmd):
        if cmd == "OPEN":
            self.open, self.path = True, []
        elif cmd == "CLOSE":
            self.open = False
        elif cmd == "BACK" and self.path:
            self.path.pop()
        elif cmd.startswith("SELECT"):
            n = int(cmd.split()[1])
            name = comms_menu.clean_item(self.items().get(n, ""))
            if n == 12:
                self.open = False
            elif "/".join(self.path + [name]) in self.TREE:
                self.path.append(name)
            elif name:
                self.done.append("/".join(self.path + [name]))
                self.open = False
        self.publish()


class TestCommsMenu(unittest.TestCase):
    def setUp(self):
        self.menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.4)
        self.hook = FakeHook(self.menu)
        wait_until(self.menu.live, 1)

    def h(self, text):
        return comms_menu.handle(self.menu, text)

    def test_open_pick_by_name_and_number(self):
        self.assertEqual(self.h("radio menu"), "Main: 1 Flight, 2 ATC, 3 Ground Crew, 4 Other.")
        self.assertEqual(self.h("ATC"), "ATC: 1 Kobuleti, 2 Batumi.")
        self.assertEqual(self.h("back"), "Main: 1 Flight, 2 ATC, 3 Ground Crew, 4 Other.")
        self.assertEqual(self.h("F3"), "Ground Crew: 1 Rearm & Refuel, 2 Repair.")
        self.assertEqual(self.h("rearm"), "Rearm & Refuel.")
        self.assertEqual(self.hook.done, ["Ground Crew/Rearm & Refuel"])
        self.assertIsNone(self.h("fuel state"))                   # menu closed: not ours

    def test_one_sentence(self):
        said = self.h("radio, ATC, Kobuleti, request startup")
        self.assertEqual(said, "ATC, Kobuleti, Request Startup.")
        self.assertEqual(self.hook.done, ["ATC/Kobuleti/Request Startup"])
        said = self.h("request taxi")
        self.assertEqual(said, "ATC: 1 Kobuleti, 2 Batumi.")           # stops where it must ask
        self.assertEqual(self.h("Kobuleti"), "Kobuleti: 1 Request Startup, 2 Request Taxi to Runway, 3 Inbound.")
        self.assertEqual(self.h("close menu"), "Menu closed.")
        self.assertFalse(self.hook.open)

    def test_keys_without_hook(self):
        pressed = []
        m = comms_menu.CommsMenu(speak=lambda t: None, press_keys=lambda k: pressed.extend(k) or True)
        self.assertEqual(comms_menu.handle(m, "radio menu"), "Radio menu.")
        self.assertEqual(comms_menu.handle(m, "five"), "F5.")
        self.assertEqual(comms_menu.handle(m, "close menu"), "Menu closed.")
        self.assertEqual(pressed, ["\\", "f5", "f12"])
        self.assertIsNone(comms_menu.handle(m, "ATC request startup"))   # can't read it: not handled
        # misheard F keys while the menu is up by keys; nothing else may reach the LLM meanwhile
        comms_menu.handle(m, "radio menu")
        self.assertEqual(comms_menu.handle(m, "F level"), "F11.")
        self.assertEqual(comms_menu.handle(m, "AP6"), "F6.")
        self.assertTrue(m.blocks_llm())
        comms_menu.handle(m, "close menu")
        self.assertFalse(m.blocks_llm())


def set_words(values: dict):
    with state_store._lock:
        state_store.bios_words.update(values)
    state_store.mark_bios_frame()


def put_field(key, value):
    addr, mask, shift, _ = abm.APACHE_SWITCHES[key]
    w = state_store.bios_words_snapshot().get(addr, 0)
    set_words({addr: (w & ~mask) | ((value << shift) & mask)})


def put_eufd(lines):
    for i, addr in enumerate(abm.EUFD_LINES):
        text = (lines[i] if i < len(lines) else "").ljust(abm.EUFD_LEN)[:abm.EUFD_LEN]
        set_words({addr + j: ord(text[j]) | (ord(text[j + 1]) << 8) for j in range(0, abm.EUFD_LEN, 2)})


class TestMenuFrequencies(unittest.TestCase):
    """29 Oct: "Nellis" got "say again" against "Nellis (327.000 MHz)", and the next page's caption
    was read as "7 MHz)"."""

    def test_frequency_tags_dont_count(self):
        w = comms_menu._words("Nellis")
        self.assertEqual(comms_menu.score(w, "Nellis (327.000 MHz, 132.550 MHz)"), 1.0)
        items = {1: "Creech (360.600 MHz)", 2: "Nellis (327.000 MHz)", 3: "Groom Lake [N/A]"}
        self.assertEqual(comms_menu.best_item(w, items), (2, 1.0))

    def test_readout_head(self):
        menu = {"caption": "Main. ATC. Nellis (327.000 MHz).", "items": {1: "Inbound", 2: "Request Azimuth"}}
        self.assertEqual(comms_menu.readout(menu), "Nellis: 1 Inbound, 2 Request Azimuth.")


class FakeApacheBios:
    def __init__(self):
        self.sent = []

    def send_command(self, ident, arg):
        self.sent.append((ident, arg))
        key = next(k for k, v in __import__("apache.checklists", fromlist=["SETTABLE"]).SETTABLE.items() if v == ident)
        threading.Timer(0.05, lambda: put_field(key, int(arg))).start()
        return True


class TestApache(unittest.TestCase):
    def setUp(self):
        with state_store._lock:
            state_store.bios_words = {}
        for k in abm.APACHE_SWITCHES:
            put_field(k, 0)
        set_words({0x870A: 0, 0x870C: 0})
        put_eufd([])
        self.said = []
        self.crew = ApacheCrew(self.said.append, state_store, FakeApacheBios(), readback_sec=0.5)

    def test_checklist(self):
        put_field("canopy", 0)          # closed
        put_field("park_brake", 1)      # pulled
        said = self.crew.handle("before start checklist")
        self.assertIn("Canopy, closed. Check.", said)
        self.assertIn("Parking brake, set. Check.", said)
        self.assertTrue(said.endswith("Master ignition, battery. Reads off."), said)
        said = self.crew.handle("set it")
        self.assertTrue(said.startswith("Master ign battery."), said)
        self.assertIn("Rotor brake, off. Reads lock.", said)
        put_field("rotor_brk", 2)
        said = self.crew.handle("check")
        self.assertIn("Power levers, off. Check.", said)
        self.assertTrue(said.endswith("Lights, as required."), said)
        self.assertEqual(self.crew.handle("check"), "Before starting engines checklist complete.")

    def test_levers(self):
        set_words({0x870A: 65535, 0x870C: 20000})
        self.assertEqual(self.crew._switch("levers"), "FLY and IDLE")
        self.assertEqual(self.crew._switch("pl2"), "IDLE")

    def test_eufd(self):
        lines = ["ENG1 OUT          |ENG1 OIL PSI LO   |APU ON",
                 "                  |                  |UHF 251.000"]
        self.assertEqual(messages(lines)["warning"], ["ENG1 OUT"])
        self.assertEqual(messages(lines)["caution"], ["ENG1 OIL PSI LO"])
        self.assertEqual(messages(lines)["advisory"], ["APU ON"])
        self.assertEqual(speakable("ENG1 OIL PSI LO"), "engine 1 oil pressure low")
        said = []
        mon = EufdMonitor(said.append, settle_sec=1)
        mon.check(["", ""], 0)
        mon.check(lines, 2)
        self.assertEqual(said, ["Warning, engine 1 out.", "Caution, engine 1 oil pressure low."])
        mon.check(lines, 3)
        self.assertEqual(len(said), 2)                       # once each
        put_eufd(lines)
        self.assertEqual(self.crew.handle("any cautions"),
                         "Warnings: engine 1 out. Cautions: engine 1 oil pressure low. Advisories: apu on.")


if __name__ == "__main__":
    unittest.main()


class TestCommonCheatSheet(unittest.TestCase):
    def test_every_phrase_is_handled(self):
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "COMMANDS_COMMON.md")
        with open(path, encoding="utf-8") as f:
            md = f.read()
        phrases = [p for row in re.findall(r"^\| (.+?) \|", md, flags=re.M) for p in re.findall(r"\*\*(.+?)\*\*", row)]
        self.assertGreater(len(phrases), 15)
        menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.2)
        hook = FakeHook(menu)
        wait_until(menu.live, 1)
        needs_open = {"ATC": [], "Kobuleti": ["ATC"], "ground crew": [], "rearm": [], "five": [],
                      "F5": [], "option 5": [], "back": ["ATC"], "previous menu": ["ATC"],
                      "close menu": [], "exit menu": []}
        for p in phrases:
            with self.subTest(phrase=p):
                if p in needs_open:
                    # 29 Oct: set the page directly; through the publish timers it sometimes didn't land
                    # on the PC (and a pending older timer is now ignored)
                    time.sleep(0.1)                     # let the last phrase's own publish land first
                    hook.show(needs_open[p])
                    want = ("Main. " + ". ".join(needs_open[p])).strip()   # the main page reads "Main."
                    got = menu.snapshot()[0]
                    self.assertTrue(got["visible"] and got["caption"] == want,
                                    f"menu at {got.get('caption')!r}, wanted {want!r} (hook path {hook.path})")
                from common import wingmen
                handled = (comms_menu.handle(menu, p) is not None
                           or nav_commands.handle(p, snap(), 500) is not None
                           or wingmen._MAP.search(p.lower()) is not None        # (would walk the menu)
                           or wingmen.handle(p, menu, {}, lambda t: None) is not None)
                self.assertTrue(handled)
