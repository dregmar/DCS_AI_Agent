"""instructor_probe.py (Instructor Mode Phase 0): rates, field checks, skid height, hover, verdicts.
No tests.fakes import: the probe reads config only inside main(), so nothing here touches config."""
import json
import unittest

from instructor_probe import AircraftStats, HookStats, AxisStats, Probe, valid


def packet(t, **kw):
    p = {"ac": "UH-1H", "t": t, "pos": [1000.0, 2000.0, 100.0], "vel": [0.0, 0.0, 0.0], "agl_ft": 5.0,
         "vs_fpm": 0.0, "speed_kts": 0.0, "pitch": 0.0, "roll": 0.0, "yaw": 0.0, "hdg": 90.0, "scan_t": t,
         "fuel": [500.0, 0.0], "ll": [42.0, 41.0], "alt_ft": 330.0, "cargo": None}
    p.update(kw)
    return p


def feed(st, packets, dt_wall=0.25):
    for i, p in enumerate(packets):
        st.add(p, 100.0 + i * dt_wall)


def verdict(st, item):
    return next(v for v in st.verdicts() if v["item"] == item)


class TestRate(unittest.TestCase):
    def test_four_hz_needs_i5(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(10 + i * 0.25) for i in range(41)])
        self.assertAlmostEqual(st.rate_hz(), 4.0, places=2)
        self.assertEqual(verdict(st, "I5 rate")["verdict"], "needed")

    def test_ten_hz_is_enough(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(10 + i * 0.1) for i in range(101)], dt_wall=0.1)
        self.assertEqual(verdict(st, "I5 rate")["verdict"], "not needed")

    def test_repeated_model_time_is_not_a_new_sample(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(10.0), packet(10.0), packet(10.25), packet(10.5)])
        self.assertEqual(st.dup_t, 1)
        self.assertAlmostEqual(st.rate_hz(), 4.0, places=2)

    def test_mission_reload_does_not_inflate_the_rate(self):
        # 30 Oct report: model time restarted after a reload and the rate read 22.68 Hz
        st = AircraftStats("AH-64D_BLK_II")
        feed(st, [packet(500 + i * 0.25) for i in range(200)])
        feed(st, [packet(i * 0.25) for i in range(200)])
        self.assertAlmostEqual(st.rate_hz(), 4.0, places=2)
        self.assertEqual(len(st.segments), 2)

    def test_pause_jump_is_a_new_segment(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(i * 0.25) for i in range(41)])
        feed(st, [packet(100 + i * 0.25) for i in range(41)])
        self.assertAlmostEqual(st.rate_hz(), 4.0, places=2)

    def test_no_model_time(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(None) for _ in range(5)])
        self.assertEqual(verdict(st, "I5 rate")["verdict"], "unknown")


class TestFields(unittest.TestCase):
    def test_unknown_markers_are_not_valid(self):
        self.assertFalse(valid("hdg", -1.0))
        self.assertFalse(valid("agl_ft", -1.0))
        self.assertTrue(valid("hdg", 0.0))
        self.assertFalse(valid("pos", None))
        self.assertFalse(valid("pos", [1, 2]))
        self.assertFalse(valid("t", True))
        self.assertFalse(valid("speed_kts", float("nan")))

    def test_heading_missing(self):
        st = AircraftStats("X")
        feed(st, [packet(i * 0.25, hdg=-1.0) for i in range(20)])
        self.assertEqual(verdict(st, "I1 heading")["verdict"], "missing")

    def test_g_is_always_needed(self):
        st = AircraftStats("X")
        feed(st, [packet(i * 0.25) for i in range(5)])
        self.assertEqual(verdict(st, "I1 g")["verdict"], "needed")

    def test_ias_compared_with_ground_speed(self):
        st = AircraftStats("X")
        # 50 m/s north = 97 kt ground speed, IAS 100 kt
        feed(st, [packet(i * 0.25, vel=[50.0, 0.0, 0.0], speed_kts=100.0, agl_ft=500.0, hdg=0.0) for i in range(10)])
        v = verdict(st, "I1 airspeed")
        self.assertEqual(v["verdict"], "present (IAS)")
        self.assertIn("+3 kt", v["why"])
        crab = verdict(st, "heading vs track")
        self.assertIn("+0.0 deg", crab["why"])

    def test_rounded_fields_are_reported(self):
        st = AircraftStats("X")
        feed(st, [packet(i * 0.25, agl_ft=10.0 + i, vs_fpm=0.0) for i in range(5)])
        self.assertIn("agl_ft", verdict(st, "resolution")["why"])
        st2 = AircraftStats("X")
        feed(st2, [packet(i * 0.25, agl_ft=10.3, vs_fpm=12.5, speed_kts=0.4) for i in range(5)])
        self.assertFalse(any(v["item"] == "resolution" for v in st2.verdicts()))

    def test_attitude_needs_a_turn(self):
        st = AircraftStats("X")
        feed(st, [packet(i * 0.25) for i in range(10)])
        self.assertEqual(verdict(st, "I1 attitude")["verdict"], "check")
        feed(st, [packet(10 + i * 0.25, pitch=i * 1.0, roll=-i * 2.0) for i in range(10)])
        self.assertEqual(verdict(st, "I1 attitude")["verdict"], "present")


