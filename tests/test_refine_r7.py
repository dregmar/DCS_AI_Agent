"""Phase 2 refinement R7 (approved 3 Nov 2026): a request Warlord defers (no current picture, too many flights
up) waits up to DEFER_WAIT_S and is rechecked every DEFER_CHECK_S. When Warlord would approve it: "Warlord, CAS
now approved." and Overlord launches with fresh positions; its target gone: "Overlord, CAS cancelled, the target
is no longer in the picture." One waiting request per kind; "Overlord, cancel CAS" drops it; deferred ->
approved, or failed when the wait runs out."""
import time
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from tests.test_command_roles import RolesWorld


class Deferred(RolesWorld):
    def setUp(self):
        super().setUp()
        self.pic = [False]
        self.warlord.picture_active = lambda: self.pic[0]
        self.o.defer_check_s = 0.05
        self.addCleanup(lambda: self.o._waiting.clear())

    def states(self, tid="K1"):
        return [h[0] for h in self.tasks.get(tid).history]

    def request(self):
        said = self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertEqual(self.tasks.get("K1").state, "deferred")
        self.assertIn("I'll keep it for ", said.then[0][0])
        self.assertTrue(said.then[0][0].endswith(" and call you when it's approved."))
        return said

    def test_approved_when_the_picture_is_back(self):
        self.request()
        time.sleep(0.2)
        self.assertEqual(self.tasks.get("K1").state, "deferred")              # still waiting, nothing said
        self.assertFalse(any("approved" in s for s in self.spoken))
        self.pic[0] = True
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state not in ("deferred",)))
        self.assertTrue(self.wait(lambda: any(n == "spawnAircrafts" for _, n, _ in self.sent)))
        self.assertEqual(self.states()[:4], ["requested", "deferred", "approved", "en_route"])
        self.assertIn("Enfield 1-1, Warlord, CAS now approved.", self.spoken)
        i = self.spoken.index("Enfield 1-1, Warlord, CAS now approved.")
        self.assertIn("Hog flight", self.spoken[i + 1])                       # then Overlord's tasking
        self.assertIsNone(self.tasks.get("K2"))                               # the same record, not a new one
        self.o.handle("Overlord, cancel CAS")

    def test_target_gone(self):
        self.request()
        self.world.gnd = []
        self.pic[0] = True
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state == "failed"))
        self.assertIn("Overlord, CAS cancelled, the target is no longer in the picture.", self.spoken)
        self.assertFalse(any(n == "spawnAircrafts" for _, n, _ in self.sent))

    def test_wait_runs_out(self):
        self.o.defer_wait_s = 0.2
        self.request()
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state == "failed"))
        self.assertIn("Enfield 1-1, Warlord, CAS request dropped, still no current tactical picture. "
                      "Request again when ready.", self.spoken)

    def test_cancel_and_status(self):
        self.request()
        self.assertIn("CAS request waiting: no current tactical picture, 4 minutes left",
                      self.warlord.status().replace("5 minutes", "4 minutes"))
        self.assertEqual(self.o.handle("Overlord, cancel CAS"), "Overlord copies. CAS request cancelled.")
        self.assertEqual(self.tasks.get("K1").state, "failed")
        self.pic[0] = True
        time.sleep(0.2)
        self.assertFalse(any(n == "spawnAircrafts" for _, n, _ in self.sent))
        self.assertEqual(self.o.handle("Overlord, cancel CAS"), "Overlord, no CAS flight tasked.")

    def test_one_per_kind(self):
        self.request()
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertEqual(self.tasks.get("K1").state, "failed")
        self.assertEqual(self.tasks.get("K2").state, "deferred")
        self.assertEqual(len(self.o.waiting_lines()), 1)

    def test_denied_on_recheck(self):
        self.request()
        self.world.ad = [("Buk 9S18 sr", 501, *__import__("tests.test_command_roles", fromlist=["north"]).north(-4), 300.0)]
        self.pic[0] = True
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state == "denied"))
        self.assertTrue(self.wait(lambda: any("CAS denied" in s and "Say Warlord, override" in s for s in self.spoken)),
                        self.spoken)
        self.assertFalse(any(n == "spawnAircrafts" for _, n, _ in self.sent))


class MainWiring(unittest.TestCase):
    def test_settings(self):
        import main
        if main.OVERLORD is None or main.WARLORD is None:
            self.skipTest("Olympus or command roles off")
        self.assertEqual(main.OVERLORD.defer_wait_s, 300.0)
        self.assertEqual(main.WARLORD.waiting_lines, main.OVERLORD.waiting_lines)


if __name__ == "__main__":
    unittest.main()


class MainVoices(unittest.TestCase):
    def test_role_voice(self):
        import main
        from unittest import mock
        if main.OVERLORD is None:
            self.skipTest("Olympus off")
        got = []
        with mock.patch.object(main, "speak_radio", lambda t, who, priority=None, **kw: got.append((t, who))), \
                mock.patch.object(main, "_unprompted", lambda text, fn: fn(text)):
            main.OVERLORD.say_role("Enfield 1-1, Warlord, CAS now approved.", "warlord")
        self.assertEqual(got, [("Enfield 1-1, Warlord, CAS now approved.", "warlord")])
