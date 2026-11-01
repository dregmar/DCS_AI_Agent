"""intelligence/router.py: the model's ROUTE rewrites, the plausibility gate, confirmation,
learned phrases, and how main.py / the Huey use them."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.fakes import wait_until, send_telemetry, DEFAULT_CONTACTS, state_store
from intelligence import router as R


class _Shared:
    """Stands in for main.shared_reply."""
    def __init__(self):
        self.ran = []

    def __call__(self, text):
        self.ran.append(text)
        t = text.lower()
        if t.startswith("outcast 2"):
            return ("", "george") if "air defences" in t or "rejoin" in t else ("Outcast 2, say again.", "george")
        if t.startswith("overlord"):
            return "Overlord, Weasel flight inbound.", "overlord"
        if "waypoint" in t:
            return "Waypoint Bravo marked.", "george"
        return None


class RouterCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, "learned_phrases.json")
        self.now = [100.0]
        self.shared, self.said = _Shared(), []
        self.r = R.Router(self.shared, lambda text, who: self.said.append((text, who)),
                          learned_file=self.file, clock=lambda: self.now[0])


class TestPlausible(unittest.TestCase):
    def ok(self, o, c):
        return R.plausible(o, c)[0]

    def test_outcast_needs_its_number(self):
        self.assertTrue(self.ok("Ocast 2, destroy your defence", "Outcast 2, engage air defences"))
        self.assertTrue(self.ok("forecast two come back to me", "Outcast 2, rejoin"))
        self.assertFalse(self.ok("see you well", "Outcast 2, status"))
        self.assertFalse(self.ok("Ocast 3, go home", "Outcast 2, RTB"))

    def test_overlord_needs_something_like_overlord(self):
        self.assertTrue(self.ok("over lord I need fast movers on those tanks up north", "Overlord, requesting CAS north of me"))
        self.assertFalse(self.ok("over lord I need fast movers on those tanks", "Overlord, requesting CAS north of me"))
        self.assertTrue(self.ok("send me some CAS up north", "Overlord, requesting CAS north of me"))
        self.assertFalse(self.ok("check the engine", "Overlord, picture"))

    def test_wingmen_and_waypoints(self):
        self.assertTrue(self.ok("three, go do your own thing", "three, go independent"))
        self.assertFalse(self.ok("go do your own thing", "three, go independent"))
        self.assertTrue(self.ok("put a marker where we are call it bravo", "Mark our current position as waypoint Bravo"))
        self.assertFalse(self.ok("put a marker where we are", "Mark our current position as waypoint Bravo"))
        self.assertTrue(self.ok("what's the nearest place to land", "Nearest friendly airfield"))

    def test_unchanged_is_not_a_rewrite(self):
        self.assertFalse(self.ok("Outcast 2, dance", "outcast 2 dance"))


class TestDispatch(RouterCase):
    def test_routine_order_runs_and_is_learned(self):
        self.assertTrue(self.r.dispatch("Forecast 2, re-drying", "Outcast 2, rejoin"))
        self.assertEqual(self.shared.ran, ["Outcast 2, rejoin"])
        with open(self.file) as f:
            data = json.load(f)
        self.assertEqual(data["phrases"]["forecast 2 re drying"], "Outcast 2, rejoin")
        self.assertEqual(data["words"].get("forecast"), "outcast")
        # next time: instantly, without the model
        r2 = R.Router(self.shared, lambda *a: None, learned_file=self.file)
        self.assertEqual(r2.apply_learned("Forecast 2, re-drying"), "Outcast 2, rejoin")
        self.assertEqual(r2.apply_learned("Forecast 2, status"), "outcast 2, status")

    def test_risky_order_is_confirmed_first(self):
        self.assertTrue(self.r.dispatch("Ocast 2, destroy your defence", "Outcast 2, engage air defences"))
        self.assertEqual(self.shared.ran, [])
        self.assertEqual(self.said, [("Confirm, Outcast 2, engage air defences?", "george")])
        self.assertTrue(self.r.answer_pending("Affirm."))
        self.assertEqual(self.shared.ran, ["Outcast 2, engage air defences"])
        self.assertIn("ocast", self.r.words)                      # "Ocast" -> "outcast" learned

    def test_negative_and_timeout(self):
        self.r.dispatch("over lord, send a weasel up north", "Overlord, requesting SEAD north of me")
        self.assertTrue(self.r.answer_pending("negative"))
        self.assertEqual(self.said[-1], ("Overlord, copy, disregard.", "overlord"))
        self.assertEqual(self.shared.ran, [])
        self.r.dispatch("over lord, send a weasel up north", "Overlord, requesting SEAD north of me")
        self.now[0] += R.CONFIRM_TIMEOUT_S + 1
        self.assertFalse(self.r.answer_pending("affirm"))         # too late: a new order
        self.assertEqual(self.shared.ran, [])

    def test_other_speech_drops_the_question(self):
        self.r.dispatch("over lord, send a weasel up north", "Overlord, requesting SEAD north of me")
        self.assertFalse(self.r.answer_pending("what's our fuel"))
        self.assertIsNone(self.r.pending())

    def test_failed_rewrite_is_not_learned(self):
        self.assertTrue(self.r.dispatch("Ocast 2, dance", "Outcast 2, do a barrel roll"))
        self.assertEqual(self.said, [("Outcast 2, say again.", "george")])
        self.assertFalse(os.path.exists(self.file))

    def test_refused_rewrite_is_not_learned(self):
        """30 Oct: "Overlord spawn 1, MiG-21, 10 km north" -> "Overlord, request target" answered
        "negative, no targets for you" and was learned; a refusal can't prove the rewrite was right."""
        self.r.shared = lambda text: ("Overlord, negative, no targets for you.", "overlord")
        self.assertTrue(self.r.dispatch("Overlord spawn 1, MiG-21, 10 kilometers, north.", "Overlord, request target"))
        self.assertEqual(self.said, [("Overlord, negative, no targets for you.", "overlord")])
        self.assertFalse(os.path.exists(self.file))

    def test_nothing_done_is_not_learned(self):
        """30 Oct: "Overlord end the training" -> "Overlord, cancel all" answered "no flights tasked"."""
        for answer in ("Overlord, no flights tasked.", "Overlord, no target to mark. Request a target first.",
                       "Nothing to confirm."):
            self.r.shared = lambda text, a=answer: (a, "overlord")
            self.assertTrue(self.r.dispatch("Overlord end the training", "Overlord, cancel all"))
            self.assertFalse(os.path.exists(self.file), answer)

    def test_no_handler(self):
        self.assertTrue(self.r.dispatch("what's the nearest place to land", "Nearest friendly airfield"))
        self.assertEqual(self.said, [("Say again.", "george")])

    def test_implausible_is_refused(self):
        self.assertFalse(self.r.dispatch("see you well", "Outcast 2, rejoin"))
        self.assertEqual(self.shared.ran, [])

    def test_common_words_never_learned(self):
        self.r.learn("to rejoin", "two, rejoin")
        self.r.learn("outcast two over lord", "outcast 2 overlord")
        self.assertNotIn("to", self.r.words)
        self.assertNotIn("lord", self.r.words)

    def test_bad_file_is_ignored(self):
        with open(self.file, "w") as f:
            f.write("{not json")
        r = R.Router(self.shared, lambda *a: None, learned_file=self.file)
        self.assertEqual(r.apply_learned("Outcast 2, rejoin"), "Outcast 2, rejoin")


