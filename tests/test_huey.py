"""UH-1H Huey profile: parsers, commands, crew closed loop, cockpit readback, checklists, callouts."""
import time
import unittest

from tests.fakes import wait_until, send_telemetry
from tests.huey_fakes import FakeHuey, reset_state, panel_text
from core.state import state_store
from huey import commands
from huey.checklists import ChecklistRunner
from huey.cockpit import Cockpit, switch_state
from huey.crew import HueyCrew
from huey.crew_chief import CrewChief
from huey.gauges import convert, needle_to_value, GAUGES
from huey.link import parse_packet
from huey.panel import parse_panel
from huey.profile import HueyProfile


def needle(key, value):
    """Engineering value -> needle position, using the game's table (for building fake data)."""
    for arg, (k, inp, out) in GAUGES.items():
        if k == key:
            for i in range(1, len(inp)):
                if value <= inp[i]:
                    f = (value - inp[i - 1]) / (inp[i] - inp[i - 1])
                    return arg, out[i - 1] + f * (out[i] - out[i - 1])
    raise KeyError(key)


class TestParsers(unittest.TestCase):
    def test_gauge_inverse(self):
        self.assertAlmostEqual(needle_to_value(0.604, *GAUGES[121][1:]), 600.0, places=1)
        self.assertAlmostEqual(needle_to_value(0.9, *GAUGES[123][1:]), 324.0, places=1)
        self.assertAlmostEqual(needle_to_value(0.029, *GAUGES[124][1:]), 0.0, places=1)
        v = convert({121: 0.604, 98: 1.0, 254: 1.0, 91: 0.0})
        self.assertEqual(v["egt_c"], 600.0)
        self.assertEqual(v["cautions"], ["twenty minute fuel"])
        self.assertIn("armed", v["lamps"])
        self.assertIsNone(v["rotor_rpm"])

    def test_panel(self):
        rows = [{"status": "PLAYER", "ammo": " -", "burst": "  -"},
                {"status": "HOLD", "ammo": "100%", "burst": "SHORT"},
                {"status": "RET.FIRE", "ammo": "85%", "burst": "LONG"},
                {"status": "DEAD", "ammo": " -", "burst": "  -"}]
        p = parse_panel(panel_text(rows))
        self.assertEqual(p["pilot"]["state"], "PLAYER")
        self.assertEqual(p["copilot"], {"roe": "HOLD", "state": None, "ammo": 100, "burst": "SHORT"})
        self.assertEqual(p["left"]["roe"], "RETURN")
        self.assertEqual(p["left"]["ammo"], 85)
        self.assertEqual(p["right"]["state"], "DEAD")
        self.assertEqual(parse_panel("#1#"), {})

    def test_packet(self):
        d = parse_packet("HUEY 1\nac=UH-1H\nargs=123=0.9000,98=1\nsnares=30,58\n")
        self.assertEqual(d["rotor_rpm"], 324.0)
        self.assertEqual(d["flares"], 58)
        self.assertEqual(d["panel"], {})


