"""Phase 2, P2-1: the tasking registry, the one permission table and the acting role. No behaviour
change yet: with PERMISSIONS_ENFORCE False a refusal is only logged; Overlord's flights carry a tasking
record and send their commands as OVERLORD."""
import threading
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import permissions as perm
from command.permissions import Guard, acting_as, check, current
from command.roles import DARKSIDE, OVERLORD, PILOT, WARLORD
from command.tasking import TaskRegistry


def registry():
    return TaskRegistry(printer=lambda s: None)


def flight(tasks, kind, state_path=("approved", "en_route"), units=(11, 12), owner=OVERLORD):
    t = tasks.new(kind, owner, f"{kind} flight")
    for s in state_path:
        assert tasks.set_state(t.task_id, s), s
    tasks.attach(t.task_id, "Olympus-2", units)
    return tasks.get(t.task_id)


class Registry(unittest.TestCase):
    def test_lifecycle(self):
        tasks = registry()
        t = tasks.new("SEAD", OVERLORD, "Weasel flight", targets=["dcs:5", None])
        self.assertEqual((t.task_id, t.state, t.targets), ("K1", "requested", ("dcs:5",)))
        self.assertFalse(tasks.set_state("K1", "attacking"))              # not before approval
        self.assertFalse(tasks.set_state("K1", "bogus"))
        self.assertFalse(tasks.set_state("K9", "approved"))
        for s in ("approved", "en_route", "attacking", "attacking", "complete"):
            self.assertTrue(tasks.set_state("K1", s), s)
        self.assertFalse(tasks.set_state("K1", "attacking"))              # final
        self.assertEqual([h[0] for h in tasks.get("K1").history],
                         ["requested", "approved", "en_route", "attacking", "attacking", "complete"])

    def test_denied_then_overridden(self):
        tasks = registry()
        tasks.new("CAS", OVERLORD, "Hog flight")
        self.assertTrue(tasks.set_state("K1", "denied", "SA-11 covers the target"))
        self.assertFalse(tasks.set_state("K1", "en_route"))
        self.assertTrue(tasks.set_state("K1", "overridden", "Warlord, override"))
        self.assertTrue(tasks.set_state("K1", "en_route"))

    def test_owner_of_and_reset(self):
        tasks = registry()
        t = flight(tasks, "CAP")
        self.assertEqual(tasks.owner_of(12).task_id, t.task_id)
        self.assertEqual(tasks.owner_of("11").task_id, t.task_id)
        self.assertIsNone(tasks.owner_of(99))
        self.assertIsNone(tasks.owner_of(None))
        tasks.attach(t.task_id, None, [13])                                # a new lead joins the known units
        self.assertEqual(tasks.get(t.task_id).units, frozenset({11, 12, 13}))
        tasks.set_state(t.task_id, "complete")
        self.assertIsNone(tasks.owner_of(12))                              # finished: attacks nothing ...
        self.assertEqual(tasks.owner_of(12, live_only=False).task_id, t.task_id)   # ... but flies home
        self.assertEqual(tasks.active(), [])
        tasks.reset()
        self.assertEqual(tasks.new("SEAD", OVERLORD, "x").task_id, "K1")  # ids start again

    def test_records_dont_change_under_a_reader(self):
        tasks = registry()
        t = tasks.new("SEAD", OVERLORD, "Weasel flight")
        tasks.set_state(t.task_id, "approved")
        self.assertEqual(t.state, "requested")
        self.assertEqual(tasks.get(t.task_id).state, "approved")


