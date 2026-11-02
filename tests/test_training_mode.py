"""One training mode (Andre, 1 Nov 2026): "Training mode on" switches training targets and the
instructor's exercises together; everything is said in the instructor's voice; the Training tab shows
the mode, the targets and the scores. The gate (common/training.py) and the instructor stay separate."""
import tempfile
import unittest
from unittest import mock

from tests import fakes
from tests.test_training import FakeHTTP, SECRET, ME, north
from common import olympus, training as training_mod, training_mode
from common.training import Training, TRAINING_COMMANDS, VOICE
from common.training_targets import Spawner
from instructor import catalog, session
from instructor.history import History


class FakeInstructor:
    def __init__(self, stand="", status=""):
        self.calls, self.stand, self.status = [], stand, status

    def stand_down(self, why):
        self.calls.append(("stand_down", why))
        return self.stand

    def status_line(self):
        return self.status


def make_training(snap, client=True, players="PLAYERS 1 sp", olympus_on=True):
    http = FakeHTTP()

    def make_client(guard):
        if not client:
            return None
        return olympus.Olympus("http://localhost:4512", "George", SECRET, opener=http,
                               allowed=TRAINING_COMMANDS, guard=guard)
    t = Training(lambda: snap, make_client, enabled=False, voice_switch=True, max_groups=6, max_units=24,
                 max_range_km=50, path=None, printer=lambda *_: None, templates={},
                 olympus_enabled=lambda: olympus_on)
    if players:
        t.note_players(players)
    return t, http


class _Base(unittest.TestCase):
    def setUp(self):
        self.snap = {"coalition": 2, "my_ll": ME, "self_id": 100, "olympus_enemy": [], "enemy_lists_age": 0.5,
                     "aircraft": "UH-1H"}
        self.t, self.http = make_training(self.snap)
        self.said = []
        self.sp = Spawner(self.t, lambda: self.snap, self.said.append, appear_s=0.5, poll_s=0.01, kill_poll_s=0.01)
        self.instr = FakeInstructor()
        self.tm = training_mode.TrainingMode(self.t, self.sp, self.instr)


class TestRouting(_Base):
    def test_bare_switch(self):
        for said in ("Training mode on.", "training mode on", "Start training.", "turn on training mode"):
            self.assertEqual(self.tm.wanted(said)[0], "on", said)
        for said in ("Training mode off!", "end the training", "exit training", "stop training"):
            self.assertEqual(self.tm.wanted(said)[0], "off", said)

    def test_bare_only_as_the_whole_utterance(self):
        self.assertIsNone(self.tm.wanted("George, I think we should turn the training mode on now please"))
        self.assertIsNone(self.tm.wanted("how do I turn training mode on?"))        # a question
        self.assertIsNone(self.tm.wanted("clear training targets"))                  # needs a name
        self.assertIsNone(self.tm.wanted("show the training"))                       # the kneeboard page
        self.assertIsNone(self.tm.wanted("spawn two T-72s 5 km north"))              # needs a name

    def test_overlord_and_instructor_forms(self):
        self.assertEqual(self.tm.wanted("Overlord, training mode on")[0], "on")
        self.assertEqual(self.tm.wanted("Instructor, training mode off")[0], "off")
        self.assertEqual(self.tm.wanted("Instructor mode on")[0], "on")
        self.assertEqual(self.tm.wanted("Instructor, mode off")[0], "off")
        self.assertEqual(self.tm.wanted("Instructor, on.")[0], "on")
        self.assertEqual(self.tm.wanted("Overlord, clear training targets")[0], "clear")
        self.assertEqual(self.tm.wanted("Instructor, training status")[0], "status")
        self.assertEqual(self.tm.wanted("Overlord, spawn two T-72s 5 km north of me")[0], "spawn")
        self.assertEqual(self.tm.wanted("Instructor, spawn a pair of Mi-24s 10 km east")[0], "spawn")

    def test_others_left_alone(self):
        self.assertIsNone(self.tm.wanted("Instructor, hover exercise"))             # the instructor itself
        self.assertIsNone(self.tm.wanted("Instructor, how did I do"))
        self.assertIsNone(self.tm.wanted("Overlord, request SEAD"))                 # Overlord himself
        self.assertIsNone(self.tm.wanted("Overlord, picture"))


