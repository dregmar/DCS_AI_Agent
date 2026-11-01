"""Phase 2, P2-7: the full (default) and short radio exchanges, and AI-to-AI calls spoken only with
AI_TO_AI_SPOKEN. Same fakes as tests/test_command_roles.py; nothing leaves this process."""
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import exchange
from tests.test_command_roles import SA11, RolesWorld, warlord, north


class Form(unittest.TestCase):
    def setUp(self):
        self.addCleanup(exchange.configure, False, False)

    def test_settings_and_lead(self):
        exchange.configure(False, False)
        self.assertEqual(exchange.lead("Enfield 1-1", "Darkside"), "Enfield 1-1, Darkside, ")
        exchange.configure(True, True)
        self.assertTrue(exchange.short() and exchange.ai_spoken())
        self.assertEqual(exchange.lead("Enfield 1-1", "Darkside"), "Darkside, ")

    def test_warlord_short(self):
        w = warlord()
        d = w.review("CAS", north(-8), [{"type": "T-72B", "id": 1, "lat": north(-8)[0], "lon": north(-8)[1]}],
                     {"air_defence": SA11})
        exchange.configure(True)
        self.assertEqual(w.approve_text("CAS"), "")                                  # no relay call
        self.assertTrue(w.deny_text("CAS", d).startswith("Warlord, CAS denied. SA-11 covers the target"))
        self.assertIn("Recommend SEAD first.", w.deny_text("CAS", d))               # the reason stays
        exchange.configure(False)
        self.assertEqual(w.approve_text("CAS"), "Overlord, Warlord, CAS approved.")
        self.assertTrue(w.deny_text("CAS", d).startswith("Enfield 1-1, Warlord, CAS denied."))


class Exchanges(RolesWorld):
    """The real CAS mission in each form."""
    def setUp(self):
        super().setUp()
        self.addCleanup(exchange.configure, False, False)
        self.o.callsign = lambda: "Enfield 1-1"

    def test_full_form_tasking(self):
        exchange.configure(short=False)
        said = self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertEqual(said, "Enfield 1-1, Overlord, copy CAS 8 kilometres south of you. Checking with Warlord.")
        (w, who1), (task, who2) = said.then[:2]
        self.assertEqual((w, who1), ("Overlord, Warlord, CAS approved.", "warlord"))
        self.assertEqual(who2, "overlord")
        self.assertTrue(task.startswith("Enfield 1-1, Overlord, Hog flight, two A-10s, tasked CAS"), task)
        self.assertTrue(task.endswith("Weapons hold until Darkside clears them. Contact Darkside."), task)
        self.assertTrue(self.wait(lambda: self.state() == "complete"))
        self.assertIn("Enfield 1-1, Darkside, Hog is cleared hot, T-72B, 8 kilometres south of you.", self.spoken)

    def test_short_form_tasking(self):
        exchange.configure(short=True)
        said = self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertEqual(said, "Overlord, Hog flight tasked CAS 8 kilometres south of you, "
                               + said.split(", ")[-1])                              # "... , N minutes."
        self.assertEqual(getattr(said, "then", ()), ())                             # one line, no relay
        self.assertTrue(self.wait(lambda: self.state() == "complete"))
        self.assertIn("Darkside, Hog cleared hot.", self.spoken)
        self.assertIn("Darkside, good effect, T-72B destroyed.", self.spoken)       # no "1 to go"
        self.assertIn("Darkside, area clear, Hog RTB.", self.spoken)

    def test_short_form_denial_is_warlord_alone(self):
        exchange.configure(short=True)
        self.world.ad = list(SA11)
        said = self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertEqual(said, "")                                                  # Overlord says nothing
        self.assertEqual(said.then[0][1], "warlord")
        self.assertTrue(said.then[0][0].startswith("Warlord, CAS denied. SA-11 covers the target"))
        said = self.warlord.handle("override")
        self.assertIn("Hog goes in under an SA-11", said)                           # the risk, both forms
        self.assertTrue(said.then[0][0].startswith("Overlord, Hog flight tasked CAS"), said.then)

    def test_ai_to_ai_logged_and_spoken_only_when_asked(self):
        heard = []
        self.d.say_ai = lambda text, who: heard.append((who, text))
        exchange.configure(short=False, ai_spoken=False)
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertTrue(self.wait(lambda: self.state() == "complete"))
        self.assertTrue(any(who == "ai" for who, _ in self.log))
        self.assertEqual(heard, [])
        self.o.reset()
        self.world.gnd = [("T-72B", 711, *north(-8), 300.0)]
        exchange.configure(short=False, ai_spoken=True)
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertTrue(self.wait(lambda: self.state() == "complete"))
        self.assertIn(("george", "Darkside, Hog 1, two A-10s at the IP, weapons hold."), heard)   # Hog, crew voice
        self.assertIn(("darkside", "Hog 1, cleared hot, T-72B."), heard)


class DarksideTalkOn(unittest.TestCase):
    def test_short_talk_on(self):
        from common.darkside import Darkside
        from tests.test_command_roles import FakeJtac
        self.addCleanup(exchange.configure, False, False)
        d = Darkside(FakeJtac(), lambda: {}, lambda s: None, callsign=lambda: "Enfield 1-1")
        exchange.configure(short=True)
        self.assertEqual(d.handle("request target"), "Darkside, target Shilka. Grid 38 T KM 12345 67890.")


class MainForm(unittest.TestCase):
    def setUp(self):
        self.addCleanup(exchange.configure, False, False)

    def test_handoff_full_and_short(self):
        import main
        from tests.test_jtac import snap
        old = main.JTAC.snapshot
        main.JTAC.snapshot = lambda: snap()
        self.addCleanup(setattr, main.JTAC, "snapshot", old)
        self.addCleanup(main.JTAC.reset)
        said, who, _ = main.shared_reply("Overlord, request target")
        self.assertEqual(said.pre, (("Overlord, contact Darkside.", "overlord"),))
        exchange.configure(short=True)
        said, who, _ = main.shared_reply("Overlord, request target")
        self.assertEqual(said.pre, ())                                              # Darkside straight away
        self.assertTrue(said.startswith("Darkside, target"), said)

    def test_empty_overlord_reply_keeps_warlords_line(self):
        import main
        from command.roles import Said
        if main.OVERLORD is None:
            self.skipTest("Olympus off in this config")
        with mock.patch.object(main.OVERLORD, "handle", lambda text: Said("", then=[("Warlord, x.", "warlord")])):
            said, who, _ = main.shared_reply("Overlord, request CAS")
        self.assertEqual(said.then, (("Warlord, x.", "warlord"),))

    def test_configured_from_config(self):
        import main
        self.assertFalse(exchange.short())                                          # default: full form
        self.assertFalse(exchange.ai_spoken())


if __name__ == "__main__":
    unittest.main()
