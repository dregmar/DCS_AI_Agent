"""3 Nov: Darkside's laser. "lase the target" / "mark the target with laser" gave red smoke; a laser mark is
its own order. The source (approved 3 Nov) is the nearest JTAC - a unit named JTAC / FAC / observer / recon /
SOF, 8 km - or drone (20 km slant) with terrain line of sight to the target (george_comms_hook.lua's JTACS
list and LOS answers, common/jtac_sight.py). The spot follows the target and goes off when it's destroyed,
or after two sight checks without line of sight ("JTAC Hammer lost sight of the target, laser off")."""
import threading
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from common import jtac as jtac_mod, jtac_sight, olympus_orders
from common.jtac import Jtac, lase_wanted
from command.permissions import check
from command.roles import DARKSIDE, OVERLORD
from tests.test_command_roles import quiet_tasks
from tests.test_jtac import snap

KM = 1 / 111.32                      # degrees of latitude per km


class FakeOly:
    def __init__(self):
        self.sent = []
        self.spots = {}

    def connect(self):
        return True

    def command(self, name, params):
        self.sent.append((name, params))
        if name == "fireLaser":
            self.spots[str(40 + len(self.spots))] = {"sourceUnitID": params["ID"], "active": True}
        return True, ""

    def get(self, path):
        return {"spots": dict(self.spots)}

    def names(self):
        return [n for n, _ in self.sent]


class FakeHook:
    """Plays george_comms_hook.lua: answers LOS queries from a table of who sees what."""
    def __init__(self):
        self.svc = jtac_sight.SightService(self.send, timeout=0.5)
        self.sees = {}                    # unit name -> "1" | "0" | "gone"
        self.slant = {}
        self.found = True
        self.asked = []
        self.silent = False

    def send(self, cmd):
        f = cmd.split("\t")
        self.asked.append(f[3:])
        if self.silent:
            return
        if not self.found:
            body = "NOTARGET"
        else:
            body = "\n".join(f"{n}\t{self.sees.get(n, '0')}\t{self.slant.get(n, 3000)}" for n in f[3:])
        threading.Thread(target=self.svc.on_los, args=(f"LOS {f[1]} {f[2]}\n{body}",), daemon=True).start()

    def jtacs(self, *rows):
        self.svc.on_jtacs("JTACS" + "".join("\n" + "\t".join(str(x) for x in r) for r in rows))


def ground(name, lat, lon, uid=700, label=None):
    return (uid, name, label or name, "Soldier M4", "named", "g", f"{lat:.6f}", f"{lon:.6f}", 300)


def uav(name, lat, lon, uid=900):
    return (uid, name, name, "MQ-9 Reaper", "uav", "a", f"{lat:.6f}", f"{lon:.6f}", 4000)


class Phrases(unittest.TestCase):
    def test_what_is_a_laser_order(self):
        for t in ("lase the target", "mark the target with laser", "laser on, code 1 6 8 8", "lase", "sparkle",
                  "lase it", "laze the target"):
            self.assertTrue(lase_wanted(t), t)
        for t in ("what's the laser code", "laser codes", "say again the laser code?", "request target",
                  "mark target with red smoke"):
            self.assertFalse(lase_wanted(t), t)


class HookMessages(unittest.TestCase):
    def test_parse(self):
        got = jtac_sight.parse_jtacs("JTACS\n700\tJTAC Hammer-1\tJTAC Hammer-1\tSoldier M4\tnamed\tg\t42.1\t41.9\t300\nbad")
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].id, got[0].label, got[0].air), (700, "JTAC Hammer-1", False))
        self.assertEqual(jtac_sight.parse_jtacs("JTACS"), [])
        req, s = jtac_sight.parse_los("LOS 7 801\nJTAC Hammer-1\t1\t2400\nReaper\tgone\t0")
        self.assertEqual((req, s.found, s.rows["JTAC Hammer-1"], s.rows["Reaper"][0]), (7, True, ("1", 2400.0), "gone"))
        self.assertFalse(jtac_sight.parse_los("LOS 8 801\nNOTARGET")[1].found)
        self.assertIsNone(jtac_sight.parse_los("LOS 9 801\nERR something")[1])

    def test_list_goes_stale_and_clears(self):
        clock = [0.0]
        svc = jtac_sight.SightService(lambda c: None, clock=lambda: clock[0])
        self.assertIsNone(svc.observers())
        svc.on_jtacs("JTACS")
        self.assertEqual(svc.observers(), [])
        clock[0] = 31.0
        self.assertIsNone(svc.observers())                      # the hook stopped sending
        svc.on_jtacs("JTACS")
        svc.clear()
        self.assertIsNone(svc.observers())

    def test_no_answer_is_none(self):
        svc = jtac_sight.SightService(lambda c: None, timeout=0.05)
        self.assertIsNone(svc.query(801, ["JTAC Hammer-1"]))


