"""Tactical picture step T5: the kneeboard map from the picture, the contact panel, its voice command,
and picture lines in the comms log (design section 9; test plan row "Kneeboard output")."""
import json
import types
import unittest
from dataclasses import replace

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from kneeboard import model, pages
from tactical.models import (Affiliation, Association, BEST_OF_N, Basis, CLASS_ONLY, Category, Evidence, NONE,
                             PictureEvent, PictureSnapshot, Status, ThreatAssessment, UNIQUE)
from tests.tactical_fixtures import ME, track

OLD_KEYS = {"units", "me", "route", "wp_next", "named", "outcasts", "flights", "ground", "air", "jtac", "enemy_age"}


def picture():
    osa = replace(track("T0051", "Osa 9A33 ln", 9), tags=frozenset({"air_defence"}),
                  flags=frozenset({"coalition_unreported"}))
    tank = replace(track("T0042", "T-72B", 3, km_east=1),
                   george=Association(1, "T-72B", UNIQUE, "T0042", ("T0042",), "one unit fits"))
    bmp = replace(track("T0012", "BMP-1", 6, status=Status.OUT_OF_COVERAGE, reason="beyond what the lists can show"),
                  last_seen_mono=1000.0 - 75)
    jt = replace(track("T0007", "Ural-375", 2.2), tags=frozenset({"jtac_target"}))
    gone = track("T0003", "ZU-23", 1, status=Status.GONE)
    friend = track("T0090", "Olympus-1-1", 1, aff=Affiliation.FRIENDLY, cat=Category.AIR)
    mig = track("T0020", "MiG-29S", 40, cat=Category.AIR, alt=3048.0)
    neutral = track("T0030", "Ural-375", 4, aff=Affiliation.NEUTRAL)
    threats = (ThreatAssessment("T0051", "radar SAM", 100, 10_000.0, Basis.INFERRED, 9000.0, True, 1,
                                (Evidence("DCS lists it as SAM TR", Basis.WORLD, "DCS unit attributes"),
                                 Evidence("you are inside its ring", Basis.INFERRED, "kneeboard ring table")),
                                Status.ACTIVE),)
    george = (tank.george, Association(2, "ARMORED", CLASS_ONLY, None, ("T0042", "T0012")),
              Association(3, "BMP-2", NONE, None, (), "no unit in the picture fits"))
    return PictureSnapshot(0, 1000.0, 0.4, (gone, jt, bmp, mig, neutral, tank, osa, friend), threats=threats,
                           george=george, george_unassociated=george[1:], version=3)


class Ctx:
    def __init__(self, pic=None, contact=None):
        self._pic, self._contact = pic, contact

    def picture(self):
        return self._pic

    def contact(self):
        return self._contact

    xz_to_ll = staticmethod(lambda x, z: None)
    is_air_defence = staticmethod(lambda n: False)
    imperial = staticmethod(lambda snap: False)
    named_waypoints = staticmethod(lambda snap: [])
    outcasts = staticmethod(lambda snap: [])
    flights = staticmethod(lambda snap: [])
    jtac = staticmethod(lambda snap: {"type": "Ural-375", "lat": 1.0, "lon": 1.0})     # the JTAC's frozen copy


SNAP = {"my_ll": ME, "hdg": 10.0, "alt_ft": 500, "speed_kts": 90, "aircraft": "AH-64D_BLK_II",
        "enemy_lists_age": 0.5, "enemy_ground": [("BTR-80", 99, 42.01, 42.0, 300.0)], "air_defence": [],
        "enemy_air": []}


