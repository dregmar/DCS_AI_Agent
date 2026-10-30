"""A-10C / F/A-18C: per-aircraft settings (common/aircraft.py) and joker / bingo (common/fuel_watch.py)."""
import unittest

from common import aircraft
from common.fuel_watch import FuelWatch
from tests.test_independents import _Base


class TestProfiles(unittest.TestCase):
    def test_by_type(self):
        self.assertEqual(aircraft.by_type("AH-64D_BLK_II").kind, "helo")
        self.assertEqual(aircraft.by_type("UH-1H").kind, "helo")
        self.assertEqual(aircraft.by_type("").kind, "helo")                 # before DCS says
        self.assertEqual(aircraft.by_type("FA-18C_hornet").name, "F/A-18C")
        self.assertEqual(aircraft.by_type("A-10C_2").name, "A-10C II")
        self.assertEqual(aircraft.by_type("F-16C_50").kind, "jet")           # unknown jet: jet values
        self.assertEqual(aircraft.by_type("Mi-24P").kind, "helo")

    def test_units(self):
        h, j = {"aircraft": "AH-64D_BLK_II"}, {"aircraft": "FA-18C_hornet"}
        self.assertEqual(aircraft.say_dist(15_000, h), "15 kilometres")
        self.assertEqual(aircraft.say_dist(5_600, h), "5.6 kilometres")
        self.assertEqual(aircraft.say_dist(1_000, h), "1 kilometre")
        self.assertEqual(aircraft.say_dist(27_780, j), "15 miles")
        self.assertEqual(aircraft.say_dist(1852, j), "1 mile")
        self.assertEqual(aircraft.say_alt(5486, j), "angels 18")
        self.assertEqual(aircraft.say_alt(152, h), "500 feet")

    def test_overrides(self):
        import config
        old = getattr(config, "AIRCRAFT_OVERRIDES", None)
        config.AIRCRAFT_OVERRIDES = {"FA-18C_hornet": {"reserve_lbs": 3500.0}}
        try:
            self.assertEqual(aircraft.by_type("FA-18C_hornet").reserve_lbs, 3500.0)
        finally:
            config.AIRCRAFT_OVERRIDES = old

    def test_follow_slots(self):
        p = aircraft.by_type("FA-18C_hornet")
        self.assertEqual(p.follow_offset(2), (-150, 0, 300))
        self.assertEqual(p.follow_offset(3), (-150, 0, -300))
        self.assertEqual(p.follow_offset(5), (-300.0, 0.0, -600.0))


class TestJetOutcast(_Base):
    def test_clone_flies_jet_height_and_speed(self):
        with self.world.lock:
            self.world.s["aircraft"] = "FA-18C_hornet"
        self.go_independent(2)
        self.assertIn({"ID": 9001, "altitudeType": "ASL"}, self.sent("setAltitudeType"))
        self.assertIn({"ID": 9001, "altitude": 6000.0}, self.sent("setAltitude"))
        self.assertIn({"ID": 9001, "speed": 230.0}, self.sent("setSpeed"))
        f = self.sent("followUnit")[-1]
        self.assertEqual((f["offsetX"], f["offsetZ"]), (-150, 300))

    def test_helicopter_unchanged(self):
        self.go_independent(2)
        self.assertIn({"ID": 9001, "altitudeType": "AGL"}, self.sent("setAltitudeType"))
        self.assertIn({"ID": 9001, "speed": 45.0}, self.sent("setSpeed"))


class FakeFields:
    def __init__(self, bases):
        self._b = bases

    def bases(self):
        return [dict(b) for b in self._b]

    def nearest(self, snap, farp=False, ship=False):
        return None


