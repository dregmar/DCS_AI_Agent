"""Jet checklists on the kneeboard (common/jet_checklists.py): DCS-BIOS JSON reading, following along,
list choice, page commands."""
import json
import os
import tempfile
import unittest

from common import jet_checklists as jc
from kneeboard import pages

# positions DCS-BIOS's JSON lists itself (the rest come from jc.NAMES)
JSON_POS = {"BATTERY_SW": ["ON", "OFF", "ORIDE"], "LEFT_DDI_BRT_SELECT": ["OFF", "NIGHT", "DAY"],
            "RIGHT_DDI_BRT_SELECT": ["OFF", "NIGHT", "DAY"], "FLAP_SW": ["AUTO", "HALF", "FULL"],
            "INS_SW": ["OFF", "CV", "GND", "NAV", "IFA", "GYRO", "GB", "TEST"],
            "LCP_POSITION": ["FLASH", "OFF", "STEADY"], "LANDING_LIGHTS": ["TAXI", "OFF", "LAND"]}
FRACS = {"CANOPY_POS", "EXT_HOOK", "EXT_WING_FOLDING", "EXT_LAUNCH_BAR", "APU_RPM"}


def fake_module(key):
    """A DCS-BIOS module JSON with one word per control (address 0x2000 + 2i)."""
    cat, addr = {}, {}
    for i, ident in enumerate(jc.controls_used(key)):
        a = 0x2000 + 2 * i
        if ident in FRACS:
            out = {"address": a, "mask": 0xFFFF, "shift_by": 0, "max_value": 65535, "type": "integer"}
            c = {"control_type": "analog_gauge", "outputs": [out]}
        else:
            names = JSON_POS.get(ident) or list(jc.NAMES[jc.MODULE_FOR[key]][ident])
            out = {"address": a, "mask": 0x00FF, "shift_by": 0, "max_value": len(names) - 1, "type": "integer"}
            c = {"control_type": "selector", "outputs": [out]}
            if ident in JSON_POS:
                c["positions"] = names
        cat[ident] = c
        addr[ident] = a
    return {"Panel": cat}, addr


class Cockpit:
    """DCS-BIOS words set by position name."""

    def __init__(self, key, addr):
        self.key, self.addr, self.words = key, addr, {}
        self.mod = None                                  # the BiosModule, set by the test

    def set(self, ident, value):
        names = self.mod().controls[ident][4]            # value order, as the module reads it
        self.words[self.addr[ident]] = names.index(value) if isinstance(value, str) else int(value * 65535)


