"""Phase 2 refinement R3 (approved 3 Nov 2026): your own attack orders (wingmen, Outcasts) are checked
against the mission rules - Warlord warns once per attacker and target a minute (DIRECT_ORDERS_ZONES
"warn", default) or the order isn't sent ("block"). Outcasts get a tasking record you own, which isn't in
Warlord's flight cap."""
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command.permissions import Guard, acting_as
from command.roles import OVERLORD
from tests.test_command_roles import north, quiet_tasks, warlord


class GuardDirect(unittest.TestCase):
    def setUp(self):
        self.warned = []
        self.clock = [100.0]

        def check(params):
            return {801: "inside the Gudauta town no-strike zone",
                    802: "820 metres from a friendly M-1 Abrams"}.get(params.get("targetID"))
        self.g = Guard(quiet_tasks(), enforce=True, printer=lambda s: None, direct_check=check,
                       on_direct=lambda p, why, blocked: self.warned.append((p["ID"], why, blocked)),
                       clock=lambda: self.clock[0])

    def test_warn_once_a_minute_and_send(self):
        for _ in range(3):
            self.assertEqual(self.g("attackUnit", {"ID": 5, "targetID": 801}), (True, ""))
        self.assertEqual(self.warned, [(5, "inside the Gudauta town no-strike zone", False)])
        self.g("attackUnit", {"ID": 6, "targetID": 801})                  # another attacker: warned too
        self.g("attackUnit", {"ID": 5, "targetID": 802})
        self.assertEqual(len(self.warned), 3)
        self.clock[0] += 61.0
        self.g("attackUnit", {"ID": 5, "targetID": 801})
        self.assertEqual(len(self.warned), 4)
        self.assertEqual(self.g("attackUnit", {"ID": 5, "targetID": 999}), (True, ""))   # nothing wrong
        self.assertEqual(self.g("setPath", {"ID": 5, "path": []}), (True, ""))
        self.assertEqual(len(self.warned), 4)

    def test_block(self):
        self.g.direct_block = True
        ok, why = self.g("attackUnit", {"ID": 5, "targetID": 802})
        self.assertFalse(ok)
        self.assertEqual(why, "Warlord: the target is 820 metres from a friendly M-1 Abrams")
        self.assertEqual(self.warned, [(5, "820 metres from a friendly M-1 Abrams", True)])

    def test_roles_not_affected_and_broken_check_lets_it_go(self):
        with acting_as(OVERLORD):
            self.g("attackUnit", {"ID": 5, "targetID": 801})               # a role: R1's table decides
        self.assertEqual(self.warned, [])
        self.g.direct_check = lambda p: 1 / 0
        self.assertEqual(self.g("attackUnit", {"ID": 5, "targetID": 801}), (True, ""))


class WarlordText(unittest.TestCase):
    def test_caution_and_cap(self):
        tasks = quiet_tasks()
        w = warlord(tasks, max_flights=1)
        self.assertEqual(w.caution_text("Outcast 2", "inside the Gudauta town no-strike zone"),
                         "Enfield 1-1, Warlord, caution. Outcast 2's target is inside the Gudauta town no-strike zone.")
        self.assertTrue(w.caution_text("Outcast 2", "x", True).endswith("Attack not sent."))
        o = tasks.new("OUTCAST", "PILOT", "Outcast 2")
        for s in ("approved", "en_route"):
            tasks.set_state(o.task_id, s)
        d = w.review("CAS", north(-8), [{"type": "T-72B", "id": 1, "lat": north(-8)[0], "lon": north(-8)[1]}], {})
        self.assertEqual(d.verdict, "approve")                            # your Outcast isn't in the cap of 1
        self.assertIn("Yours: Outcast 2.", w.status())
        self.assertIn("0 of 1 flights up", w.status())