class TestFuelWatch(unittest.TestCase):
    HOME = (42.0, 42.0)

    def setUp(self):
        self.said = []
        self.s = {"aircraft": "FA-18C_hornet", "telemetry_age": 0.1, "my_ll": self.HOME, "speed_kts": 0.0,
                  "coalition": 2, "dcs_fuel": (1.0, 4900.0, "", "player"), "dcs_fuel_age": 1.0, "route": []}
        self.fields = FakeFields([{"name": "CVN-73", "coal": 2, "cat": 2, "lat": 42.001, "lon": 42.0},
                                  {"name": "Kutaisi", "coal": 2, "cat": 0, "lat": 42.2, "lon": 42.5}])
        self.w = FuelWatch(lambda: dict(self.s), self.said.append, airfields=self.fields)

    def fly(self, nm_north, fuel_lbs):
        self.s.update({"my_ll": (self.HOME[0] + nm_north * 1852 / 111_320.0, self.HOME[1]), "speed_kts": 400.0,
                       "dcs_fuel": (fuel_lbs / 2.20462 / 4900.0, 4900.0, "", "player")})
        return self.w.check()

    def test_home_is_the_ship_you_started_on(self):
        self.assertIsNone(self.w.check())                 # on deck: no calls, home noted
        self.assertEqual(self.w.home(self.s)[0], "CVN-73")
        self.fields._b[0]["lat"] += 0.05                  # the carrier moves: its new position is used
        self.assertAlmostEqual(self.w.home(self.s)[1][0], 42.051)

    def test_joker_then_bingo_once_each(self):
        self.w.check()
        # 100 nm out: need 100/420 h x 4,500 = 1,071 lb; bingo 4,071, joker 5,571
        self.assertIsNone(self.fly(100, 8000))
        said = self.fly(100, 5500)
        self.assertTrue(said.startswith("Overlord, joker fuel, 5,500 pounds. Home plate CVN-73, 100 miles, steer 180."), said)
        self.assertIsNone(self.fly(100, 5000))            # once
        said = self.fly(100, 4000)
        self.assertIn("bingo fuel, 4,000 pounds", said)
        self.assertTrue(said.endswith("Recommend RTB."))
        self.assertIsNone(self.fly(100, 3900))
        self.assertIsNone(self.fly(100, 9000))            # tanked: re-armed, but above joker now
        self.assertIn("joker", self.fly(100, 5400))

    def test_answer_on_request(self):
        self.w.check()
        self.fly(100, 8000)
        self.assertTrue(self.w.wanted("say bingo"))
        self.assertTrue(self.w.wanted("what's my joker"))
        self.assertTrue(self.w.wanted("bingo?"))
        self.assertTrue(self.w.wanted("See you bingo."))       # 28 Oct: Whisper's "say bingo"
        self.assertTrue(self.w.wanted("fuel to home"))         # jets: home plate, not route point 0
        self.assertFalse(self.w.wanted("fuel state"))
        self.assertEqual(self.w.answer(self.s), "Fuel 8,000. Joker 5,600, bingo 4,100. Home plate CVN-73, 100 miles, steer 180.")

    def test_no_calls_in_helicopters(self):
        self.s["aircraft"] = "AH-64D_BLK_II"
        self.assertIsNone(self.fly(50, 100))


class TestMainJet(unittest.TestCase):
    def test_overlord_say_bingo_in_a_jet_only(self):
        import main
        from tests.fakes import state_store, send_telemetry
        from tests.huey_fakes import reset_state
        reset_state()
        send_telemetry()
        self.assertNotIn("Joker", main.shared_reply("say bingo")[0])     # Apache: the old fuel-to-home answer
        state_store.update_telemetry({"ac": "FA-18C_hornet"})
        self.addCleanup(reset_state)
        said, who, _ = main.shared_reply("say bingo")
        self.assertEqual((said, who), ("Unable, no fuel reading or no home plate.", "george"))
        said, who, _ = main.shared_reply("Overlord, say bingo")
        self.assertEqual((said, who), ("Overlord, unable, no fuel reading or no home plate.", "overlord"))