class TestCommands(unittest.TestCase):
    def m(self, text, active=False):
        r = commands.match(text, active)
        return r and {k: v for k, v in r.items() if k not in ("speech", "source")}

    def test_crew(self):
        self.assertEqual(self.m("door gunners weapons free"),
                         {"action": "CREW_ROE", "seats": ["left", "right"], "value": "FREE"})
        self.assertEqual(self.m("George, cease fire"),
                         {"action": "CREW_ROE", "seats": ["left", "right", "copilot"], "value": "HOLD"})
        self.assertEqual(self.m("right gunner, return fire only"),
                         {"action": "CREW_ROE", "seats": ["right"], "value": "RETURN"})
        self.assertEqual(self.m("left gunner long bursts"),
                         {"action": "CREW_BURST", "seats": ["left"], "value": "LONG"})
        self.assertEqual(self.m("copilot weapons free")["seats"], ["copilot"])
        self.assertIsNone(self.m("don't open fire"))
        self.assertEqual(self.m("crew status")["action"], "CREW_REPORT")
        self.assertEqual(self.m("ammo check")["action"], "CREW_REPORT")

    def test_switches(self):
        self.assertEqual(self.m("master arm on"), {"action": "SWITCH", "key": "master_arm", "value": "ARMED"})
        self.assertEqual(self.m("select rockets"), {"action": "SWITCH", "key": "arm_sel", "value": "ROCKETS"})
        self.assertEqual(self.m("rocket pairs three"), {"action": "SWITCH", "key": "rocket_pair", "value": "3"})
        self.assertEqual(self.m("landing light on"), {"action": "SWITCH", "key": "ldg_light", "value": "ON"})
        self.assertEqual(self.m("search light stow")["value"], "STOW")
        self.assertEqual(self.m("pop flares"), {"action": "FLARE"})
        self.assertEqual(self.m("flares armed"), {"action": "SWITCH", "key": "cm_arm", "value": "ARMED"})
        self.assertIsNone(self.m("don't turn the battery off"))

    def test_autopilot_radio_checklist(self):
        self.assertEqual(self.m("autopilot attitude hold"), {"action": "AUTOPILOT", "value": "ATTITUDE"})
        self.assertEqual(self.m("autopilot on"), {"action": "AUTOPILOT", "value": "TOGGLE"})
        self.assertEqual(self.m("autopilot orbit"), {"action": "AUTOPILOT", "value": "ORBIT"})
        self.assertEqual(self.m("UHF 251.5"), {"action": "RADIO", "key": "uhf", "mhz": 251.5})
        self.assertEqual(self.m("set VHF one two four decimal seven five"),
                         {"action": "RADIO", "key": "vhf_am", "mhz": 124.75})
        self.assertEqual(self.m("FM 30 decimal 5")["mhz"], 30.5)
        self.assertEqual(self.m("before start checklist"), {"action": "CHECKLIST_START", "value": "before_start"})
        self.assertEqual(self.m("run up checklist")["value"], "runup")
        self.assertEqual(self.m("before takeoff checklist")["value"], "before_takeoff")
        self.assertEqual(self.m("shutdown checklist")["value"], "shutdown")
        self.assertEqual(self.m("check", active=True), {"action": "CHECKLIST_REPLY", "value": "CHECK"})
        self.assertEqual(self.m("set it", active=True)["value"], "SET")
        self.assertIsNone(self.m("check"))                       # no checklist running
        self.assertEqual(self.m("stop checklist", active=True)["value"], "STOP")

    def test_flight_test_phrases(self):
        self.assertEqual(self.m("okay let's start engines"), {"action": "CHECKLIST_START", "value": "start"})
        self.assertEqual(self.m("walk me through the start of sequence."), {"action": "CHECKLIST_START", "value": "start"})
        self.assertEqual(self.m("fuel estate")["action"], "FUEL")
        self.assertEqual(self.m("engine stop us")["action"], "ENGINE")

    def test_queries(self):
        for text, action in (("status", "STATUS"), ("fuel state", "FUEL"), ("how much fuel", "FUEL"),
                             ("engine check", "ENGINE"), ("any contacts", "CONTACTS"),
                             ("caution lights", "CAUTIONS")):
            self.assertEqual(self.m(text)["action"], action, text)

    def test_llm_needs_related_words(self):
        self.assertEqual(commands.sanitize({"action": "FLARE"}, "a press board")["action"], "NONE")
        self.assertEqual(commands.sanitize({"action": "TALKON_START"}, "we're point two")["action"], "NONE")
        self.assertEqual(commands.sanitize({"action": "FLARE"}, "chaff and flares please")["action"], "FLARE")

    def test_sanitize(self):
        self.assertEqual(commands.sanitize({"action": "SWITCH", "key": "battery", "value": "on"})["value"], "ON")
        self.assertEqual(commands.sanitize({"action": "SWITCH", "key": "warp", "value": "ON"})["action"], "NONE")
        self.assertEqual(commands.sanitize({"action": "LAUNCH_NUKES"})["action"], "NONE")
        r = commands.sanitize({"action": "CREW_ROE", "value": "HOLD"})
        self.assertEqual(r["seats"], ["left", "right", "copilot"])


class HueyCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.game = FakeHuey()
        wait_until(lambda: state_store.profile() == "huey", 2)

    def tearDown(self):
        self.game.stop()
        time.sleep(0.15)
        reset_state()


class TestCrew(HueyCase):
    def crew(self):
        return HueyCrew(self.game.link, state_store.huey_snapshot, step_sec=0.5, panel_idle_hide_s=0.3)

    def test_profile_detected(self):
        self.assertEqual(state_store.profile(), "huey")

    def test_roe_closed_loop_opens_and_hides_panel(self):
        ok, said = self.crew().set_roe(["left", "right"], "FREE")
        self.assertTrue(ok, said)
        self.assertEqual(said, "Gunners, free fire.")
        self.assertEqual(self.game.roe["left"], "FREE")
        self.assertEqual(self.game.roe["right"], "FREE")
        self.assertEqual(self.game.roe["copilot"], "HOLD")
        self.assertTrue(wait_until(lambda: not self.game.panel_visible, 2))   # auto-hidden again

    def test_hold_from_free(self):
        self.game.roe["left"] = "FREE"
        self.game.panel_visible = True
        ok, said = self.crew().set_roe(["left"], "HOLD")
        self.assertTrue(ok, said)
        self.assertEqual(self.game.roe["left"], "HOLD")
        self.assertEqual(said, "Left gunner, weapons hold.")
        self.assertTrue(self.game.panel_visible)            # it was up already: stays up

    def test_burst(self):
        ok, said = self.crew().set_burst(["right"], "LONG")
        self.assertTrue(ok)
        self.assertEqual(self.game.burst["right"], "LONG")
        ok, said = self.crew().set_burst(["right"], "LONG")
        self.assertTrue(ok)
        self.assertEqual(self.game.burst["right"], "LONG")   # already: no extra press

    def test_dead_and_unresponsive(self):
        self.game.dead.add("left")
        ok, said = self.crew().set_roe(["left", "right"], "FREE")
        self.assertFalse(ok)
        self.assertIn("Right gunner, free fire.", said)
        self.assertIn("Left gunner is down.", said)
        self.game.respond = False
        ok, said = self.crew().set_roe(["right"], "HOLD")
        self.assertFalse(ok)
        self.assertIn("not responding", said)

    def test_report(self):
        self.game.roe["left"], self.game.ammo["left"] = "FREE", 40
        ok, said = self.crew().report()
        self.assertTrue(ok)
        self.assertIn("Left gunner, free fire, 40 percent, short bursts.", said)


class TestCockpit(HueyCase):
    def cockpit(self):
        return Cockpit(self.game.bios, state_store.bios_words_snapshot,
                       lambda: state_store.snapshot()["bios_age"], readback_sec=0.6)

    def test_switch_readback(self):
        c = self.cockpit()
        self.assertEqual(switch_state(state_store.bios_words_snapshot(), "master_arm"), "SAFE")
        ok, said = c.set_switch("master_arm", "ARMED")
        self.assertTrue(ok, said)
        self.assertEqual(said, "Master arm armed.")
        ok, said = c.set_switch("master_arm", "ARMED")
        self.assertIn("already", said)
        self.game.bios_responds = False
        ok, said = c.set_switch("ldg_light", "ON")
        self.assertFalse(ok)
        self.assertIn("didn't move", said)

    def test_radio(self):
        c = self.cockpit()
        self.assertEqual(c.dial_freq("uhf"), 232.0)
        ok, said = c.set_radio("uhf", 251.35)               # INC turns this dial down: learned
        self.assertTrue(ok, said)
        self.assertEqual(said, "UHF 251 decimal 35.")
        self.assertEqual(c.dial_freq("uhf"), 251.35)
        ok, said = c.set_radio("vhf_am", 125.8)
        self.assertTrue(ok, said)
        self.assertEqual(c.dial_freq("vhf_am"), 125.8)
        ok, said = c.set_radio("vhf_fm", 30.55)
        self.assertTrue(ok, said)
        self.assertEqual(said, "FM 30 decimal 55.")
        self.assertFalse(c.set_radio("uhf", 100.0)[0])
        self.assertIn("50 kilohertz steps", c.set_radio("uhf", 251.33)[1])
        self.game.uhf_follows = False
        ok, said = c.set_radio("uhf", 240.0)
        self.assertIn("mode dial", said)

    def test_flare_arms_first(self):
        c = self.cockpit()
        flares = lambda: state_store.huey_snapshot()[0].get("flares")
        ok, said = c.dispense_flare(lambda: self.game.flares)
        self.assertTrue(ok, said)
        self.assertTrue(said.startswith("Flares armed. Flare away"), said)


