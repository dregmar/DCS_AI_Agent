"""Kneeboard alerts tab and ownship track (agreed 30 Oct 2026): the alert registry (severities,
updates instead of duplicates, acknowledge, cause gone, info expiry, history), lost / restored links,
the track recorder (thresholds, turns, jumps, thinning, reset, switch), the voice commands, and the
wiring in main.py (fuel, Overlord's bandit calls, EUFD, training, mission reset). Visual only:
nothing here may speak."""
import json
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from kneeboard import alerts as al
from kneeboard.alerts import Alerts, LinkWatch
from kneeboard.track import Track, MAX_POINTS
from kneeboard.pages import match


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.a = Alerts(wall=self.clock, clock=self.clock)

    def test_raise_update_escalate(self):
        self.a.raise_("fuel", "warning", "Joker fuel")
        self.clock.t += 5
        self.a.raise_("fuel", "critical", "Bingo fuel")
        self.a.raise_("fuel", "warning", "Joker fuel again")           # a repeat can't lower it
        v = self.a.view()
        self.assertEqual(len(v["active"]), 1)
        self.assertEqual(v["active"][0]["severity"], "critical")
        self.assertEqual(v["active"][0]["count"], 3)
        self.assertEqual(v["top"], {"count": 1, "severity": "critical", "text": "Joker fuel again"})
        with self.assertRaises(ValueError):
            self.a.raise_("x", "urgent", "?")

    def test_order_and_marker(self):
        self.a.raise_("lost", "warning", "Olympus lost")
        self.clock.t += 1
        self.a.raise_("popup", "info", "Pop-up group")
        self.clock.t += 1
        self.a.raise_("bandit", "critical", "Bandit closing")
        self.clock.t += 1
        self.a.raise_("popup2", "info", "Another pop-up")
        v = self.a.view()
        self.assertEqual([x["key"] for x in v["active"]], ["popup2", "bandit", "popup", "lost"])   # newest first
        self.assertEqual(v["top"]["severity"], "critical")                                          # worst on the marker
        self.assertEqual(v["top"]["count"], 4)

    def test_acknowledge(self):
        self.a.raise_("a", "warning", "A")
        self.clock.t += 1
        self.a.raise_("b", "critical", "B")
        self.assertTrue(self.a.ack_newest())
        self.assertEqual([x["key"] for x in self.a.view()["active"]], ["a"])
        self.assertEqual(self.a.view()["recent"][0]["state"], "acknowledged")
        self.assertEqual(self.a.ack_all(), 1)
        self.assertIsNone(self.a.view()["top"])
        self.assertFalse(self.a.ack_newest())

    def test_cause_gone_and_info_expiry(self):
        gone = {"v": False}
        self.a.raise_("fuel", "critical", "Bingo", resolved=lambda: gone["v"])
        self.a.raise_("popup", "info", "Pop-up")
        self.a.raise_("broken", "warning", "W", resolved=lambda: 1 / 0)     # a bad check never clears it
        self.a.tick()
        self.assertEqual(len(self.a.view()["active"]), 3)
        gone["v"] = True
        self.clock.t += al.INFO_TTL_S + 1
        self.a.tick()
        v = self.a.view()
        self.assertEqual([x["key"] for x in v["active"]], ["broken"])
        self.assertEqual(sorted(x["state"] for x in v["recent"]), ["expired", "resolved"])
        self.clock.t += al.HISTORY_S + 1
        self.a.tick()
        self.assertEqual(self.a.view()["recent"], [])

    def test_view_is_plain_data_and_only_changes_with_alerts(self):
        self.a.raise_("fuel", "critical", "Bingo", resolved=lambda: False)
        v1 = json.dumps(self.a.view())
        self.clock.t += 5
        self.a.tick()
        self.assertEqual(json.dumps(self.a.view()), v1)

    def test_bounded(self):
        for i in range(al.MAX_ACTIVE + 10):
            self.a.raise_(f"k{i}", "warning", "x")
        self.assertEqual(len(self.a.view()["active"]), al.MAX_ACTIVE)

    def test_reset_moves_to_history(self):
        self.a.raise_("fuel", "critical", "Bingo")
        self.a.reset()
        self.assertEqual(self.a.view()["active"], [])
        self.assertEqual(len(self.a.view()["recent"]), 1)


