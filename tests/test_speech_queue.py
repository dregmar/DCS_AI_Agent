"""Audit step 4 (audio/tts.py): the comms log gets a line only once Piper has made its audio, and the
speech backlog is capped by dropping the oldest routine callouts (never pilot replies or alerts).
The playback path itself (dithered tail, sleep(total + 0.1), sd.stop) is covered by test_metrics."""
import queue
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (fast timings first)
from tests.test_voices import FakeVoice
from audio import tts
from core.metrics import Metrics


class Silent(FakeVoice):
    """Piper produced nothing for this line."""

    def synthesize(self, text):
        self.said.append(text)
        return iter(())


def item(text, priority=tts.PRIORITY_PILOT, age=0.0, max_age=None, speaker=tts.GEORGE, uid=None, seq=0):
    return (priority, seq, time.monotonic() - age, max_age, speaker, text, uid)


class TestLogAfterSynthesis(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.m = Metrics(printer=lambda s: None)
        self._p = [mock.patch.object(tts, "METRICS", self.m),
                   mock.patch.object(tts, "ON_SPOKEN", lambda who, text: self.events.append(("log", who, text))),
                   mock.patch.object(tts.sd, "play", lambda a, samplerate, **k: self.events.append(("play",)),
                                     create=True),
                   mock.patch.object(tts.sd, "stop", lambda: None, create=True)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()

    def run_worker(self, items, voices):
        q = mock.Mock()
        q.get.side_effect = list(items) + [SystemExit]
        with mock.patch.object(tts, "_queue", q), mock.patch.dict(tts.VOICES, voices):
            with self.assertRaises(SystemExit):
                tts._worker()

    def test_logged_just_before_playback(self):
        self.run_worker([item("Two, rejoining.")], {tts.GEORGE: FakeVoice(16000, "ryan")})
        self.assertEqual(self.events, [("log", "george", "Two, rejoining."), ("play",)])

    def test_no_audio_no_log(self):
        """Before: the line was logged first, so a Piper failure still showed it as said."""
        uid = self.m.start(captured=time.monotonic())
        self.m.queued(uid)
        self.m.handled(uid, 0.1)
        self.run_worker([item("Fuel sixty percent.", uid=uid)], {tts.GEORGE: Silent(16000, "ryan")})
        self.assertEqual(self.events, [])
        self.assertEqual(self.m.summary()["last"]["note"], "reply dropped")

    def test_stale_not_logged(self):
        self.run_worker([item("Old callout.", tts.PRIORITY_ROUTINE, age=60, max_age=5)],
                        {tts.GEORGE: FakeVoice(16000, "ryan")})
        self.assertEqual(self.events, [])

    def test_log_error_doesnt_stop_playback(self):
        def boom(who, text):
            raise ValueError("log full")
        with mock.patch.object(tts, "ON_SPOKEN", boom):
            self.run_worker([item("Overlord, copy.", speaker=tts.OVERLORD)],
                            {tts.GEORGE: FakeVoice(16000, "ryan"), tts.OVERLORD: FakeVoice(22050, "joe")})
        self.assertEqual(self.events, [("play",)])


class TestBacklogCap(unittest.TestCase):
    def setUp(self):
        self.q = queue.PriorityQueue()
        self.m = Metrics(printer=lambda s: None)
        self._p = [mock.patch.object(tts, "_queue", self.q), mock.patch.object(tts, "MAX_QUEUED", 3),
                   mock.patch.object(tts, "METRICS", self.m),
                   mock.patch.object(tts, "start_speech_worker", lambda: None),
                   mock.patch.dict(tts.VOICES, {tts.GEORGE: FakeVoice(16000, "ryan"),
                                                tts.OVERLORD: FakeVoice(22050, "joe")})]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()

    def texts(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait()[5])
        return out

    def test_oldest_routine_dropped_first(self):
        tts.speak_george_radio("Routine one.", tts.PRIORITY_ROUTINE)
        tts.speak_george_radio("Reply.", tts.PRIORITY_PILOT)
        tts.speak_george_radio("Routine two.", tts.PRIORITY_ROUTINE)
        tts.speak_overlord("Pop-up threat.", tts.PRIORITY_ALERT)          # 4 > 3: "Routine one." goes
        self.assertEqual(self.m.summary()["counts"], {"speech_dropped_full": 1})
        self.assertEqual(self.texts(), ["Pop-up threat.", "Reply.", "Routine two."])

    def test_never_drops_replies_or_alerts(self):
        for n in range(4):
            tts.speak_george_radio(f"Reply {n}.", tts.PRIORITY_PILOT)
        tts.speak_overlord("Alert.", tts.PRIORITY_ALERT)
        self.assertEqual(self.q.qsize(), 5)                                   # over the cap, nothing routine
        self.assertEqual(self.m.summary()["counts"], {})
        tts.speak_george_radio("Routine.", tts.PRIORITY_ROUTINE)              # the only routine one: dropped
        self.assertEqual(self.q.qsize(), 5)
        self.assertNotIn("Routine.", self.texts())

    def test_dropped_reply_record_closed(self):
        uid = self.m.start(captured=time.monotonic())
        with self.m.bound(uid):
            tts.speak_george_radio("Fuel state routine.", tts.PRIORITY_ROUTINE)
        self.m.handled(uid, 0.1)
        for n in range(3):
            tts.speak_george_radio(f"Reply {n}.", tts.PRIORITY_PILOT)
        self.assertEqual(self.m.summary()["last"]["note"], "reply dropped")

    def test_under_the_cap_untouched(self):
        for n in range(3):
            tts.speak_george_radio(f"Routine {n}.", tts.PRIORITY_ROUTINE)
        self.assertEqual(self.texts(), ["Routine 0.", "Routine 1.", "Routine 2."])


if __name__ == "__main__":
    unittest.main()
