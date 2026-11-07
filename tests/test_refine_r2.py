"""Phase 2 refinement R2 (approved 3 Nov 2026): the taskings, visible - the kneeboard Status page's
Taskings list, the flight's state on the map, "Overlord, status" with why it's holding, and
"Warlord, status"."""
import os
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import tasking
from command.roles import OVERLORD
from tests.test_command_roles import RolesWorld, quiet_tasks, warlord

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Rows(unittest.TestCase):
    def test_live_then_finished(self):
        clock = [100.0]
        tasks = tasking.TaskRegistry(clock=lambda: clock[0], printer=lambda s: None)
        a = tasks.new("CAS", OVERLORD, "Hog flight")
        for s in ("approved", "en_route"):
            tasks.set_state(a.task_id, s)
        tasks.set_state(a.task_id, "holding", "friendly M-1 Abrams 900 metres from the target")
        b = tasks.new("SEAD", OVERLORD, "Weasel flight")
        for s in ("approved", "en_route", "attacking", "complete"):
            tasks.set_state(b.task_id, s, "area clear" if s == "complete" else "")
        c = tasks.new("CAS", OVERLORD, "Hog flight")
        tasks.set_state(c.task_id, "denied", "SA-11 covers the target and no SEAD is up")
        clock[0] = 220.0
        rows = tasking.rows(tasks, clock[0], held=(c.task_id, 41.6))
        self.assertEqual([r["id"] for r in rows], ["K1", "K3", "K2"])     # live first, newest finished next
        self.assertEqual(rows[0]["text"], "K1 CAS, Hog flight: holding, friendly M-1 Abrams 900 metres from the "
                                          "target (2 min)")
        self.assertEqual(rows[1]["text"], "K3 CAS, Hog flight: denied, SA-11 covers the target and no SEAD is up, "
                                          "override for 41 s (2 min)")
        self.assertEqual(rows[2]["text"], "K2 SEAD, Weasel flight: complete, area clear (2 min)")
        self.assertTrue(rows[0]["live"])
        self.assertFalse(rows[2]["live"])

    def test_at_the_ip_and_en_route(self):
        tasks = quiet_tasks()
        a = tasks.new("CAS", OVERLORD, "Hog flight")
        for s in ("approved", "en_route"):
            tasks.set_state(a.task_id, s, "spawning 2 A-10C_2")
        self.assertEqual(tasking.rows(tasks, 0.0)[0]["text"].split(" (")[0], "K1 CAS, Hog flight: en route")
        tasks.set_state(a.task_id, "holding", "at the IP")
        self.assertIn("holding at the IP (", tasking.rows(tasks, 0.0)[0]["text"])


class WarlordStatus(unittest.TestCase):
    def test_flights_held_and_overrides(self):
        from tests.test_command_roles import SA11, north
        tasks = quiet_tasks()
        clock = [100.0]
        w = warlord(tasks, clock=lambda: clock[0])
        self.assertEqual(w.handle("status"), "Enfield 1-1, Warlord, 0 of 4 flights up.")
        t = tasks.new("CAP", OVERLORD, "Eagle flight")
        tasks.set_state(t.task_id, "approved")
        d = w.review("CAS", north(-8), [{"type": "T-72B", "id": 1, "lat": north(-8)[0], "lon": north(-8)[1]}],
                     {"air_defence": SA11})
        k = tasks.new("CAS", OVERLORD, "Hog flight")
        tasks.set_state(k.task_id, "denied", d.reason)
        w.hold(k.task_id, "CAS", d, lambda: "x")
        clock[0] = 115.0
        w.debrief.overrides = ["SEAD despite: x"]
        self.assertEqual(w.handle("sitrep"), "Enfield 1-1, Warlord, 1 of 4 flights up: Eagle flight CAP. CAS denial "
                                             "waiting for an override, 45 seconds left. 1 override this mission.")
        self.assertEqual(w.held_left(), (k.task_id, 45.0))


class OverlordStatus(RolesWorld):
    def test_holding_says_why(self):
        from tests.test_command_roles import north
        self.world.kill_on_attack = False
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertTrue(self.wait(lambda: self.state() in ("attacking", "cleared")))
        self.d.handle("abort")
        said = self.o.status()
        self.assertIn("Hog flight holding at the IP, abort called by the pilot, tasked CAS", said)
        self.o.handle("Overlord, cancel CAS")


class Kneeboard(unittest.TestCase):
    def test_status_feed_and_flights(self):
        import main
        st = main.kneeboard_status()
        self.assertIn("taskings", st)
        self.assertIsInstance(st["taskings"], list)
        with open(os.path.join(ROOT, "kneeboard", "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("<h2>Taskings</h2>", html)
        self.assertIn('${f.task ? ": " + f.task : ""}', html)

    def test_flight_carries_its_task(self):
        import main
        if main.OVERLORD is None:
            self.skipTest("Olympus off in this config")
        ov = main.OVERLORD
        m = type("M", (), {"cs": "Hog", "kind": type("K", (), {"key": "CAS"})(), "state": "engaging",
                           "task_id": "K9"})()
        tasks = quiet_tasks()
        t = tasks.new("CAS", OVERLORD, "Hog flight")
        tasks.set_state(t.task_id, "approved")
        tasks.set_state(t.task_id, "en_route")
        tasks.set_state(t.task_id, "holding", "at the IP")
        m.task_id = t.task_id
        ctx = main._KneeboardContext
        with mock.patch.object(ov, "active", lambda: [m]), mock.patch.object(ov, "tasks", tasks), \
                mock.patch.object(ov, "_flight", lambda snap, mm: [("Olympus-2", "Olympus-2-1", 1, 42.0, 42.0, 3000)]):
            got = ctx.flights({})
        self.assertEqual(got[0]["task"], "holding")


if __name__ == "__main__":
    unittest.main()
