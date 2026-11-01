"""Fixes from the 1 Nov 10:24 flight (AH-64D, Phase 2 checks): the mission callsign thrown away because
Export.lua names you by pilot name, 40 CAS targets including roads and outposts, two "good effect"
calls for one kill, "CAC" heard for CAS, "Overlord, override", and the kneeboard's ConnectionAbortedError
tracebacks."""
import io
import sys
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import roles


class Callsign(unittest.TestCase):
    def test_pilot_name_and_mission_unit(self):
        snap = {"callsign_raw": "Apache11", "callsign_unit": "Rotary-2-1", "callsign_age": 2.0,
                "my_unit": "RedArcher",                                  # Export: the pilot name
                "flight_units": [(1, "Rotary-2-1", "Player", True)]}     # the hook's FLIGHT line-up
        self.assertEqual(roles.callsign(snap), ("Apache 1-1", "DCS"))
        snap["flight_units"] = [(1, "Rotary-2-1", "High", False)]        # an AI unit: still not yours
        self.assertEqual(roles.callsign(snap)[0], "Nomad")
        snap["flight_units"] = [("bad",)]
        self.assertEqual(roles.callsign(snap)[0], "Nomad")


class CasTargets(unittest.TestCase):
    def rows(self):
        from common import overlord as ov
        rows = [{"type": "ZU-23 Emplacement", "id": 1}] + [{"type": "outpost_road", "id": 10 + i} for i in range(30)]
        rows += [{"type": "BTR-80", "id": 2}, {"type": "Ural-375", "id": 3}, {"type": "outpost", "id": 4}]
        return ov, rows

    def test_structures_out_and_capped(self):
        ov, rows = self.rows()
        with mock.patch("common.unit_types.TYPES.attrs", lambda name: frozenset()):     # names only
            got = ov.cas_targets(rows, keep_first=True)
        self.assertEqual([t["id"] for t in got], [1, 2, 3])               # the named target, then vehicles
        with mock.patch("common.unit_types.TYPES.attrs", lambda name: frozenset()):
            only = ov.cas_targets([{"type": "outpost", "id": 4}, {"type": "outpost_road", "id": 5}])
        self.assertEqual(len(only), 2)                                    # nothing else there: structures it is
        many = [{"type": "BTR-80", "id": i} for i in range(20)]
        self.assertEqual(len(ov.cas_targets(many)), ov.CAS_MAX_TARGETS)

    def test_dcs_attributes_decide(self):
        from common import overlord as ov
        attrs = {"Sandbag Bunker": frozenset({"Fortifications"}), "Road Outpost Truck": frozenset({"Trucks"})}
        with mock.patch("common.unit_types.TYPES.attrs", lambda name: attrs.get(name, frozenset())):
            self.assertTrue(ov.is_structure("Sandbag Bunker"))
            self.assertFalse(ov.is_structure("Road Outpost Truck"))       # DCS says truck, the name doesn't matter

    def test_cac_is_cas(self):
        from common import overlord as ov
        self.assertIn("CAS", ov.Overlord._kinds_named("request cac on my position"))


class OneGoodEffect(unittest.TestCase):
    def test_jtac_tick_then_cas_loop(self):
        from common.darkside import Darkside
        jtac = type("J", (), {"current": {"id": 16785152}, "wanted": lambda s, t: False})()
        said = []
        d = Darkside(jtac, lambda: {}, said.append, callsign=lambda: "Apache 1-1")
        d.jtac_speak("Overlord, good effect, outpost road destroyed.")
        m = type("M", (), {"cs": "Hog", "task_id": "K1"})()
        d.good_effect(m, "outpost road", 33, 16785152)                    # the same kill from the CAS loop
        d.good_effect(m, "BTR-80", 32, 777)
        self.assertEqual(said, ["Apache 1-1, Darkside, good effect, outpost road destroyed.",
                                "Apache 1-1, Darkside, good effect, BTR-80 destroyed. 32 to go."])
        d.reset()                                                          # new mission: ids start again
        d.good_effect(m, "BTR-80", 0, 777)
        self.assertEqual(len(said), 3)


class OverlordOverride(unittest.TestCase):
    def test_goes_to_warlord_only_with_a_denial_held(self):
        import main
        if main.WARLORD is None:
            self.skipTest("command roles off")
        with mock.patch.object(main.WARLORD, "held", lambda: ("K1",)), \
                mock.patch.object(main.WARLORD, "override", lambda: "Nomad, Warlord, override noted. Logged."):
            self.assertEqual(main._shared_reply("Overlord, override."),
                             ("Nomad, Warlord, override noted. Logged.", "warlord", "warlord"))
        with mock.patch.object(main.WARLORD, "held", lambda: None):
            got = main._shared_reply("Overlord, override.")
            self.assertNotEqual(got[1], "warlord")                         # nothing held: Overlord's as before


class KneeboardQuiet(unittest.TestCase):
    def test_dropped_connection_isnt_printed(self):
        from kneeboard.server import _QuietServer
        srv = _QuietServer.__new__(_QuietServer)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            try:
                raise ConnectionAbortedError(10053, "aborted")
            except ConnectionAbortedError:
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")
        with mock.patch.object(sys, "stderr", err):
            try:
                raise ValueError("a real bug")
            except ValueError:
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("ValueError", err.getvalue())                       # real errors still show


if __name__ == "__main__":
    unittest.main()