class TestPrompts(unittest.TestCase):
    def test_route_is_in_every_crew_prompt(self):
        from intelligence.llm_client import GEORGE_SYSTEM
        from huey import commands
        for p in (GEORGE_SYSTEM, commands.llm_system(), R.ROUTER_SYSTEM):
            self.assertIn('"action": "ROUTE"', p)
            self.assertIn("Outcast N, engage air defences", p)

    def test_state_and_hints(self):
        self.assertEqual(R.routing_state([2], ["three"], ["Alpha"], True),
                         "Outcasts: Outcast 2 | Wingmen: three | Named waypoints: Alpha | Olympus up")
        self.assertEqual(R.routing_state([], [], [], False), "Outcasts: none | Olympus down")
        self.assertEqual(R.stt_hints([2, 3], ["Bravo"]), " Outcast 2, Outcast 3, waypoint Bravo.")
        self.assertEqual(R.stt_hints([], []), "")


class TestHueyRoute(unittest.TestCase):
    def _profile(self, res, routed):
        from tests.huey_fakes import FakeHuey, reset_state
        from huey.profile import HueyProfile
        reset_state()
        self.game = FakeHuey()
        self.addCleanup(self.game.stop)
        said = []
        p = HueyProfile(said.append, state_store, self.game.bios, llm=lambda *a: res,
                        link=self.game.link, route=lambda text, cmd: routed.append((text, cmd)) or True)
        return p, said

    def test_route_goes_to_the_router(self):
        routed = []
        p, said = self._profile({"action": "ROUTE", "command": "Outcast 2, rejoin"}, routed)
        p.handle("Forecast two, come back")
        self.assertEqual(routed, [("Forecast two, come back", "Outcast 2, rejoin")])
        self.assertEqual(said, [])

    def test_deferred_say_again_when_the_crew_would_act(self):
        routed, spoken = [], []
        p, said = self._profile({"action": "FLARE", "speech": "Flare."}, routed)
        p.handle("Ocast 2, flare out", deferred=lambda: spoken.append("Outcast 2, say again."))
        self.assertEqual(spoken, ["Outcast 2, say again."])
        time.sleep(0.2)
        self.assertEqual(said, [])                              # no flare