class Table(unittest.TestCase):
    def setUp(self):
        self.tasks = registry()
        self.sead = flight(self.tasks, "SEAD", units=(11, 12))
        self.cas = flight(self.tasks, "CAS", units=(21, 22))

    def ok(self, name, params, role, task_id=None):
        return check(name, params, role, task_id, self.tasks)[0]

    def test_pilot_always(self):
        for name in ("attackUnit", "cloneUnits", "deleteUnit", "smoke", "spawnAircrafts"):
            self.assertTrue(self.ok(name, {"ID": 21}, PILOT))

    def test_overlord_attacks_with_his_sead_cap_escort_only(self):
        self.assertTrue(self.ok("attackUnit", {"ID": 11, "targetID": 5}, OVERLORD))
        self.assertTrue(self.ok("setROE", {"ID": 11, "ROE": 0}, OVERLORD))
        ok, why = check("attackUnit", {"ID": 21, "targetID": 5}, OVERLORD, None, self.tasks)
        self.assertFalse(ok)
        self.assertIn("Darkside clears it hot", why)
        self.assertFalse(self.ok("setROE", {"ID": 21, "ROE": 0}, OVERLORD))
        self.assertFalse(self.ok("attackUnit", {"ID": 99, "targetID": 5}, OVERLORD))   # an Outcast / wingman

    def test_darkside_clears_cas_only(self):
        self.assertFalse(self.ok("attackUnit", {"ID": 21, "targetID": 5}, DARKSIDE))   # not cleared yet
        self.tasks.set_state(self.cas.task_id, "holding")
        self.tasks.set_state(self.cas.task_id, "cleared")
        self.assertTrue(self.ok("attackUnit", {"ID": 21, "targetID": 5}, DARKSIDE))
        self.assertTrue(self.ok("setROE", {"ID": 22, "ROE": 0}, DARKSIDE))
        self.assertFalse(self.ok("attackUnit", {"ID": 11, "targetID": 5}, DARKSIDE))   # SEAD isn't his

    def test_anyone_can_stop(self):
        for role in (OVERLORD, DARKSIDE, WARLORD):
            for uid in (11, 21, 99):
                self.assertTrue(self.ok("setROE", {"ID": uid, "ROE": 4}, role), (role, uid))
                self.assertTrue(self.ok("setROE", {"ID": uid, "ROE": 3}, role))
        self.assertFalse(self.ok("setROE", {"ID": 11, "ROE": 0}, WARLORD))
        self.assertFalse(self.ok("setROE", {"ID": 11, "ROE": "x"}, WARLORD))

    def test_spawn_needs_an_approved_overlord_task(self):
        t = self.tasks.new("CAS", OVERLORD, "Hog flight")
        self.assertFalse(self.ok("spawnAircrafts", {}, OVERLORD, t.task_id))         # requested only
        self.assertFalse(self.ok("spawnAircrafts", {}, OVERLORD, None))
        self.tasks.set_state(t.task_id, "approved")
        self.assertTrue(self.ok("spawnAircrafts", {}, OVERLORD, t.task_id))
        self.assertFalse(self.ok("spawnAircrafts", {}, DARKSIDE, t.task_id))
        self.assertFalse(self.ok("spawnAircrafts", {}, WARLORD, t.task_id))

    def test_marks_moves_and_the_rest(self):
        self.assertTrue(self.ok("smoke", {"color": "red"}, DARKSIDE))
        self.assertTrue(self.ok("fireLaser", {"ID": 21}, DARKSIDE))
        self.assertFalse(self.ok("smoke", {"color": "red"}, OVERLORD))
        self.assertTrue(self.ok("setPath", {"ID": 21, "path": []}, OVERLORD))         # his flight, even CAS
        self.assertTrue(self.ok("followUnit", {"ID": 11}, OVERLORD))
        self.assertFalse(self.ok("setPath", {"ID": 99, "path": []}, OVERLORD))
        self.assertFalse(self.ok("setPath", {"ID": 21, "path": []}, DARKSIDE))
        self.assertTrue(self.ok("deleteUnit", {"ID": 11}, OVERLORD))
        self.assertFalse(self.ok("deleteUnit", {"ID": 99}, OVERLORD))
        self.assertFalse(self.ok("cloneUnits", {}, OVERLORD))
        self.tasks.set_state(self.sead.task_id, "complete")
        self.assertTrue(self.ok("setPath", {"ID": 11, "path": []}, OVERLORD))         # home after the task
        self.assertFalse(self.ok("attackUnit", {"ID": 11, "targetID": 5}, OVERLORD))  # but no more attacks
        self.assertFalse(self.ok("attackUnit", "not a dict", OVERLORD))


class ActingRole(unittest.TestCase):
    def test_nesting_and_threads(self):
        self.assertEqual(current(), (PILOT, None))
        seen = []
        with acting_as(OVERLORD, "K1"):
            with acting_as(DARKSIDE, "K2"):
                self.assertEqual(current(), (DARKSIDE, "K2"))
            self.assertEqual(current(), (OVERLORD, "K1"))
            th = threading.Thread(target=lambda: seen.append(current()))
            th.start()
            th.join()
        self.assertEqual(seen, [(PILOT, None)])                            # another thread: the pilot
        self.assertEqual(current(), (PILOT, None))

    def test_exception_unwinds(self):
        with self.assertRaises(ValueError):
            with acting_as(WARLORD):
                raise ValueError
        self.assertEqual(current(), (PILOT, None))


