"""Audit step 3: how old Export.lua's enemy / air-defence / Olympus lists are. A failed world scan
keeps sending the previous lists; past ENEMY_LIST_STALE_SEC nothing is called dead and no new tasking
is taken from them. Without the new fields (old Export.lua) everything works as before."""
import os
import re
import threading
import time
import types
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (unit types isolated, fast timings)
from core import state as st
from common import overlord as ov, independents
from common.jtac import Jtac
from kneeboard import model
from tests.test_overlord import FakeWorld, FakeOly, RADARS, north
from tests.test_jtac import snap as jtac_snap


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.s = st.ApacheStateStore()

    def test_old_export_lua(self):
        self.s.update_telemetry({"alt_ft": 100, "gnd": [["T-72B", 1, 42.0, 42.0, 300]]})
        snap = self.s.snapshot()
        self.assertIsNone(snap["enemy_lists_age"])
        self.assertFalse(st.enemy_lists_stale(snap))
        self.assertIsNone(st.stale_reason(snap))

    def test_age_from_model_time(self):
        self.s.update_telemetry({"t": 100.0, "scan_t": 99.5})
        age = self.s.snapshot()["enemy_lists_age"]
        self.assertGreaterEqual(age, 0.5)
        self.assertLess(age, 1.0)
        self.s.update_telemetry({"t": 112.25, "scan_t": 99.5})            # the scan failed since
        snap = self.s.snapshot()
        self.assertTrue(st.enemy_lists_stale(snap))
        self.assertEqual(st.stale_reason(snap), "my picture is 12 seconds old")

    def test_never_scanned(self):
        self.s.update_telemetry({"t": 3.0, "scan_t": -1})
        snap = self.s.snapshot()
        self.assertEqual(snap["enemy_lists_age"], float("inf"))
        self.assertEqual(st.stale_reason(snap), "I have no picture from DCS")

    def test_age_grows_without_packets(self):
        self.s.update_telemetry({"t": 50.0, "scan_t": 49.0})
        with mock.patch.object(st.time, "monotonic", return_value=time.monotonic() + 10):
            self.assertTrue(st.enemy_lists_stale(self.s.snapshot()))


