"""Latency per utterance and event counts (core/metrics.py, audit step 1): the registry itself, and its
wiring into the voice thread, the speech worker, the Ollama client and the Huey worker."""
import queue
import threading
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (fast timings first)
from core.metrics import Metrics


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.clock, self.lines = Clock(), []
        self.m = Metrics(clock=self.clock, printer=self.lines.append)

    def test_full_record_with_reply(self):
        uid = self.m.start(captured=99.5)                  # picked 0.5 s after capture
        self.m.text(uid, "two, rejoin")
        self.m.stage(uid, "stt", 1.1)
        with self.m.bound(uid):
            self.m.model_time(0.7)
            self.m.model_time(0.3)                          # summed
            self.m.queued(self.m.current())
        self.m.handled(uid, 1.4)
        self.assertEqual(self.lines, [])                    # waiting for its reply
        self.m.playing(uid, enqueued=101.0, synth_secs=0.2, started=101.6)
        self.assertEqual(len(self.lines), 1)
        line = self.lines[0]
        for part in ("queue 0.50", "stt 1.10", "model 1.00", "handler 1.40", "speech_wait 0.40",
                     "synth 0.20", "to_audio 2.10", "'two, rejoin'"):
            self.assertIn(part, line)
        s = self.m.summary()
        self.assertEqual(s["n"], 1)
        self.assertAlmostEqual(s["to_audio"]["median"], 2.1)

    def test_reply_plays_before_handler_ends(self):
        """The handler time is still in the record (reported when handle_command returns)."""
        uid = self.m.start(captured=100.0)
        self.m.queued(uid)
        self.m.playing(uid, 100.1, 0.1, 100.3)
        self.assertEqual(self.lines, [])
        self.m.handled(uid, 0.9)
        self.assertIn("handler 0.90", self.lines[0])
        self.assertIn("to_audio 0.30", self.lines[0])

    def test_no_reply_and_dropped_reply(self):
        a = self.m.start(captured=100.0)
        self.m.handled(a, 0.05)
        self.assertIn("(no reply spoken)", self.lines[-1])
        b = self.m.start(captured=100.0)
        self.m.queued(b)
        self.m.handled(b, 0.1)
        self.m.dropped(b)
        self.assertIn("(reply dropped)", self.lines[-1])
        self.assertNotIn("to_audio", self.lines[-1])

    def test_only_first_transmission_counts(self):
        uid = self.m.start(captured=100.0)
        self.m.queued(uid)
        self.m.queued(uid)
        self.m.handled(uid, 0.1)
        self.m.playing(uid, 100.0, 0.1, 100.5)
        self.m.playing(uid, 100.0, 0.1, 104.0)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("to_audio 0.50", self.lines[0])

    def test_unbound_and_forgotten(self):
        self.m.queued(None)
        self.m.playing(None, 0, 0, 1)
        self.m.model_time(1.0)                              # no utterance bound: ignored
        uid = self.m.start(captured=100.0)
        self.m.forget(uid)
        self.m.handled(uid, 1.0)
        self.assertEqual(self.lines, [])
        self.assertEqual(self.m.summary()["n"], 0)

    def test_binding_is_per_thread(self):
        uid = self.m.start(captured=100.0)
        seen = []
        with self.m.bound(uid):
            t = threading.Thread(target=lambda: seen.append(self.m.current()))
            t.start()
            t.join()
            self.assertEqual(self.m.current(), uid)
        self.assertEqual(seen, [None])
        self.assertIsNone(self.m.current())

    def test_hold_and_release(self):
        uid = self.m.start(captured=100.0)
        self.m.queued(uid)                                   # handed to a worker
        self.m.handled(uid, 0.1)
        self.assertEqual(self.lines, [])
        self.m.release(uid)                                  # the worker said nothing
        self.assertIn("(no reply spoken)", self.lines[0])

    def test_counts_and_bounded(self):
        self.m.count("llm_timeouts")
        self.m.count("llm_timeouts")
        for _ in range(80):
            self.m.start(captured=100.0)                     # never handled: kept at most 50
        self.assertLessEqual(len(self.m._open), 50)
        self.assertEqual(self.m.summary()["counts"], {"llm_timeouts": 2})


