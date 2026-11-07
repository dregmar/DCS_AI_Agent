"""3 Nov: a selected target's position on the kneeboard in the same format as yours - MGRS at your
aircraft's precision (from the program), or lat / long DMS / DDM (worked out on the page)."""
import os
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from kneeboard import model
from common.position import mgrs_text
from tests.test_kneeboard_tactical import SNAP, Ctx, picture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Feed(unittest.TestCase):
    def test_every_target_has_your_grid_format(self):
        m = model.build_map(SNAP, Ctx(picture()))
        digits = len(m["me"]["grid"].split()[-1])                 # the Apache's precision
        for u in m["ground"] + m["air"]:
            self.assertEqual(u["grid"], mgrs_text(u["lat"], u["lon"], digits), u)
        self.assertEqual(m["jtac"]["grid"], mgrs_text(m["jtac"]["lat"], m["jtac"]["lon"], digits))

    def test_lists_fallback_too(self):
        ctx = Ctx(None)
        frozen = {"type": "Ural-375", "lat": 1.0, "lon": 1.0}
        ctx.jtac = lambda snap: frozen
        m = model.build_map(SNAP, ctx)
        self.assertTrue(m["ground"] and all(u.get("grid") for u in m["ground"]))
        self.assertTrue(m["jtac"]["grid"])
        self.assertNotIn("grid", frozen)                             # the JTAC's copy isn't changed

    def test_voice_panel_carries_the_position(self):
        m = model.build_map(SNAP, Ctx(picture(), contact=42))
        pos = m["panel"]["pos"]
        tank = next(u for u in m["ground"] if u["tid"] == "T42")
        self.assertEqual((pos["lat"], pos["lon"], pos["grid"]), (tank["lat"], tank["lon"], tank["grid"]))


class Page(unittest.TestCase):
    def test_same_formatter_for_you_and_targets(self):
        with open(os.path.join(ROOT, "kneeboard", "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("function posPairs(o)", html)
        self.assertIn("posPairs(me).map", html)                   # your position
        self.assertIn("posPairs(p.pos)", html)                    # "kneeboard, contact 42"
        self.assertIn("posPairs(u).forEach", html)                # a tapped marker


if __name__ == "__main__":
    unittest.main()
