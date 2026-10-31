"""Kneeboard pages (kneeboard/pages.py): voice page commands, the Briefing and Checklist pages."""
import json
import os
import tempfile
import time
import unittest
import urllib.request

from common.briefing import KnowledgeBase, Entry
from huey.checklists import ChecklistRunner, CHECKLISTS
from kneeboard import pages
from kneeboard.comms_log import CommsLog
from kneeboard.server import Kneeboard


class TestMatch(unittest.TestCase):
    def test_pages(self):
        for said, page in [("Show the map.", "map"), ("show me the briefing", "briefing"),
                           ("Kneeboard, briefing", "briefing"), ("George, show the comms log", "comms"),
                           ("open status page", "status"), ("show the checklist", "checklist"),
                           ("bring up the mission brief", "briefing"), ("show comms", "comms"),
                           ("kneeboard map", "map"), ("show system status", "status")]:
            got = pages.match(said)
            self.assertIsNotNone(got, said)
            self.assertEqual(got["page"], page, said)

    def test_named_checklist(self):
        self.assertEqual(pages.match("show the shutdown checklist")["checklist"], "shutdown")
        self.assertEqual(pages.match("show before takeoff checklist")["checklist"], "before_takeoff")
        self.assertIsNone(pages.match("show the checklist")["checklist"])
        self.assertIsNone(pages.match("show the banana checklist"))

    def test_scroll(self):
        self.assertEqual(pages.match("scroll down")["by"], 1)
        self.assertEqual(pages.match("Kneeboard, page up.")["by"], -1)
        self.assertEqual(pages.match("next page")["by"], 1)
        self.assertEqual(pages.match("scroll to the top")["by"], 0)

    def test_apache_scroll_left_for_george(self):
        """28 Oct: "scroll down" moves George's target list in the Apache."""
        self.assertIsNone(pages.match("scroll down", bare_scroll=False))
        self.assertIsNone(pages.match("George, scroll up", bare_scroll=False))
        self.assertEqual(pages.match("kneeboard, scroll down", bare_scroll=False)["by"], 1)
        self.assertEqual(pages.match("page down", bare_scroll=False)["by"], 1)
        self.assertEqual(pages.match("next page", bare_scroll=False)["by"], 1)

    def test_flight_sound_alikes(self):
        """29 Oct flight: how Whisper heard "kneeboard" and the page names."""
        for said, page in [("New board comms.", "comms"), ("Knee board comes.", "comms"),
                           ("Niebord, that's us.", "status"), ("Neebord, map", "map"),
                           ("Knee board, stats", "status")]:
            got = pages.match(said)
            self.assertIsNotNone(got, said)
            self.assertEqual(got["page"], page, said)
        for said, page in [("New word, show status.", "status"), ("New ward status.", "status"),
                           ("New word checklist.", "checklist")]:                     # 30 Oct flight
            self.assertEqual(pages.match(said)["page"], page, said)
        for said in ["that's us", "stats", "comes", "new board", "new word"]:
            self.assertIsNone(pages.match(said), said)

    def test_zoom_needs_kneeboard(self):
        """ "zoom in" alone is George's TADS in the Apache."""
        self.assertEqual(pages.match("Knee board, zoom out."), {"kind": "zoom", "by": 1})
        self.assertEqual(pages.match("kneeboard map zoom in"), {"kind": "zoom", "by": -1})
        self.assertIsNone(pages.match("zoom in"))
        self.assertIsNone(pages.match("George, zoom out"))

    def test_not_page_commands(self):
        """Orders meant for others are left alone (whole utterances only)."""
        for said in ["show the comms menu", "comms", "status", "show me the wheel", "before start checklist",
                     "Overlord, show me the map", "next", "check", "two, show yourself", "map carrier menu",
                     "start checklist", "what's on the map", "page"]:
            self.assertIsNone(pages.match(said), said)