class TestSpeechWiring(unittest.TestCase):
    """audio/tts.py: speech queued while an utterance is bound is credited to it; stale drops count."""

    def setUp(self):
        from audio import tts
        self.tts = tts
        self.m = Metrics(printer=lambda s: None)
        self.p = mock.patch.object(tts, "METRICS", self.m)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_queue_item_carries_the_utterance(self):
        tts = self.tts
        q = queue.PriorityQueue()
        uid = self.m.start(captured=time.monotonic())
        with mock.patch.object(tts, "_queue", q), mock.patch.object(tts, "start_speech_worker", lambda: None):
            with self.m.bound(uid):
                tts.speak_george_radio("Two, rejoining.")
            tts.speak_george_radio("Unprompted callout.")
        a, b = q.get_nowait(), q.get_nowait()
        self.assertEqual((a[5], a[6]), ("Two, rejoining.", uid))
        self.assertEqual((b[5], b[6]), ("Unprompted callout.", None))
        self.assertEqual(self.m._open[uid]["pending"], 1)

    def test_play_reports_start_and_keeps_the_audio_path(self):
        """on_start is called once, before playback; the dithered tail and timed stop are unchanged."""
        tts = self.tts
        calls = []
        from tests.test_voices import FakeVoice
        with mock.patch.dict(tts.VOICES, {tts.GEORGE: FakeVoice(16000, "ryan")}), \
                mock.patch.object(tts.sd, "play", create=True) as play, \
                mock.patch.object(tts.sd, "stop", create=True) as stop, \
                mock.patch.object(tts.time, "sleep") as sleep:
            tts._play("Fuel sixty percent.", tts.GEORGE, on_start=lambda s: calls.append((s, play.call_count)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 0)                     # before sd.play
        self.assertEqual(play.call_count, 1)
        full, = play.call_args[0][:1]
        sr = play.call_args[1]["samplerate"]
        want = len(full) / sr + 0.1        # other test threads may also sleep while this is patched
        self.assertTrue(any(c.args and abs(c.args[0] - want) < 1e-6 for c in sleep.call_args_list))
        self.assertEqual(stop.call_count, 1)
        tail = full[-int(sr * 0.5):]
        self.assertTrue(0 < abs(tail).max() < 0.001)

    def test_stale_drop_is_counted(self):
        tts = self.tts
        uid = self.m.start(captured=time.monotonic())
        self.m.queued(uid)
        self.m.handled(uid, 0.1)
        item = (tts.PRIORITY_ROUTINE, 0, time.monotonic() - 60, 5.0, tts.GEORGE, "Old callout.", uid)
        q = mock.Mock()
        q.get.side_effect = [item, SystemExit]               # one item, then stop the loop
        with mock.patch.object(tts, "_queue", q), mock.patch.object(tts, "_play") as play:
            with self.assertRaises(SystemExit):
                tts._worker()
        play.assert_not_called()
        self.assertEqual(self.m.summary()["counts"], {"speech_dropped_stale": 1})
        self.assertEqual(self.m.summary()["last"]["note"], "reply dropped")


    def test_nothing_synthesised_releases_the_record(self):
        tts = self.tts
        uid = self.m.start(captured=time.monotonic())
        self.m.queued(uid)
        self.m.handled(uid, 0.1)
        item = (tts.PRIORITY_PILOT, 0, time.monotonic(), None, tts.GEORGE, "x", uid)
        q = mock.Mock()
        q.get.side_effect = [item, SystemExit]
        with mock.patch.object(tts, "_queue", q), mock.patch.object(tts, "_play", lambda *a, **k: None), \
                mock.patch.object(tts, "ON_SPOKEN", None):
            with self.assertRaises(SystemExit):
                tts._worker()
        self.assertEqual(self.m.summary()["last"]["note"], "reply dropped")


class TestOllamaWiring(unittest.TestCase):
    def test_model_time_and_failures(self):
        from intelligence import ollama_chat
        m = Metrics(printer=lambda s: None)
        uid = m.start(captured=time.monotonic())
        ok = lambda payload, timeout: {"message": {"content": '{"action": "NONE"}'}}

        def slow(payload, timeout):
            raise TimeoutError("timed out")

        with mock.patch.object(ollama_chat, "METRICS", m), m.bound(uid):
            ollama_chat.chat([{"role": "user", "content": "x"}], "qwen3:8b", post=ok)
            with self.assertRaises(TimeoutError):
                ollama_chat.chat([{"role": "user", "content": "x"}], "qwen3:8b", post=slow)
        self.assertIsNotNone(m._open[uid].get("model"))
        self.assertEqual(m.summary()["counts"], {"llm_timeouts": 1})


class TestVoiceWorkerWiring(unittest.TestCase):
    """main.voice_worker: one record per utterance; filtered ones aren't samples; errors counted."""

    @classmethod
    def setUpClass(cls):
        cls.main = fakes.pipeline_module().main   # main with the real driver shut down

    def run_worker(self, items, texts, handle=None):
        main = self.main
        m = Metrics(printer=lambda s: None)
        q = queue.Queue()
        for it in items:
            q.put(it)
        texts = iter(texts)

        class Seg:
            def __init__(self, text):
                self.text = text

        class Model:
            def transcribe(self, audio, **kw):
                return [Seg(next(texts))], None

        def stop():
            raise SystemExit

        q.put(None)
        real_get = q.get

        def get():
            it = real_get()
            if it is None:
                stop()
            return it

        cq = queue.Queue()                       # step 5: the voice thread queues, the command worker runs

        def cget():
            try:
                return cq.get_nowait()
            except queue.Empty:
                raise SystemExit

        with mock.patch.object(main, "METRICS", m), mock.patch.object(main, "utterances", mock.Mock(get=get)), \
                mock.patch.object(main, "handle_command", handle or (lambda text, captured: None)), \
                mock.patch.object(main, "COMMS_LOG", mock.Mock()):
            with mock.patch.object(main, "commands", cq), self.assertRaises(SystemExit):
                main.voice_worker(Model())
            with mock.patch.object(main, "commands", mock.Mock(get=cget)), self.assertRaises(SystemExit):
                main.command_worker()
        return m

    def test_records_and_filters(self):
        import numpy as np
        now = time.monotonic()
        audio = np.zeros(16000, dtype=np.float32)
        m = self.run_worker([(now - 30, audio), (now, audio), (now, audio)],
                            ["two, rejoin", "thank you"])
        s = m.summary()
        self.assertEqual(s["n"], 1)                           # the stale one and "thank you" aren't samples
        self.assertEqual(s["counts"], {"utterances_dropped_stale": 1, "stt_no_speech": 1})
        self.assertEqual(s["last"]["text"], "two, rejoin")
        self.assertIsNotNone(s["last"]["stt"])
        self.assertIsNotNone(s["last"]["handler"])

    def test_handler_error_counted_and_worker_continues(self):
        import numpy as np
        now = time.monotonic()
        audio = np.zeros(16000, dtype=np.float32)
        calls = []

        def handle(text, captured):
            calls.append(text)
            if text == "boom":
                raise ValueError("bad")

        m = self.run_worker([(now, audio), (now, audio)], ["boom", "two, rejoin"], handle)
        self.assertEqual(calls, ["boom", "two, rejoin"])
        self.assertEqual(m.summary()["counts"], {"worker_errors": 1})

    def test_status_page_rows(self):
        main = self.main
        m = Metrics(printer=lambda s: None)
        uid = m.start(captured=100.0, picked=100.2)
        m.queued(uid)
        m.handled(uid, 0.5)
        m.playing(uid, 100.3, 0.2, 101.5)
        m.count("llm_timeouts")
        with mock.patch.object(main, "METRICS", m):
            rows = {r["name"]: r["value"] for r in main.kneeboard_status()["times"]}
        self.assertIn("median 1.50 s", rows["Release to first word"])
        self.assertEqual(rows["Dropped / failed"], "llm timeouts 1")


class TestOlympusStatusRow(unittest.TestCase):
    """main._olympus_link: why Olympus is down, how long ago, failure counts (audit step 2)."""

    def test_rows(self):
        main = fakes.pipeline_module().main

        class Oly:
            def __init__(self, st):
                self.st = st
                self.connected = st["connected"]

            def status(self):
                return self.st

        ex = lambda st: mock.Mock(oly=Oly(st))
        self.assertEqual(main._olympus_link(None)["detail"], "off in config")
        down = main._olympus_link(ex({"connected": False, "last_error": "Olympus not reachable (refused)",
                                      "error_age": 12.0, "ok_age": None, "fails": {"unreachable": 3}}))
        self.assertFalse(down["ok"])
        self.assertEqual(down["detail"], "not connected; last error 12 s ago: Olympus not reachable (refused); "
                                         "failures: unreachable 3")
        up = main._olympus_link(ex({"connected": True, "last_error": "old", "error_age": 300.0,
                                    "ok_age": 2.0, "fails": {"http": 1}}))
        self.assertEqual(up["detail"], "connected; last answer 2 s ago; failures: http 1")
        bad = main._olympus_link(ex({"connected": True, "last_error": "Olympus didn't take setROE (HTTP 500)",
                                     "error_age": 1.0, "ok_age": 30.0, "fails": {"http": 1}}))
        self.assertIn("last error 1 s ago: Olympus didn't take setROE", bad["detail"])


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


if __name__ == "__main__":
    unittest.main()
