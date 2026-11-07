"""Phase 2 refinement R1 (approved 3 Nov 2026): no-strike zone and danger close are hard denials that
"Warlord, override" can't undo; a single SEAD target inside a zone is skipped after launch, because the
permission table refuses any role's attack on it. A big SAM with no SEAD up stays overridable."""
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import rules
from command.permissions import Guard, acting_as
from command.roles import DARKSIDE, OVERLORD, PILOT
from tests.test_command_roles import BLOCK, SA11, RolesWorld, north, quiet_tasks, warlord


def tgt(km, typ="T-72B", tid=701):
    lat, lon = north(km)
    return {"type": typ, "id": tid, "lat": lat, "lon": lon}


class HardDenials(unittest.TestCase):
    def test_no_strike_and_danger_close_are_hard(self):
        w = warlord()
        d = w.review("CAS", north(20), [tgt(20)], {})
        self.assertEqual((d.verdict, d.hard, d.rule), ("deny", True, "no_strike"))
        self.assertTrue(w.deny_text("CAS", d).endswith("Pick another target. This one can't be overridden."))
        d = w.review("CAS", north(-8), [tgt(-8)], {"friendly_units": [("M-1 Abrams", 6, *north(-7.6), 100.0, "g")]})
        self.assertEqual((d.verdict, d.hard, d.rule), ("deny", True, "danger_close"))
        d = w.review("CAS", north(-8), [tgt(-8)], {"air_defence": SA11})
        self.assertEqual((d.verdict, d.hard, d.rule), ("deny", False, "sam"))
        self.assertIn("Say Warlord, override", w.deny_text("CAS", d))

    def test_hard_denial_isnt_held(self):
        w = warlord()
        d = w.review("CAS", north(20), [tgt(20)], {})
        launched = []
        w.hold("K1", "CAS", d, lambda: launched.append(1) or "x")
        self.assertIsNone(w.held())
        self.assertEqual(w.override(), "Enfield 1-1, Warlord, nothing to override. The last denial was a "
                                       "no-strike zone and can't be overridden.")
        self.assertEqual(launched, [])

    def test_sead_target_inside_is_skipped_not_denied(self):
        w = warlord()
        radars = [tgt(20, "Osa 9A33 ln", 801), tgt(12, "Kub 1S91 str", 802)]       # the first is in Gudauta town
        d = w.review("SEAD", north(12), radars, {})
        self.assertEqual(d.verdict, "approve")
        self.assertIn("SA-8 in the Gudauta town no-strike zone will be skipped", d.advice)
        d = w.review("SEAD", north(16), [tgt(20, "Osa 9A33 ln", 801)], {})
        self.assertEqual((d.verdict, d.hard), ("deny", True))                     # every target inside


class GuardZone(unittest.TestCase):
    def setUp(self):
        self.tasks = quiet_tasks()
        t = self.tasks.new("SEAD", OVERLORD, "Weasel flight")
        for s in ("approved", "en_route"):
            self.tasks.set_state(t.task_id, s)
        self.tasks.attach(t.task_id, "Olympus-2", [11])
        zones = BLOCK["no_strike"]
        snap = {"air_defence": [("Osa 9A33 ln", 801, *north(20), 300.0), ("Kub 1S91 str", 802, *north(12), 300.0)]}
        self.check = lambda tid: rules.no_strike_zone_of(snap, zones, tid)

    def test_role_attack_inside_refused_pilot_not(self):
        from tactical import link
        was = link._get()
        link.set_provider(None)
        self.addCleanup(link.set_provider, was)
        g = Guard(self.tasks, enforce=True, printer=lambda s: None, zone_check=self.check)
        with acting_as(OVERLORD):
            self.assertEqual(g("attackUnit", {"ID": 11, "targetID": 801}),
                             (False, "target inside the Gudauta town no-strike zone"))
            self.assertEqual(g("attackUnit", {"ID": 11, "targetID": 802}), (True, ""))
            self.assertEqual(g("setPath", {"ID": 11, "path": []}), (True, ""))            # only attacks
        self.assertEqual(g("attackUnit", {"ID": 5, "targetID": 801}), (True, ""))       # your own order (R3 warns)
        g.enforce = False
        with acting_as(OVERLORD):
            self.assertEqual(g("attackUnit", {"ID": 11, "targetID": 801}), (True, ""))   # watching: logged only

    def test_broken_check_refuses(self):
        g = Guard(self.tasks, enforce=True, printer=lambda s: None, zone_check=lambda tid: 1 / 0)
        with acting_as(OVERLORD):
            ok, why = g("attackUnit", {"ID": 11, "targetID": 802})
        self.assertFalse(ok)
        self.assertIn("no-strike check failed", why)

    def test_unknown_position_isnt_refused(self):
        self.assertIsNone(rules.no_strike_zone_of({}, BLOCK["no_strike"], 999))
        self.assertIsNone(rules.no_strike_zone_of({}, [], 801))


class SeadSkipsInFlight(RolesWorld):
    """A real SEAD flight: one radar in the zone is skipped, the other attacked, the guard enforcing."""
    def test_skip_and_carry_on(self):
        from tactical import link
        link.set_provider(None)
        self.world.ad = [("Osa 9A33 ln", 801, *north(20), 300.0), ("Kub 1S91 str", 802, *north(12), 300.0)]
        self.guard.zone_check = lambda tid: rules.no_strike_zone_of(self.world.snap(), BLOCK["no_strike"], tid)
        self.o.handle("Overlord, request SEAD 16 km north of me")
        self.assertTrue(self.wait(lambda: self.state() == "complete"), self.tasks.get("K1"))
        self.assertTrue(any("skipping the SA-8 in the Gudauta town no-strike zone" in s for s in self.spoken),
                        self.spoken)
        targets = [p for who, n, p in self.sent if n == "attackUnit"]
        self.assertEqual(len(targets), 1)                                    # only the Kub went out


class OverlordHardDenial(RolesWorld):
    def test_no_hold_and_override_refused(self):
        self.world.gnd = [("T-72B", 701, *north(20), 300.0)]
        said = self.o.handle("Overlord, request CAS 20 km north of me")
        self.assertIn("can't be overridden", said.then[0][0])
        self.assertEqual(self.state(), "denied")
        self.assertIn("can't be overridden", self.warlord.handle("override"))
        self.assertFalse(any(n == "spawnAircrafts" for _, n, _ in self.sent))


class MainWiring(unittest.TestCase):
    def test_zone_check_installed(self):
        import main
        if main.WARLORD is not None:
            self.assertIsNotNone(main.PERMISSIONS.zone_check)
            self.assertIsNone(main.PERMISSIONS.zone_check(424242))               # unknown id: no zone


if __name__ == "__main__":
    unittest.main()