class TestExportLua(unittest.TestCase):
    """No Lua interpreter here: check the packet's format and its arguments line up."""

    def test_packet_fields(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dcs_scripts", "Export.lua")
        with open(path, encoding="utf-8") as f:
            lua = f.read()
        # every packet format (Phase 3: the 10 Hz flight block is a second one): its specs and arguments line up
        fmts = []
        for m in re.finditer(r"local payload = string\.format\(\s*'([^']*)',", lua):
            depth, n, i, args = 1, 1, m.end(), ""
            while depth:                                   # the arguments, up to string.format's own ")"
                ch = lua[i]
                if ch == '"':                              # skip string literals ("%.1f", "[]")
                    j = lua.index('"', i + 1)
                    args += lua[i:j + 1]
                    i = j + 1
                    continue
                depth += ch in "([{"
                depth -= ch in ")]}"
                n += ch == "," and depth == 1
                args += ch if depth else ""
                i += 1
            fmts.append((m.group(1), args, n))
        self.assertEqual(len(fmts), 2)
        for spec, args, n in fmts:
            self.assertEqual(len(re.findall(r"%[-0-9.]*[dfs]", spec)), n, spec[:40])
        full = next(f for f in fmts if '"olyx"' in f[0])
        self.assertIn('"t":%.2f,"scan_t":%.2f,"olyx":%s,"g":%s}', full[0])     # + step 7's Olympus list, Phase 3's g
        self.assertTrue(full[1].strip().endswith('george_scan_t or -1, george_olyx or "[]", george_num(george_g(), "%.2f")'))
        self.assertIn("george_scan_t = now", lua)                         # set only when a scan completes


class StaleWorld(FakeWorld):
    def __init__(self, radars, age=None):
        super().__init__(radars)
        self.age = age

    def snap(self):
        s = super().snap()
        s["enemy_lists_age"] = self.age
        return s


class TestOverlord(unittest.TestCase):
    def setUp(self):
        self._p = [mock.patch.object(ov, "POLL_S", 0.05), mock.patch.object(ov, "APPEAR_TIMEOUT_S", 2.0)]
        for p in self._p:
            p.start()
        self.world = StaleWorld(RADARS, age=0.5)
        self.oly = FakeOly(self.world)
        self.said = []
        self.o = ov.Overlord(self.oly, self.world.snap, self.said.append)

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.o.reset()

    def test_target_state(self):
        gone = {"type": "ZSU-23-4 Shilka", "id": 999, "lat": north(9)[0], "lon": 42.0, "src": "air_defence"}
        self.assertEqual(ov.target_state(self.world.snap(), gone), "dead")
        self.world.age = 12.0
        self.assertEqual(ov.target_state(self.world.snap(), gone), "unknown")
        self.world.age = None                                             # old Export.lua
        self.assertEqual(ov.target_state(self.world.snap(), gone), "dead")

    def test_no_tasking_from_a_frozen_picture(self):
        self.world.age = 12.0
        self.assertEqual(self.o.handle("Overlord, SEAD north of me"),
                         "Overlord, unable, my picture is 12 seconds old. Say again in a moment.")
        self.assertIn("unable, my picture", self.o.handle("Overlord, picture"))
        self.assertEqual(self.oly.sent, [])
        self.assertIn("no flights", self.o.handle("Overlord, status"))   # not a tasking: answered
        self.world.age = float("inf")
        self.assertIn("I have no picture from DCS", self.o.handle("Overlord, CAP over me"))

    def test_no_splash_while_stale(self):
        """A frozen list, then the bandit gone with the list still stale: no splash call."""
        self.world.kill_on_attack = False
        with self.world.lock:
            self.world.air = [("Mi-24V", 901, *north(5), 300)]
        self.o.handle("Overlord, CAP over me")
        self.assertTrue(self._wait(lambda: any("committing" in s for s in self.said)), self.said)
        self.world.age = 20.0
        with self.world.lock:
            self.world.air = []
        time.sleep(0.4)
        self.assertFalse(any("splash" in s for s in self.said), self.said)
        self.world.age = 0.5                                              # fresh again: now it's dead
        self.assertTrue(self._wait(lambda: any("splash one Mi-24V" in s for s in self.said)), self.said)

    def test_cancel_still_works_when_stale(self):
        self.world.kill_on_attack = False
        self.o.handle("Overlord, SEAD north of me")
        self.assertTrue(self._wait(lambda: any(n == "attackUnit" for n, _ in self.oly.sent)))
        self.world.age = 30.0
        self.assertIn("cancelled", self.o.handle("Overlord, cancel SEAD"))

    def _wait(self, cond, t=5.0):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()


class TestJtac(unittest.TestCase):
    def test_request_and_bda(self):
        s = jtac_snap()
        j = Jtac(lambda: s)
        self.assertTrue(j.handle("request target").startswith("Overlord, target"))
        s["enemy_lists_age"] = 9.0
        self.assertEqual(j.bda(), "Overlord, unable to assess, my picture is 9 seconds old.")
        self.assertIsNone(j.tick())                                      # no "destroyed" from a stale list
        s["enemy_ground"] = []
        self.assertIsNone(j.tick())
        self.assertEqual(j.handle("request target"), "Overlord, unable, my picture is 9 seconds old. "
                                                     "Say again in a moment.")


class TestOutcastHunt(unittest.TestCase):
    def fake(self, age):
        f = types.SimpleNamespace(age=age, spoken=[], followed=[], _lock=threading.Lock(), _hunt={}, _by_n={})
        f.snapshot = lambda: {"enemy_lists_age": f.age, "my_ll": (42.0, 42.0)}
        f._say = lambda n: f"Outcast {n}"
        f._unit = lambda snap, n: ("Outcast-2", "Outcast-2-1", 2001, 42.0, 42.0, 100)
        f._hunt_targets = lambda snap, unit, ad: []
        f.speak = f.spoken.append
        f._follow = lambda n, uid: f.followed.append(n)
        return f

    def test_start_refused_when_stale(self):
        f = self.fake(15.0)
        self.assertEqual(independents.Independents._start_hunt(f, 2, 2001), "Outcast 2, unable, my picture is 15 seconds old.")

    def test_loop_waits_for_a_fresh_picture(self):
        f = self.fake(15.0)
        stop = threading.Event()
        with mock.patch.object(independents, "POLL_S", 0.02), mock.patch.object(independents, "HUNT_IDLE_S", 0.15):
            t = threading.Thread(target=independents.Independents._hunt_loop, args=(f, 2, stop))
            t.start()
            time.sleep(0.4)
            self.assertEqual(f.spoken, [])                       # didn't rejoin "for no targets"
            f.age = 0.5
            t.join(2)
        stop.set()
        self.assertEqual(f.followed, [2])
        self.assertIn("no more targets", f.spoken[0])


class TestKneeboard(unittest.TestCase):
    def test_enemy_age_buckets(self):
        self.assertIsNone(model.enemy_age({}))
        self.assertIsNone(model.enemy_age({"enemy_lists_age": 2.0}))
        self.assertEqual(model.enemy_age({"enemy_lists_age": 13.7}), 10)
        self.assertEqual(model.enemy_age({"enemy_lists_age": float("inf")}), -1)

    def test_status_row(self):
        main = fakes.pipeline_module().main
        row = main._enemy_picture_link
        self.assertIsNone(row({})["ok"])
        self.assertIn("copy the new Export.lua", row({})["detail"])
        self.assertEqual(row({"enemy_lists_age": 0.8}), {"name": "Enemy picture (Export.lua scan)", "ok": True,
                                                         "detail": "fresh"})
        self.assertIn("12 s old", row({"enemy_lists_age": 12.4})["detail"])
        self.assertFalse(row({"enemy_lists_age": float("inf")})["ok"])


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


if __name__ == "__main__":
    unittest.main()
