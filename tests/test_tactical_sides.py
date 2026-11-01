"""Tactical picture step T8: Export.lua's neutral and own-side lists (I-T2 / I-T3) and DCS's own unit
deaths from george_comms_hook.lua (I-T4). No Lua here: the Lua side is checked by its text; the first
flight confirms it."""
import os
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from core import state as st
from tactical import sources
from tactical.clock import PictureClock
from tactical.models import Affiliation, Category, Relation, Status
from tactical.queries import worth_reporting
from tactical.models import PictureEvent
from tests.test_tactical_service import FakeStore, service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lua(name):
    with open(os.path.join(ROOT, "dcs_scripts", name), encoding="utf-8") as f:
        return f.read()


class StateStore(unittest.TestCase):
    def test_lists_parsed(self):
        s = st.ApacheStateStore()
        self.assertIsNone(s.snapshot()["neutral_units"])                 # older Export.lua: not sent
        self.assertIsNone(s.snapshot()["friendly_units"])
        s.update_telemetry({"neu": [["Ural-375", 5, 42.0, 42.0, 100, "g"], ["bad", 1]],
                            "fr": [["M-1 Abrams", 6, 42.1, 42.0, 120, "g"], ["F-16C_50", 7, 42.5, 42.0, 5000, "a"]]})
        snap = s.snapshot()
        self.assertEqual(snap["neutral_units"], [("Ural-375", 5, 42.0, 42.0, 100, "g")])
        self.assertEqual(len(snap["friendly_units"]), 2)
        s.update_telemetry({"alt_ft": 100})                                # a packet without them keeps them
        self.assertEqual(len(s.snapshot()["friendly_units"]), 2)


def reports(snap):
    clock = PictureClock()
    scan = clock.scan_time(snap, 1000.0)
    return sources.world_reports(snap, scan)


BASE = {"my_ll": (42.0, 42.0), "t_model": 100.0, "scan_t": 99.7, "enemy_lists_age": 0.3, "telemetry_age": 0.05,
        "enemy_ground": [("T-72B", 12, 42.02, 42.0, 300.0)], "contacts": [], "air_defence": [], "enemy_air": [],
        "olympus_units": [], "olympus_enemy": [], "wing": [], "coalition": 2}


class Sources(unittest.TestCase):
    def test_sides_known(self):
        snap = dict(BASE, neutral_units=[("Ural-375", 5, 42.01, 42.0, 100.0, "g"), ("Boat", 8, 42.0, 42.1, 0.0, "n")],
                    friendly_units=[("M-1 Abrams", 6, 42.0, 42.01, 120.0, "g"),
                                    ("F-16C_50", 7, 42.5, 42.0, 5000.0, "a")])
        rs, health = reports(snap)
        by = {r.key: r for r in rs}
        self.assertEqual(by["dcs:12"].coalition, 1)                       # the other side
        self.assertNotIn("coalition_unreported", by["dcs:12"].flags)
        self.assertEqual((by["dcs:5"].coalition, by["dcs:5"].relation), (0, Relation.OTHER))
        self.assertEqual(by["dcs:8"].category, Category.NAVAL)
        self.assertEqual((by["dcs:6"].relation, by["dcs:6"].coalition), (Relation.OWN, 2))
        self.assertEqual(by["dcs:7"].category, Category.AIR)
        self.assertTrue(health["world.neu"].up and health["world.fr"].up)

    def test_older_export(self):
        rs, health = reports(dict(BASE))
        r = next(r for r in rs if r.key == "dcs:12")
        self.assertIsNone(r.coalition)
        self.assertIn("coalition_unreported", r.flags)                    # enemy OR neutral, as before
        self.assertFalse(health["world.neu"].up)


class Picture(unittest.TestCase):
    def test_neutrals_and_friends(self):
        store = FakeStore()
        store.put(12, "T-72B", 3)
        store.extra = {"coalition": 2, "neutral_units": [("Ural-375", 5, 42.0 + 2 / 111.32, 42.0, 100.0, "g")],
                       "friendly_units": [("M-1 Abrams", 6, 42.0 + 1 / 111.32, 42.0, 120.0, "g")]}
        svc, _ = service(store)
        pic = svc.tick()
        aff = {t.key: t.affiliation for t in pic.tracks}
        self.assertEqual(aff, {"dcs:12": Affiliation.HOSTILE, "dcs:5": Affiliation.NEUTRAL,
                               "dcs:6": Affiliation.FRIENDLY})
        threats = {pic.track(a.track_id).key for a in pic.threats}
        self.assertNotIn("dcs:5", threats)
        self.assertNotIn("dcs:6", threats)

    def test_neutral_comings_and_goings_arent_news(self):
        store = FakeStore()
        store.extra = {"coalition": 2, "neutral_units": [("Osa 9A33 ln", 5, 42.01, 42.0, 100.0, "g")]}
        svc, _ = service(store)
        pic = svc.tick()
        t = pic.tracks[0]
        self.assertEqual(t.affiliation, Affiliation.NEUTRAL)
        self.assertFalse(worth_reporting(PictureEvent("new", t.track_id, "New: x.", 1000.0, 0), pic))