class TestLinkWatch(unittest.TestCase):
    def setUp(self):
        self.a = Alerts()
        self.w = LinkWatch(self.a)

    def link(self, name, ok):
        return {"name": name, "ok": ok, "detail": ""}

    def test_lost_then_back(self):
        self.w.update([self.link("DCS Olympus", False)])               # never up: no alert
        self.assertEqual(self.a.view()["active"], [])
        self.w.update([self.link("DCS Olympus", True)])
        self.w.update([self.link("DCS Olympus", False)])
        self.assertEqual(self.a.view()["top"]["text"], "Olympus lost")
        self.w.update([self.link("DCS Olympus", False)])               # still down: one alert
        self.assertEqual(len(self.a.view()["active"]), 1)
        self.w.update([self.link("DCS Olympus", True)])
        v = self.a.view()
        self.assertEqual([(x["severity"], x["text"]) for x in v["active"]], [("info", "Olympus back")])

    def test_names_and_unknowns(self):
        links = [self.link("DCS telemetry (Export.lua)", True), self.link("Ollama (qwen3:8b)", True),
                 self.link("Enemy picture (Export.lua scan)", True), self.link("Voices (Piper)", True),
                 self.link("Training (Game Master)", True), self.link("DCS-BIOS", None)]
        self.w.update(links)
        self.w.update([dict(l, ok=False) for l in links])
        texts = sorted(x["text"] for x in self.a.view()["active"])
        self.assertEqual(texts, ["DCS telemetry lost", "Enemy picture stale", "Ollama lost"])

    def test_acknowledged_link_alert_stays_quiet(self):
        self.w.update([self.link("Comms hook", True)])
        self.w.update([self.link("Comms hook", False)])
        self.a.ack_all()
        self.w.update([self.link("Comms hook", False)])
        self.assertEqual(self.a.view()["active"], [])
        self.w.update([self.link("Comms hook", True)])                 # nothing to clear: no "back"
        self.assertEqual(self.a.view()["active"], [])


ME = (42.0, 42.0)


def north_m(m):
    return ME[0] + m / 111_320.0


class TestTrack(unittest.TestCase):
    def setUp(self):
        self.kind = "helo"
        self.t = Track(lambda: {}, kind=lambda s: self.kind)

    def at(self, north=0.0, hdg=0.0, age=0.2, east=0.0):
        import math
        return {"my_ll": (north_m(north), ME[1] + east / (111_320.0 * math.cos(math.radians(42)))),
                "hdg": hdg, "telemetry_age": age}

    def test_thresholds(self):
        self.assertTrue(self.t.sample(self.at(0)))
        self.assertFalse(self.t.sample(self.at(30)))                  # helicopter: 50 m
        self.assertTrue(self.t.sample(self.at(60)))
        self.kind = "jet"
        self.assertFalse(self.t.sample(self.at(260)))                 # jet: 300 m
        self.assertTrue(self.t.sample(self.at(400)))
        self.assertEqual(self.t.points(), 3)

    def test_turns_kept(self):
        self.t.sample(self.at(0, hdg=0))
        self.assertFalse(self.t.sample(self.at(15, hdg=5)))
        self.assertTrue(self.t.sample(self.at(25, hdg=40)))           # turned 40°, moved 25 m

    def test_stale_or_missing_ignored(self):
        self.assertFalse(self.t.sample(self.at(0, age=9)))
        self.assertFalse(self.t.sample({"telemetry_age": 0.1}))
        self.assertEqual(self.t.points(), 0)

    def test_jump_starts_a_new_segment(self):
        self.t.sample(self.at(0))
        self.t.sample(self.at(100))
        self.t.sample(self.at(60_000))                                 # respawned 60 km away
        self.t.sample(self.at(60_100))
        segs = self.t.view()["segs"]
        self.assertEqual([len(s) for s in segs], [2, 2])

    def test_whole_mission_thinned_not_dropped(self):
        t = Track(lambda: {}, kind=lambda s: "helo", max_points=100)
        for i in range(400):
            t.sample(self.at(i * 60.0))
        segs = t.view()["segs"]
        self.assertLessEqual(t.points(), 100)
        self.assertAlmostEqual(segs[0][0][0], round(north_m(0), 5))   # the start is still there
        self.assertAlmostEqual(segs[-1][-1][0], round(north_m(399 * 60.0), 5))

    def test_reset_and_switch(self):
        self.t.sample(self.at(0))
        v = self.t.view()["v"]
        self.t.visible = False
        hidden = self.t.view()
        self.assertEqual((hidden["on"], hidden["segs"]), (False, []))
        self.assertGreater(hidden["v"], v)
        self.t.sample(self.at(100))                                    # still recording while hidden
        self.t.visible = True
        self.assertEqual(len(self.t.view()["segs"][0]), 2)
        self.t.reset()
        self.assertEqual(self.t.view()["segs"], [])
        self.assertTrue(self.t.sample(self.at(5000)))                  # fresh start, no jump segment

    def test_default_off_from_config(self):
        self.assertFalse(Track(lambda: {}, visible=False).view()["on"])


