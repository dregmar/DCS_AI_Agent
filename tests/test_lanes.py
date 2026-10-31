"""Audit step 5 (approved 30 Oct): the urgent lane. A safety order runs at once, even while the
command worker waits on the model; it voids the model answer in progress and older queued commands
(dropped, logged, comms log only); it never calls the model; normal commands keep their order."""
import queue
import threading
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from core import lanes
from core.lanes import Lanes, is_safety_order
from intelligence import ollama_chat


class TestSafetyOrders(unittest.TestCase):
    def test_matches(self):
        for t in ["Cease fire.", "George, cease fire!", "Two, weapons hold.", "Flight, weapons tight",
                  "stop lasing", "Outcast 2, stop lasing.", "Abort.", "Hold fire, hold fire.",
                  "Crew, hold your fire", "seize fire", "laser off", "check fire now", "Gunners, cease fire",
                  "ceasefire"]:
            self.assertTrue(is_safety_order(t), t)

    def test_not_matches(self):
        for t in ["Should we hold fire?", "two, attack the tank", "cease fire on the tanks to the north please",
                  "abort the checklist", "weapons free", "hold", "fire", "Overlord, abort", "",
                  "George, cease fire and turn left now please"]:
            self.assertFalse(is_safety_order(t), t)


class TestHandlerLock(unittest.TestCase):
    def test_released_frees_it_for_another_thread(self):
        h = lanes.HandlerLock()
        got = threading.Event()
        with h.held():
            with h.released():
                def other():
                    with h.held():
                        got.set()
                t = threading.Thread(target=other)
                t.start()
                self.assertTrue(got.wait(1))
                t.join(1)
            self.assertTrue(h.holding())
        self.assertFalse(h.holding())

    def test_released_is_a_no_op_when_not_held(self):
        h = lanes.HandlerLock()
        with h.released():
            self.assertFalse(h.holding())
        self.assertFalse(h._lock.locked())


class TestModelCall(unittest.TestCase):
    def setUp(self):
        self.l = Lanes()
        self.p = mock.patch.object(ollama_chat, "LANES", self.l)
        self.p.start()
        self.ok = lambda payload, timeout: {"message": {"content": '{"action": "NONE"}'}}

    def tearDown(self):
        self.p.stop()

    def test_never_on_the_urgent_lane(self):
        with self.l.working(time.monotonic(), urgent=True), self.assertRaises(lanes.NeedsModel):
            ollama_chat.chat([{"role": "user", "content": "x"}], "m", post=self.ok)

    def test_answer_dropped_when_superseded(self):
        t = time.monotonic()
        with self.l.working(t):
            self.assertEqual(ollama_chat.chat([{"role": "user", "content": "x"}], "m", post=self.ok),
                             {"action": "NONE"})
            self.l.note_safety(t + 1)
            with self.assertRaises(lanes.Superseded):
                ollama_chat.chat([{"role": "user", "content": "x"}], "m", post=self.ok)

    def test_error_then_superseded_is_quiet(self):
        """A timeout that ends after a safety order is dropped, not reported as 'system fault'."""
        t = time.monotonic()

        def slow(payload, timeout):
            self.l.note_safety(t + 1)
            raise TimeoutError("timed out")

        with self.l.working(t), self.assertRaises(lanes.Superseded):
            ollama_chat.chat([{"role": "user", "content": "x"}], "m", post=slow)

    def test_not_holding_the_lock_is_unaffected(self):
        """Warm-up / priming threads: no lock, no lanes."""
        self.assertEqual(ollama_chat.chat([{"role": "user", "content": "x"}], "m", post=self.ok),
                         {"action": "NONE"})


