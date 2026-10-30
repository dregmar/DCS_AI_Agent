"""DCS manuals in the knowledge base: manual_import.py (PDF bookmarks -> entries), per-aircraft
answers, the kneeboard's Manual section."""
import json
import os
import tempfile
import time
import unittest

import manual_import as mi
from common.briefing import KnowledgeBase, fits, is_manual_question, tokens
from kneeboard import pages

HEADER = "DCS [F/A-18C] EAGLE DYNAMICS"
PAGES = [
    HEADER + "\nTable of Contents\nWeapons ..... 3\n1",
    HEADER + "\nAir-to-Ground Weapons\nThe Hornet carries a wide range of air-to-ground stores. Each is selected on the "
             "stores page of the left DDI.\n2",
    HEADER + "\nAGM-65 Maverick\nThe AGM-65 is a guided missile. It is used against armour and small targets.\n"
             "Employment\nTo employ the Maverick:\n1. Select the MAV on the stores page.\n2. Uncage the seeker "
             "with the cage/uncage button.\n3. Slew the seeker to the target and lock it.\n4. Press the weapon "
             "release button when in range.\n3",
    HEADER + "\nINS Alignment\nThe inertial navigation system must be aligned before taxi. Set the INS knob to "
             "GND for a ground alignment. Alignment takes about four minutes; wait for OK on the HSI.\n4",
    HEADER + "\nWeapon data table\nMk-82 500 lb\nMk-83 1000 lb\nMk-84 2000 lb\nGBU-12 500 lb\n5",
    HEADER + "\nIndex\nAGM-65, 3\nINS, 4\n6",
]
OUTLINE = [(0, "Table of Contents", 0), (0, "Air-to-Ground Weapons", 1), (1, "AGM-65 Maverick", 2),
           (2, "Employment", 2), (0, "INS Alignment", 3), (0, "Weapon data table", 4), (0, "Index", 5)]


class TestImportPure(unittest.TestCase):
    def setUp(self):
        self.entries, self.stats = mi.build_entries(OUTLINE, PAGES)
        self.by = {e["section"]: e for e in self.entries}

    def test_sections_kept_and_skipped(self):
        self.assertIn("Air-to-Ground Weapons > AGM-65 Maverick > Employment", self.by)
        self.assertIn("INS Alignment", self.by)
        for s in self.by:
            self.assertNotIn("Contents", s)
            self.assertNotIn("Index", s)
            self.assertNotIn("Weapon data table", s)          # a table: no sentences
        self.assertEqual(self.stats["skipped: contents / index / legal"], 2)

    def test_header_and_page_numbers_dropped(self):
        for e in self.entries:
            self.assertNotIn("EAGLE DYNAMICS", e["text"])

    def test_same_page_sections_are_cut_at_the_heading(self):
        mav = self.by["Air-to-Ground Weapons > AGM-65 Maverick"]["text"]
        self.assertIn("guided missile", mav)
        self.assertNotIn("Uncage", mav)                     # that belongs to Employment
        emp = self.by["Air-to-Ground Weapons > AGM-65 Maverick > Employment"]["text"]
        self.assertTrue(emp.startswith("To employ the Maverick"), emp)
        self.assertIn("\n2. Uncage the seeker", emp)        # steps keep their own lines
        self.assertEqual(self.by["INS Alignment"]["page"], 4)

    def test_long_sections_split(self):
        long_page = HEADER + "\nRadar\n" + " ".join(f"The radar does thing {i}." for i in range(200))
        e, _ = mi.build_entries([(0, "Radar", 0)], [long_page] * 1)
        self.assertGreater(len(e), 3)
        self.assertTrue(all(len(x["text"]) <= mi.PIECE for x in e))
        self.assertEqual(e[1]["title"], "Radar (2)")