class Laser(unittest.TestCase):
    def setUp(self):
        from tactical import link
        was = link._get()
        link.set_provider(None)
        self.addCleanup(link.set_provider, was)
        self.s = snap()
        self.oly = FakeOly()
        self.ex = olympus_orders.Executor(self.oly, lambda: self.s)
        self.hook = FakeHook()
        self.notes = []
        self.j = Jtac(lambda: self.s, oly=self.oly, speak=lambda t: None, laser=self.ex)
        self.j.sight, self.j.notify = self.hook.svc, self.notes.append
        self.j.handle("request target")                       # the Shilka (id 2)
        self.t = self.j.current
        self.addCleanup(self.ex.lase_stop, [{"id": 700}])
        self.addCleanup(self.ex.lase_stop, [{"id": 900}])

    def at(self, km_south):
        return self.t["lat"] - km_south * KM, self.t["lon"]

    def test_ground_jtac_with_sight_lases(self):
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(3)))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        r = self.j.handle("mark the target with laser")
        self.assertEqual(self.oly.names(), ["fireLaser"])
        self.assertEqual((self.oly.sent[0][1]["ID"], self.oly.sent[0][1]["code"]), (700, 1688))
        self.assertTrue(r.startswith("Overlord, laser on the target, code 1 6 8 8, from JTAC Hammer-1, "), r)
        self.assertIn("south of the target", r)
        self.assertEqual(self.hook.asked[-1], ["JTAC Hammer-1"])

    def test_nearest_with_sight_wins_over_nearest(self):
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(2), uid=700), uav("Reaper 1", *self.at(12), uid=900))
        self.hook.sees = {"JTAC Hammer-1": "0", "Reaper 1": "1"}
        r = self.j.handle("lase the target code 1 5 1 1")
        self.assertEqual((self.oly.sent[-1][1]["ID"], self.oly.sent[-1][1]["code"]), (900, 1511))
        self.assertIn("from the MQ-9 Reaper", r)

    def test_ranges(self):
        self.hook.jtacs(ground("JTAC Far", *self.at(9)), uav("Reaper Far", *self.at(25)))
        self.assertEqual(self.j.handle("lase the target"),
                         "Overlord, unable to lase, no JTAC within 8 km or drone within 20 km of the target. "
                         "Say mark target for smoke.")
        self.assertEqual(self.hook.asked, [])                  # nobody in range: DCS isn't asked
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(7.5)))
        self.hook.sees, self.hook.slant = {"JTAC Hammer-1": "1"}, {"JTAC Hammer-1": 8400}   # slant too far
        self.assertIn("has no line of sight", self.j.handle("lase the target"))
        self.assertEqual(self.oly.sent, [])

    def test_no_sight_refusals(self):
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(3)))
        self.assertEqual(self.j.handle("lase the target"),
                         "Overlord, unable to lase, JTAC Hammer-1 has no line of sight to the target.")
        self.hook.jtacs(ground("JTAC A", *self.at(3), uid=701), ground("JTAC B", *self.at(4), uid=702))
        self.assertEqual(self.j.handle("lase the target"),
                         "Overlord, unable to lase, none of the 2 JTACs in range can see the target.")
        self.hook.found = False
        self.assertEqual(self.j.handle("lase the target"), "Overlord, unable to lase, DCS can't find the target.")
        self.hook.found, self.hook.silent = True, True
        self.assertEqual(self.j.handle("lase the target"), "Overlord, unable to lase, no line of sight answer from DCS.")
        self.assertEqual(self.oly.sent, [])

    def test_no_list_target_or_olympus(self):
        self.assertEqual(self.j.handle("lase the target"),
                         "Overlord, unable to lase, DCS hasn't sent the JTAC list. Is the comms hook up to date?")
        j = Jtac(lambda: self.s, oly=self.oly, laser=self.ex)
        self.assertEqual(j.handle("lase the target"), "Overlord, no target to lase. Request a target first.")
        j = Jtac(lambda: self.s, oly=None, laser=None)
        j.handle("request target")
        self.assertEqual(j.handle("lase the target"), "Overlord, unable to lase, Olympus isn't running.")

    def test_laser_off(self):
        self.assertEqual(self.j.handle("laser off"), "Overlord, no laser on.")
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(3)))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        self.j.handle("lase the target")
        self.assertEqual(self.j.handle("stop lasing"), "Overlord, laser off.")
        self.assertEqual(self.oly.sent[-1], ("deleteSpot", {"spotID": 40}))
        self.assertEqual(self.ex.lasing(), [])

    def wait_for(self, name):
        deadline = time.monotonic() + 3
        while name not in self.oly.names() and time.monotonic() < deadline:
            time.sleep(0.02)
        return name in self.oly.names()

    def test_follows_then_off_when_destroyed(self):
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(3)))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        with mock.patch.object(olympus_orders, "SPOT_UPDATE_SEC", 0.02):
            self.j.handle("lase the target")
            self.s["enemy_ground"] = [(r[0], r[1], r[2] + 0.002, r[3], r[4]) if r[1] == 2 else r
                                      for r in self.s["enemy_ground"]]
            self.assertTrue(self.wait_for("moveSpot"))
            self.s["enemy_ground"] = [r for r in self.s["enemy_ground"] if r[1] != 2]     # destroyed
            self.assertTrue(self.wait_for("deleteSpot"))
        self.assertEqual(self.notes, [])                      # a kill isn't "lost sight"

    def test_lost_sight_twice_then_off(self):
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(3)))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        with mock.patch.object(olympus_orders, "SPOT_UPDATE_SEC", 0.02):
            self.j.handle("lase the target")
            self.hook.sees = {"JTAC Hammer-1": "0"}           # the target drove behind the ridge
            self.assertTrue(self.wait_for("deleteSpot"))
        self.assertEqual(self.notes, ["Overlord, JTAC Hammer-1 lost sight of the target, laser off."])
        self.assertGreaterEqual(sum(1 for a in self.hook.asked if a == ["JTAC Hammer-1"]), 3)  # 1 lase + 2 checks
        self.assertEqual(self.j.handle("laser off"), "Overlord, no laser on.")

    def test_jtac_killed(self):
        self.hook.jtacs(ground("JTAC Hammer-1", *self.at(3)))
        self.hook.sees = {"JTAC Hammer-1": "1"}
        with mock.patch.object(olympus_orders, "SPOT_UPDATE_SEC", 0.02):
            self.j.handle("lase the target")
            self.hook.sees = {"JTAC Hammer-1": "gone"}
            self.assertTrue(self.wait_for("deleteSpot"))
        self.assertEqual(self.notes, ["Overlord, JTAC Hammer-1 is down, laser off."])


