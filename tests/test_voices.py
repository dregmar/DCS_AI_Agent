"""Two voices: George (intercom) and Overlord (command radio). Which voice says what, and the
fallback to George's voice when the Overlord model isn't there. Nothing is played."""
import threading
import unittest
from unittest import mock

import numpy as np

from audio import tts


class FakeChunk:
    def __init__(self, n):
        self.audio_int16_bytes = np.zeros(n, dtype=np.int16).tobytes()


class FakeVoice:
    def __init__(self, rate, name):
        self.config = type("C", (), {"sample_rate": rate})()
        self.name = name
        self.said = []

    def synthesize(self, text):
        self.said.append(text)
        yield FakeChunk(800)


class TestVoices(unittest.TestCase):
    def setUp(self):
        self.george = FakeVoice(16000, "ryan")
        self.overlord = FakeVoice(22050, "joe")
        self.played = []
        self._p = [mock.patch.dict(tts.VOICES, {tts.GEORGE: self.george, tts.OVERLORD: self.overlord}),
                   mock.patch.object(tts.sd, "play", lambda a, samplerate, **k: self.played.append(samplerate),
                                     create=True),
                   mock.patch.object(tts.sd, "stop", lambda: None, create=True),
                   mock.patch.object(tts.time, "sleep", lambda s: None)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_each_speaker_uses_its_own_model_and_rate(self):
        tts._play("Two, attacking.", tts.OVERLORD)
        tts._play("Master arm on.", tts.GEORGE)
        self.assertEqual(self.overlord.said, ["Two, attacking."])
        self.assertEqual(self.george.said, ["Master arm on."])
        self.assertEqual(self.played, [22050, 16000])

    def test_transmission_keeps_the_dithered_tail(self):
        out = tts._build_transmission(np.zeros(2205, dtype=np.float32), 22050)
        tail = out[-int(22050 * 0.5):]
        self.assertTrue(0 < np.abs(tail).max() < 0.001)

    def test_overlord_falls_back_to_george(self):
        with mock.patch.dict(tts.VOICES, {tts.OVERLORD: None}):
            tts._play("Two, attacking.", tts.OVERLORD)
        self.assertEqual(self.george.said, ["Two, attacking."])

    def test_queue_carries_the_speaker(self):
        with mock.patch.object(tts, "start_speech_worker", lambda: None):
            q = tts._queue
            tts.speak_overlord("Flight, rejoin.")
            tts.speak_george_radio("Fuel sixty percent.")
            items = [q.get_nowait() for _ in range(2)]
        self.assertEqual([(i[4], i[5]) for i in items],
                         [("overlord", "Flight, rejoin."), ("george", "Fuel sixty percent.")])


class TestRouting(unittest.TestCase):
    """main.handle_shared: wingman / Olympus replies go to Overlord, everything else to George."""

    @classmethod
    def setUpClass(cls):
        from tests.fakes import pipeline_module   # main with the real driver shut down
        cls.tp = pipeline_module()
        cls.main = cls.tp.main

    def setUp(self):
        self.tp.SPOKEN.clear()
        self.tp.OVERLORD_SPOKEN.clear()

    def test_overlord_request_is_overlord(self):
        fake = mock.Mock()
        fake.handle.return_value = "Overlord copies."
        with mock.patch.object(self.main, "OVERLORD", fake):
            self.assertTrue(self.main.handle_shared("Overlord, requesting SEAD north of me"))
        self.assertEqual(self.tp.OVERLORD_SPOKEN, ["Overlord copies."])
        self.assertEqual(self.tp.SPOKEN, [])

    def test_overlord_with_olympus_off(self):
        with mock.patch.object(self.main, "OVERLORD", None):
            self.assertTrue(self.main.handle_shared("Overlord, SEAD north of me"))
        self.assertIn("turned off", self.tp.OVERLORD_SPOKEN[0])

    def test_wingman_reply_is_george(self):
        with mock.patch.object(self.main.wingmen, "handle", lambda *a: "Two is down."):
            self.assertTrue(self.main.handle_shared("two, attack my target"))
        self.assertEqual(self.tp.SPOKEN, ["Two is down."])
        self.assertEqual(self.tp.OVERLORD_SPOKEN, [])

    def test_silent_wingman_order_says_nothing(self):
        with mock.patch.object(self.main.wingmen, "handle", lambda *a: ""):
            self.assertTrue(self.main.handle_shared("two, attack my target"))
        self.assertEqual(self.tp.OVERLORD_SPOKEN + self.tp.SPOKEN, [])


class TestBriefingRouting(unittest.TestCase):
    """Briefing questions: George answers on the intercom, Overlord on the radio."""

    @classmethod
    def setUpClass(cls):
        from tests.fakes import pipeline_module
        cls.tp = pipeline_module()
        cls.main = cls.tp.main

    def setUp(self):
        self.tp.SPOKEN.clear()
        self.tp.OVERLORD_SPOKEN.clear()

    def test_george_answers_briefing_questions(self):
        with mock.patch.object(self.main, "briefing_answer", lambda q, who="george": f"{who}: {q}"):
            self.main.handle_command("what's our package priority today?")
        self.assertEqual(self.tp.SPOKEN, ["george: what's our package priority today?"])

    def test_overlord_answers_without_olympus(self):
        with mock.patch.object(self.main, "OVERLORD", None), \
                mock.patch.object(self.main, "briefing_answer", lambda q, who="george": f"{who}: {q}"):
            self.main.handle_shared("Overlord, what are the rules of engagement?")
        self.assertEqual(self.tp.OVERLORD_SPOKEN, ["overlord: what are the rules of engagement?"])