class TestHandling(_Base):
    def test_on_says_it_in_the_instructor_voice(self):
        said = self.tm.handle(*self.tm.wanted("training mode on"))
        self.assertEqual(said, "Instructor, training mode on. Training targets allowed.")
        self.assertTrue(self.t.on)

    def test_on_without_targets_still_on(self):
        for kw, why in (({"client": False}, "no Game Master login in config"),
                        ({"players": "PLAYERS 3 mp"}, "other players are in the mission"),
                        ({"olympus_on": False}, "Olympus is turned off in config"),
                        ({"players": None}, "I can't tell who else is in the mission")):
            t, _ = make_training(self.snap, **kw)
            tm = training_mode.TrainingMode(t, Spawner(t, lambda: self.snap, self.said.append), None)
            said = tm.handle("on")
            self.assertTrue(t.on, kw)
            self.assertIn("training mode on. Training targets unavailable, " + why, said)
            self.assertIn(why, tm.handle("spawn", "spawn a tank 5 km north"))
            self.assertEqual(t.view()["on"], True)

    def test_off_stops_the_exercise_first(self):
        self.t.on = True
        self.instr.stand = "Instructor, exercise cancelled."
        said = self.tm.handle("off")
        self.assertEqual(self.instr.calls, [("stand_down", "training mode off")])
        self.assertEqual(said, "Instructor, exercise cancelled. Training mode off.")
        self.assertFalse(self.t.on)

    def test_off_removes_targets(self):
        self.t.on = True
        e = self.t.add_group(None, [], "two T-72B", requested=2)
        self.t.bind(e, "Olympus-7", [("u1", 701), ("u2", 702)])
        said = self.tm.handle("off")
        self.assertEqual(said, "Instructor, 1 training group removed. Training mode off.")
        self.assertEqual(sorted(c["deleteUnit"]["ID"] for c in self.http.sent), [701, 702])

    def test_status_joins_the_exercise(self):
        self.t.on = True
        self.instr.status = "Hover exercise level 2 selected. Say begin."
        self.assertEqual(self.tm.handle("status"),
                         "Instructor, training mode on, 0 groups, 0 units alive. Hover exercise level 2 selected. Say begin.")
        self.t.on = False
        self.assertEqual(self.tm.handle("status"), "Instructor, training mode off.")

    def test_spawn_replies_in_the_instructor_voice(self):
        self.t.on = True
        said = self.tm.handle(*self.tm.wanted("Instructor, spawn two T-72s 5 km north of me"))
        self.assertTrue(said.startswith(VOICE), said)
        self.assertIn("T-72B", said)


class TestKillsToTheScoreLog(_Base):
    def test_kill_goes_to_the_instructor_history(self):
        hist = History(None)
        inst = session.Instructor(lambda: self.snap, lambda *_: None, hist, run_async=False,
                                  mission_name=lambda: "Op Test", enabled=lambda: self.t.on)
        self.sp.on_kill = inst.record_kill
        self.t.on = True
        lat, lon = north(5)
        said = self.sp.handle("spawn a tank 5 km north")
        self.assertIn("T-72B", said)
        self.snap["olympus_enemy"] = [["Olympus-9", "u1", 901, lat, lon, 100.0]]
        self.assertTrue(self._wait(lambda: "Instructor, training group is up." in self.said), self.said)
        self.snap["olympus_enemy"] = []
        self.assertTrue(self._wait(lambda: hist.kills()))
        k = hist.kills()[0]
        self.assertEqual((k["what"], k["aircraft"], k["mission"]), ("T-72B", "UH-1H", "Op Test"))
        self.assertGreaterEqual(k["seconds"], 0.0)
        view = inst.view()
        self.assertEqual(view["kills"][0]["what"], "T-72B")
        self.assertTrue(view["training_on"])

    def _wait(self, cond, t=3.0):
        import time
        end = time.monotonic() + t
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def test_kills_kept_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            h = History(d, "andre")
            h.add_kill({"what": "SA-8", "seconds": 64.2, "when": "2026-11-01T18:00:00"})
            self.assertEqual(History(d, "andre").kills()[0]["what"], "SA-8")
            self.assertEqual(History(d, "andre").recent(), [])          # not mixed with the evaluations