class DarksideSpeaks(unittest.TestCase):
    def test_his_words_and_permissions(self):
        from tactical import link
        was = link._get()
        link.set_provider(None)
        self.addCleanup(link.set_provider, was)
        from common.darkside import Darkside
        s = snap()
        oly = FakeOly()
        ex = olympus_orders.Executor(oly, lambda: s)
        j = Jtac(lambda: s, oly=oly, laser=ex)
        hook = FakeHook()
        j.sight = hook.svc
        said_out = []
        d = Darkside(j, lambda: s, said_out.append, callsign=lambda: "Enfield 1-1", oly=oly)
        d.handle("request target")
        hook.jtacs(uav("Reaper 1", j.current["lat"] - 5 * KM, j.current["lon"]))
        hook.sees = {"Reaper 1": "1"}
        self.addCleanup(ex.lase_stop, [{"id": 900}])
        said = d.handle("lase the target")
        self.assertTrue(said.startswith("Enfield 1-1, Darkside, laser on the target, code 1 6 8 8"), said)
        d.notice("Overlord, JTAC Hammer-1 lost sight of the target, laser off.")
        self.assertEqual(said_out[-1], "Enfield 1-1, Darkside, JTAC Hammer-1 lost sight of the target, laser off.")
        self.assertTrue(d.wanted("laser off"))
        tasks = quiet_tasks()
        for name in ("fireLaser", "moveSpot", "deleteSpot"):
            self.assertTrue(check(name, {"ID": 900, "spotID": 40}, DARKSIDE, tasks=tasks)[0], name)
            self.assertFalse(check(name, {"ID": 900, "spotID": 40}, OVERLORD, tasks=tasks)[0], name)


class HookLua(unittest.TestCase):
    """No Lua interpreter: check the hook's new pieces by text (the first flight confirms them)."""
    def test_hook(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "dcs_scripts", "george_comms_hook.lua"), encoding="utf-8") as f:
            lua = f.read()
        for needle in ('"jtac", "observer", "fac", "recon", "sof"', 'at["UAVs"] and "uav"', "land.isVisible, eye, aim",
                       'msg:sub(1, 4) == "LOS\\t"', "send_jtacs(now)", "gch.callsign_t, gch.jtacs_t = 0, 0",
                       '"LOS %d %s\\n%s"', 'string.format("%q", parts[i])'):
            self.assertIn(needle, lua)
        fmt = '"%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%.6f\\t%.6f\\t%.0f", clean(u.id_), un, label,\n                            clean(u:getTypeName()), why, cat == Group.Category.GROUND and "g" or "a", lat, lon, p.y)'
        self.assertIn(fmt, lua)                                # 9 fields, 9 values
        body = lua[lua.index("local JTACS_CODE"):lua.index("local function send_jtacs")]
        import re
        opens = len(re.findall(r"\b(?:function|do|then)\b", body)) - len(re.findall(r"\belseif\b", body))
        self.assertEqual(opens, len(re.findall(r"\bend\b", body)))      # blocks balance


class MainWiring(unittest.TestCase):
    def test_installed(self):
        import main
        from common import comms_menu
        if main.JTAC.laser is not None:                       # (test_jtac's teardown clears wingmen.EXECUTOR)
            self.assertIs(main.JTAC.laser.oly, main.JTAC.oly)
        self.assertIs(main.JTAC.sight, main.JTAC_SIGHT)
        self.assertEqual(comms_menu.ON_LOS, main.JTAC_SIGHT.on_los)
        self.assertEqual(jtac_mod.GROUND_LASER_RANGE_M, 8000.0)
        self.assertFalse(main.JTAC.wanted("what's the laser code"))
        if main.DARKSIDE is not None:
            self.assertEqual(main.JTAC.notify, main.DARKSIDE.notice)


if __name__ == "__main__":
    unittest.main()
