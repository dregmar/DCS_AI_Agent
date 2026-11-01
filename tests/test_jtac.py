"""Overlord as JTAC (common/jtac.py)."""
import unittest

from common.jtac import Jtac, say_ddm, say_grid, text_coords

ME = (25.0, 56.0)
NM = 1852 / 111_320.0          # degrees of latitude per nautical mile


def snap(aircraft="A-10C_2", ground=None, ad=None):
    return {"aircraft": aircraft, "my_ll": ME, "telemetry_age": 0.1, "hdg": 0.0,
            "enemy_ground": ground if ground is not None else [
                ("Ural-375", 1, ME[0] + 5 * NM, ME[1], 312.0),
                ("ZSU-23-4 Shilka", 2, ME[0] + 6 * NM, ME[1], 318.0),
                ("T-72B", 3, ME[0] - 4 * NM, ME[1], 290.0),
                ("Osa 9A33 ln", 4, ME[0] + 7 * NM, ME[1], 330.0),        # SA-8, 1 nm north of the ZSU
                ("Kub 1S91 str", 5, ME[0] + 6 * NM, ME[1] + 0.02, 320.0),  # SA-6 radar: never a JTAC target
            ],
            "air_defence": ad or []}


class FakeOly:
    def __init__(self):
        self.sent = []

    def connect(self):
        return True

    def command(self, name, params):
        self.sent.append((name, params))
        return True, ""


class TestFormats(unittest.TestCase):
    def test_coordinates(self):
        self.assertEqual(say_ddm(25.6075, 56.8956), "North 25 degrees 36.450, East 56 degrees 53.736.")
        self.assertTrue(say_grid(25.6075, 56.8956, 5).startswith("Grid 40 Romeo, "))
        self.assertEqual(len(say_grid(25.6075, 56.8956, 5).split(", ")[2].split()), 5)
        self.assertIn("|", text_coords(25.6075, 56.8956))


class TestJtac(unittest.TestCase):
    def setUp(self):
        self.s = snap()
        self.said = []
        self.oly = FakeOly()
        self.j = Jtac(lambda: self.s, oly=self.oly, speak=self.said.append)

    def test_request_target_a10(self):
        r = self.j.handle("request target")
        self.assertTrue(r.startswith("Overlord, target Shilka."), r)           # most dangerous first
        self.assertIn("Grid 40 ", r)
        self.assertIn("Elevation 1,040 feet.", r)                              # 318 m
        self.assertIn("6 miles north of you.", r)
        self.assertIn("Threats: SA-8, 1 mile north of the target", r)
        self.assertNotIn("SA-6.", r.split("Threats")[0])                       # big SAM isn't a target ...
        self.assertIn("SA-6", r)                                               # ... but is a threat

    def test_hornet_gets_lat_long(self):
        self.s["aircraft"] = "FA-18C_hornet"
        r = self.j.handle("request target")
        self.assertIn("North 25 degrees", r)
        self.assertNotIn("Grid", r)

    def test_direction_kind_next_repeat(self):
        r = self.j.handle("request target, armour")
        self.assertTrue(r.startswith("Overlord, target T-72B."), r)
        r = self.j.handle("request target south of me")
        self.assertTrue(r.startswith("Overlord, target T-72B."), r)
        first = self.j.handle("request target")
        second = self.j.handle("next target")
        self.assertNotEqual(first.split(".")[0], second.split(".")[0])
        self.assertEqual(self.j.handle("say again target"), second)

    def test_mark_and_bda(self):
        self.assertEqual(self.j.handle("mark target"), "Overlord, no target to mark. Request a target first.")
        self.j.handle("request target")
        self.assertEqual(self.j.handle("mark target with white smoke"), "Overlord, white smoke on the target.")
        self.assertEqual(self.oly.sent[-1][0], "smoke")
        self.assertEqual(self.j.handle("BDA"), "Overlord, BDA, Shilka still active.")
        self.s["enemy_ground"] = [g for g in self.s["enemy_ground"] if g[1] != 2]    # destroyed
        self.assertEqual(self.j.tick(), "Overlord, good effect, Shilka destroyed.")
        self.assertIsNone(self.j.tick())                                           # once
        self.assertTrue(self.j.handle("BDA").startswith("Overlord, BDA, Shilka destroyed. Good effect."))
        self.assertIsNone(self.j.my_target())                                      # dead: no longer "my target"

    def test_nothing_near(self):
        self.s["enemy_ground"] = []
        self.assertEqual(self.j.handle("request target"), "Overlord, negative, no targets for you.")

    def test_not_jtac(self):
        self.assertFalse(self.j.wanted("requesting CAS on my target"))
        self.assertFalse(self.j.wanted("picture"))
        self.assertTrue(self.j.wanted("request tasking"))