class TestPageControl(unittest.TestCase):
    def test_seq_and_choice(self):
        pc = pages.PageControl()
        self.assertEqual(pc.view()["seq"], 0)
        pc.apply({"kind": "page", "page": "checklist", "checklist": "shutdown"})
        pc.apply({"kind": "scroll", "by": 1})
        v = pc.view()
        self.assertEqual((v["seq"], v["kind"], v["by"]), (2, "scroll", 1))
        self.assertEqual(pc.chosen_checklist(), "shutdown")
        pc.apply({"kind": "page", "page": "checklist", "checklist": None})   # keeps the choice
        self.assertEqual(pc.chosen_checklist(), "shutdown")
        pc.clear_checklist()
        self.assertIsNone(pc.chosen_checklist())


class TestBriefingPage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "campaign_briefing.json"), "w", encoding="utf-8") as fh:
            json.dump({"entries": [{"title": "Package frequency", "text": "Package on 251.000 AM.",
                                    "say": "Package on two five one decimal zero."}]}, fh)
        with open(os.path.join(self.dir, "apache_procedures.json"), "w", encoding="utf-8") as fh:
            json.dump({"entries": [{"title": "Hellfire LOBL", "text": "Lock on before launch."}]}, fh)
        with open(os.path.join(self.dir, "huey_procedures.json"), "w", encoding="utf-8") as fh:
            json.dump({"entries": [{"title": "Sling load", "text": "Hook the load."}]}, fh)
        extra = lambda: [Entry("chk-AH-64D-start", "procedures", "Start checklist", "APU on.", aircraft="AH-64D")]
        self.kb = KnowledgeBase(self.dir, extra=extra)
        self.kb.set_mission_briefing("Op Test", {"Situation": "Enemy armour near Bravo.", "Task": "Destroy it."})

    def test_sections(self):
        b = pages.build_briefing(self.kb, "AH-64D")
        keys = [s["key"] for s in b["sections"]]
        self.assertEqual(keys, ["mission", "campaign", "procedures"])
        self.assertEqual(b["sections"][0]["title"], "Mission briefing: Op Test")
        procs = [e["title"] for e in b["sections"][2]["entries"]]
        self.assertEqual(procs, ["Hellfire LOBL"])          # no Huey procedures, no checklists
        self.assertTrue(b["sections"][2]["closed"])
        self.assertIsNone(b["last"])
        json.dumps(b)

    def test_last_answer_marked(self):
        said = self.kb.answer("what's the package frequency", "george", "AH-64D")
        self.assertIn("two five one", said)
        b = pages.build_briefing(self.kb, "AH-64D")
        camp = b["sections"][1]["entries"][0]
        self.assertEqual(b["last"]["ids"], [camp["id"]])
        self.assertEqual(b["last"]["question"], "what's the package frequency")

    def test_empty(self):
        kb = KnowledgeBase(tempfile.mkdtemp())
        self.assertEqual(pages.build_briefing(kb, "OTHER")["sections"], [])


def _runner(switches):
    return ChecklistRunner(lambda k: switches.get(k), lambda k: None, lambda: [],
                           lambda key, state: (True, f"{key} set."))


class TestChecklistRunnerView(unittest.TestCase):
    def test_results(self):
        sw = {"inverter": "OFF", "main_gen": "OFF"}
        r = _runner(sw)
        self.assertEqual(r.view()["shown"], None)
        r.start("before_start")                 # item 1 needs the pilot
        r.reply("CHECK")                        # breakers: pilot; inverter seen; main gen reads off
        v = r.view()
        self.assertEqual(v["running"], "before_start")
        self.assertEqual(v["results"], ["pilot", "seen"])
        self.assertEqual(v["index"], 2)
        r.reply("SET")
        r.reply("SKIP")
        self.assertEqual(r.view()["results"][2:4], ["set", "skipped"])
        r.reply("STOP")
        v = r.view()
        self.assertEqual((v["running"], v["shown"], v["outcome"]), (None, "before_start", "stopped"))

    def test_complete(self):
        r = _runner({"starter_gen": "STBY GEN"})
        r.start("before_takeoff")
        for _ in range(5):
            if r.active:
                r.reply("CHECK")
        self.assertEqual(r.view()["outcome"], "complete")
        self.assertEqual(len(r.view()["results"]), len(CHECKLISTS["before_takeoff"][1]))


