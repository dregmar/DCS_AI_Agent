"""Jets have no crew (Andre, 1 Nov): replies that aren't a wingman, an Outcast or the radio menu are
Overlord's, in his voice ("Overlord, steer 270, 12 miles."). The Apache and the Huey are unchanged."""
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)


class JetVoice(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main

    def reply(self, profile, got):
        m = self.main
        with mock.patch.object(m.state_store, "profile", lambda: profile), \
                mock.patch.object(m, "_shared_reply", lambda text: got):
            return m.shared_reply("anything")

    def test_wording(self):
        a = self.main.as_overlord
        self.assertEqual(a("Steer 270, 12 miles."), "Overlord, steer 270, 12 miles.")
        self.assertEqual(a("SA-8, radar SAM, 9 miles north."), "Overlord, SA-8, radar SAM, 9 miles north.")
        self.assertEqual(a("JTAC gave a BMP-1."), "Overlord, JTAC gave a BMP-1.")
        self.assertEqual(a("Overlord, picture clean."), "Overlord, picture clean.")
        self.assertEqual(a(""), "")

    def test_jet_replies_are_overlords(self):
        for source in ("crew", "tactical"):
            with self.subTest(source=source):
                self.assertEqual(self.reply("other", ("Bingo is 3,400 pounds.", "george", source)),
                                 ("Overlord, bingo is 3,400 pounds.", "overlord", source))

    def test_wingmen_and_radio_menu_keep_the_crew_voice(self):
        for source in ("wingmen", "radio", "kneeboard"):
            with self.subTest(source=source):
                got = ("Two, copy." if source != "kneeboard" else "", "george", source)
                self.assertEqual(self.reply("other", got), got)

    def test_helicopters_unchanged(self):
        for profile in ("apache", "huey"):
            got = ("Steer 270, 12 kilometres.", "george", "crew")
            self.assertEqual(self.reply(profile, got), got)

    def test_overlord_and_nothing_pass_through(self):
        got = ("Overlord, picture clean.", "overlord", "overlord")
        self.assertEqual(self.reply("other", got), got)
        self.assertIsNone(self.reply("other", None))

    def test_fallback_is_overlord(self):
        m = self.main
        spoken = []
        with mock.patch.object(m, "ROUTER_ON", False), \
                mock.patch.object(m, "_speak_as", lambda said, who: spoken.append((said, who))), \
                mock.patch.object(m, "_OTHER_SAID", False):
            m._route_other("warp threats are unknown", [])
            m._route_other("something else", [])
        self.assertEqual(spoken[0][1], "overlord")
        self.assertTrue(spoken[0][0].startswith("Overlord, say again. In this aircraft"))
        self.assertEqual(spoken[1], ("Overlord, say again.", "overlord"))

    def test_real_handler_in_a_jet(self):
        """A real shared handler (debrief) answers in Overlord's voice in a jet, the crew's in the Apache."""
        m = self.main
        with mock.patch.object(m.state_store, "profile", lambda: "other"):
            said, who, _ = m.shared_reply("debrief")
        self.assertEqual(who, "overlord")
        self.assertTrue(said.startswith("Overlord, "))
        with mock.patch.object(m.state_store, "profile", lambda: "apache"):
            _, who, _ = m.shared_reply("debrief")
        self.assertEqual(who, "george")


class RouterKnowsThePicture(unittest.TestCase):
    def test_prompt_and_gate(self):
        from intelligence import router
        self.assertIn('"Overlord, what threats are known"', router.ROUTER_SYSTEM)
        self.assertIn('"Tactical picture"', router.ROUTER_SYSTEM)
        ok, why = router.plausible("warp threats are unknown", "Overlord, what threats are known")
        self.assertTrue(ok, why)                                  # "threats" was said
        ok, _ = router.plausible("warp speed please", "Overlord, what threats are known")
        self.assertFalse(ok)                                      # nothing like Overlord or threats


if __name__ == "__main__":
    unittest.main()