class GuardTest(unittest.TestCase):
    def test_watching_logs_once_and_lets_through(self):
        said = []
        g = Guard(registry(), enforce=False, printer=said.append)
        with acting_as(OVERLORD):
            for _ in range(3):
                self.assertEqual(g("attackUnit", {"ID": 5, "targetID": 6}), (True, ""))
        self.assertEqual(g.count, 3)
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("[PERMISSIONS] would refuse attackUnit from OVERLORD"))
        self.assertIn("would refuse", g.link()["detail"])
        self.assertEqual(g("attackUnit", {"ID": 5}), (True, ""))           # the pilot: not counted
        self.assertEqual(g.count, 3)

    def test_enforced_refuses_through_the_client(self):
        from common import olympus
        g = Guard(registry(), enforce=True, printer=lambda s: None)
        oly = olympus.Olympus("http://127.0.0.1:1", "u", "p", guard=g)
        with acting_as(WARLORD):
            ok, why = oly.command("attackUnit", {"ID": 1, "targetID": 2})
        self.assertFalse(ok)
        self.assertIn("Warlord", why)
        self.assertIn("refused", g.link()["detail"])


class OverlordFlights(unittest.TestCase):
    """Overlord's real missions against the fake Olympus of tests/test_overlord.py, with the guard in
    front: SEAD goes through clean; CAS shows the two commands P2-6 will hand to Darkside."""
    def setUp(self):
        from common import overlord as ov
        from tests.test_overlord import FakeOly, FakeWorld, RADARS, north
        self.ov, self.north = ov, north
        self._p = [mock.patch.object(ov, "POLL_S", 0.05), mock.patch.object(ov, "APPEAR_TIMEOUT_S", 2.0)]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])
        self.world = FakeWorld(RADARS)
        self.tasks = registry()
        self.guard = Guard(self.tasks, enforce=False, printer=lambda s: None)
        oly = FakeOly(self.world)
        inner = oly.command
        oly.command = lambda n, p: (self.guard(n, p), inner(n, p))[1]
        self.oly = oly
        self.o = ov.Overlord(oly, self.world.snap, lambda s: None, tasks=self.tasks)

    def wait(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_sead_record_and_no_refusals(self):
        said = self.o.handle("Overlord, request SEAD 10 km north of me")
        self.assertIn("Weasel", said)
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state == "complete"), self.tasks.get("K1"))
        t = self.tasks.get("K1")
        self.assertEqual(t.owner, OVERLORD)
        self.assertEqual(t.group, "Olympus-2")
        states = [h[0] for h in t.history]
        self.assertEqual(states[:3], ["requested", "approved", "en_route"])
        self.assertIn("attacking", states)
        self.assertEqual(self.guard.count, 0, self.guard.recent())

    def test_cas_would_need_darkside(self):
        self.world.gnd = [("T-72B", 701, *self.north(5), 300.0)]
        said = self.o.handle("Overlord, request CAS 5 km north of me")
        self.assertIn("Hog", said)
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state == "complete"))
        # R5: with no Darkside on the net the record names Overlord as the CAS controller, so his own
        # attack orders are no longer refusals (they were logged-only refusals before R5)
        self.assertEqual(self.tasks.get("K1").controller, OVERLORD)
        self.assertEqual(self.guard.count, 0, self.guard.recent())
        self.assertTrue(any(n == "attackUnit" for n, _ in self.oly.sent))

    def test_cancel_records_aborted(self):
        self.world.kill_on_attack = False
        self.o.handle("Overlord, request SEAD 10 km north of me")
        self.assertTrue(self.wait(lambda: self.tasks.get("K1").state == "attacking"))
        self.o.handle("Overlord, cancel SEAD")
        self.assertEqual(self.tasks.get("K1").state, "aborted")
        self.assertEqual(self.guard.count, 0, self.guard.recent())                    # setPath home as OVERLORD
        self.o.reset()
        self.assertEqual(self.tasks.recent(), [])


class MainWiring(unittest.TestCase):
    def test_guard_and_status_row(self):
        import main
        self.assertTrue(main.PERMISSIONS.enforce)                           # P2-6: enforced with the roles on
        names = [l["name"] for l in main.kneeboard_status()["links"]]
        self.assertIn("Command roles (permissions)", names)
        if main.wingmen.EXECUTOR is not None:
            self.assertIs(main.wingmen.EXECUTOR.oly.guard, main.PERMISSIONS)


if __name__ == "__main__":
    unittest.main()
