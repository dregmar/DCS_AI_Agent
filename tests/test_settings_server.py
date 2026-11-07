"""S2 of the kneeboard Settings page (approved 6 Nov 2026): GET /settings, POST /settings and
POST /settings/reset on the kneeboard server, with the per-start key and the local-host check."""
import http.client
import json
import os
import shutil
import tempfile
import types
import unittest

from tests import fakes  # noqa: F401  (config overrides, no settings file)
from core import settings
from kneeboard.server import Kneeboard


class Log:
    def since(self, i):
        return []


class Server(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "settings_local.json")
        self.store = settings.SettingsStore(self.path, cfg=types.SimpleNamespace(WARLORD_MAX_FLIGHTS=4),
                                            printer=lambda s: None, base=self.dir)
        self.kb = Kneeboard(lambda: {}, lambda: {}, Log(), port=0, settings=self.store).start()
        self.addCleanup(self.kb.stop)

    def req(self, method, path, body=None, key=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.kb.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-KB-Key"] = self.kb.key if key is True else key
        if host:
            headers["Host"] = host
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if data and data[:1] in (b"{", b"[") else data)

    def test_get_set_reset(self):
        code, got = self.req("GET", "/settings", key=False)
        self.assertEqual(code, 200)
        self.assertEqual(len(got["settings"]), 37)
        self.assertEqual(got["restart"], [])
        code, got = self.req("POST", "/settings", {"name": "WARLORD_MAX_FLIGHTS", "value": 3})
        self.assertEqual((code, got["ok"], got["setting"]["value"], got["setting"]["source"]), (200, True, 3, "saved"))
        self.assertEqual(self.store.get("WARLORD_MAX_FLIGHTS"), 3)
        code, got = self.req("POST", "/settings/reset", {"name": "WARLORD_MAX_FLIGHTS"})
        self.assertEqual((code, got["setting"]["value"], got["setting"]["source"]), (200, 4, "config"))

    def test_refusals(self):
        code, got = self.req("POST", "/settings", {"name": "WARLORD_MAX_FLIGHTS", "value": 99})
        self.assertEqual((code, got["ok"], got["why"]), (400, False, "Flights up at once: 1 to 8"))
        code, got = self.req("POST", "/settings", {"name": "OLYMPUS_PASSWORD", "value": "x"})
        self.assertEqual((code, got["why"]), (400, "OLYMPUS_PASSWORD can't be changed here"))
        code, got = self.req("POST", "/settings", {"name": "WARLORD_MAX_FLIGHTS", "value": 3}, key=False)
        self.assertEqual((code, got["why"]), (403, "the page is out of date: reload it"))
        code, got = self.req("POST", "/settings", {"name": "WARLORD_MAX_FLIGHTS", "value": 3}, key="guess")
        self.assertEqual(code, 403)
        code, got = self.req("POST", "/settings", {"name": "WARLORD_MAX_FLIGHTS", "value": 3},
                             host="evil.example.com")                    # DNS rebinding
        self.assertEqual((code, got["why"]), (403, "not a local request"))
        self.assertEqual(self.req("GET", "/settings", key=False, host="evil.example.com:5000")[0], 403)
        code, got = self.req("POST", "/settings", {"value": 3})
        self.assertEqual(code, 400)
        code, got = self.req("POST", "/settings", {"name": "WARLORD_MAX_FLIGHTS"})
        self.assertEqual((code, got["why"]), (400, "no value"))
        self.assertEqual(self.store.saved(), {})
        self.assertFalse(os.path.exists(self.path))

    def test_too_large(self):
        code, got = self.req("POST", "/settings", {"name": "OPERATOR_CALLSIGN", "value": "x" * 5000})
        self.assertEqual(code, 413)

    def test_restart_list(self):
        self.req("POST", "/settings", {"name": "TACTICAL_ENABLED", "value": False})
        self.req("POST", "/settings", {"name": "NINE_LINE_READBACK", "value": True})
        self.assertEqual(self.req("GET", "/settings", key=False)[1]["restart"], ["TACTICAL_ENABLED"])

    def test_key_in_the_page_and_version_unchanged(self):
        import kneeboard.server as srv
        page = os.path.join(srv.STATIC, "index.html")
        with open(page, "rb") as f:
            raw = f.read()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "index.html"), "wb") as f:
            f.write(raw + b"\n<meta name=\"kb-key\" content=\"__SETTINGS_KEY__\">")
        from unittest import mock
        with mock.patch.object(srv, "STATIC", tmp):
            code, body = self.req("GET", "/", key=False)
            self.assertEqual(code, 200)
            self.assertIn(self.kb.key.encode(), body)
            self.assertNotIn(b"__SETTINGS_KEY__", body)
            self.assertNotIn(self.kb.key, self.kb.page_version()["v"])

    def test_no_settings_no_routes(self):
        kb = Kneeboard(lambda: {}, lambda: {}, Log(), port=0).start()
        self.addCleanup(kb.stop)
        c = http.client.HTTPConnection("127.0.0.1", kb.port, timeout=5)
        c.request("GET", "/settings")
        self.assertEqual(c.getresponse().status, 404)
        c.close()


class MainWiring(unittest.TestCase):
    def test_store_passed(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("settings=settings.store(),", src)
        self.assertIn("sample=_voice_sample).start()", src)


if __name__ == "__main__":
    unittest.main()
