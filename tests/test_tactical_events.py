"""Tactical picture step T2: which table changes become picture events, and their wording."""
import unittest

from tactical.events import EventLog
from tactical.models import Status
from tactical.tracks import Transition


def st(tid, before, after, reason="", destroyed=False, by=None, gen=0, mono=1.0):
    return Transition("status", mono, gen, tid, "dcs:" + tid[1:], before, after, reason, by, destroyed)


NAMES = {"T0001": "SA-8 Osa", "T0002": "BMP-2", "T0003": "T-72B"}


class Events(unittest.TestCase):
    def test_first_scan_is_one_summary(self):
        log = EventLog()
        ev = log.add([st("T0001", None, Status.ACTIVE), st("T0002", None, Status.ACTIVE)], NAMES)
        self.assertEqual([e.text for e in ev], ["Picture: 2 units in the DCS scan."])
        ev = log.add([st("T0003", None, Status.ACTIVE)], NAMES)
        self.assertEqual([e.text for e in ev], ["New: T-72B (T0003)."])

    def test_a_single_first_unit_is_named(self):
        ev = EventLog().add([st("T0001", None, Status.ACTIVE)], NAMES)
        self.assertEqual([e.text for e in ev], ["New: SA-8 Osa (T0001)."])

    def test_gone_wording(self):
        log = EventLog()
        log.add([st("T0001", None, Status.ACTIVE), st("T0002", None, Status.ACTIVE)], NAMES)
        ev = log.add([st("T0002", Status.ACTIVE, Status.GONE, "no longer in the picture, cause unknown"),
                      st("T0001", Status.ACTIVE, Status.GONE, "destroyed by Hog flight", True, "Hog flight")], NAMES)
        self.assertEqual([e.text for e in ev], ["BMP-2 (T0002) no longer in the picture.",
                                                "SA-8 Osa (T0001) destroyed by Hog flight."])
        self.assertEqual([e.kind for e in ev], ["gone", "destroyed"])

    def test_noise_is_left_out(self):
        log = EventLog()
        ev = log.add([st("T0001", Status.ACTIVE, Status.OUT_OF_COVERAGE), st("T0001", Status.ACTIVE, Status.STALE),
                      st("T0002", Status.OUT_OF_COVERAGE, Status.ACTIVE), st("T0003", Status.STALE, Status.ACTIVE)],
                     NAMES)
        self.assertEqual(ev, [])

    def test_lost_and_back(self):
        log = EventLog()
        ev = log.add([st("T0002", Status.OUT_OF_COVERAGE, Status.LOST), st("T0003", Status.GONE, Status.ACTIVE)],
                     NAMES)
        self.assertEqual([e.text for e in ev], ["BMP-2 (T0002) out of the picture.", "T-72B (T0003) back in the picture."])

    def test_picture_level_lines(self):
        log = EventLog()
        ev = log.add([Transition("picture_stale", 5.0, 0, reason="picture is 7 seconds old"),
                      Transition("picture_fresh", 9.0, 0, reason="picture is current"),
                      Transition("new_picture", 10.0, 1, reason="mission loaded")], NAMES)
        self.assertEqual([e.text for e in ev], ["Picture stale: picture is 7 seconds old.", "Picture current again.",
                                                "New mission: picture reset."])

    def test_new_mission_summarises_again(self):
        log = EventLog()
        log.add([st("T0001", None, Status.ACTIVE), st("T0002", None, Status.ACTIVE)], NAMES)
        log.add([Transition("new_picture", 10.0, 1)], NAMES)
        ev = log.add([st("T0001", None, Status.ACTIVE, gen=1), st("T0002", None, Status.ACTIVE, gen=1)], NAMES)
        self.assertEqual(ev[0].kind, "picture")

    def test_unknown_name_and_bound(self):
        log = EventLog(maxlen=3)
        for i in range(5):
            log.add([st(f"T00{10 + i}", Status.OUT_OF_COVERAGE, Status.LOST, mono=float(i))], {})
        self.assertEqual(len(log), 3)
        self.assertEqual(log.dropped, 2)
        self.assertEqual(log.recent()[0].text, "unknown type (T0012) out of the picture.")
        self.assertEqual(len(log.recent(since_mono=3.0)), 2)


if __name__ == "__main__":
    unittest.main()