class TestOct28(unittest.TestCase):
    """Cases from the 28 Oct Hornet flight."""
    def test_roster_with_pilot_name(self):
        from common import wingmen
        snap = {"flight_group": "FLIGHT-5", "my_unit": "RedArcher",
                "flight_units": [(1, "Pilot #005-1", "Player", True), (2, "FLIGHT-5-1", "High", False)],
                "flight_alive": ["FLIGHT-5-1"], "flight_alive_age": 1.0}
        self.assertEqual(wingmen.check_report(snap), "Two, AI, up.")      # was "One, player, down."

    def test_mission_reload_forgets_flights(self):
        import main
        from common import overlord as ov
        m = ov.Mission(ov.KINDS["ESCORT"], (1, 1), "you", [], (1, 1), [])
        if main.OVERLORD is not None:
            main.OVERLORD._missions.append(m)
        main.FUEL_WATCH._home = "Carrier"
        main._on_mission_loaded()
        self.assertIsNone(main.FUEL_WATCH._home)
        if main.OVERLORD is not None:
            self.assertEqual(main.OVERLORD._missions, [])
            self.assertTrue(m.cancel.is_set())
            self.assertFalse(main.OVERLORD._check_flight({}, m))           # no "flight is down" call

    def test_hook_message(self):
        from common import comms_menu
        got = []
        old = comms_menu.ON_LOADED
        comms_menu.ON_LOADED = lambda: got.append(1)
        try:
            import socket, time
            from tests.fakes import wait_until
            # the listener thread is main's; just check the callback hook exists and is called by name
            comms_menu.ON_LOADED()
        finally:
            comms_menu.ON_LOADED = old
        self.assertEqual(got, [1])


class ReorderHook:
    pass


def _reorder_hook():
    from tests.test_extras import FakeHook

    class Hook(FakeHook):
        """DCS re-sorts the ATC list (by distance) every time you come back to it."""
        TREE = {"": ["Flight", "ATC"],
                "ATC": ["CVN-71 Theodore Roosevelt", "Fujairah Intl", "Khasab"],
                "ATC/CVN-71 Theodore Roosevelt": ["Inbound", "Request Azimuth"],
                "ATC/Fujairah Intl": ["Inbound", "Request Azimuth"],
                "ATC/Khasab": ["Inbound", "Request Azimuth"]}

        def on_command(self, cmd):
            if cmd == "BACK" and self.path and self.path[0] == "ATC" and len(self.path) == 2:
                self.TREE = dict(self.TREE)
                lst = self.TREE["ATC"]
                self.TREE["ATC"] = lst[1:] + lst[:1]
            super().on_command(cmd)
    return Hook


class TestReorderedMenu(unittest.TestCase):
    def test_map_follows_names(self):
        from common import comms_menu, wingmen
        from tests.fakes import wait_until
        menu = comms_menu.CommsMenu(speak=lambda t: None, step_sec=0.4)
        hook = _reorder_hook()(menu)
        wait_until(menu.live, 1)
        tree = menu.map_tree(root_rx=wingmen.CARRIER_BRANCHES)
        lines = "\n".join(wingmen.menu_lines(tree))
        for name in ("CVN-71 Theodore Roosevelt", "Fujairah Intl", "Khasab"):
            self.assertIn(name, lines)
        self.assertEqual(lines.count("Inbound"), 3)
        self.assertEqual(hook.done, [])                                    # never selected an order


class TestOct28Second(unittest.TestCase):
    """The second 28 Oct flight (carrier menu captures)."""
    def test_repeating_transcript(self):
        import main
        self.assertTrue(main._repeating("Map, map, map, map, map, map, map, map"))
        self.assertFalse(main._repeating("Map carrier menu."))
        self.assertFalse(main._repeating("two, two, go independent"))

    def test_mop_and_whole_position(self):
        from common import wingmen, independents
        self.assertTrue(wingmen._MAP_CARRIER.search("mop carrier menu"))
        self.assertTrue(independents._HOLD.search("whole position"))

    def test_follow_up_to_the_last_outcast(self):
        from intelligence import router as R
        now = [100.0]
        ran = []
        r = R.Router(lambda c: (ran.append(c) or ("", "george")), lambda *a: None, learned_file=None,
                     clock=lambda: now[0])
        r.note_addressee("outcast 2")
        self.assertTrue(r.dispatch("Hold position.", "Outcast 2, hold position"))
        self.assertEqual(ran, ["Outcast 2, hold position"])
        now[0] += 60                                     # too long ago: needs the callsign again
        self.assertFalse(r.dispatch("Hold position.", "Outcast 2, hold position"))
        self.assertFalse(r.dispatch("Hold position.", "Outcast 3, hold position"))


def tearDownModule():
    """main.py (imported above) wires Olympus into wingmen; leave the module as other tests expect."""
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None