class TestChecklist(unittest.TestCase):
    def test_flow(self):
        switches = {"inverter": "OFF", "main_gen": "ON", "starter_gen": "START", "battery": "OFF"}
        sets = []

        def set_switch(key, state):
            sets.append((key, state))
            switches[key] = state
            return True, f"{key} {state}."
        r = ChecklistRunner(lambda k: switches.get(k), lambda k: None, lambda: [], set_switch)
        said = r.start("before_start")
        self.assertTrue(said.startswith("Before starting engine checklist. Circuit breakers in"), said)
        said = r.reply("CHECK")
        self.assertIn("Inverter, off. Check.", said)
        self.assertIn("Main generator, on. Check.", said)
        self.assertTrue(said.endswith("Battery, on. Reads off."), said)
        self.assertIn("Still reads off", r.reply("CHECK"))
        said = r.reply("SET")
        self.assertEqual(sets, [("battery", "ON")])
        self.assertTrue(said.startswith("battery ON. Fire warning light, test."), said)
        said = r.reply("SKIP")
        self.assertIn("Skipped.", said)
        self.assertIn("Stopped".lower(), r.reply("STOP").lower())
        self.assertFalse(r.active)

    def test_gauges_and_complete(self):
        r = ChecklistRunner(lambda k: None, lambda k: {"engine_rpm": 6600}.get(k), lambda: [],
                            lambda k, s: (False, ""))
        said = r.start("before_takeoff")
        self.assertIn("RPM, sixty-six hundred. Check.", said)
        self.assertIn("no cautions. Check.", said)
        said = r.reply("CHECK")
        self.assertEqual(said, "Before take-off checklist complete.")