class TestMainRoute(unittest.TestCase):
    """main.handle_command with a fake model."""
    @classmethod
    def setUpClass(cls):
        import main
        from common.user_waypoints import UserWaypoints
        cls.main = main
        main.george.shutdown()
        main.ROUTER.file = None
        main.ROUTER.phrases, main.ROUTER.words = {}, {}
        main.USER_WAYPOINTS = UserWaypoints(store_file=None)

    def setUp(self):
        from tests.huey_fakes import reset_state
        m = self.main
        reset_state()
        send_telemetry(DEFAULT_CONTACTS)
        state_store.update_telemetry({"ll": [42.0, 42.0]})
        wait_until(lambda: state_store.profile() == "apache", 2)
        self.george, self.overlord = [], []
        self._old = (m.speak_george_radio, m.speak_overlord, m.query_qwen)
        m.speak_george_radio = lambda text, **k: self.george.append(text)
        m.speak_overlord = lambda text, **k: self.overlord.append(text)
        m.ROUTER.phrases, m.ROUTER.words = {}, {}

    def tearDown(self):
        self.main.speak_george_radio, self.main.speak_overlord, self.main.query_qwen = self._old

    def model(self, res):
        seen = []

        def q(text, george_state="", model=None, extra_state=""):
            seen.append((text, extra_state))
            return res, state_store.snapshot()
        self.main.query_qwen = q
        return seen

    def test_george_routes_a_waypoint_mark(self):
        seen = self.model({"action": "ROUTE", "command": "Mark our current position as waypoint Bravo"})
        self.main.handle_command("put a marker where we are, call it bravo")
        self.assertIn("Outcasts: none", seen[0][1])
        self.assertIsNotNone(self.main.USER_WAYPOINTS.get("Bravo"))
        self.assertTrue(self.george and self.george[-1].startswith("Waypoint Bravo marked"))
        self.assertIn("put a marker where we are call it bravo", self.main.ROUTER.phrases)

    def test_overlord_say_again_goes_to_the_model_first(self):
        seen = self.model({"action": "ROUTE", "command": "Overlord, picture"})
        self.main.handle_command("Overlord, what's flying around out there")
        self.assertEqual(len(seen), 1)
        self.assertTrue(self.overlord and "say again" not in self.overlord[-1].lower(), self.overlord)

    def test_overlord_say_again_when_the_model_has_nothing(self):
        self.model({"action": "CONSENT_TO_FIRE", "speech": "Firing."})
        self.main.handle_command("Overlord, blah blah blah")
        self.assertEqual(len(self.overlord), 1)
        self.assertIn("say again", self.overlord[0].lower())
        self.assertEqual(self.george, [])                       # George didn't fire

    def test_route_refused_says_again(self):
        self.model({"action": "ROUTE", "command": "Outcast 3, RTB"})
        self.main.handle_command("see you well")
        self.assertEqual(self.george, ["Say again."])

    def test_affirm_with_nothing_pending(self):
        self.model({"action": "CONSENT_TO_FIRE", "speech": "Firing."})
        self.main.handle_command("Affirm.")
        self.assertEqual(self.george, ["Nothing to confirm."])

    def test_overlord_request_read_back_by_overlord(self):
        # 24 Oct: the model made up "over Alpha" and dropped "up north"; the direction is put back
        self.model({"action": "ROUTE", "command": "Overlord, requesting CAS over Alpha"})
        self.main.handle_command("Overlord, I need some fast movers on the armour up north")
        self.assertEqual(self.overlord[-1], "Overlord, confirm CAS north of you?")
        self.assertEqual(self.george, [])
        self.main.handle_command("negative")
        self.assertEqual(self.overlord[-1], "Overlord, copy, disregard.")
        self.assertEqual(self.george, [])

    def test_bare_answers(self):
        self.assertEqual(R.bare_answer("AFROM."), "yes")
        self.assertEqual(R.bare_answer("Negative."), "no")
        for not_bare in ("send it", "roger", "no", "yes", "cleared hot", "affirm, send it now"):
            self.assertIsNone(R.bare_answer(not_bare), not_bare)

    def test_whisper_hints(self):
        self.main.USER_WAYPOINTS.set("Alpha", {"lat": 42.0, "lon": 42.0})
        self.assertTrue(self.main.stt_prompt().endswith("waypoint Alpha."))
        self.main.USER_WAYPOINTS.delete("Alpha")