class OutcastRecords(unittest.TestCase):
    def test_up_and_lost(self):
        from common import independents
        from command import tasking
        tasks = quiet_tasks()
        ind = independents.Independents.__new__(independents.Independents)
        import threading
        ind._lock = threading.Lock()
        ind._by_n = {2: {"group": "Olympus-3", "name": "Rotary-2-2", "state": "up"}}
        ind._hunt = {}
        ind.snapshot = lambda: {"olympus_units": [("Olympus-3", "Olympus-3-1", 777, 42.0, 42.0, 60)]}
        with mock.patch.object(tasking, "TASKS", tasks):
            ind._task_up(2)
            t = tasks.get(ind.known(2)["task"])
            self.assertEqual((t.kind, t.owner, t.state, t.flight), ("OUTCAST", "PILOT", "en_route", "Outcast 2"))
            self.assertIn(777, t.units)
            ind._task_lost(2)
            self.assertEqual(tasks.get(t.task_id).state, "lost")


class HuntSkipsBlocked(unittest.TestCase):
    def test_blocked_target_skipped(self):
        from common import independents
        import threading
        ind = independents.Independents.__new__(independents.Independents)
        ind._lock = threading.Lock()
        ind._by_n = {2: {"group": "Olympus-3", "name": "x", "state": "up"}}
        ind._hunt = {}
        spoken, sent = [], []
        ind.speak = spoken.append
        snap = {"olympus_units": [("Olympus-3", "Olympus-3-1", 777, 42.0, 42.0, 60)], "enemy_lists_age": 0.5}
        ind.snapshot = lambda: snap
        stop = threading.Event()

        def command(name, p):
            sent.append(p["targetID"])
            if p["targetID"] == 1:
                return False, "Warlord: the target is inside the Gudauta town no-strike zone"
            stop.set()
            return True, ""
        ind.ex = type("E", (), {"oly": type("O", (), {"command": staticmethod(command)})()})()
        cands = [{"id": 1, "type": "T-72B"}, {"id": 2, "type": "BMP-2"}]
        with mock.patch.object(ind, "_hunt_targets", lambda s, u, a: list(cands)), \
                mock.patch.object(independents, "POLL_S", 0.01):
            ind._hunt_loop(2, stop)
        self.assertEqual(sent, [1, 2])
        self.assertFalse(any("unable" in s for s in spoken))


class MainWiring(unittest.TestCase):
    def test_installed_and_checks(self):
        import main
        if main.WARLORD is None:
            self.skipTest("command roles off")
        self.assertIsNotNone(main.PERMISSIONS.direct_check)
        self.assertFalse(main.PERMISSIONS.direct_block)                   # default "warn"
        zones = [{"name": "Gudauta town", "lat": 42.0, "lon": 42.0, "radius_m": 2000.0}]
        snap = {"enemy_ground": [("T-72B", 801, 42.0, 42.0, 300.0), ("BMP-2", 802, 42.2, 42.0, 300.0)],
                "friendly_units": [("M-1 Abrams", 9, 42.2 + 0.007, 42.0, 100.0, "g")]}
        from tactical import link
        was = link._get()
        link.set_provider(None)
        self.addCleanup(link.set_provider, was)
        with mock.patch.dict(main.WARLORD.block, {"no_strike": zones}), \
                mock.patch.object(main.state_store, "snapshot", lambda: snap):
            self.assertEqual(main._direct_order_check({"targetID": 801}), "inside the Gudauta town no-strike zone")
            self.assertEqual(main._direct_order_check({"targetID": 802}), "779 metres from a friendly M-1 Abrams")
            self.assertIsNone(main._direct_order_check({"targetID": 5}))
            said = []
            with mock.patch.object(main, "speak_warlord", lambda t, priority=None: said.append(t)):
                main._direct_order_warn({"ID": 424242, "targetID": 801}, "inside the Gudauta town no-strike zone", False)
            self.assertTrue(said and "caution. your wingman's target is inside" in said[0], said)


if __name__ == "__main__":
    unittest.main()
