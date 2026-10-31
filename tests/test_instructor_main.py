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
        self.assertIn("Instructor, hover exercise", main.stt_prompt())

    def test_mission_load_reaches_the_instructor(self):
        with mock.patch.object(main.INSTRUCTOR, "on_mission_loaded") as m:
            main._on_mission_loaded()
        m.assert_called_once()

    def test_disabled_instructor_falls_back(self):
        with mock.patch.object(main, "INSTRUCTOR", None):
            self.assertIsNone(main.shared_reply("Instructor, hover exercise"))


if __name__ == "__main__":
    unittest.main()
