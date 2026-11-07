"""Detection model D0 (3 Nov 2026): the probe. No Lua interpreter here: the Lua is checked by its text
(blocks closed, only while the flag exists, read-only); the tool by synthetic logs. The first flight
with the flag on confirms the Lua."""
import os
import re
import tempfile
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
import detection_probe as dp
from tests.test_tactical_sides import balance

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lua(name):
    with open(os.path.join(ROOT, "dcs_scripts", name), encoding="utf-8") as f:
        return f.read()


class HookLua(unittest.TestCase):
    def setUp(self):
        self.s = lua("george_comms_hook.lua")
        i = self.s.index("local DPROBE_CODE = [==[") + len("local DPROBE_CODE = [==[")
        self.body = self.s[i:self.s.index("]==]", i)]

    def test_blocks_and_wiring(self):
        self.assertEqual(balance(self.s), 0)
        self.assertEqual(balance(self.body), 0)                           # the code run in the mission state
        self.assertIn("    detection_probe(now)\n", self.s)
        fn = self.s[self.s.index("local function detection_probe(now)"):self.s.index("local function send_route")]
        self.assertLess(fn.index("lfs.attributes(DPROBE_FLAG) == nil"), fn.index("run(st,"))   # flag first
        self.assertIn("gch.dprobe_t = now + 10.0", fn)                     # every 10 s at most

    def test_read_only(self):
        for verb in ("setTask", "pushTask", "setOption", "destroy", "addGroup", "dostring", "trigger.action",
                     "coalition.add", "explosion", "setCommand"):
            self.assertNotIn(verb, self.body, verb)
        for call in ("getDetectedTargets", "isTargetDetected", "land.isVisible", "coalition.getGroups"):
            self.assertIn(call, self.body)
        self.assertIn('local MAXOBS, MAXT, LOS_M, MAXLOS = 6, 80, 20000, 30', self.body)   # bounded work


class ExportLua(unittest.TestCase):
    def setUp(self):
        self.s = lua("Export.lua")

    def test_blocks_and_flag(self):
        self.assertEqual(balance(self.s), 0)
        frame = self.s[self.s.index("function LuaExportBeforeNextFrame()"):]
        i = frame.index("if lfs.attributes(george_probe_flag) ~= nil then")
        self.assertLess(i, frame.index("pcall(george_probe_sensors, now)"))
        self.assertIn("george_probe_next = now + 5.0", frame)
        self.assertEqual(len(re.findall(r"local payload = string\.format", self.s)), 2)   # packets unchanged

    def test_only_reads(self):
        fn = self.s[self.s.index("local function george_probe_sensors(now)"):self.s.index("function LuaExportBeforeNextFrame()")]
        self.assertNotIn("udp", fn)
        self.assertNotIn("LoSetCommand", fn)
        for api in ("LoGetTWSInfo", "LoGetTargetInformation", "LoGetLockedTargetInformation", "LoGetSightingSystemInfo"):
            self.assertIn(api, fn)


HOOK_LOG = """== 19:20:01  query 3.4 ms  6 lines
DPROBE
SUMMARY\tside 2\tobservers 2\tenemy ground units 12
OBS\tOverlord-1\tE-3A\tawacs\talt 9000\tdetected 3
DET\tOverlord-1\t1001\tMiG-29S\t1\t0\tvisible=true\ttypeKnown=true\tdistKnown=true\tlastTime=120\trange=80000\tposErr=0\tlos=true
DET\tOverlord-1\t1002\tMi-24V\t1\t0\tvisible=false\ttypeKnown=false\tdistKnown=true\tlastTime=90\trange=60000\tposErr=850\tlos=false
OBS\tJTAC 1\tSoldier M4\tnamed\talt 300\tdetected 1
DET\tJTAC 1\t2001\tT-72B\t1\t2\tvisible=true\ttypeKnown=true\tdistKnown=true\tlastTime=121\trange=3000\tposErr=0\tlos=true
LOS\tJTAC 1\t2001\tT-72B\trange=3000\tlos=true\tdetected=true
LOS\tJTAC 1\t2002\tT-72B\trange=4500\tlos=false\tdetected=false
LOS\tJTAC 1\t2003\tBMP-2\trange=5000\tlos=true\tdetected=false
== 19:20:11  query 5.0 ms  1 lines
DPROBE NOUNIT
"""

EXPORT_LOG = """120.0  aircraft AH-64D_BLK_II
120.0  RWR mode 0, 1 emitters
120.0    RWR {ID=1002, Power=0.5, Azimuth=1.2}
120.0  LoGetTargetInformation: nil
120.0  LoGetLockedTargetInformation: not available
120.0  sight {LaserTracking=false}
"""


class Tool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dcs = self.tmp.name
        os.makedirs(os.path.join(self.dcs, "Scripts"))
        os.makedirs(os.path.join(self.dcs, "Logs"))

    def test_flag_on_off(self):
        self.assertIn("Probe ON", dp.set_flag(self.dcs, True))
        self.assertTrue(os.path.exists(os.path.join(self.dcs, dp.FLAG)))
        self.assertIn("Probe OFF", dp.set_flag(self.dcs, False))
        self.assertFalse(os.path.exists(os.path.join(self.dcs, dp.FLAG)))
        self.assertIn("already off", dp.set_flag(self.dcs, False))
        self.assertIn("No Scripts folder", dp.set_flag(os.path.join(self.dcs, "nope"), True))

    def test_report(self):
        with open(os.path.join(self.dcs, dp.HOOK_LOG), "w", encoding="utf-8") as f:
            f.write(HOOK_LOG)
        with open(os.path.join(self.dcs, dp.EXPORT_LOG), "w", encoding="utf-8") as f:
            f.write(EXPORT_LOG)
        text = dp.report(self.dcs)
        self.assertIn("queries: 2, real time per query: median 4.2 ms, worst 5.0 ms", text)
        self.assertIn("problem: DPROBE NOUNIT", text)
        self.assertIn("observer Overlord-1 (E-3A, awacs)", text)
        self.assertIn("visible 50%, type known 50%", text)
        self.assertIn("detected WITHOUT terrain line of sight: 1 (e.g. Mi-24V at 60.0 km)", text)
        self.assertIn("last-position error: median 425 m, max 850 m", text)
        self.assertIn("in sight & detected 1, in sight NOT detected 1, hidden & detected 0, hidden & not detected 1", text)
        self.assertIn("aircraft: AH-64D_BLK_II", text)
        self.assertIn("RWR: 1 readings, emitters max 1", text)
        self.assertIn("LoGetTargetInformation: not available 0, nil 1", text)
        self.assertIn("LoGetLockedTargetInformation: not available 1", text)
        self.assertIn("probe is off", text)

    def test_empty_logs(self):
        text = dp.report(self.dcs)
        self.assertIn("no queries logged", text)
        self.assertIn("no observers found", text)
        self.assertIn("no lines", text)

    def test_cli(self):
        self.assertEqual(dp.main(["on", "--dcs", self.dcs]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.dcs, dp.FLAG)))
        self.assertEqual(dp.main(["off", "--dcs", self.dcs]), 0)


if __name__ == "__main__":
    unittest.main()
