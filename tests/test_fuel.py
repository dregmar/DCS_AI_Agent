"""Fuel units per aircraft (22 Oct: the Apache's 2.49 was read as kg and said "10 pounds")."""
import unittest

from core import fuel


class TestFuel(unittest.TestCase):
    def test_apache_is_a_fraction_of_1140_kg(self):
        self.assertEqual(fuel.say_fuel("AH-64D_BLK_II", 0.5523), "Fuel 1390 pounds.")    # 24 Oct, cockpit 1,380
        self.assertEqual(fuel.say_fuel("AH-64D_BLK_II", 0.98), "Fuel 2460 pounds.")
        self.assertEqual(fuel.say_fuel("AH-64D_BLK_II", 0.5523, max_kg=1130.0), "Fuel 1380 pounds.")
        self.assertEqual(fuel.say_fuel("UH-1H", 0.5, max_kg=631.0), "Fuel 700 pounds.")   # a fraction, max known

    def test_others(self):
        self.assertEqual(fuel.say_fuel("FA-18C_hornet", 1100.0), "Fuel 2430 pounds.")    # kg
        self.assertEqual(fuel.say_fuel("SomeModule", 0.62), "Fuel 62 percent.")          # a fraction


class TestFuelFromDcs(unittest.TestCase):
    """24 Oct: fuel straight from DCS (Unit:getFuel x fuelMassMax, via the comms hook)."""
    def snap(self, frac, max_kg=1157.0, unit="Ghost Flt-1-1", mine="Ghost Flt-1-1", age=2.0, source="name"):
        return {"dcs_fuel": (frac, max_kg, unit, source), "dcs_fuel_age": age, "my_unit": mine}

    def test_internal_and_external(self):
        lbs, ext = fuel.from_dcs(self.snap(0.8421))
        self.assertEqual(round(lbs), 2148)
        self.assertFalse(ext)
        lbs, ext = fuel.from_dcs(self.snap(2.1676))                 # two external tanks
        self.assertEqual(fuel.say_dcs_fuel(lbs, ext), "Fuel 5530 pounds, with the external tanks.")

    def test_not_used_when_stale_or_someone_else(self):
        self.assertIsNone(fuel.from_dcs(self.snap(0.8, age=60.0)))
        self.assertIsNone(fuel.from_dcs(self.snap(0.8, unit="Player-1")))
        self.assertIsNone(fuel.from_dcs(self.snap(0.8, max_kg=-1)))
        self.assertIsNotNone(fuel.from_dcs(self.snap(0.8, mine="")))     # Export hasn't said who you are
        # 24 Oct: Export called you "RedArcher" (the pilot); DCS's own player unit is still yours
        self.assertIsNotNone(fuel.from_dcs(self.snap(0.55526, 1140.0, unit="AH-64D-1", mine="RedArcher",
                                                     source="player")))
        self.assertIsNone(fuel.from_dcs({}))

    def test_hook_message(self):
        from common.comms_menu import parse_fuel
        self.assertEqual(parse_fuel("FUEL 2.16760 1157.0 name Gunfighter-1"), (2.1676, 1157.0, "Gunfighter-1", "name"))
        self.assertEqual(parse_fuel("FUEL 0.55526 1140.0 player AH-64D-1"), (0.55526, 1140.0, "AH-64D-1", "player"))
        self.assertEqual(parse_fuel("FUEL 0.50000 -1.0 "), (0.5, -1.0, "", ""))
        self.assertIsNone(parse_fuel("FUEL nope"))

    def test_status_uses_it(self):
        from tests.fakes import state_store, send_telemetry
        import main
        send_telemetry()
        state_store.set_dcs_fuel(0.8421, 1157.0, "", "player")
        self.addCleanup(setattr, state_store, "dcs_fuel", None)
        self.assertEqual(main.fuel_report(state_store.snapshot()), "Fuel 2150 pounds.")
