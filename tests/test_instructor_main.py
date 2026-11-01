"""main.py wiring for Instructor Mode: "Instructor, ..." reaches only the instructor (its own voice),
the Status page row, the STT prompt words, the mission-load hook, and that tests never touch the
PC's data/instructor."""
import unittest
from unittest import mock

from tests import fakes
from tests.fakes import send_telemetry, DEFAULT_CONTACTS, state_store

pipeline = fakes.pipeline_module()          # main.py, imported once with its speech captured
main = pipeline.main


class TestInstructorWiring(unittest.TestCase):
    def setUp(self):
        self.said = []
        self._p = mock.patch.object(main, "speak_instructor", lambda text, **k: self.said.append(text))
        self._p.start()
        pipeline.SPOKEN.clear()
        pipeline.OVERLORD_SPOKEN.clear()
        send_telemetry(DEFAULT_CONTACTS)
        main.INSTRUCTOR.handle("Instructor, off")

    def tearDown(self):
        self._p.stop()

    def test_history_is_memory_only_in_tests(self):
        self.assertIsNone(main.INSTRUCTOR.history.dir)

    def test_instructor_answers_in_its_own_voice(self):
        main.handle_command("Instructor, hover exercise")
        self.assertEqual(len(self.said), 1)
        self.assertIn("hover exercise, level 1", self.said[0])
        self.assertEqual(pipeline.SPOKEN, [])                 # George said nothing
        self.assertEqual(pipeline.OVERLORD_SPOKEN, [])

    def test_unknown_instructor_order_never_reaches_george(self):
        main.handle_command("Instructor, fire a hellfire")
        self.assertIn("say again", self.said[0])
        self.assertEqual(pipeline.SPOKEN, [])

    def test_status_row(self):
        names = [l["name"] for l in main.kneeboard_status()["links"]]
        self.assertIn("Instructor", names)

    def test_stt_prompt_has_the_wake_word(self):
        self.assertIn("Instructor: hover, approach, fuel", main.stt_prompt())

    def test_mission_load_reaches_the_instructor(self):
        with mock.patch.object(main.INSTRUCTOR, "on_mission_loaded") as m:
            main._on_mission_loaded()
        m.assert_called_once()

    def test_kneeboard_feed_and_briefing(self):
        self.assertIn("levels", main.kneeboard_instructor())
        self.assertFalse(any(sec.get("key") == "instructor" for sec in main.kneeboard_briefing()["sections"]))
        main.handle_command("Instructor, hover exercise")
        secs = main.kneeboard_briefing()["sections"]
        self.assertEqual(secs[0]["key"], "instructor")

    def test_approach_target_waypoint_then_farp(self):
        snap = state_store.snapshot()
        with mock.patch.object(main.USER_WAYPOINTS, "ll", lambda name, s: None):
            self.assertIn("waypoint Alpha isn't set", main._instructor_target("approach to Alpha", snap))
        with mock.patch.object(main.USER_WAYPOINTS, "ll", lambda name, s: (42.1, 41.2)):
            self.assertEqual(main._instructor_target("approach to waypoint Bravo", snap),
                             {"name": "waypoint Bravo", "ll": (42.1, 41.2), "kind": "waypoint"})
        base = {"name": "FARP London", "lat": 42.0, "lon": 41.0, "cat": 1, "coal": 2}
        with mock.patch.object(main.AIRFIELDS, "nearest", lambda s, **k: (base, 2500.0, True)):
            self.assertEqual(main._instructor_target("approach exercise", snap)["name"], "FARP London")
        with mock.patch.object(main.AIRFIELDS, "nearest", lambda s, **k: (base, 25000.0, True)):
            self.assertIsNone(main._instructor_target("approach exercise", snap))

    def test_approach_to_numbered_route_waypoints(self):
        """31 Oct flight: "Instructor, approach, waypoint 1" / "approach for waypoint 4" found no point."""
        from common import geo
        snap = dict(state_store.snapshot(), route=[(0, 1000.0, 2000.0), (1, 5000.0, 6000.0), (4, 9000.0, 9000.0)])
        with mock.patch.object(geo.FIT, "to_ll", lambda x, z: (42.0 + x / 1e5, 41.0 + z / 1e5)):
            got = main._instructor_target("Instructor, approach, waypoint 1", snap)
            self.assertEqual(got, {"name": "waypoint 1", "ll": (42.05, 41.06), "kind": "route"})
            self.assertEqual(main._instructor_target("approach for waypoint four", snap)["name"], "waypoint 4")
            self.assertEqual(main._instructor_target("approach way of point tool", snap),
                             "Instructor, no waypoint 2 in the mission route.")
        with mock.patch.object(geo.FIT, "to_ll", lambda x, z: None):
            self.assertIn("can't place waypoint 1 on the map yet", main._instructor_target("approach waypoint 1", snap))

    def test_named_point_without_to(self):
        snap = state_store.snapshot()
        with mock.patch.object(main.USER_WAYPOINTS, "ll", lambda name, s: (42.1, 41.2) if name == "Alpha" else None):
            self.assertEqual(main._instructor_target("Instructor, approach, Alpha", snap)["name"], "waypoint Alpha")

    def test_numbered_waypoint_reaches_the_instructor(self):
        """The resolver is asked again when the approach is already selected and a waypoint is named."""
        asked = []
        with mock.patch.object(main.INSTRUCTOR, "targets", lambda text, snap: asked.append(text) or None):
            main.handle_command("Instructor, approach exercise")
            main.handle_command("Instructor, approach for waypoint 4")
        self.assertEqual(len(asked), 2)
        from instructor import voice
        self.assertEqual(voice.names_target("approach for waypoint 4"), "4")
        self.assertEqual(voice.names_target("approach, waypoint 1"), "1")

    def test_kneeboard_map_sound_alikes(self):
        from kneeboard import pages
        for said in ("Kneeboard Mup.", "Kneeboard, mop.", "kneeboard map"):
            self.assertEqual(pages.match(said), {"kind": "page", "page": "map"}, said)

    def test_show_the_instructor_page(self):
        with mock.patch.object(main, "KNEEBOARD_ON", True):
            main.handle_command("show the instructor")
        self.assertEqual(main.KB_PAGES.view().get("page"), "instructor")

    def test_disabled_instructor_falls_back(self):
        with mock.patch.object(main, "INSTRUCTOR", None):
            self.assertIsNone(main.shared_reply("Instructor, hover exercise"))


if __name__ == "__main__":
    unittest.main()
