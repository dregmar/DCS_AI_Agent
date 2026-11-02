"""1 Nov 2026 redesign (amber command-and-control look): the page kept everything the program and the
voice commands rely on. Static checks on kneeboard/static/index.html; the page itself was exercised in
headless Chromium against kneeboard/server.py with simulated feeds when it was delivered."""
import os
import re
import unittest

from kneeboard import server as kb_server


def page() -> str:
    with open(os.path.join(kb_server.STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


class Layout(unittest.TestCase):
    def setUp(self):
        self.src = page()

    def test_every_feed_is_still_listened_to(self):
        for ev in ("map", "comms", "status", "briefing", "checklist", "ui", "alerts", "track", "instructor", "page"):
            self.assertIn(f'es.addEventListener("{ev}"', self.src, ev)

    def test_seven_pages_and_their_tabs(self):
        for pid in ("pmap", "pbrief", "pcheck", "pcomms", "pstatus", "palerts", "pinstr"):
            self.assertIn(f'id="{pid}"', self.src)
            self.assertIn(f'<button data-p="{pid}"', self.src)
        self.assertIn('id="alerttab"', self.src)

    def test_voice_page_names_unchanged(self):
        m = re.search(r"const PAGE_ID = \{(.*?)\};", self.src, re.S)
        self.assertIsNotNone(m)
        for name in ("map", "briefing", "checklist", "comms", "status", "alerts", "instructor"):
            self.assertIn(f"{name}:", m.group(1))

    def test_map_controls_and_panels(self):
        for el in ('<canvas id="map">', 'id="zin"', 'id="zout"', 'id="orient"', 'id="mapinfo"', 'id="mapnote"',
                   'id="panel"', 'id="legend"', 'id="alertbar"', 'id="layerbtn"', 'id="layoutbtn"'):
            self.assertIn(el, self.src)

    def test_saved_settings_keep_their_keys(self):
        for key in ("kb.tab", "kb.zoom", "kb.trackup", "kb.layers", "kb.layout"):
            self.assertIn(f'"{key}"', self.src)

    def test_colours_by_side(self):
        self.assertIn("u.aff ||", self.src)               # the picture's side; raw enemy lists stay hostile
        for var in ("--friendly", "--hostile", "--neutral", "--unknown"):
            self.assertIn(var + ":", self.src)

    def test_still_light_for_vr(self):
        self.assertNotIn("@keyframes", self.src)
        self.assertNotRegex(self.src, r"\banimation\s*:|\btransition\s*:|backdrop-filter\s*:")
        self.assertNotIn("shadowBlur", self.src)
        self.assertNotRegex(self.src, r"<script[^>]+src=|<link[^>]+href=\"http")   # nothing from the internet


if __name__ == "__main__":
    unittest.main()
