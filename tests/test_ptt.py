"""Push-to-talk: the joystick button poller (fake backend) and keyboard + joystick together."""
import time
import unittest

import numpy as np

from tests.fakes import wait_until
from core.joystick_ptt import JoystickPTT


class FakeBackend:
    name = "fake"

    def __init__(self, devices):
        self.devs = devices            # [(index, name, nbuttons)]
        self.buttons = {}              # (index, button) -> bool
        self.plugged = {d[0] for d in devices}

    def devices(self):
        return [d for d in self.devs if d[0] in self.plugged]

    def open(self, index):
        return index

    def button(self, handle, number):
        if handle not in self.plugged:
            return None
        return self.buttons.get((handle, number), False)


DEVICES = [(0, "Thrustmaster T.16000M", 16), (5, "TECS MODERN THROTTLE", 64)]


class JoystickPoller(unittest.TestCase):
    def make(self, backend, **kw):
        self.events = []
        joy = JoystickPTT(lambda: self.events.append("down"), lambda: self.events.append("up"),
                          poll_hz=200, rescan_sec=0.1, backend_factory=lambda: (backend, ""), **kw)
        self.addCleanup(joy.stop)
        return joy.start()

    def test_found_by_name_and_presses(self):
        be = FakeBackend(DEVICES)
        joy = self.make(be, name_contains="tecs modern", index=0, button=17)
        self.assertTrue(wait_until(lambda: joy.ok))
        self.assertIn("TECS MODERN THROTTLE [5]", joy.device)        # name beats the index
        be.buttons[(5, 17)] = True
        self.assertTrue(wait_until(lambda: self.events == ["down"]))
        be.buttons[(5, 17)] = False
        self.assertTrue(wait_until(lambda: self.events == ["down", "up"]))

    def test_index_fallback(self):
        be = FakeBackend(DEVICES)
        joy = self.make(be, name_contains="Warthog", index=5, button=3)
        self.assertTrue(wait_until(lambda: joy.ok))
        self.assertIn("[5]", joy.device)

    def test_unplug_releases_and_replug_is_found(self):
        be = FakeBackend(DEVICES)
        joy = self.make(be, name_contains="TECS", button=17)
        self.assertTrue(wait_until(lambda: joy.ok))
        be.buttons[(5, 17)] = True
        self.assertTrue(wait_until(lambda: self.events == ["down"]))
        be.plugged.discard(5)                                          # pulled mid-transmission
        self.assertTrue(wait_until(lambda: self.events == ["down", "up"]))
        self.assertTrue(wait_until(lambda: joy.ok is False))
        be.buttons[(5, 17)] = False
        be.plugged.add(5)
        self.assertTrue(wait_until(lambda: joy.ok))

    def test_button_the_device_does_not_have(self):
        be = FakeBackend(DEVICES)
        joy = self.make(be, name_contains="T.16000M", button=40)
        time.sleep(0.3)
        self.assertFalse(joy.ok)


class KeyboardAndJoystick(unittest.TestCase):
    """main.ptt_press / ptt_release: two sources, one transmission."""

    @classmethod
    def setUpClass(cls):
        import main
        main.george.shutdown()            # never let the real-keyboard driver act during tests
        cls.main = main
        main.TRAILING_CAPTURE_SEC = 0.05

    def drain(self):
        while not self.main.utterances.empty():
            self.main.utterances.get_nowait()

    def test_overlapping_presses_make_one_transmission(self):
        m = self.main
        self.drain()
        m.ptt_press("keyboard")
        m.record_callback(np.zeros((8000, 1), np.float32), 8000, None, None)
        m.ptt_press("joystick")                       # joins, doesn't restart
        m.record_callback(np.zeros((8000, 1), np.float32), 8000, None, None)
        m.ptt_release("keyboard")                     # stick still held: keep recording
        time.sleep(0.15)
        self.assertTrue(m.utterances.empty())
        self.assertTrue(m.is_recording)
        m.record_callback(np.zeros((8000, 1), np.float32), 8000, None, None)
        m.ptt_release("joystick")                     # last one up: send
        self.assertTrue(wait_until(lambda: not m.utterances.empty(), 2))
        _, audio = m.utterances.get()
        self.assertEqual(len(audio), 24000)           # all three chunks, one utterance
        self.assertTrue(m.utterances.empty())

    def test_key_repeat_is_ignored(self):
        m = self.main
        self.drain()
        m.ptt_press("keyboard")
        s = m.session
        m.ptt_press("keyboard")                       # Windows auto-repeat
        self.assertEqual(m.session, s)
        m.ptt_release("keyboard")


if __name__ == "__main__":
    unittest.main()
