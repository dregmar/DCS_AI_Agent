"""core/state.py for Instructor Mode: the packet's model time and a packet counter in the snapshot,
so the instructor's observer takes each telemetry packet exactly once."""
import unittest

from tests import fakes  # noqa: F401
from core.state import ApacheStateStore


class TestModelTime(unittest.TestCase):
    def test_t_model_and_sequence(self):
        s = ApacheStateStore()
        snap = s.snapshot()
        self.assertEqual((snap["t_model"], snap["tel_seq"]), (None, 0))
        s.update_telemetry({"t": 123.25, "pos": [1, 2, 3]})
        s.update_telemetry({"t": 123.5, "pos": [1, 2, 3]})
        snap = s.snapshot()
        self.assertEqual((snap["t_model"], snap["tel_seq"]), (123.5, 2))

    def test_old_export_without_t(self):
        s = ApacheStateStore()
        s.update_telemetry({"t": 5.0})
        s.update_telemetry({"alt_ft": 100})                 # an old Export.lua sends no "t"
        snap = s.snapshot()
        self.assertEqual((snap["t_model"], snap["tel_seq"]), (None, 2))

    def test_bad_t(self):
        s = ApacheStateStore()
        for bad in ("12", True, None):
            s.update_telemetry({"t": bad})
            self.assertIsNone(s.snapshot()["t_model"], bad)


if __name__ == "__main__":
    unittest.main()
