"""Tactical picture step T1: model time vs time.monotonic(), new / same / older scans, and mission
generations (design sections 3 and 7)."""
import unittest

from tactical import clock as c


def snap(t=None, scan_t=None, age=None, tel_age=0.1):
    return {"t_model": t, "scan_t": scan_t, "enemy_lists_age": age, "telemetry_age": tel_age}


class ScanTimes(unittest.TestCase):
    def setUp(self):
        self.clk = c.PictureClock()

    def test_seen_mono_from_age(self):
        st = self.clk.scan_time(snap(100.0, 99.5, 0.7), now_mono=1000.0)
        self.assertEqual(st.kind, c.NEW)
        self.assertAlmostEqual(st.seen_mono, 999.3)
        self.assertEqual(st.seen_model, 99.5)
        self.assertTrue(st.age_known)
        self.assertEqual(st.generation, 0)

    def test_same_scan_twice(self):
        self.clk.scan_time(snap(100.0, 99.5, 0.5), 1000.0)
        st = self.clk.scan_time(snap(100.25, 99.5, 0.75), 1000.25)       # next packet, same scan
        self.assertEqual(st.kind, c.SAME)
        self.assertAlmostEqual(st.seen_mono, 999.5)                       # the same moment

    def test_older_scan_ignored(self):
        self.clk.scan_time(snap(101.0, 100.5, 0.5), 1000.0)
        st = self.clk.scan_time(snap(100.8, 99.5, 1.3), 1000.1)          # an out-of-order packet
        self.assertEqual(st.kind, c.OLDER)
        self.assertFalse(st.restarted)                                    # 0.2 s back is not a restart

    def test_no_scan_yet(self):
        st = self.clk.scan_time(snap(5.0, -1.0, float("inf")), 10.0)
        self.assertEqual(st.kind, c.NO_SCAN)
        self.assertIsNone(st.seen_mono)

    def test_old_export_lua(self):
        st = self.clk.scan_time(snap(None, None, None, tel_age=0.4), 10.0)
        self.assertEqual(st.kind, c.AGE_UNKNOWN)
        self.assertFalse(st.age_known)
        self.assertAlmostEqual(st.seen_mono, 9.6)                         # the packet's receive time
        st = self.clk.scan_time(snap(None, None, None, tel_age=float("inf")), 10.0)
        self.assertIsNone(st.seen_mono)                                   # never received

    def test_model_time_restart_is_a_new_generation(self):
        self.clk.scan_time(snap(500.0, 499.5, 0.5), 1000.0)
        st = self.clk.scan_time(snap(2.0, 1.5, 0.5), 1010.0)              # mission restarted
        self.assertTrue(st.restarted)
        self.assertEqual(st.generation, 1)
        self.assertEqual(st.kind, c.NEW)                                  # not "older": a new picture

    def test_mission_loaded(self):
        self.clk.scan_time(snap(50.0, 49.5, 0.5), 100.0)
        self.assertEqual(self.clk.mission_loaded(), 1)
        st = self.clk.scan_time(snap(50.5, 49.5, 1.0), 100.5)             # same scan, but a new picture
        self.assertEqual(st.kind, c.NEW)
        self.assertEqual(st.generation, 1)
        self.assertFalse(st.restarted)

    def test_one_mission_load_is_one_new_picture(self):
        # Export.lua's model time restarts first, then the comms hook's LOADED arrives: one generation
        self.clk.scan_time(snap(500.0, 499.5, 0.5), 1000.0)
        self.assertTrue(self.clk.scan_time(snap(0.5, 0.2, 0.3), 1001.0).restarted)
        self.assertEqual(self.clk.mission_loaded(1001.3), 1)
        self.assertEqual(self.clk.merged, 1)
        self.assertEqual(self.clk.scan_time(snap(1.0, 0.2, 0.8), 1001.5).kind, c.SAME)
        # ... or LOADED first, then a late packet from the old mission and the new model time
        clk = c.PictureClock()
        clk.scan_time(snap(500.0, 499.5, 0.5), 1000.0)
        self.assertEqual(clk.mission_loaded(1000.1), 1)
        clk.scan_time(snap(500.25, 499.5, 0.75), 1000.2)
        st = clk.scan_time(snap(0.5, 0.2, 0.3), 1000.4)
        self.assertFalse(st.restarted)
        self.assertEqual((st.generation, st.kind), (1, c.NEW))

    def test_two_loads_apart_are_two_pictures(self):
        self.clk.mission_loaded(100.0)
        self.assertEqual(self.clk.mission_loaded(111.0), 2)

    def test_garbage_times(self):
        st = self.clk.scan_time({"t_model": "x", "scan_t": True, "enemy_lists_age": 0.5}, 10.0)
        self.assertEqual(st.kind, c.NEW)                                  # age usable, scan time not
        self.assertIsNone(st.seen_model)
        self.assertAlmostEqual(st.seen_mono, 9.5)


if __name__ == "__main__":
    unittest.main()
