"""S3 of the kneeboard Settings page (approved 6 Nov 2026): the Settings tab (index.html), "kneeboard,
settings", the voice sample button (POST /settings/sample -> audio.tts.speak_sample) and voice defaults."""
import http.client
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, no settings file)
from core import settings
from kneeboard import pages
from kneeboard.server import Kneeboard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Voice(unittest.TestCase):
    def test_kneeboard_settings(self):
        for said in ("kneeboard, settings", "show the settings page", "kneeboard, options", "open settings"):
            self.assertEqual(pages.match(said, bare_scroll=True), {"kind": "page", "page": "settings"}, said)
        self.assertIsNone(pages.match("settings", bare_scroll=True))          # never bare


class Page(unittest.TestCase):
    def test_hooks(self):
        with open(os.path.join(ROOT, "kneeboard", "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ('data-p="pset"', 'id="sets"', 'const SETTINGS_KEY = "__SETTINGS_KEY__";', '"X-KB-Key": SETTINGS_KEY',
                       'settings: "pset"', 'if (id === "pset") loadSettings();', '"/settings/sample"', '"/settings/reset"',
                       'data-act="resetall"', "RESTART"):
            self.assertIn(needle, html)
        self.assertNotIn("setInterval(loadSettings", html)                    # nothing polls


class Sample(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)
        os.makedirs(os.path.join(self.base, "voices"))
        for n in ("voices/en_US-joe-medium.onnx", "voices/en_US-joe-medium.onnx.json"):
            open(os.path.join(self.base, n), "w").close()
        self.store = settings.SettingsStore(None, cfg=types.SimpleNamespace(), printer=lambda s: None, base=self.base)
        self.played = []
        self.kb = Kneeboard(lambda: {}, lambda: {}, type("L", (), {"since": lambda s, i: []})(), port=0,
                            settings=self.store, sample=self.played.append).start()
        self.addCleanup(self.kb.stop)

    def post(self, body, key=True):
        c = http.client.HTTPConnection("127.0.0.1", self.kb.port, timeout=5)
        c.request("POST", "/settings/sample", json.dumps(body),
                  {"Content-Type": "application/json", **({"X-KB-Key": self.kb.key} if key else {})})
        r = c.getresponse()
        d = json.loads(r.read())
        c.close()
        return r.status, d

    def test_only_listed_voices(self):
        good = os.path.join(self.base, "voices", "en_US-joe-medium.onnx")
        self.assertEqual(self.post({"path": good}), (200, {"ok": True, "why": ""}))
        fakes.wait_until(lambda: self.played, 2.0)
        self.assertEqual(self.played, [good])
        self.assertEqual(self.post({"path": "C:\\Windows\\notepad.onnx"})[1]["why"], "not one of the voices")
        self.assertEqual(self.post({"path": good}, key=False)[0], 403)

    def test_voice_defaults_named(self):
        rows = {r["name"]: r for r in self.store.describe()}
        self.assertEqual(rows["DARKSIDE_MODEL_PATH"]["value"], "en_US-bryce-medium.onnx")
        self.assertEqual(rows["DARKSIDE_MODEL_PATH"]["source"], "default")
        self.assertEqual([os.path.basename(c) for c in rows["DARKSIDE_MODEL_PATH"]["choices"]], ["en_US-joe-medium.onnx"])


class SpeakSample(unittest.TestCase):
    def test_loaded_once_and_queued(self):
        import audio.tts as tts
        loads, queued = [], []
        with mock.patch.object(tts, "_load", lambda path, label: loads.append(path) or object()), \
                mock.patch.object(tts, "speak_radio", lambda text, who, priority=None, max_age=None: queued.append(who)), \
                mock.patch.dict(tts.VOICES, {}), mock.patch.object(tts, "_sample_path", None):
            self.assertTrue(tts.speak_sample("a.onnx", "x"))
            self.assertTrue(tts.speak_sample("a.onnx", "y"))
            self.assertTrue(tts.speak_sample("b.onnx", "z"))
        self.assertEqual(loads, ["a.onnx", "b.onnx"])
        self.assertEqual(queued, [tts.SAMPLE] * 3)

    def test_not_on_the_status_page(self):
        import main
        import audio.tts as tts
        with mock.patch.dict(tts.VOICES, {tts.SAMPLE: object()}):
            row = next(l for l in main.kneeboard_status()["links"] if l["name"] == "Voices (Piper)")
        self.assertNotIn("sample", row["detail"])


if __name__ == "__main__":
    unittest.main()
