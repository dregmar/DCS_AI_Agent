"""Fixes from the 4 Nov flight log: Whisper's versions of "lase target", "talk on" / "follow-on target",
a destroyed target isn't "lost sight", Overlord's own flights aren't friendly ground forces for danger
close, Darkside points strike requests to Overlord."""
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from common import olympus_orders
from common.jtac import Jtac, heard
from tactical import link
from tactical.models import Affiliation, Category, PictureSnapshot, Status
from tests.tactical_fixtures import track
from tests.test_darkside_laser import KM, FakeHook, FakeOly, ground
from tests.test_jtac import snap
from tests.test_refine_r4 import Stub
from tests.test_command_roles import north


class Heard(unittest.TestCase):
    def test_whisper_versions_of_lase(self):
        for said in ("request, layers target", "please target", "nears my target", "least target", "lace the target",
                     "lays it"):
            self.assertTrue(heard(said).endswith(("lase target", "lase my target", "lase the target", "lase it")), said)
        for said in ("request target please", "request nearest target", "please say again target", "layers"):
            self.assertNotIn("lase", heard(said), said)

    def test_new_phrases(self):
        j = Jtac(lambda: {})
        for t in ("request, layers target", "please target", "request talk on", "requesting follow on target",
                  "follow-on target"):
            self.assertTrue(j.wanted(t), t)


class DeadTargetIsntLostSight(unittest.TestCase):
    def test_not_found_turns_off_quietly(self):
        was = link._get()
        link.set_provider(None)
        self.addCleanup(link.set_provider, was)
        s = snap()
        oly, hook, notes = FakeOly(), FakeHook(), []
        ex = olympus_orders.Executor(oly, lambda: s)
        j = Jtac(lambda: s, oly=oly, laser=ex)
        j.sight, j.notify = hook.svc, notes.append
        j.handle("request target")
        hook.jtacs(ground("JTAC Hammer-1", j.current["lat"] - 3 * KM, j.current["lon"]))
        hook.sees = {"JTAC Hammer-1": "1"}
        self.addCleanup(ex.lase_stop, [{"id": 700}])
        with mock.patch.object(olympus_orders, "SPOT_UPDATE_SEC", 0.02):
            j.handle("please target")                                 # Whisper's "lase target"
            self.assertEqual(oly.names(), ["fireLaser"])
            hook.found = False                                        # destroyed: DCS no longer has it
            import time
            deadline = time.monotonic() + 3
            while "deleteSpot" not in oly.names() and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertIn("deleteSpot", oly.names())
        self.assertEqual(notes, [])                                   # no "lost sight" call


class OverlordFlightIsntDangerClose(unittest.TestCase):
    def test_olympus_flight_not_a_friendly_ground_unit(self):
        was = link._get()
        self.addCleanup(link.set_provider, was)
        hog = track("T0050", None, -7.3, aff=Affiliation.FRIENDLY, cat=Category.UNKNOWN)     # Olympus-2-1
        tank = track("T0051", "M-1 Abrams", -30, aff=Affiliation.FRIENDLY)
        ship = track("T0052", "CVN-74", -7.5, aff=Affiliation.FRIENDLY, cat=Category.NAVAL)
        link.set_provider(Stub(PictureSnapshot(0, 1000.0, 0.4, (hog, tank, ship))))
        self.assertEqual([r["id"] for r in link.friendlies_near(*north(-8), 1000)], [52])
        hog_only = PictureSnapshot(0, 1000.0, 0.4, (hog, tank))
        link.set_provider(Stub(hog_only))
        from command import rules
        self.assertEqual(rules.friendlies_near({"friendly_units": []}, *north(-8), 1000), [])


class DarksideStrikeRequest(unittest.TestCase):
    def test_points_to_overlord(self):
        import main
        if main.DARKSIDE is None:
            self.skipTest("command roles off")
        with mock.patch.object(main, "my_callsign", lambda: "Springfield 1-1"):
            got = main.role_reply("Darkside, request strike on SA-11.")
        self.assertIn("request CAS or strikes through Overlord", got[0])

    def test_prompt_has_the_words(self):
        import main
        if main.COMMAND_ROLES_ON:
            self.assertIn("lase target", main.stt_prompt())


if __name__ == "__main__":
    unittest.main()


class Flight1105(unittest.TestCase):
    """5 Nov flight (george_20261105_192909.log)."""
    def test_commas_and_vase(self):
        j = Jtac(lambda: {})
        for t in ("request, target.", "requesting follow on, target.", "requesting, tasking.", "vase my target."):
            self.assertTrue(j.wanted(t), t)
        self.assertEqual(heard("vase my target."), "lase my target")
        self.assertFalse(j.wanted("what's the laser code?"))

    def test_overlord_hands_tasking_to_darkside(self):
        import main
        if main.DARKSIDE is None:
            self.skipTest("command roles off")
        self.assertTrue(main.JTAC.wanted("requesting, tasking."))