class TestJetChecklists(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.dir = os.path.join(root, "doc", "json")
        lua_dir = os.path.join(root, "lib", "modules", "aircraft_modules")
        os.makedirs(self.dir)
        os.makedirs(lua_dir)
        self.cockpits = {}
        for key, fname in (("FA-18C_hornet", "FA-18C_hornet"), ("A-10C", "A-10C")):
            data, addr = fake_module(key)
            with open(os.path.join(self.dir, fname + ".json"), "w") as fh:
                json.dump(data, fh)
            self.cockpits[key] = Cockpit(key, addr)
            self.cockpits[key].mod = (lambda k: lambda: self.j.module(k))(key)
        with open(os.path.join(lua_dir, "FA-18C_hornet.lua"), "w") as fh:     # positions listed top first
            fh.write('FA_18C_hornet:define3PosTumb("BATTERY_SW", 3, 3001, 404, "Elec", "Battery Switch")\n'
                     'FA_18C_hornet:define3PosTumb("FLAP_SW", 2, 3007, 234, "Sel", "FLAP Switch")\n')
        self.ac = "FA-18C_hornet"
        self.live = True
        self.j = jc.JetChecklists(lambda: self.cockpits[jc.JetChecklists.key_for(self.ac)].words,
                                  lambda: self.live, json_dir=self.dir)

    def hornet(self):
        return self.cockpits["FA-18C_hornet"]

    def test_addresses_and_module(self):
        want = set(self.cockpits["FA-18C_hornet"].addr.values()) | set(self.cockpits["A-10C"].addr.values())
        self.assertEqual(self.j.addresses(), want)
        self.assertEqual(jc.JetChecklists.key_for("A-10C_2"), "A-10C")
        self.assertIsNone(jc.JetChecklists.key_for("AH-64D_BLK_II"))

    def test_cold_start_follows_along(self):
        h = self.hornet()
        for ident, v in (("BATTERY_SW", "OFF"), ("L_GEN_SW", "NORM"), ("R_GEN_SW", "NORM"), ("APU_READY_LT", "OFF"),
                         ("LEFT_DDI_BRT_SELECT", "OFF"), ("RIGHT_DDI_BRT_SELECT", "OFF"), ("INS_SW", "OFF"),
                         ("RADAR_SW", "OFF"), ("OBOGS_SW", "OFF"), ("FLAP_SW", "AUTO")):
            h.set(ident, v)
        v = self.j.view(self.ac)
        self.assertEqual(v["shown"], "cold_start")
        self.assertEqual(v["items"][0]["state"], "current")          # battery
        self.assertEqual((v["items"][0]["ok"], v["items"][0]["seen"]), (False, "off"))
        h.set("BATTERY_SW", "ON")
        v = self.j.view(self.ac)
        states = [i["state"] for i in v["items"][:5]]
        self.assertEqual(states, ["seen", "seen", "seen", "current", "pending"])    # gens were NORM; fire test now
        for pos in ("TEST A", "OFF", "TEST B", "OFF"):              # fire test A, then B
            h.set("FIRE_TEST_SW", pos)
            self.j.view(self.ac)
        h.set("APU_READY_LT", "ON")
        v = self.j.view(self.ac)
        self.assertEqual(v["items"][3]["state"], "seen")
        self.assertEqual(v["items"][4]["state"], "seen")
        self.assertEqual(v["items"][5]["state"], "current")           # engine crank right
        h.set("APU_READY_LT", "OFF")                                 # APU off later: stays ticked
        self.assertEqual(self.j.view(self.ac)["items"][4]["state"], "seen")

    def test_next_list_when_done_and_asked_for(self):
        h = self.hornet()
        for ident, v in (("BATTERY_SW", "ON"), ("L_GEN_SW", "NORM"), ("R_GEN_SW", "NORM"), ("APU_READY_LT", "ON"),
                         ("LEFT_DDI_BRT_SELECT", "DAY"), ("RIGHT_DDI_BRT_SELECT", "NIGHT"), ("INS_SW", "GND"),
                         ("RADAR_SW", "OPR"), ("OBOGS_SW", "ON"), ("FLAP_SW", "HALF"),
                         ("EJECTION_SEAT_ARMED", "SAFE"), ("MASTER_ARM_SW", "SAFE")):
            h.set(ident, v)
        h.set("CANOPY_POS", 1.0)
        h.set("EXT_HOOK", 0.0)
        h.set("EXT_WING_FOLDING", 0.0)
        h.set("EXT_LAUNCH_BAR", 0.0)
        for pos in ("TEST A", "TEST B", "OFF"):
            h.set("FIRE_TEST_SW", pos)
            v = self.j.view(self.ac)
        self.assertEqual(v["shown"], "taxi")                          # every checked cold-start item done
        self.assertEqual(v["items"][1]["ok"], False)                  # canopy reads open
        self.assertEqual(v["items"][1]["seen"], "open")
        v = self.j.view(self.ac, "carrier_launch")
        self.assertEqual(v["shown"], "carrier_launch")
        self.assertEqual(self.j.view(self.ac, "before_takeoff")["shown"], "taxi")   # the crews' name, aliased

    def test_no_bios(self):
        self.live = False
        v = self.j.view(self.ac)
        self.assertIn("note", v)
        self.assertTrue(all(i["state"] in ("current", "pending") for i in v["items"]))

    def test_a10_names_and_reset(self):
        self.ac = "A-10C_2"
        c = self.cockpits["A-10C"]
        c.set("EPP_BATTERY_PWR", "ON")
        c.set("EPP_INVERTER", "STBY")
        c.set("EPP_AC_GEN_PWR_L", "PWR")
        c.set("EPP_AC_GEN_PWR_R", "OFF")
        v = self.j.view(self.ac)
        self.assertEqual([i["state"] for i in v["items"][:4]], ["seen", "seen", "seen", "current"])
        self.assertEqual(v["items"][3]["seen"], "off")
        self.j.reset()
        c.set("EPP_BATTERY_PWR", "OFF")
        self.assertEqual(self.j.view(self.ac)["items"][0]["state"], "current")

    def test_three_position_lists_turned_round(self):
        """29 Oct: the battery read ORIDE as ON. DCS-BIOS lists define3PosTumb positions top first."""
        m = self.j.module("FA-18C_hornet")
        self.assertEqual(m.controls["BATTERY_SW"][4], ("ORIDE", "OFF", "ON"))
        self.assertEqual(m.controls["FLAP_SW"][4], ("FULL", "HALF", "AUTO"))
        self.assertEqual(m.controls["LEFT_DDI_BRT_SELECT"][4], ("OFF", "NIGHT", "DAY"))   # a knob: as listed
        self.hornet().words[self.hornet().addr["BATTERY_SW"]] = 2
        self.assertEqual(m.state(self.hornet().words, "BATTERY_SW"), "ON")
        a10 = self.j.module("A-10C")                       # no Lua there: left as listed
        self.assertEqual(a10.controls["LCP_POSITION"][4], ("FLASH", "OFF", "STEADY"))

    def test_fire_test_needs_a_and_b(self):
        h = self.hornet()
        h.set("BATTERY_SW", "ON")
        h.set("L_GEN_SW", "NORM")
        h.set("R_GEN_SW", "NORM")
        h.set("FIRE_TEST_SW", "OFF")
        h.set("APU_READY_LT", "OFF")
        v = self.j.view(self.ac)
        self.assertEqual(v["items"][3]["state"], "current")
        self.assertEqual(v["items"][3]["seen"], "test a, test b to do")
        h.set("FIRE_TEST_SW", "TEST A")
        self.j.view(self.ac)
        h.set("FIRE_TEST_SW", "OFF")
        v = self.j.view(self.ac)
        self.assertEqual((v["items"][3]["state"], v["items"][3]["seen"]), ("current", "test b to do"))
        h.set("FIRE_TEST_SW", "TEST B")
        v = self.j.view(self.ac)
        self.assertEqual(v["items"][3]["state"], "seen")
        self.assertEqual(v["items"][4]["state"], "current")          # APU next

    def test_missing_json(self):
        j = jc.JetChecklists(lambda: {}, lambda: True, json_dir=tempfile.mkdtemp())
        self.assertEqual(j.addresses(), set())
        v = j.view("FA-18C_hornet")
        self.assertEqual(v["shown"], "cold_start")
        self.assertTrue(all(i.get("ok") is None for i in v["items"]))

    def test_not_a_jet(self):
        self.assertEqual(self.j.view("Ka-50")["lists"], [])


class TestPageNames(unittest.TestCase):
    def test_names(self):
        self.assertEqual(pages.match("show the cold start checklist")["jet_checklist"], "cold_start")
        self.assertEqual(pages.match("show the carrier launch checklist")["jet_checklist"], "carrier_launch")
        self.assertEqual(pages.match("show the case one checklist")["jet_checklist"], "carrier_landing")
        t = pages.match("show the takeoff checklist")
        self.assertEqual((t["checklist"], t["jet_checklist"]), ("before_takeoff", "takeoff"))

    def test_control_keeps_both(self):
        pc = pages.PageControl()
        pc.apply(pages.match("show the takeoff checklist"))
        self.assertEqual(pc.chosen_checklist(), "before_takeoff")
        self.assertEqual(pc.chosen_checklist(jet=True), "takeoff")
        pc.apply(pages.match("show the checklist"))                 # no name: keeps the choice
        self.assertEqual(pc.chosen_checklist(jet=True), "takeoff")


class TestMainJets(unittest.TestCase):
    def test_kneeboard_checklist_in_a_jet(self):
        import main
        old = main.state_store.aircraft
        try:
            main.state_store.aircraft = "FA-18C_hornet"
            c = main.kneeboard_checklist()
            json.dumps(c)
            self.assertEqual(c["shown"], "cold_start")
            self.assertFalse(c["auto_show"])
        finally:
            main.state_store.aircraft = old


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


if __name__ == "__main__":
    unittest.main()
