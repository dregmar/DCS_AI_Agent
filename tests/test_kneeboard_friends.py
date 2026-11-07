"""1 Nov (evening): your grid on the kneeboard map, and your side's units within 5 km."""
import json
import os
import unittest

from tests import fakes  # noqa: F401  (config overrides)
from kneeboard import model
from kneeboard import server as kb_server
from tests.test_kneeboard import SNAP, Ctx

ME = SNAP["my_ll"]
KM_LAT = 1000 / 111_320.0


def snap(**kw):
    s = dict(SNAP)
    s.update(kw)
    return s


class Grid(unittest.TestCase):
    def test_grid_in_the_feed(self):
        m = model.build_map(snap(), Ctx())
        self.assertRegex(m["me"]["grid"], r"^\d{1,2}[C-X] [A-Z]{2} \d{4} \d{4}$")       # 8 digits by default

    def test_a10_gets_ten_digits(self):
        m = model.build_map(snap(aircraft="A-10C_2"), Ctx())
        self.assertRegex(m["me"]["grid"], r"^\d{1,2}[C-X] [A-Z]{2} \d{5} \d{5}$")

    def test_no_position_no_grid(self):
        self.assertIsNone(model.build_map({}, Ctx())["me"])


class Friends(unittest.TestCase):
    ROWS = [("M1A2", 50, ME[0] + 1 * KM_LAT, ME[1], 300.0, "g"),          # 1 km
            ("AH-64D", 51, ME[0] + 4 * KM_LAT, ME[1], 500.0, "a"),        # 4 km, air
            ("Far tank", 52, ME[0] + 6 * KM_LAT, ME[1], 300.0, "g"),      # 6 km: not shown
            ("Me again", 53, ME[0] + 0.00005, ME[1], 300.0, "g"),        # 5 m: taken to be you
            ("Ship", 54, ME[0] - 2 * KM_LAT, ME[1], 0.0, "n"),
            ("bad row", "x", None, None, None, "g")]

    def test_within_5_km_nearest_first(self):
        m = model.build_map(snap(friendly_units=self.ROWS, wing=[("Rotary-2-2", 60, ME[0] + 0.5 * KM_LAT, ME[1])]), Ctx())
        f = m["friends"]
        self.assertEqual([x["id"] for x in f], [60, 50, 54, 51])
        self.assertEqual({x["id"]: x["cat"] for x in f}, {60: "air", 50: "ground", 54: "naval", 51: "air"})
        self.assertTrue(f[0]["wing"])
        self.assertEqual(f[3]["alt_ft"], 1640)
        self.assertNotIn("alt_ft", f[1])                              # ground: no altitude label
        json.dumps(m)

    def test_radius_from_config(self):
        self.assertEqual([x["id"] for x in model.friends_near(snap(friendly_units=self.ROWS), ME, 7000)],
                         [50, 54, 51, 52])

    def test_range_is_sent_for_the_legend(self):
        import config
        was = getattr(config, "FRIENDLY_SHOW_M", None)
        try:
            config.FRIENDLY_SHOW_M = 20000
            m = model.build_map(snap(friendly_units=self.ROWS), Ctx())
            self.assertEqual(m["friends_m"], 20000.0)
            self.assertIn(52, [x["id"] for x in m["friends"]])          # 6 km away: shown now
            config.FRIENDLY_SHOW_M = None
            self.assertEqual(model.build_map(snap(), Ctx())["friends_m"], 5000.0)
        finally:
            if was is None:
                delattr(config, "FRIENDLY_SHOW_M")
            else:
                config.FRIENDLY_SHOW_M = was

    def test_nothing_without_position_or_lists(self):
        self.assertEqual(model.friends_near(snap(friendly_units=self.ROWS), None), [])
        self.assertEqual(model.build_map(snap(), Ctx())["friends"], [])

    def test_distance_steps_keep_the_feed_quiet(self):
        f = model.friends_near(snap(friendly_units=[("M1A2", 50, ME[0] + 1.01 * KM_LAT, ME[1], 0.0, "g")]), ME)
        self.assertEqual(f[0]["d"] % 50, 0)


class Page(unittest.TestCase):
    def test_page_draws_grid_and_friends(self):
        with open(os.path.join(kb_server.STATIC, "index.html"), encoding="utf-8") as fh:
            src = fh.read()
        for needle in ('id="mapgrid"', "showGrid(me)", "M.friends", '"fr:" + u.id', '["Grid", o.grid]', 'id="frrange"',
                       "M.friends_m"):
            self.assertIn(needle, src)

    def test_position_format_choice(self):
        """MGRS / lat-long DMS / lat-long DDM, chosen in the layers panel and remembered (kb.gridfmt)."""
        with open(os.path.join(kb_server.STATIC, "index.html"), encoding="utf-8") as fh:
            src = fh.read()
        for needle in ('id="gridfmt"', '"kb.gridfmt"', '["mgrs", "MGRS grid"]', '["dms", "Lat / long DMS"]',
                       '["ddm", "Lat / long DDM"]'):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main()