class TestMainLanes(unittest.TestCase):
    """main.py: the command worker, the urgent lane and the drops, with a fake Ollama that waits."""

    @classmethod
    def setUpClass(cls):
        cls.main = fakes.pipeline_module().main

    def setUp(self):
        main = self.main
        self.l = Lanes()
        self.log, self.done = [], []
        self.release = threading.Event()
        self.cq = queue.Queue()
        self._p = [mock.patch.object(main, "LANES", self.l), mock.patch.object(ollama_chat, "LANES", self.l),
                   mock.patch.object(main, "commands", self.cq),
                   mock.patch.object(main, "COMMS_LOG", mock.Mock(add=lambda who, text: self.log.append(text))),
                   mock.patch.object(main, "handle_command", self.handle)]
        for p in self._p:
            p.start()

    def tearDown(self):
        self.release.set()
        for p in reversed(self._p):
            p.stop()

    def slow_post(self, payload, timeout):
        self.release.wait(5)
        return {"message": {"content": '{"action": "ENGAGE"}'}}

    def handle(self, text, captured):
        """Stands in for handle_command: George's model call for orders, the fast path for safety."""
        if lanes.is_safety_order(text):
            self.done.append(text)
            return
        if text.startswith("model:"):
            ollama_chat.chat([{"role": "user", "content": text}], "m", post=self.slow_post)
        self.done.append(text)

    def worker(self):
        def get():
            while True:
                try:
                    return self.cq.get(timeout=0.05)
                except queue.Empty:
                    if self.stop.is_set():
                        raise SystemExit
        self.stop = threading.Event()
        t = threading.Thread(target=lambda: self._loop(get), daemon=True)
        t.start()
        return t

    def _loop(self, get):
        with mock.patch.object(self.main, "commands", mock.Mock(get=get)):
            try:
                self.main.command_worker()
            except SystemExit:
                pass

    def test_cease_fire_runs_during_the_model_call_and_voids_it(self):
        main = self.main
        t = self.worker()
        t0 = time.monotonic()
        self.cq.put((t0, "model: George, engage the tank", None))
        time.sleep(0.2)                                   # the worker is waiting on the model
        self.assertTrue(main.run_urgent("George, cease fire", t0 + 1, None))
        self.assertEqual(self.done, ["George, cease fire"])   # ran while the model was still thinking
        self.release.set()
        time.sleep(0.3)
        self.stop.set()
        t.join(2)
        self.assertEqual(self.done, ["George, cease fire"])   # the engage never ran
        self.assertEqual(self.log, ["Dropped after a safety order: 'model: George, engage the tank'"])

    def test_queued_older_commands_dropped_newer_kept(self):
        main = self.main
        t0 = time.monotonic()
        self.cq.put((t0, "Outcast 2, attack", None))
        main.run_urgent("Outcast 2, weapons hold", t0 + 1, None)
        self.cq.put((t0 + 2, "Outcast 2, rejoin", None))
        t = self.worker()
        time.sleep(0.3)
        self.stop.set()
        t.join(2)
        self.assertEqual(self.done, ["Outcast 2, weapons hold", "Outcast 2, rejoin"])
        self.assertEqual(self.log, ["Dropped after a safety order: 'Outcast 2, attack'"])

    def test_urgent_needing_the_model_is_queued(self):
        """'Abort' to George has no fixed handler: it goes to the model, but has voided older orders."""
        main = self.main
        t0 = time.monotonic()
        with mock.patch.object(main, "handle_command",
                               lambda text, c: ollama_chat.chat([{"role": "user", "content": text}], "m",
                                                                post=self.slow_post)):
            self.assertFalse(main.run_urgent("Abort", t0, None))
        self.assertTrue(self.l.superseded(t0 - 1))
        self.assertFalse(self.l.handler._lock.locked())

    def test_order_kept(self):
        t = self.worker()
        t0 = time.monotonic()
        for i, text in enumerate(["two, rejoin", "flight, follow me", "Outcast 2, status"]):
            self.cq.put((t0 + i * 0.01, text, None))
        time.sleep(0.3)
        self.stop.set()
        t.join(2)
        self.assertEqual(self.done, ["two, rejoin", "flight, follow me", "Outcast 2, status"])

    def test_urgent_waits_for_a_handler_in_fixed_code(self):
        """The lock is held during a fixed handler: the safety order runs right after, never alongside."""
        main = self.main
        order = []
        inside = threading.Event()

        def busy():
            with self.l.handler.held():
                inside.set()
                time.sleep(0.2)
                order.append("handler done")

        b = threading.Thread(target=busy)
        b.start()
        inside.wait(1)
        with mock.patch.object(main, "handle_command", lambda text, c: order.append(text)):
            main.run_urgent("cease fire", time.monotonic(), None)
        b.join(2)
        self.assertEqual(order, ["handler done", "cease fire"])

    def test_stale_command_still_dropped(self):
        t = self.worker()
        self.cq.put((time.monotonic() - 30, "two, rejoin", None))
        time.sleep(0.2)
        self.stop.set()
        t.join(2)
        self.assertEqual(self.done, [])
        self.assertEqual(self.log, [])                    # stale, not "dropped after a safety order"

    def test_voice_thread_routes_safety_orders(self):
        import numpy as np
        main = self.main
        texts = iter(["two, rejoin", "cease fire"])

        class Seg:
            def __init__(self, text):
                self.text = text

        class Model:
            def transcribe(self, audio, **kw):
                return [Seg(next(texts))], None

        items = [(time.monotonic(), np.zeros(16000, dtype=np.float32))] * 2 + [None]
        it = iter(items)

        def get():
            x = next(it)
            if x is None:
                raise SystemExit
            return x

        with mock.patch.object(main, "utterances", mock.Mock(get=get)), \
                mock.patch.object(main, "run_urgent", lambda text, c, uid: self.done.append("URGENT " + text) or True), \
                self.assertRaises(SystemExit):
            main.voice_worker(Model())
        self.assertEqual(self.done, ["URGENT cease fire"])
        self.assertEqual([x[1] for x in list(self.cq.queue)], ["two, rejoin"])


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


if __name__ == "__main__":
    unittest.main()
