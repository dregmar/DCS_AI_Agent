"""The kneeboard page reloads itself after an update (31 Oct: OpenKneeboard kept the old page, without the
Instructor tab, until its tab was reloaded by hand), and seven tabs fit without overflowing."""
import json
import os
import re
import time
import unittest
import urllib.request

from kneeboard import server as kb_server
from kneeboard.comms_log import CommsLog
from kneeboard.server import Kneeboard


class TestPageVersion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = Kneeboard(lambda: {}, lambda: {"links": []}, CommsLog(), port=0).start()
        cls.base = f"http://127.0.0.1:{cls.kb.port}"

    @classmethod
    def tearDownClass(cls):
        cls.kb.stop()

    def test_served_page_carries_the_version_on_disk(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        m = re.search(r'const PAGE_VERSION = "([0-9a-f]{12})"', html)
        self.assertIsNotNone(m)
        self.assertNotIn("__PAGE_VERSION__", html)
        self.assertEqual(m.group(1), self.kb.page_version()["v"])

    def test_events_send_the_version(self):
        r = urllib.request.urlopen(self.base + "/events", timeout=3)
        got, t0 = "", time.time()
        while time.time() - t0 < 3 and "event: page" not in got:
            got += r.readline().decode()
        line = r.readline().decode()
        r.close()
        self.assertIn("event: page", got)
        self.assertEqual(json.loads(line.split("data:", 1)[1])["v"], self.kb.page_version()["v"])

    def test_page_reloads_only_on_a_real_difference(self):
        with open(os.path.join(kb_server.STATIC, "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("__PAGE_VERSION__"), 1)
        self.assertIn('es.addEventListener("page"', src)
        self.assertIn("PAGE_VERSION.length === 12 && v !== PAGE_VERSION) location.reload()", src)

    def test_seven_tabs_shrink_instead_of_overflowing(self):
        with open(os.path.join(kb_server.STATIC, "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(len(re.findall(r'<button data-p="p', src)), 7)
        self.assertIn("#tabs button { flex: 1 1 0; min-width: 0;", src)


if __name__ == "__main__":
    unittest.main()