class DcsEvents(unittest.TestCase):
    def test_parse(self):
        text = ("EVENTS\nDEAD;16777473;Ground-1-1;T-72B;;;\nKILL;16777473;Ground-1-1;T-72B;16777300;Hog 1-1;A-10C\n"
                "LOST;16777480;Ground-1-2;BMP-2;;;\nCRASH;16777490;MiG;MiG-29S;;;\nbroken line\nDEAD;-1;x;y;;;\n"
                "KILL;16777500;x;y;16777301;;F-16C_50")
        self.assertEqual(sources.dcs_events(text), {"dcs:16777473": "a kill by Hog 1-1", "dcs:16777480": "it lost",
                                                    "dcs:16777490": "it crashed", "dcs:16777500": "a kill by F-16C_50"})
        self.assertEqual(sources.dcs_events("MENU 1"), {})
        self.assertEqual(sources.dcs_events(None), {})

    def destroy(self, store, svc, clock, oid):
        store.drop(oid)
        for _ in range(3):
            store.scan()
            clock.t += 1.0
            svc.tick()

    def test_dcs_names_the_kill(self):
        store = FakeStore()
        store.put(12, "T-72B", 3)
        store.put(13, "BMP-2", 4)
        svc, clock = service(store)
        svc.tick()
        svc.note_dcs_events("EVENTS\nKILL;12;Ground-1-1;T-72B;900;Hog 1-1;A-10C")
        svc.note_dcs_events("EVENTS\nDEAD;12;Ground-1-1;T-72B;;;")        # a later DEAD keeps who killed it
        self.assertEqual(svc.dcs_events(), {"dcs:12": "a kill by Hog 1-1"})
        self.destroy(store, svc, clock, 12)
        self.destroy(store, svc, clock, 13)
        reason = {t.key: t.status_reason for t in svc.picture().tracks}
        self.assertEqual(reason["dcs:12"], "destroyed: DCS reports a kill by Hog 1-1")
        self.assertEqual(reason["dcs:13"], "no longer in the picture, cause unknown")   # no evidence (D3)
        self.assertIn("T-72B (T0001) destroyed: DCS reports a kill by Hog 1-1.", [e.text for e in svc.events()])

    def test_expiry_mission_load_and_bad_input(self):
        store = FakeStore()
        svc, clock = service(store)
        svc.note_dcs_events("EVENTS\nDEAD;12;a;b;;;")
        svc.note_dcs_events("garbage")
        svc.note_dcs_events(None)
        self.assertEqual(svc.dcs_events(), {"dcs:12": "it dead"})
        clock.t += 601.0
        self.assertEqual(svc.dcs_events(), {})
        svc.note_dcs_events("EVENTS\nDEAD;14;a;b;;;")
        svc.mission_loaded()
        svc.tick()
        self.assertEqual(svc.dcs_events(), {})                            # unit ids start again


def balance(code):
    """Lua blocks opened minus closed, ignoring comments, strings and long strings (which are code
    for another Lua state: checked on their own)."""
    import re
    code = re.sub(r"--\[(=*)\[.*?\]\1\]", " ", code, flags=re.S)          # long comments
    code = re.sub(r"\[(=*)\[.*?\]\1\]", '""', code, flags=re.S)           # long strings
    code = re.sub(r'"(?:\\.|[^"\\\n])*"' + r"|'(?:\\.|[^'\\\n])*'", '""', code)   # strings
    code = re.sub(r"--[^\n]*", " ", code)                                  # comments
    words = re.findall(r"\b(function|if|do|repeat|end|until|while|for)\b", code)
    n = 0
    for w in words:
        if w in ("function", "if", "do", "repeat"):
            n += 1
        elif w in ("end", "until"):
            n -= 1
    return n


class Lua(unittest.TestCase):
    def test_export_sides(self):
        s = lua("Export.lua")
        self.assertIn("local function george_enemy(c, mine)", s)
        self.assertIn("george_neu, george_fr = nearest(neu, GEORGE_SIDE_MAX), nearest(fr, GEORGE_SIDE_MAX)", s)
        # the enemy lists use the coalition rule, not "not mine"; only the Olympus lists keep ~= my_coal
        self.assertEqual(s.count("george_enemy(o.CoalitionID, my_coal)"), 4)
        self.assertEqual(s.count("o.CoalitionID ~= my_coal"), 1)

    def test_hook_events(self):
        s = lua("george_comms_hook.lua")
        self.assertIn("local EVENTS_CODE = [==[", s)
        self.assertIn("world.addEventHandler(GEORGE_EV.h)", s)
        self.assertIn("    send_events(now)\n", s)
        self.assertIn("gch.events_t = 0, 0, 0, 0, 0, 0", s)
        body = s[s.index("local EVENTS_CODE = [==[") + 24:s.index("]==]", s.index("local EVENTS_CODE"))]
        for word in ("S_EVENT_DEAD", "S_EVENT_KILL", "S_EVENT_UNIT_LOST", "S_EVENT_CRASH", ">= 200"):
            self.assertIn(word, body)
        self.assertEqual(balance(body), 0)                                # the code run in DCS's mission state

    def test_blocks_balance(self):
        """No Lua interpreter: at least every block opened is closed (function / if / do ... end)."""
        for name in ("Export.lua", "george_comms_hook.lua"):
            with self.subTest(name=name):
                self.assertEqual(balance(lua(name)), 0)

    def test_main_wiring(self):
        import main
        from common import comms_menu
        if main.TACTICAL is not None:
            self.assertEqual(comms_menu.ON_EVENTS, main.TACTICAL.note_dcs_events)


if __name__ == "__main__":
    unittest.main()