class TestImportPdf(unittest.TestCase):
    def test_real_pdf_round_trip(self):
        try:
            from reportlab.pdfgen import canvas
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("reportlab / pypdf not installed")
        d = tempfile.mkdtemp()
        path = os.path.join(d, "Hornet Guide.pdf")
        c = canvas.Canvas(path)
        for i, text in enumerate(PAGES):
            y = 800
            for line in text.splitlines():
                c.drawString(40, y, line)
                y -= 16
            for depth, title, page in OUTLINE:
                if page == i:
                    key = f"k{title}"
                    c.bookmarkPage(key)
                    c.addOutlineEntry(title, key, level=depth)
            c.showPage()
        c.save()
        out = os.path.join(d, "out")
        old = mi.OUT_DIR
        mi.OUT_DIR = out
        try:
            mi.main([path, "--aircraft", "FA-18C_hornet"])
        finally:
            mi.OUT_DIR = old
        with open(os.path.join(out, "hornet_guide.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["aircraft"], ["FA-18C_hornet"])
        secs = {e["section"] for e in data["entries"]}
        self.assertIn("INS Alignment", secs)
        self.assertIn("Air-to-Ground Weapons > AGM-65 Maverick > Employment", secs)


class TestManualAnswers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "manuals"))
        entries, _ = mi.build_entries(OUTLINE, PAGES)
        with open(os.path.join(self.dir, "manuals", "hornet.json"), "w", encoding="utf-8") as fh:
            json.dump({"aircraft": ["FA-18C_hornet"], "entries": entries}, fh)
        with open(os.path.join(self.dir, "campaign_briefing.json"), "w", encoding="utf-8") as fh:
            json.dump({"entries": [{"title": "ROE", "text": "Weapons tight near the town."}]}, fh)
        self.kb = KnowledgeBase(self.dir)

    def test_only_for_its_aircraft(self):
        self.assertTrue(self.kb.has_manual("FA-18C_hornet"))
        self.assertFalse(self.kb.has_manual("A-10C_2"))
        said = self.kb.answer("how do I align the INS", "george", "FA-18C_hornet")
        self.assertIn("GND", said)
        self.assertIn("not in the briefing", self.kb.answer("how do I align the INS", "george", "A-10C_2"))
        self.assertIn("not in the briefing", self.kb.answer("how do I align the INS", "overlord", None).lower())

    def test_employment_answer_and_kneeboard_section(self):
        said = self.kb.answer("how do I employ the Maverick?", "george", "FA-18C_hornet")
        self.assertIn("Maverick", said)
        b = pages.build_briefing(self.kb, "FA-18C_hornet")
        man = [s for s in b["sections"] if s["key"] == "manual"]
        self.assertEqual(len(man), 1)
        self.assertIn("Employment", man[0]["title"])
        self.assertTrue(any(e["id"] in b["last"]["ids"] for e in man[0]["entries"]))
        self.assertNotIn("INS", json.dumps(man))            # only the answered section is sent

    def test_no_manual_section_before_a_manual_answer(self):
        self.kb.answer("what's the ROE", "george", "FA-18C_hornet")
        keys = [s["key"] for s in pages.build_briefing(self.kb, "FA-18C_hornet")["sections"]]
        self.assertNotIn("manual", keys)

    def test_big_manual_is_fast(self):
        rows = [{"title": f"System {i} > Page {i % 40}", "text": f"The system {i} controls the thing number "
                 f"{i} with the knob and the switch on panel {i % 17}. " * 4, "section": f"System {i}"}
                for i in range(6000)]
        with open(os.path.join(self.dir, "manuals", "big.json"), "w", encoding="utf-8") as fh:
            json.dump({"aircraft": ["A-10C_2"], "entries": rows}, fh)
        kb = KnowledgeBase(self.dir)
        kb.entries()                                        # load once
        t0 = time.perf_counter()
        kb.search("how does the knob on panel 5 work", aircraft="A-10C_2")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestHelpers(unittest.TestCase):
    def test_fits_type_suffixes(self):
        self.assertTrue(fits(["A-10C"], "A-10C_2"))
        self.assertTrue(fits("AH-64D", "AH-64D_BLK_II"))
        self.assertFalse(fits(["FA-18C_hornet"], "AH-64D"))
        self.assertTrue(fits("any", "UH-1H"))

    def test_stems(self):
        self.assertEqual(set(tokens("alignment")), set(tokens("aligning")))
        self.assertIn("employ", tokens("employment"))

    def test_manual_question(self):
        self.assertTrue(is_manual_question("How does the TGP work?"))
        self.assertTrue(is_manual_question("what is the master mode switch"))
        self.assertFalse(is_manual_question("where's the nearest contact"))
        self.assertFalse(is_manual_question("two, rejoin"))


if __name__ == "__main__":
    unittest.main()