class TestOct24(RouterCase):
    """Cases from the 24 Oct flights."""
    def test_no_invented_places(self):
        o = "Overlord, and need some fast move-worth on the armour up north."
        self.assertFalse(R.plausible(o, "Overlord, requesting CAS over Alpha")[0])
        self.assertFalse(R.plausible("Outcast 2, go to the bridge", "Outcast 2, go to waypoint 3")[0])
        self.assertTrue(R.plausible("Outcast 2, head over to waypoint three", "Outcast 2, go to waypoint 3")[0])

    def test_direction_kept(self):
        o = "Overlord, I need some fast movers on the armour up north."
        self.assertEqual(R.repair(o, "Overlord, requesting CAS on my target"), "Overlord, requesting CAS north of me")
        o = "Overlord, I need some fast movers on the armour to the north-west"
        self.assertEqual(R.repair(o, "Overlord, requesting CAS on my target"), "Overlord, requesting CAS north west of me")
        self.assertEqual(R.repair("Overlord, fast movers on my target", "Overlord, requesting CAS on my target"),
                         "Overlord, requesting CAS on my target")

    def test_overlord_reads_back_in_his_voice(self):
        self.r.dispatch("Overlord, fast movers on the armour up north", "Overlord, requesting CAS on my target")
        self.assertEqual(self.said, [("Overlord, confirm CAS north of you?", "overlord")])
        self.assertTrue(self.r.answer_pending("AFROM."))          # Whisper's "affirm"
        self.assertEqual(self.shared.ran, ["Overlord, requesting CAS north of me"])

    def test_unclear_short_answer_asks_again(self):
        self.r.dispatch("Ocast 2, destroy your defence", "Outcast 2, engage air defences")
        self.assertTrue(self.r.answer_pending("Tool."))
        self.assertEqual(self.said[-1], ("Say again, affirm or negative?", "george"))
        self.assertTrue(self.r.answer_pending("A firm."))
        self.assertEqual(self.shared.ran, ["Outcast 2, engage air defences"])

    def test_answers(self):
        for yes in ("Affirm.", "AFROM.", "A FIRM.", "affirmative", "Afirm"):
            self.assertEqual(R.answer(yes), "yes", yes)
        for no in ("Negative.", "negativ", "no"):
            self.assertEqual(R.answer(no), "no", no)
        for other in ("fuel", "status", "Outcast 2, rejoin", "Tool retrain."):
            self.assertIsNone(R.answer(other), other)

    def test_not_understood_is_not_read_back(self):
        r = R.Router(self.shared, lambda t, w: self.said.append((t, w)), learned_file=None,
                     understands=lambda c: False)
        self.assertFalse(r.dispatch("Ocast 2, engage TUNKS, MARTWEST", "Outcast 2, engage TUNKS, MARTWEST"))
        self.assertEqual(self.said, [])

    def test_learned_words_across_punctuation(self):
        self.r.words = {"ok cas": "outcast"}
        self.assertEqual(self.r.apply_learned("OK, CAS 2, status"), "outcast 2, status")


