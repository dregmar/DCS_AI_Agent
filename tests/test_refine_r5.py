"""Phase 2 refinement R5 (approved 3 Nov 2026): each tasking record names its controller - the role that may
send the flight in - and the permission table reads it instead of a built-in "CAS is Darkside's". Nothing
you can hear changes."""
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command.permissions import check
from command.roles import DARKSIDE, OVERLORD, PILOT
from command.tasking import default_controller
from tests.test_command_roles import RolesWorld, quiet_tasks


def live(tasks, kind, owner, unit, state="en_route", controller=None):
    t = tasks.new(kind, owner, f"{kind} flight", controller=controller)
    for s in ("approved", "en_route") + ((state,) if state not in ("en_route",) else ()):
        if s == "cleared":
            tasks.set_state(t.task_id, "holding")
        tasks.set_state(t.task_id, s)
    tasks.attach(t.task_id, "Olympus-9", [unit])
    return tasks.get(t.task_id)


class Defaults(unittest.TestCase):
    def test_who_sends_it_in(self):
        self.assertEqual(default_controller("CAS", OVERLORD), DARKSIDE)
        for kind in ("SEAD", "CAP", "ESCORT"):
            self.assertEqual(default_controller(kind, OVERLORD), OVERLORD)
        self.assertEqual(default_controller("OUTCAST", PILOT), PILOT)
        tasks = quiet_tasks()
        self.assertEqual(tasks.new("CAS", OVERLORD, "Hog flight").controller, DARKSIDE)
        self.assertEqual(tasks.new("SEAD", OVERLORD, "Weasel flight").controller, OVERLORD)


class Table(unittest.TestCase):
    def test_reads_the_record(self):
        tasks = quiet_tasks()
        live(tasks, "CAS", OVERLORD, 11, state="cleared")
        live(tasks, "SEAD", OVERLORD, 12)
        live(tasks, "CAS", OVERLORD, 13, controller=OVERLORD)                 # no Darkside: Overlord runs it
        live(tasks, "CAS", OVERLORD, 14, state="holding")
        ok = lambda unit, role: check("attackUnit", {"ID": unit, "targetID": 1}, role, tasks=tasks)
        self.assertEqual(ok(11, DARKSIDE), (True, ""))
        self.assertEqual(ok(11, OVERLORD), (False, "CAS flight is CAS: Darkside clears it hot, not Overlord"))
        self.assertEqual(ok(12, OVERLORD), (True, ""))
        self.assertEqual(ok(12, DARKSIDE), (False, "SEAD flight (SEAD) isn't Darkside's to send in"))
        self.assertEqual(ok(13, OVERLORD), (True, ""))
        self.assertFalse(ok(13, DARKSIDE)[0])
        self.assertEqual(ok(14, DARKSIDE), (False, "CAS flight isn't cleared hot (holding)"))
        self.assertTrue(ok(14, PILOT)[0])                                   # anything outside a role is yours
        self.assertTrue(check("setROE", {"ID": 12, "ROE": 4}, DARKSIDE, tasks=tasks)[0])   # anyone may stop


class Overlord(RolesWorld):
    def test_his_cas_names_darkside(self):
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertTrue(self.wait(lambda: self.tasks.get("K1") is not None))
        self.assertEqual(self.tasks.get("K1").controller, DARKSIDE)
        self.o.handle("Overlord, cancel CAS")


if __name__ == "__main__":
    unittest.main()