class TestCrewChief(HueyCase):
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

    def test_limits_and_lamps(self):
        send_telemetry()
        state_store.update_telemetry({"agl_ft": 200, "speed_kts": 80, "vs_fpm": 0})
        self.gauges(rotor_rpm=324, engine_rpm=6600, n1_pct=90, egt_c=550, torque_psi=30)
        self.chief.check(now=100)
        self.chief.check(now=106)
        self.assertEqual(self.said, [])
        self.gauges(rotor_rpm=290)
        self.chief.check(now=107)
        self.chief.check(now=107.5)
        self.assertEqual(self.said, ["Rotor RPM low!"])        # once, not every check
        self.game.gauges[98] = 1.0
        self.game.publish()
        self.chief.check(now=108)
        self.assertIn("Twenty minutes of fuel.", self.said)
        for a in range(99, 107):                                # lamp test: many at once -> quiet
            self.game.gauges[a] = 1.0
        self.game.publish()
        n = len(self.said)
        self.chief.check(now=109)
        self.assertEqual(len(self.said), n)

    def test_flight_test_fixes(self):
        # torque sitting on 50 in fast cruise: one callout per minute, not one per wobble
        send_telemetry()
        state_store.update_telemetry({"agl_ft": 300, "speed_kts": 120, "vs_fpm": 0})
        self.gauges(rotor_rpm=324, engine_rpm=6600, n1_pct=100, egt_c=590, torque_psi=49)
        self.chief.check(now=100)
        self.chief.check(now=106)
        for t, tq in ((107, 51), (110, 44), (115, 51), (120, 44), (125, 51), (170, 44), (171, 51)):
            self.gauges(torque_psi=tq)
            self.chief.check(now=t)
        self.assertEqual(self.said.count("Torque fifty, over limit."), 2)
        # EGT over 625 during a start (engine not yet running) is not called
        self.said.clear()
        state_store.update_telemetry({"agl_ft": 0, "speed_kts": 0})
        self.gauges(engine_rpm=3000, n1_pct=45, egt_c=664, torque_psi=5)
        self.chief.check(now=200)
        self.assertEqual(self.said, [])

    def test_start_call_and_contacts(self):
        send_telemetry(contacts=[{"id": 7, "type": "ZU-23", "dist_m": 900, "brg": 90}], hdg=0)
        state_store.update_telemetry({"agl_ft": 0, "speed_kts": 0})
        self.gauges(n1_pct=10, rotor_rpm=5)
        self.chief.check(now=1)
        self.gauges(n1_pct=20, rotor_rpm=20)
        self.chief.check(now=2)
        self.gauges(n1_pct=41)
        self.chief.check(now=3)
        self.assertIn("Rotor's turning.", self.said)
        self.assertIn("Forty percent. Release the starter.", self.said)
        self.assertIn("Contact, ZU-23, 3 o'clock, 900 meters.", self.said)


class TestProfile(HueyCase):
    def test_voice_to_crew(self):
        said = []
        p = HueyProfile(said.append, state_store, self.game.bios, llm=None, link=self.game.link,
                        crew_step_sec=0.5, readback_sec=0.6)
        p.handle("door gunners, weapons free")
        self.assertTrue(wait_until(lambda: said, 4))
        self.assertEqual(said[0], "Gunners, free fire.")
        p.handle("master arm on")
        self.assertTrue(wait_until(lambda: len(said) > 1, 3))
        self.assertEqual(said[1], "Master arm armed.")
        state_store.update_telemetry({"agl_ft": 300, "speed_kts": 90})
        self.assertIn("not in flight", p.execute({"action": "SWITCH", "key": "main_fuel", "value": "OFF"}))
        st = p.status_report()
        self.assertIn("Gunners free fire.", st)
        self.assertLess(len(st), 160, st)
        # sling load: talk-on needs the crew chief's TalkDown; release goes over the crew link
        from huey.talkdown import TalkDown
        p.talkdown = TalkDown(said.append, calibration_file=None)
        state_store.update_telemetry({"pos": [0.0, 0.0, 50.0], "cargo": [40.0, 0.0, 45.0], "hdg": 0.0})
        self.assertEqual(p.execute({"action": "LOAD_REPORT"}), "Load, 12 o'clock, 40 meters.")
        self.assertTrue(p.execute({"action": "TALKON_START"}).startswith("Talking you on."))
        self.assertTrue(p.talkdown.talkon)
        self.assertEqual(p.execute({"action": "RELEASE_LOAD"}), "Load released.")
        self.assertIn((3005, 1.0), self.game.sent)
        p.handle("something the fast path does not know")
        self.assertTrue(wait_until(lambda: len(said) > 2, 2))
        self.assertEqual(said[2], "Say again.")


if __name__ == "__main__":
    unittest.main()


class TestCheatSheet(unittest.TestCase):
    def test_every_bold_phrase_is_instant(self):
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "COMMANDS_HUEY.md")
        phrases = []
        for bold in re.findall(r"\*\*(.+?)\*\*", open(path, encoding="utf-8").read()):
            if bold.startswith(("SPACE",)):
                continue
            words = bold.split()
            if "/" in words[-1]:
                phrases += [" ".join(words[:-1] + [w]) for w in words[-1].split("/")]
            else:
                phrases.append(bold)
        missed = [p for p in phrases if commands.match(p, checklist_active=True) is None]
        self.assertEqual(missed, [])
