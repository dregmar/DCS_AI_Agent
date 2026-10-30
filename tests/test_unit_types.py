"""Unit types from DCS: the hook's TYPES message, the cache, and what uses it."""
import os
import tempfile
import unittest
from unittest import mock

from common import unit_types as ut

MSG = ("TYPES\n"
       "ZBD04A\tZBD-04\tground\tIFV,Armored vehicles,Ground vehicles\n"
       "HQ-16A_LN\tHQ-16 LN\tground\tSAM LL,MR SAM,Air Defence\n"
       "HQ-16A_STR\tHQ-16 STR\tground\tSAM TR,MR SAM,RADAR_BAND1_FOR_ARM,Air Defence\n"
       "Totally New Truck\tNew Truck\tground\tTrucks,Unarmed vehicles\n"
       "PLZ05\tPLZ-05\tground\tArtillery,Indirect fire\n")


class TestUnitTypes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = ut.UnitTypes(os.path.join(self.dir, "unit_types.json"))
        self.t.on_types_message(MSG)

    def test_weights_from_attributes(self):
        self.assertEqual(self.t.weight("ZBD04A"), 50)
        self.assertEqual(self.t.weight("ZBD-04"), 50)                 # George's display name
        self.assertEqual(self.t.weight("HQ-16 STR"), 100)
        self.assertEqual(self.t.weight("Totally New Truck"), 10)
        self.assertEqual(self.t.weight("PLZ-05"), 30)
        self.assertIsNone(self.t.weight("Never Heard Of It"))

    def test_sead_and_big_sams(self):
        self.assertTrue(self.t.is_arm_target("HQ-16A_STR"))
        self.assertFalse(self.t.is_arm_target("HQ-16A_LN"))           # launcher: no radar to home on
        self.assertIsNone(self.t.is_arm_target("unknown thing"))
        self.assertTrue(self.t.is_big_sam("HQ-16A_LN"))

    def test_cached_for_next_time(self):
        again = ut.UnitTypes(os.path.join(self.dir, "unit_types.json"))
        self.assertEqual(again.weight("ZBD04A"), 50)
        self.assertEqual(self.t.on_types_message(MSG), 0)             # nothing new: not rewritten

    def test_used_by_threats_targets_and_overlord(self):
        from intelligence.threat_engine import threat_weight
        from common import overlord, targets
        with mock.patch.object(ut, "TYPES", self.t):
            self.assertEqual(threat_weight("Totally New Truck"), 10)
            self.assertTrue(overlord.is_sead_target("HQ-16A_STR"))
            self.assertFalse(overlord.is_sead_target("HQ-16A_LN"))
            snap = {"my_ll": (42.0, 42.0), "air_defence": [],
                    "enemy_ground": [("HQ-16A_STR", 5, 42.05, 42.0), ("ZBD04A", 6, 42.05, 42.0)]}
            self.assertEqual([r["id"] for r in overlord.radars_near(snap, (42.0, 42.0))], [5])
            c = [{"id": 1, "type": "Totally New Truck", "dist_m": 900, "brg": 0, "lat": 1, "lon": 1},
                 {"id": 2, "type": "ZBD04A", "dist_m": 500, "brg": 0, "lat": 1, "lon": 1}]
            self.assertEqual(targets.by_description({"contacts": c, "hdg": 0}, "the truck")["id"], 1)
