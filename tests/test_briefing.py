"""The briefing knowledge base: search, spoken answers, the model's answers checked against the
extracts, the DCS mission briefing, and who may answer from what. No model, no DCS."""
import json
import os
import shutil
import tempfile
import time
import unittest

from common import briefing as kb

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")


class TestBriefing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(DATA, "examples", "campaign_briefing.sample.json"),
                    os.path.join(self.dir, "campaign_briefing.json"))
        shutil.copy(os.path.join(DATA, "apache_procedures.json"), self.dir)
        self.kb = kb.KnowledgeBase(self.dir, extra=kb.checklist_entries)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_package_priority_is_said_as_written(self):
        said = self.kb.answer("what's our package priority today?", "george", "AH-64D")
        self.assertTrue(said.startswith("Package priority: one, the SA-6 battery"), said)

    def test_roe_by_sector(self):
        self.assertIn("Sector Bravo", self.kb.answer("what are the rules of engagement in sector bravo", "george"))
        self.assertIn("Return fire only", self.kb.answer("what's the ROE in sector charlie?", "george"))

    def test_overlord_briefing_only_with_prefix(self):
        said = self.kb.answer("what's the frequency for the JTAC", "overlord")
        self.assertTrue(said.startswith("Overlord, "), said)
        self.assertIn("30.5", said)
        # procedures aren't Overlord's business
        self.assertEqual(self.kb.answer("how do I get you to fire rockets", "overlord"),
                         "Overlord, negative, that's not in the briefing.")

    def test_procedures_for_the_right_aircraft(self):
        self.assertIn("rockets", self.kb.answer("can you fire rockets", "george", "AH-64D").lower())
        self.assertIn("Starting engines", self.kb.answer("what's the engine start procedure", "george", "AH-64D"))
        huey = self.kb.answer("what's the engine start procedure", "george", "UH-1H")
        self.assertNotIn("APU", huey)

    def test_not_found(self):
        self.assertEqual(self.kb.answer("what's the weather on mars", "george"), "That's not in the briefing.")

    def test_long_text_uses_model_and_checks_numbers(self):
        long = {"entries": [{"title": "Threats", "text": "Expected threats in the AO. " * 10 +
                             "An SA-11 battery sits 14 kilometres east of FARP Hammer. Shilkas escort convoys."}]}
        with open(os.path.join(self.dir, "campaign_briefing.json"), "w") as f:
            json.dump(long, f)
        self.kb._checked = -1e9
        good = lambda msgs: {"say": "An SA-11 battery 14 kilometres east of FARP Hammer."}
        self.assertEqual(self.kb.answer("what are the expected threats", "george", llm=good),
                         "An SA-11 battery 14 kilometres east of FARP Hammer.")
        invented = lambda msgs: {"say": "An SA-11 battery 40 kilometres east."}
        self.assertTrue(self.kb.answer("what are the expected threats", "george", llm=invented)
                        .startswith("Expected threats in the AO."))
        def broken(msgs):
            raise TimeoutError("no model")
        self.assertTrue(self.kb.answer("what are the expected threats", "george", llm=broken)
                        .startswith("Expected threats"))
        seen = []
        self.kb.answer("what are the expected threats", "overlord", llm=lambda m: seen.append(m) or {"say": "x"})
        self.assertIn("Use ONLY the extracts", seen[0][0]["content"])
        self.assertIn("SA-11 battery", seen[0][1]["content"])

    def test_reloads_when_the_file_changes(self):
        self.assertEqual(self.kb.answer("what's the codeword for the push", "george"), "That's not in the briefing.")
        time.sleep(0.01)
        path = os.path.join(self.dir, "campaign_briefing.json")
        data = json.load(open(path))
        data["entries"].append({"title": "Codewords", "text": "Codeword for the push: Thunder.",
                                "tags": ["codeword"]})
        json.dump(data, open(path, "w"))
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.kb._checked = -1e9
        self.assertIn("Thunder", self.kb.answer("what's the codeword for the push", "george"))

    def test_mission_briefing_from_dcs(self):
        msg = ("BRIEF Granite Spear\n##SITUATION\nEnemy armour is moving south on the MSR.\n\n"
               "An SA-6 battery covers the Gori bridge.\n##TASK\nDestroy the SA-6 battery first, then the armour.\n"
               "##SORTIE\nSpear 1")
        self.kb.on_brief_message(msg)
        with open(os.path.join(self.dir, "mission_briefing.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["mission"], "Granite Spear")
        self.assertEqual(len(saved["entries"]), 3)       # short first line joins the next one
        self.assertFalse(self.kb.set_mission_briefing(*kb.parse_brief_message(msg)))   # unchanged: not rewritten
        said = self.kb.answer("what's our task for this mission", "overlord")
        self.assertIn("Destroy the SA-6 battery first", said)

    def test_question_detection(self):
        for q in ("what's our package priority today", "George, what are the rules of engagement?",
                  "what's the frequency for overlord", "how do I start the engines", "where's the FARP"):
            self.assertTrue(kb.is_briefing_question(q), q)
        for q in ("fuel", "status", "where's the priority threat", "what are the threats", "lock target 3",
                  "where is target 2", "weapons free"):
            self.assertFalse(kb.is_briefing_question(q), q)

    def test_paragraph_split(self):
        parts = kb.split_paragraphs("A. " * 400)
        self.assertTrue(all(len(p) <= 600 for p in parts))


class TestImporter(unittest.TestCase):
    def test_headings_become_entries(self):
        import subprocess, sys
        d = tempfile.mkdtemp()
        src = os.path.join(d, "brief.txt")
        with open(src, "w") as f:
            f.write("PACKAGE PRIORITY\nOne, the SA-6. Two, the armour.\n\n## Rules of engagement\n"
                    "Weapons free in sector Bravo.\n\nFrequencies:\nPackage common 251.0.\n")
        out = os.path.join(d, "out.json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "briefing_import.py"), src, "--out", out],
                           capture_output=True, text=True, cwd=os.path.join(HERE, ".."))
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as f:
            data = json.load(f)
        self.assertEqual([e["title"] for e in data["entries"]], ["Package Priority", "Rules of engagement",
                                                                  "Frequencies"])
        shutil.rmtree(d, ignore_errors=True)


class TestBriefingOct21(unittest.TestCase):
    """The 21 Oct mission: 'what are current mission' got 'An alternate 2016:'."""
    MSG = ("BRIEF tempMission\n##SITUATION\nAn alternate 2016:\n\nThe long-dreaded war, between the US and the PRC, "
           "over Taiwan has broken out.\n\nSoon after the war began, a PLAN amphibious task force invaded Saipan.\n\n"
           "That is about to change. A large Chinese cargo ship docked at the Port of Saipan with IFVs, howitzers "
           "and tanks.\n\nA bold counter strike as been ordered. The goal, to eliminate as much of the PLA's "
           "combat strength before doing so becomes impossible.")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.kb = kb.KnowledgeBase(self.dir)
        self.kb.on_brief_message(self.MSG)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fragment_joins_the_next_paragraph(self):
        with open(os.path.join(self.dir, "mission_briefing.json")) as f:
            first = json.load(f)["entries"][0]["text"]
        self.assertTrue(first.startswith("An alternate 2016: The long-dreaded war"), first)

    def test_mission_question_without_model_gives_the_goal(self):
        said = self.kb.answer("what are current mission", "george")
        self.assertTrue(said.startswith("A bold counter strike as been ordered."), said)

    def test_mission_question_with_model_sees_everything(self):
        seen = []
        said = self.kb.answer("what's our mission?", "overlord",
                              llm=lambda m: seen.append(m) or {"say": "Counter strike: destroy the new PLA armour."})
        self.assertEqual(said, "Overlord, Counter strike: destroy the new PLA armour.")
        self.assertIn("bold counter strike", seen[0][1]["content"])
        self.assertIn("long-dreaded war", seen[0][1]["content"])