class MapFromPicture(unittest.TestCase):
    def setUp(self):
        self.m = model.build_map(SNAP, Ctx(picture()))
        self.g = {u["tid"]: u for u in self.m["ground"]}
        json.dumps(self.m)

    def test_keys_and_source(self):
        self.assertTrue(OLD_KEYS <= set(self.m))
        self.assertEqual(self.m["source"], "picture")

    def test_who_is_drawn(self):
        self.assertEqual(set(self.g), {"T7", "T12", "T30", "T42", "T51"})   # not gone, not friendly, not raw lists
        self.assertEqual([a["tid"] for a in self.m["air"]], ["T20"])
        self.assertEqual(self.m["air"][0]["alt_ft"], 10000)
        self.assertTrue(self.g["T30"]["neutral"])

    def test_affiliation_and_category_for_the_page(self):
        """1 Nov redesign: the page colours by "aff" (unknown = yellow) and filters by "cat"."""
        self.assertEqual((self.g["T42"]["aff"], self.g["T42"]["cat"]), ("hostile", "ground"))
        self.assertEqual(self.g["T30"]["aff"], "neutral")
        self.assertEqual((self.m["air"][0]["aff"], self.m["air"][0]["cat"]), ("hostile", "air"))
        pic = picture()
        unk = track("T0061", "Ship", 5, aff=Affiliation.UNKNOWN, cat=Category.NAVAL)
        m = model.build_map(SNAP, Ctx(replace(pic, tracks=pic.tracks + (unk,))))
        u = next(g for g in m["ground"] if g["tid"] == "T61")
        self.assertEqual((u["aff"], u["cat"], u["neutral"]), ("unknown", "naval", False))
        lists = model.build_map(SNAP, Ctx(None))                              # raw lists: no aff, drawn hostile
        self.assertNotIn("aff", lists["ground"][0])

    def test_last_known_markers(self):
        bmp = self.g["T12"]
        self.assertEqual((bmp["status"], bmp["age_s"]), ("out_of_coverage", 70))     # 75 s in 10 s steps
        self.assertIsNone(self.g["T42"]["age_s"])
        self.assertEqual(self.m["ground"][-1]["tid"], "T12")                      # last-known drawn after current
        self.assertEqual(self.g["T12"]["conf"], "medium")

    def test_rings_george_and_jtac(self):
        self.assertEqual(self.g["T51"]["ring_m"], 10_000.0)                       # from the threat assessment
        self.assertTrue(self.g["T51"]["ad"])
        self.assertIsNone(self.g["T42"]["ring_m"])
        self.assertEqual(self.g["T42"]["george"], 1)
        self.assertEqual(self.m["jtac"]["tid"], "T7")                             # by track, at its live position
        self.assertAlmostEqual(self.m["jtac"]["lat"], self.g["T7"]["lat"])
        self.assertEqual(self.m["george"], ["#2 ARMORED: class only, 2 units could fit", "#3 BMP-2: no position"])

    def test_no_picture_falls_back_to_the_lists(self):
        m = model.build_map(SNAP, Ctx(None))
        self.assertEqual(m["source"], "lists")
        self.assertEqual([g["id"] for g in m["ground"]], [99])
        self.assertEqual(m["jtac"], {"type": "Ural-375", "lat": 1.0, "lon": 1.0})
        self.assertEqual((m["george"], m["panel"]), ([], None))
        old = types.SimpleNamespace(**{k: getattr(Ctx, k) for k in ("xz_to_ll", "is_air_defence", "imperial",
                                                                     "named_waypoints", "outcasts", "flights", "jtac")})
        self.assertEqual(model.build_map(SNAP, old)["source"], "lists")          # a context without picture()

    def test_jtac_not_in_the_picture(self):
        pic = picture()
        pic = replace(pic, tracks=tuple(replace(t, tags=frozenset()) if t.track_id == "T0007" else t for t in pic.tracks))
        self.assertEqual(model.build_map(SNAP, Ctx(pic))["jtac"]["lat"], 1.0)   # the JTAC's own copy


class Panel(unittest.TestCase):
    def rows(self, n, pic=None):
        m = model.build_map(SNAP, Ctx(pic or picture(), contact=n))
        p = m["panel"]
        return p, {r[0]: r[1] for r in p["rows"] if r[0]}, [r[1] for r in p["rows"] if not r[0]]

    def test_threat(self):
        p, rows, extra = self.rows(51)
        self.assertEqual((p["tid"], p["title"]), ("T51", "Osa 9A33 ln"))
        self.assertTrue(rows["Status"].startswith("active: in the DCS world scan"))
        self.assertTrue(rows["Where"].startswith("9.0 km north"))
        self.assertIn("simulator truth", rows["Known from"])
        self.assertEqual(rows["Exists"], "high: in a fresh DCS world scan")
        self.assertEqual(rows["Threat"], "radar SAM, priority 1")
        self.assertEqual(extra, ["DCS lists it as SAM TR", "you are inside its ring"])
        self.assertIn("not your side", rows["Side"])

    def test_last_known_george_and_jtac(self):
        _, rows, _ = self.rows(12)
        self.assertIn("(last known, 75 s ago)", rows["Where"])
        _, rows, _ = self.rows(42)
        self.assertEqual(rows["George"], "his list #1 (unique: one unit fits)")
        _, rows, _ = self.rows(7)
        self.assertEqual(rows["JTAC"], "the current JTAC target")

    def test_unknown_and_no_picture(self):
        p, _, extra = self.rows(500)
        self.assertEqual(extra, ["Not in the picture (never seen, or dropped)."])
        p = model.build_map(SNAP, Ctx(None, contact=5))["panel"]
        self.assertEqual(p["rows"], [["", "The tactical picture isn't running."]])

    def test_short_ids(self):
        self.assertEqual(model.short_tid("T0042"), "T42")
        self.assertEqual(model.short_tid("T12345"), "T12345")


