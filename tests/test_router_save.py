"""Audit step 9: learned phrases are written outside the router lock (the voice thread's
apply_learned no longer waits on the disk), one write at a time, newest wins."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from intelligence import router as router_mod
from intelligence.router import Router


class TestLearnedSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.dir.name, "data", "learned_phrases.json")
        self.r = Router(lambda t: None, lambda t, s: None, learned_file=self.file)

    def tearDown(self):
        self.dir.cleanup()

    def on_disk(self):
        with open(self.file, encoding="utf-8") as f:
            return json.load(f)

    def test_lock_free_during_a_slow_write(self):
        started, release = threading.Event(), threading.Event()
        real_dump = json.dump

        def slow_dump(*a, **k):
            started.set()
            release.wait(5)
            return real_dump(*a, **k)

        with mock.patch.object(router_mod.json, "dump", slow_dump):
            t = threading.Thread(target=self.r.learn, args=("out cast two rejoin", "Outcast 2, rejoin"))
            t.start()
            self.assertTrue(started.wait(2))
            t0 = time.monotonic()
            self.assertEqual(self.r.apply_learned("out cast two rejoin"), "Outcast 2, rejoin")
            self.assertLess(time.monotonic() - t0, 0.5)              # didn't wait for the disk
            release.set()
            t.join(5)
        self.assertEqual(self.on_disk()["phrases"], {"out cast two rejoin": "Outcast 2, rejoin"})

    def test_newest_copy_wins(self):
        """Two learns racing: whichever write runs last, the file holds both phrases."""
        self.r.learn("out cast two rejoin", "Outcast 2, rejoin")
        self.r.learn("over lord picture", "Overlord, picture")
        self.assertEqual(set(self.on_disk()["phrases"]), {"out cast two rejoin", "over lord picture"})
        with self.r._lock:                                           # an old copy can't overwrite
            self.r._saved_version = self.r._version + 5
        self.r.learn("out cast three rejoin", "Outcast 3, rejoin")
        self.assertNotIn("out cast three rejoin", self.on_disk()["phrases"])

    def test_forget_saves(self):
        self.r.learn("out cast two rejoin", "Outcast 2, rejoin")
        self.assertTrue(self.r.forget("Out cast two, rejoin."))
        self.assertEqual(self.on_disk()["phrases"], {})
        self.assertFalse(self.r.forget("never learned"))

    def test_reload(self):
        self.r.learn("over lord picture", "Overlord, picture")
        again = Router(lambda t: None, lambda t, s: None, learned_file=self.file)
        self.assertEqual(again.apply_learned("Over lord, picture."), "Overlord, picture")


if __name__ == "__main__":
    unittest.main()