class TestChecklistPage(unittest.TestCase):
    def test_running(self):
        sw = {"inverter": "OFF", "main_gen": "OFF", "battery": "ON"}
        r = _runner(sw)
        r.start("before_start")
        r.reply("CHECK")
        c = pages.build_checklist(r, CHECKLISTS, "UH-1H")
        self.assertEqual(c["running"], "before_start")
        states = [i["state"] for i in c["items"]]
        self.assertEqual(states[:4], ["pilot", "seen", "current", "pending"])
        cur = c["items"][2]
        self.assertEqual((cur["ok"], cur["seen"]), (False, "off"))      # main generator reads off
        battery = c["items"][4]
        self.assertEqual((battery["ok"], battery["state"]), (True, "pending"))
        self.assertFalse(c["items"][5]["auto"])                            # fire warning test: pilot
        self.assertEqual(len(c["lists"]), len(CHECKLISTS))
        json.dumps(c)

    def test_chosen_and_last(self):
        r = _runner({})
        c = pages.build_checklist(r, CHECKLISTS, "UH-1H")
        self.assertIsNone(c["shown"])
        c = pages.build_checklist(r, CHECKLISTS, "UH-1H", chosen="shutdown")
        self.assertEqual(c["shown"], "shutdown")
        self.assertTrue(all(i["state"] == "pending" for i in c["items"]))
        r.start("start")
        r.reply("STOP")
        c = pages.build_checklist(r, CHECKLISTS, "UH-1H")
        self.assertEqual((c["shown"], c["outcome"]), ("start", "stopped"))
        c = pages.build_checklist(r, CHECKLISTS, "UH-1H", chosen="shutdown")    # asked for after the run
        self.assertEqual((c["shown"], c["outcome"]), ("shutdown", None))

    def test_no_crew(self):
        c = pages.build_checklist(None, None, "FA-18C_hornet")
        self.assertEqual(c["lists"], [])


class TestFeeds(unittest.TestCase):
    def test_extra_feeds_on_sse_and_state(self):
        pc = pages.PageControl()
        kb = Kneeboard(lambda: {"me": None}, lambda: {"links": []}, CommsLog(), port=0,
                       feeds={"ui": (pc.view, 0.2), "briefing": (lambda: {"sections": []}, 1.0)}).start()
        try:
            base = f"http://127.0.0.1:{kb.port}"
            st = json.loads(urllib.request.urlopen(base + "/state", timeout=3).read())
            self.assertEqual(st["ui"]["seq"], 0)
            self.assertIn("briefing", st)
            r = urllib.request.urlopen(base + "/events", timeout=3)
            pc.apply({"kind": "page", "page": "briefing"})
            got, t0 = "", time.time()
            while time.time() - t0 < 3 and not ('"seq":1' in got and "event: briefing" in got):
                got += r.readline().decode()
            r.close()
            self.assertIn("event: briefing", got)
            self.assertIn('"page":"briefing"', got)
        finally:
            kb.stop()

    def test_page_has_new_tabs(self):
        with open(os.path.join(os.path.dirname(pages.__file__), "static", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        for needle in ('id="pbrief"', 'id="pcheck"', 'addEventListener("ui"', 'addEventListener("checklist"'):
            self.assertIn(needle, html)


class TestMainPages(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.was = main.KNEEBOARD_ON
        main.KNEEBOARD_ON = True

    def tearDown(self):
        self.main.KNEEBOARD_ON = self.was

    def test_voice_page_command(self):
        m = self.main
        seq = m.KB_PAGES.view()["seq"]
        got = m.shared_reply("Show the briefing.")
        self.assertEqual(got, ("", "george", "kneeboard"))
        v = m.KB_PAGES.view()
        self.assertEqual((v["seq"], v["page"]), (seq + 1, "briefing"))

    def test_off_when_no_kneeboard(self):
        m = self.main
        m.KNEEBOARD_ON = False
        got = m.shared_reply("scroll down")
        self.assertNotEqual(got and got[2], "kneeboard")

    def test_feeds_build(self):
        m = self.main
        json.dumps(m.kneeboard_briefing())
        json.dumps(m.kneeboard_checklist())


def tearDownModule():
    from common import wingmen
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


if __name__ == "__main__":
    unittest.main()