class Voice(unittest.TestCase):
    def test_phrases(self):
        cases = {"Kneeboard, contact 42.": 42, "knee board, show contact tango four two": 42,
                 "kneeboard contact T42": 42, "kneeboard, contact forty two": 42, "new board, contact twelve": 12,
                 "kneeboard, show track number 7": 7, "kneeboard, contact one hundred and five": 105}
        for text, n in cases.items():
            with self.subTest(text=text):
                self.assertEqual(pages.match(text), {"kind": "contact", "n": n})
        for text in ("Kneeboard, close contact", "kneeboard, hide the panel"):
            self.assertEqual(pages.match(text), {"kind": "contact", "n": None})
        for text in ("contact 42", "kneeboard, contact the tank", "George, contact 4"):
            self.assertIsNone(pages.match(text), text)
        self.assertEqual(pages.match("kneeboard, track on")["kind"], "track")    # your track, as before

    def test_page_control(self):
        pc = pages.PageControl()
        pc.apply({"kind": "contact", "n": 42})
        self.assertEqual(pc.contact(), 42)
        pc.apply({"kind": "page", "page": "status"})
        self.assertEqual(pc.contact(), 42)                                       # stays until closed
        pc.apply({"kind": "contact", "n": None})
        self.assertIsNone(pc.contact())


class MainWiring(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.was = main.KNEEBOARD_ON
        main.KNEEBOARD_ON = True

    def tearDown(self):
        self.main.KNEEBOARD_ON = self.was
        self.main.KB_PAGES.clear_contact()

    def test_voice_opens_the_panel(self):
        m = self.main
        self.assertEqual(m.shared_reply("Kneeboard, contact 42."), ("", "george", "kneeboard"))
        self.assertEqual(m.KB_PAGES.contact(), 42)
        self.assertEqual(m.KB_PAGES.view()["kind"], "contact")
        m._on_mission_loaded()
        self.assertIsNone(m.KB_PAGES.contact())                                  # new mission, new numbers

    def test_map_uses_the_picture_only_while_current(self):
        m = self.main
        was = m.TACTICAL.last_tick_mono
        try:
            m.TACTICAL.tick()
            self.assertEqual(m.kneeboard_map()["source"], "picture")
            m.TACTICAL.last_tick_mono -= 60.0                                    # the service stopped updating
            self.assertEqual(m.kneeboard_map()["source"], "lists")
        finally:
            m.TACTICAL.last_tick_mono = was

    def test_comms_log_lines(self):
        m = self.main
        pic = picture()
        stub = types.SimpleNamespace(picture=lambda: pic)
        old_t, before = m.TACTICAL, m.COMMS_LOG.last_id()
        m.state_store.update_telemetry({"ll": list(ME)})
        try:
            m.TACTICAL = stub
            ev = lambda kind, tid, text: PictureEvent(kind, tid, text, 1000.0, 0)
            m._picture_events([
                ev("new", "T0051", "New: Osa 9A33 ln (T0051)."),            # air defence: logged, with where
                ev("new", "T0042", "New: T-72B (T0042)."),                  # an ordinary tank: not logged
                ev("new", "T0020", "New: MiG-29S (T0020)."),                # aircraft: logged
                ev("destroyed", "T0042", "T-72B (T0042) destroyed by George."),
                ev("gone", "T0012", "BMP-1 (T0012) no longer in the picture."),
                ev("new", "T0090", "New: Olympus-1-1 (T0090)."),            # our own flight: not logged
                ev("stale", None, "Picture stale: picture is 7 seconds old."),
            ])
        finally:
            m.TACTICAL = old_t
        lines = [(e["who"], e["text"]) for e in m.COMMS_LOG.since(before)]
        self.assertEqual(lines, [("picture", "New: Osa 9A33 ln (T51), 9 km north of you."),
                                 ("picture", "New: MiG-29S (T20), 40 km north of you."),
                                 ("picture", "T-72B (T42) destroyed by George."),
                                 ("picture", "BMP-1 (T12) no longer in the picture."),
                                 ("picture", "Picture stale: picture is 7 seconds old.")])


if __name__ == "__main__":
    unittest.main()