class TestVoice(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(match("Kneeboard, acknowledge."), {"kind": "ack", "all": False})
        self.assertEqual(match("knee board, acknowledge all"), {"kind": "ack", "all": True})
        self.assertEqual(match("Kneeboard, track off"), {"kind": "track", "on": False})
        self.assertEqual(match("kneeboard, show the track"), {"kind": "track", "on": True})
        self.assertEqual(match("show alerts")["page"], "alerts")
        self.assertEqual(match("Kneeboard, alerts")["page"], "alerts")

    def test_never_bare(self):
        for said in ["acknowledge", "acknowledged", "acknowledge all", "track off", "alerts", "warnings",
                     "George, acknowledge", "Overlord, acknowledge"]:
            self.assertIsNone(match(said), said)


class TestSources(unittest.TestCase):
    def test_eufd_hook(self):
        from apache.eufd import EufdMonitor
        said, new = [], []
        m = EufdMonitor(said.append, settle_sec=0)
        m.on_new = lambda kind, text: new.append((kind, text))
        lines = lambda *w: [w[0] if w else None] + [None] * 10
        with mock.patch("apache.eufd.messages", side_effect=[{"warning": [], "caution": [], "advisory": []},
                                                             {"warning": ["ENG 1 FIRE"], "caution": [], "advisory": []},
                                                             {"warning": [], "caution": [], "advisory": []}]):
            m.check(lines(), 0)
            m.check(lines(), 10)
            self.assertEqual(new, [("warning", "ENG 1 FIRE")])
            self.assertEqual(len(said), 1)                              # still spoken as before
            self.assertTrue(m.showing("warning", "ENG 1 FIRE"))
            m.check(lines(), 11)
            self.assertFalse(m.showing("warning", "ENG 1 FIRE"))

    def test_annunciator_active(self):
        from intelligence.llm_client import AutonomousAnnunciator
        a = AutonomousAnnunciator(lambda t: None)
        self.assertFalse(a.active("low_alt"))
        a._alert_states["low_alt"] = True
        self.assertTrue(a.active("low_alt"))


class TestMainWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = fakes.pipeline_module().main

    def setUp(self):
        self.a = Alerts()
        self._p = [mock.patch.object(self.main, "ALERTS", self.a), mock.patch.object(self.main, "KNEEBOARD_ON", True)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()

    def test_voice_ack_and_track_are_silent(self):
        main = self.main
        self.a.raise_("x", "warning", "X")
        self.a.raise_("y", "warning", "Y")
        self.assertEqual(main.shared_reply("Kneeboard, acknowledge")[0], "")
        self.assertEqual(len(self.a.view()["active"]), 1)
        self.assertEqual(main.shared_reply("Kneeboard, acknowledge all")[0], "")
        self.assertEqual(self.a.view()["active"], [])
        was = main.TRACK.visible
        try:
            self.assertEqual(main.shared_reply("Kneeboard, track off")[0], "")
            self.assertFalse(main.TRACK.visible)
            main.shared_reply("Kneeboard, track on")
            self.assertTrue(main.TRACK.visible)
        finally:
            main.TRACK.visible = was

    def test_fuel(self):
        main = self.main
        active = {"joker": True, "bingo": False}
        with mock.patch.object(main.FUEL_WATCH, "stage_active", lambda s: active[s]):
            main._alert_fuel("Overlord, joker fuel, 4,000 pounds. Home plate, 40 miles, steer 270.")
            self.assertEqual(self.a.view()["top"]["severity"], "warning")
            self.assertTrue(self.a.view()["top"]["text"].startswith("Joker fuel"))
            active["bingo"] = True
            main._alert_fuel("Overlord, bingo fuel, 3,000 pounds. Home plate, 40 miles, steer 270. Recommend RTB.")
            self.assertEqual(self.a.view()["top"]["severity"], "critical")
            self.a.tick()
            self.assertEqual(len(self.a.view()["active"]), 1)
            active["bingo"] = False                                    # refuelled
            self.a.tick()
            self.assertEqual(self.a.view()["active"], [])
        main._alert_fuel("Overlord, say again.")                       # not a fuel call
        self.assertEqual(self.a.view()["active"], [])

    def test_air_warnings(self):
        main = self.main
        main._alert_air("Overlord, pop-up group, 2 ship Mi-24V, 25 kilometres north of you, 500 feet.")
        self.assertEqual(self.a.view()["top"]["severity"], "info")
        snap = {"my_ll": (42.0, 42.0), "enemy_air": [("MiG-29S", 9, 42.05, 42.0, 7000)], "enemy_lists_age": 0.5,
                "aircraft": "FA-18C_hornet"}
        with mock.patch.object(main.state_store, "snapshot", lambda: snap):
            main._alert_air("Overlord, bandit, MiG-29S, 3 miles north of you, closing.")
            self.assertEqual(self.a.view()["top"]["severity"], "critical")
            self.a.tick()
            self.assertIn("bandit", self.a.active_keys())
            snap["enemy_air"] = []
            self.a.tick()
            self.assertNotIn("bandit", self.a.active_keys())

    def test_training_and_mission_reset(self):
        main = self.main
        main._alert_training(["two T-72s"])
        self.assertEqual(self.a.view()["top"]["text"], "Training target destroyed: two T-72s")
        main.TRACK.sample({"my_ll": (42.0, 42.0), "hdg": 0, "telemetry_age": 0.1})
        main._on_mission_loaded()
        self.assertEqual(self.a.view()["active"], [])
        self.assertEqual(main.TRACK.points(), 0)

    def test_feeds_are_json(self):
        json.dumps(self.main.TRACK.view())
        self.a.raise_("fuel", "critical", "Bingo", resolved=lambda: False)
        json.dumps(self.a.view())


if __name__ == "__main__":
    unittest.main()