class TestOverlordWakeWord(unittest.TestCase):
    def test_soundalikes(self):
        from common.overlord import addressed
        self.assertEqual(addressed("Overlaid, requesting CAS"), "requesting CAS")
        self.assertEqual(addressed("Over Lourd, picture"), "picture")
        self.assertIsNone(addressed("Overhead the target"))
        self.assertIsNone(addressed("Over the bridge, two"))
        self.assertIsNone(addressed("Outcast 2, rejoin"))


class TestOct31(RouterCase):
    """31 Oct: "george waypoint alpha" was rewritten with the prompt's example grid and learned."""
    BAD = ("george waypoint alpha", "Set grid 38T LM 7218 4018 as waypoint Alpha")

    def test_invented_grid_refused(self):
        self.assertEqual(R.plausible(*self.BAD), (False, "38t not said"))
        self.assertFalse(R.plausible("overlord cas east of me", "Overlord, requesting CAS 10 km east of me")[0])

    def test_spoken_numbers_accepted(self):
        self.assertTrue(R.plausible("set grid three eight tango lima mike seven two one eight four zero one eight "
                                    "as waypoint charlie", "Set grid 38T LM 7218 4018 as waypoint Charlie")[0])
        self.assertTrue(R.plausible("set grid 38T LM 72184018 as alpha", "Set grid 38T LM 7218 4018 as waypoint Alpha")[0])
        self.assertTrue(R.plausible("overlord cas ten clicks east of me", "Overlord, requesting CAS 10 km east of me")[0])
        self.assertTrue(R.plausible("overlord cas twenty five km north", "Overlord, requesting CAS 25 km north of me")[0])
        self.assertTrue(R.plausible("outcast two attack target three", "Outcast 2, attack target 3")[0])

    def test_rejected_and_not_learned(self):
        self.assertFalse(self.r.dispatch(*self.BAD))
        self.assertEqual(self.shared.ran, [])
        self.assertNotIn(self.BAD[0], self.r.phrases)

    def test_bad_saved_phrase_dropped_on_load(self):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"phrases": {self.BAD[0]: self.BAD[1], "return to rejoin": "Outcast 2, rejoin"}, "words": {}}, f)
        r = R.Router(self.shared, lambda *a: None, learned_file=self.file)
        self.assertEqual(r.apply_learned("George, waypoint Alpha"), "George, waypoint Alpha")
        self.assertEqual(r.apply_learned("return to rejoin"), "Outcast 2, rejoin")

    def test_prompt_has_no_example_grid(self):
        self.assertNotIn("7218", R.ROUTE_PROMPT)


if __name__ == "__main__":
    unittest.main()