class TestMyTarget(unittest.TestCase):
    def test_outcast_attacks_the_jtac_target(self):
        from common.olympus_orders import Executor
        s = snap()
        j = Jtac(lambda: s)
        j.handle("request target")
        sent = []

        class Oly:
            connected = True
            def connect(self): return True
            def command(self, n, p): sent.append((n, p)); return True, ""
        ex = Executor(Oly(), lambda: s, lambda sn: j.my_target())
        said = ex.run("attack", {"which": "mine", "text": "my target"}, [{"n": 2, "name": "Outcast 2", "id": 77}], "Outcast 2")
        self.assertEqual(said, "")
        self.assertEqual(sent[-1], ("attackUnit", {"ID": 77, "targetID": 2}))


class TestMain(unittest.TestCase):
    def test_overlord_and_jtac_wake(self):
        import main
        from tests.fakes import state_store
        old = main.JTAC.snapshot
        main.JTAC.snapshot = lambda: snap()
        self.addCleanup(setattr, main.JTAC, "snapshot", old)
        self.addCleanup(main.JTAC.reset)
        # Phase 2 (P2-4): Overlord hands JTAC requests to Darkside, who answers in his voice and words
        said, who, _ = main.shared_reply("Overlord, request target")
        self.assertEqual(who, "darkside")
        self.assertRegex(said, r"^\w[\w ]*(?: \d+-\d)?, Darkside, target Shilka\.")
        self.assertEqual(said.pre, (("Overlord, contact Darkside.", "overlord"),))
        said, who, _ = main.shared_reply("JTAC, say again target")
        self.assertEqual(who, "darkside")
        self.assertIn(", Darkside, target Shilka.", said)
        self.assertEqual(main._george_top(state_store.snapshot())["id"], 2)       # "my target" = the JTAC's


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


class TestOct28Flight(unittest.TestCase):
    """The 28 Oct A-10C JTAC flight."""
    def test_smoke_and_lase_wording(self):
        j = Jtac(lambda: snap())
        self.assertTrue(j.wanted("smoke on my target"))
        self.assertTrue(j.wanted("request smoke on my target."))
        from common.olympus_orders import parse
        self.assertEqual(parse("lays my target")[0], "lase")

    def test_bare_next_target_in_a_jet(self):
        import main
        from tests.fakes import state_store, send_telemetry
        from tests.huey_fakes import reset_state
        reset_state()
        send_telemetry()
        state_store.update_telemetry({"ac": "A-10C_2"})
        self.addCleanup(reset_state)
        old = main.JTAC.snapshot
        main.JTAC.snapshot = lambda: snap()
        self.addCleanup(setattr, main.JTAC, "snapshot", old)
        self.addCleanup(main.JTAC.reset)
        said, who, _ = main.shared_reply("Next target.")
        self.assertEqual(who, "darkside")                                  # Phase 2: Darkside's (P2-4)
        self.assertIn(", Darkside, target", said)
        said, who, _ = main.shared_reply("smoke on my target")
        self.assertEqual(who, "darkside")