class TestSkidAndHover(unittest.TestCase):
    def test_skid_height_after_three_seconds_still(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(i * 0.25, agl_ft=9.0) for i in range(8)])      # 1.75 s
        self.assertIsNone(st.skid_ft)
        feed(st, [packet(2.0 + i * 0.25, agl_ft=9.0) for i in range(8)])
        self.assertEqual(st.skid_ft, 9.0)

    def test_moving_resets_ground_timer(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(i * 0.25, agl_ft=9.0) for i in range(8)])
        st.add(packet(2.0, agl_ft=9.0, vel=[3.0, 0.0, 0.0]), 200.0)
        feed(st, [packet(2.25 + i * 0.25, agl_ft=9.0) for i in range(8)])
        self.assertIsNone(st.skid_ft)

    def test_hover_time_and_spacing(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(i * 0.25, agl_ft=9.0) for i in range(16)])     # learn 9 ft
        feed(st, [packet(10 + i * 0.25, agl_ft=19.0, vel=[0.5, 0.2, 0.0]) for i in range(41)])  # 10 s at 10 ft
        self.assertAlmostEqual(st.hover_s, 10.0, places=2)
        self.assertIn("spacing median 0.25 s", verdict(st, "hover samples")["why"])

    def test_steady_low_hover_does_not_relearn_the_ground(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(i * 0.25, agl_ft=5.0) for i in range(16)])
        feed(st, [packet(10 + i * 0.25, agl_ft=11.0, vel=[0.1, 0.1, 0.0]) for i in range(40)])   # 6 ft hover, very still
        self.assertEqual(st.skid_ft, 5.0)
        feed(st, [packet(30 + i * 0.25, agl_ft=19.0, vel=[0.1, 0.1, 0.0]) for i in range(40)])
        self.assertEqual(st.skid_ft, 5.0)

    def test_no_hover_before_skid_height(self):
        st = AircraftStats("UH-1H")
        feed(st, [packet(i * 0.25, agl_ft=19.0, vel=[0.5, 0.2, 0.0], vs_fpm=100.0) for i in range(41)])
        self.assertEqual(st.hover_s, 0.0)

    def test_cargo_distance_in_metres(self):
        st = AircraftStats("UH-1H")
        st.add(packet(1.0, cargo=[1003.0, 2004.0, 90.0]), 1.0)
        self.assertAlmostEqual(st.cargo_last["horiz_m"], 5.0)
        self.assertAlmostEqual(st.cargo_last["below_m"], 10.0)
        self.assertIn("5.0 m horizontal (16 ft)", verdict(st, "cargo (F10 check)")["why"])


class TestProbe(unittest.TestCase):
    def test_aircraft_change_splits_stats(self):
        pr = Probe()
        for i in range(5):
            pr.telemetry(json.dumps(packet(i * 0.25)).encode(), i * 0.25)
        for i in range(3):
            pr.telemetry(json.dumps(packet(i * 0.25, ac="AH-64D_BLK_II")).encode(), 10 + i * 0.25)
        self.assertEqual({k: v.n for k, v in pr.aircraft.items()}, {"UH-1H": 5, "AH-64D_BLK_II": 3})

    def test_bad_packets_are_counted_not_raised(self):
        pr = Probe()
        pr.telemetry(b"{not json", 0.0)
        pr.telemetry(b"[1,2]", 0.0)
        pr.telemetry(b"\xff\xfe", 0.0)
        self.assertEqual(pr.bad_packets, 3)
        self.assertTrue(any("nothing received" in ln for ln in pr.report()))

    def test_report_names_every_item(self):
        pr = Probe()
        for i in range(20):
            pr.telemetry(json.dumps(packet(i * 0.25)).encode(), i * 0.25)
        text = "\n".join(pr.report())
        for item in ("I5 rate", "I1 heading", "I1 airspeed", "I1 attitude", "I1 g", "skid height",
                     "comms hook", "joystick axes"):
            self.assertIn(item, text)


class TestHookAndAxes(unittest.TestCase):
    def test_hook_kinds(self):
        h = HookStats()
        for i in range(4):
            h.add("PLAYERS 1 sp", i * 5.0)
        h.add("AIRBASES\nKobuleti\t2\t0\t41.9\t41.8\nFARP\t2\t1\t42.0\t41.9\n", 1.0)
        h.add("FUEL 0.81000 1100.0 player UH-1H", 2.0)
        h.add("MENU 0\n", 3.0)
        text = "\n".join(h.summary_lines())
        self.assertIn("PLAYERS    4, every 5.0 s", text)
        self.assertIn("airbases listed: 2", text)
        self.assertIn("EVT, item I3): not sent yet", text)
        self.assertIn("FUEL 0.81000", text)

    def test_axes_ranges(self):
        a = AxisStats()
        for v in (-1.0, 0.0, 0.98):
            a.add("Gladiator", 0, v)
        a.add("Gladiator", 1, 0.1)
        text = "\n".join(a.summary_lines())
        self.assertIn("Gladiator axis 0", text)
        self.assertIn("moved", text.split("axis 0")[1].split("\n")[0])
        self.assertNotIn("moved", text.split("axis 1")[1])


if __name__ == "__main__":
    unittest.main()
