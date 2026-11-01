"""Tactical picture step T6: spoken answers from fixed templates (design section 10; D1, D6; test plan row
"George tactical queries")."""
import glob
import os
import re
import types
import unittest
from dataclasses import replace
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from tactical import queries as q
from tactical.models import (Association, BEST_OF_N, PictureEvent, PictureSnapshot, Status, UNIQUE)
from tests.tactical_fixtures import ME
from tests.test_kneeboard_tactical import picture

SAY = q.Say(dist=lambda m: f"{m / 1000:.0f} kilometres", type=lambda n: {"Osa 9A33 ln": "SA-8"}.get(n, n),
            compass=lambda b: ["north", "north east", "east", "south east", "south", "south west", "west",
                               "north west"][int((b % 360 + 22.5) // 45) % 8])


def ctx(**kw):
    base = dict(me_ll=ME, say=SAY)
    base.update(kw)
    return q.Context(**base)


def ev(kind, tid, text, mono=1000.0):
    return PictureEvent(kind, tid, text, mono, 0)


class Matching(unittest.TestCase):
    def test_questions(self):
        cases = {
            "Tactical picture.": "picture", "George, give me the tactical picture": "picture",
            "ground picture please": "picture", "What's out there?": "picture",
            "What threats are known?": "threats", "Threat summary.": "threats", "Any threats?": "threats",
            "Give me a threat summary": "threats", "what threats do we have": "threats", "Threats": "threats",
            "Where are the unknown contacts?": "unknowns", "What can't George place?": "unknowns",
            "What has JTAC reported?": "jtac", "JTAC report": "jtac", "What's the JTAC target?": "jtac",
            "Which contacts are stale?": "stale", "Stale contacts": "stale", "What's old?": "stale",
            "What's changed?": "changes", "Anything new?": "changes", "What has changed recently?": "changes",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(q.match(text), want)

    def test_not_ours(self):
        for text in ("Overlord, picture", "picture", "request picture", "what do you see", "status", "sitrep",
                     "where is the priority threat", "toggle threat warnings", "air threats",
                     "JTAC, request target", "JTAC target", "list targets", "target 2", "next target",
                     "what threats are near waypoint three and can you also check the fuel state for me"):
            with self.subTest(text=text):
                self.assertIsNone(q.match(text))

    def test_no_cheat_sheet_phrase_is_taken(self):
        """Every bold phrase in COMMANDS*.md belongs to someone else."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        phrases = []
        for path in glob.glob(os.path.join(root, "COMMANDS*.md")):
            with open(path, encoding="utf-8") as f:
                phrases += re.findall(r"\*\*(.+?)\*\*", f.read())
        self.assertGreater(len(phrases), 50)
        taken = [p for p in phrases if q.match(p)]
        self.assertEqual(taken, [])


class Answers(unittest.TestCase):
    def test_no_picture(self):
        self.assertEqual(q.answer("picture", None, ctx()), "No tactical picture yet.")

    def test_picture(self):
        said = q.answer("picture", picture(), ctx())
        self.assertEqual(said, "Picture: 4 ground, 1 air, 1 last known only. Top threat SA-8, radar SAM, "
                               "9 kilometres north, you're inside its approximate ring. "
                               "George has 2 entries he can't place.")

    def test_picture_stale_and_empty(self):
        empty = PictureSnapshot(0, 1000.0, 8.0, ())
        said = q.answer("picture", empty, ctx(stale=True, stale_reason="picture is 8 seconds old"))
        self.assertEqual(said, "Picture stale, picture is 8 seconds old. Picture: no enemy units in DCS's lists "
                               "around you.")

    def test_threats(self):
        self.assertEqual(q.answer("threats", picture(), ctx()),
                         "1 threat. SA-8, radar SAM, 9 kilometres north, you're inside its approximate ring.")
        empty = PictureSnapshot(0, 1000.0, 0.5, ())
        self.assertEqual(q.answer("threats", empty, ctx()), "No threats in the picture.")
        self.assertEqual(q.answer("threats", empty, ctx(stale=True, stale_reason="no picture from DCS")),
                         "Picture stale, no picture from DCS. No threats listed.")     # never "no threats"

    def test_unknowns(self):
        self.assertEqual(q.answer("unknowns", picture(), ctx()),
                         "2 entries George can't place: number 2, armored, class only, 2 units could fit; "
                         "number 3, BMP-2, no position.")
        pic = replace(picture(), george=(Association(1, "T-72B", UNIQUE, "T0042", ("T0042",)),),
                      george_unassociated=())
        self.assertEqual(q.answer("unknowns", pic, ctx()), "None. Every entry on George's list is in the picture.")
        self.assertIn("isn't read yet", q.answer("unknowns", replace(pic, george=()), ctx()))

    def test_jtac(self):
        self.assertEqual(q.answer("jtac", picture(), ctx()), "JTAC gave a Ural-375, still active, 2 kilometres north.")
        pic = picture()
        gone = tuple(replace(t, status=Status.GONE, status_reason="destroyed by Hog flight")
                     if t.track_id == "T0007" else t for t in pic.tracks)
        self.assertEqual(q.answer("jtac", replace(pic, tracks=gone), ctx()),
                         "JTAC gave a Ural-375, destroyed by Hog flight.")
        none = tuple(replace(t, tags=frozenset()) for t in pic.tracks)
        self.assertEqual(q.answer("jtac", replace(pic, tracks=none), ctx()), "No JTAC target assigned.")
        self.assertEqual(q.answer("jtac", replace(pic, tracks=none), ctx(jtac={"type": "BMP-1", "id": 3})),
                         "JTAC gave a BMP-1; it isn't in the picture now.")

    def test_stale(self):
        self.assertEqual(q.answer("stale", picture(), ctx()),
                         "1 contact not current: BMP-1 last seen 75 seconds ago, 6 kilometres north.")
        self.assertIn("Every contact is from the last scan",
                      q.answer("stale", picture(), ctx(stale=True, stale_reason="picture is 9 seconds old")))
        pic = picture()
        fresh = replace(pic, tracks=tuple(t for t in pic.tracks if t.track_id != "T0012"))
        self.assertEqual(q.answer("stale", fresh, ctx()), "All contacts are current.")

    def test_changes(self):
        events = [ev("new", "T0042", "New: T-72B (T0042).", 950.0),                 # a tank: not news
                  ev("new", "T0051", "New: Osa 9A33 ln (T0051).", 960.0),
                  ev("destroyed", "T0042", "T-72B (T0042) destroyed by George.", 990.0),
                  ev("new", "T0090", "New: Olympus-1-1 (T0090).", 995.0),             # our own flight
                  ev("gone", "T0012", "BMP-1 (T0012) no longer in the picture.", 700.0)]   # too old
        self.assertEqual(q.answer("changes", picture(), ctx(events=events)),
                         "Last 2 minutes: T-72B destroyed by George. New Osa 9A33 ln.")
        self.assertEqual(q.answer("changes", picture(), ctx()), "No changes in the last 2 minutes.")

    def test_only_picture_facts(self):
        """D6: every number and unit name spoken is in the picture (or the wording helpers)."""
        pic = picture()
        allowed = {t.type_name for t in pic.tracks} | {"SA-8", "BMP-2", "ARMORED"}
        for key in ("picture", "threats", "unknowns", "jtac", "stale"):
            said = q.answer(key, pic, ctx())
            for word in re.findall(r"[A-Z][A-Za-z]*-\d+[A-Za-z]*|Ural-375|Osa 9A33 ln", said):
                self.assertTrue(any(word in a for a in allowed if a), (key, word, said))


class Reporting(unittest.TestCase):
    def test_worth_reporting(self):
        pic = picture()
        self.assertTrue(q.worth_reporting(ev("stale", None, "Picture stale."), pic))
        self.assertTrue(q.worth_reporting(ev("new", "T0051", ""), pic))        # air defence
        self.assertTrue(q.worth_reporting(ev("new", "T0020", ""), pic))        # aircraft
        self.assertFalse(q.worth_reporting(ev("new", "T0042", ""), pic))       # a tank appearing
        self.assertTrue(q.worth_reporting(ev("destroyed", "T0042", ""), pic))
        self.assertFalse(q.worth_reporting(ev("new", "T0090", ""), pic))       # ours
        self.assertFalse(q.worth_reporting(ev("gone", "T9999", ""), pic))      # not in the picture


class MainWiring(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.pic = picture()
        self.stub = types.SimpleNamespace(picture=lambda: self.pic, events=lambda since=None: [], stale=False,
                                          stale_reason="", last_tick_mono=None)

    def _with_picture(self):
        return mock.patch.multiple(self.main, TACTICAL=self.stub), \
            mock.patch.object(self.main._KB_CTX, "picture", lambda: self.pic)

    def test_crew_answers(self):
        m = self.main
        p1, p2 = self._with_picture()
        with p1, p2:
            m.state_store.update_telemetry({"ll": list(ME)})
            said, speaker, who = m.shared_reply("What threats are known?")
        self.assertEqual((speaker, who), ("george", "tactical"))
        self.assertTrue(said.startswith("1 threat. SA-8, radar SAM"))

    def test_overlord_answers_in_his_voice(self):
        m = self.main
        p1, p2 = self._with_picture()
        with p1, p2:
            said, speaker, who = m.shared_reply("Overlord, tactical picture.")
            self.assertIsNone(m.tactical_answer("picture"))                 # his air picture stays his
        self.assertEqual((speaker, who), ("overlord", "overlord"))
        self.assertTrue(said.startswith("Overlord, picture: 4 ground"))

    def test_not_updating(self):
        m = self.main
        with mock.patch.object(m._KB_CTX, "picture", lambda: None):
            self.assertEqual(m.tactical_answer("tactical picture"), "The tactical picture isn't updating.")

    def test_off(self):
        with mock.patch.object(self.main, "TACTICAL", None):
            self.assertIsNone(self.main.tactical_answer("tactical picture"))


if __name__ == "__main__":
    unittest.main()