class TestInstructorGate(unittest.TestCase):
    def setUp(self):
        self.on = False
        self.snap = {"aircraft": "UH-1H"}
        self.said = []
        self.inst = session.Instructor(lambda: self.snap, self.said.append, History(None), run_async=False,
                                       enabled=lambda: self.on)

    def test_exercises_need_training_mode(self):
        for order in ("Instructor, hover exercise", "Instructor, begin", "Instructor, harder", "Instructor, level 3",
                      "Instructor, approach exercise"):
            self.assertEqual(self.inst.handle(order), session.TRAINING_OFF, order)
        self.assertEqual(self.inst.state, session.IDLE)
        self.on = True
        self.assertIn("hover exercise, level 1", self.inst.handle("Instructor, hover exercise"))

    def test_results_and_status_always_answer(self):
        self.assertIn("no results yet", self.inst.handle("Instructor, how did I do"))
        self.assertEqual(self.inst.handle("Instructor, status"), "Instructor, standing by.")
        self.assertNotEqual(self.inst.handle("Instructor, what should I practise"), session.TRAINING_OFF)

    def test_unit_label(self):
        from common.training_targets import unit_label
        self.assertEqual(unit_label("two T-72B"), "T-72B")
        self.assertEqual(unit_label("SA-6 site"), "SA-6 site")

    def test_stand_down(self):
        self.on = True
        self.inst.handle("Instructor, hover exercise")
        self.inst.state = session.ARMED
        self.assertEqual(self.inst.stand_down(), "Instructor, exercise cancelled.")
        self.assertEqual(self.inst.state, session.IDLE)
        self.inst.handle("Instructor, hover exercise")
        self.inst.state = session.RUNNING
        with mock.patch.object(self.inst, "_end_locked", return_value=("args",)) as end, \
                mock.patch.object(self.inst, "_evaluate") as ev:
            self.assertEqual(self.inst.stand_down("training mode off"), "")
        end.assert_called_once_with("training mode off")        # stored as aborted, not scored
        ev.assert_called_once_with("args")
        self.assertEqual(self.inst.stand_down(), "")             # nothing running: nothing to say

    def test_status_line(self):
        self.on = True
        self.assertEqual(self.inst.status_line(), "")
        self.inst.handle("Instructor, hover exercise")
        self.assertEqual(self.inst.status_line(), "Hover level 1 selected. Say begin.")

    def test_no_gate_without_training(self):
        inst = session.Instructor(lambda: self.snap, self.said.append, History(None), run_async=False)
        self.assertIn("hover exercise, level 1", inst.handle("Instructor, hover exercise"))


class TestTrainingView(_Base):
    def test_groups_and_reason(self):
        self.assertEqual(self.t.view(), {"on": False, "spawn_unavailable": None, "groups": []})
        self.t.on = True
        with mock.patch.object(training_mod.config, "OLYMPUS_GM_PASSWORD", "set", create=True):
            e = self.t.add_group(None, [], "two T-72B", requested=2)
            v = self.t.view()
            self.assertIsNone(v["spawn_unavailable"])
            self.assertEqual(v["groups"], [{"what": "two T-72B", "alive": None, "total": 2, "min": 0}])
            self.t.bind(e, "Olympus-7", [("u1", 701), ("u2", 702)])
            self.snap["olympus_enemy"] = [["Olympus-7", "u1", 701, 0, 0, 0]]
            self.assertEqual(self.t.view()["groups"][0]["alive"], 1)
            self.t.note_players("PLAYERS 2 mp")
            self.assertEqual(self.t.view()["spawn_unavailable"], "other players are in the mission")
        self.assertEqual(self.t.view()["spawn_unavailable"], "no Game Master login in config")


class TestMainWiring(unittest.TestCase):
    def setUp(self):
        self.main = fakes.pipeline_module().main

    def tearDown(self):
        self.main.TRAINING.on = False

    def test_bare_phrase_and_voice(self):
        said, who, src = self.main.shared_reply("Training mode on.")
        self.assertEqual((who, src), ("instructor", "instructor"))
        self.assertTrue(said.startswith("Instructor, training mode on"))
        self.assertTrue(self.main.TRAINING.on)
        said, who, _ = self.main.shared_reply("Instructor mode off")
        self.assertEqual(who, "instructor")
        self.assertFalse(self.main.TRAINING.on)

    def test_instructor_gated_by_training(self):
        said, who, _ = self.main.shared_reply("Instructor, hover exercise")
        self.assertEqual((said, who), (session.TRAINING_OFF, "instructor"))

    def test_spawner_calls_in_the_instructor_voice(self):
        got = []
        with mock.patch.object(self.main, "speak_instructor", lambda t, **k: got.append(t)), \
                mock.patch.object(self.main, "speak_overlord", lambda t, **k: self.fail("Overlord spoke")):
            self.main.SPAWNER.speak("Instructor, training group is up.")
        self.assertEqual(got, ["Instructor, training group is up."])

    def test_training_tab_feed(self):
        d = self.main.kneeboard_instructor()
        self.assertIn("training", d)
        self.assertEqual(set(d["training"]), {"on", "spawn_unavailable", "groups"})
        self.assertIn("kills", d)

    def test_tab_is_called_training(self):
        import os
        html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "kneeboard", "static", "index.html"), encoding="utf-8").read()
        self.assertIn('<span class="lf">Training</span>', html)
        self.assertNotIn('<span class="lf">Instructor</span>', html)


if __name__ == "__main__":
    unittest.main()
