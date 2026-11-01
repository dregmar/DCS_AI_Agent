"""Phase 2, P2-4 / P2-5 / P2-6: Darkside (JTAC calls, CAS terminal control), Warlord (the rules,
denial and override), and Overlord's CAS under Darkside with the permission table enforced. A fake
Olympus and a fake DCS (tests/test_overlord.py) stand in; nothing leaves this process."""
import time
import types
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from command import rules
from command.permissions import Guard
from command.roles import DARKSIDE, OVERLORD, Said
from command.tasking import TaskRegistry
from tactical import link

ME = (42.0, 42.0)
KM = 1000 / 111_320.0


def north(km, east_km=0.0):
    return (ME[0] + km * KM, ME[1] + east_km * KM / 0.743)


def quiet_tasks():
    return TaskRegistry(printer=lambda s: None)


class FakeDebrief:
    def __init__(self):
        self.overrides = []

    def override(self, what):
        self.overrides.append(what)


BLOCK = rules.parse_block({"warlord": {
    "objectives": ["Destroy the SA-11 battery", "Hold the bridge"], "roe": "Weapons free north of the river.",
    "no_strike": [{"name": "Gudauta town", "lat": north(20)[0], "lon": ME[1], "radius_m": 2000}],
    "priority_areas": [{"name": "Objective Bravo", "lat": north(-8)[0], "lon": ME[1], "radius_m": 4000}]}})


class Rules(unittest.TestCase):
    def test_block(self):
        self.assertEqual(BLOCK["objectives"][0], "Destroy the SA-11 battery")
        self.assertEqual(BLOCK["no_strike"][0]["name"], "Gudauta town")
        bad = rules.parse_block({"warlord": {"no_strike": [{"name": "x"}, "junk", {"lat": 1, "lon": 2}],
                                             "objectives": "not a list"}})
        self.assertEqual(len(bad["no_strike"]), 1)                         # the one with a position
        self.assertEqual(bad["objectives"], [])
        self.assertEqual(rules.parse_block(None)["priority_areas"], [])
        self.assertEqual(rules.load_block("/nonexistent/x.json")["roe"], "")

    def test_friendlies(self):
        snap = {"friendly_units": [("M-1 Abrams", 6, *north(5.5), 100.0, "g"), ("F-16C_50", 7, *north(5), 3000, "a"),
                                   ("LAV-25", 8, *north(9), 100.0, "g")]}
        near = rules.friendlies_near(snap, *north(5), 1000)
        self.assertEqual([f["type"] for f in near], ["M-1 Abrams"])         # not aircraft, not 4 km away
        self.assertIsNone(rules.friendlies_near({}, *north(5)))             # older Export.lua: can't tell


def warlord(tasks=None, block=BLOCK, picture=True, **kw):
    from common.warlord import Warlord
    return Warlord(lambda: {}, callsign=lambda: "Enfield 1-1", block=block, tasks=tasks or quiet_tasks(),
                   picture_active=lambda: picture, debrief=FakeDebrief(), **kw)


SA11 = [("Buk 9S18 sr", 501, *north(-4), 300.0)]


class WarlordRules(unittest.TestCase):
    def tgt(self, km, typ="T-72B", tid=701):
        lat, lon = north(km)
        return {"type": typ, "id": tid, "lat": lat, "lon": lon}

    def test_order(self):
        w = warlord()
        t = self.tgt(-8)
        self.assertEqual(warlord(picture=False).review("CAS", north(-8), [t], {}).verdict, "defer")
        d = w.review("CAS", north(20), [self.tgt(20)], {})
        self.assertEqual((d.verdict, d.reason), ("deny", "the target is inside the Gudauta town no-strike zone"))
        d = w.review("CAS", north(-8), [t], {"air_defence": SA11})
        self.assertEqual(d.verdict, "deny")
        self.assertEqual(d.reason, "SA-11 covers the target and no SEAD is up")
        self.assertEqual(d.risk, "Hog goes in under an SA-11")
        snap = {"friendly_units": [("M-1 Abrams", 6, *north(-7.5), 100.0, "g")]}
        d = w.review("CAS", north(-8), [t], snap)
        self.assertEqual(d.verdict, "deny")
        self.assertIn("friendly M-1 Abrams", d.reason)
        self.assertEqual(w.review("CAS", north(-8), [t], {}).verdict, "approve")
        d = w.review("CAP", north(30), [], {})
        self.assertEqual(d.verdict, "approve")
        self.assertIn("outside the priority areas. Objective Bravo come first", d.advice)

    def test_sead_up_lets_cas_under_a_sam(self):
        tasks = quiet_tasks()
        w = warlord(tasks)
        s = tasks.new("SEAD", OVERLORD, "Weasel flight")
        tasks.set_state(s.task_id, "approved")
        self.assertEqual(w.review("CAS", north(-8), [self.tgt(-8)], {"air_defence": SA11}).verdict, "approve")

    def test_too_many_flights(self):
        tasks = quiet_tasks()
        for _ in range(2):
            t = tasks.new("CAP", OVERLORD, "Eagle flight")
            tasks.set_state(t.task_id, "approved")
        d = warlord(tasks, max_flights=2).review("CAS", north(-8), [self.tgt(-8)], {})
        self.assertEqual((d.verdict, d.reason), ("defer", "2 flights already up"))

    def test_words_hold_and_override(self):
        tasks = quiet_tasks()
        clock = [100.0]
        w = warlord(tasks, clock=lambda: clock[0])
        d = w.review("CAS", north(-8), [self.tgt(-8)], {"air_defence": SA11})
        self.assertEqual(w.deny_text("CAS", d),
                         "Enfield 1-1, Warlord, CAS denied. SA-11 covers the target and no SEAD is up. "
                         "Recommend SEAD first. Say Warlord, override, to go anyway.")
        t = tasks.new("CAS", OVERLORD, "Hog flight")
        tasks.set_state(t.task_id, "denied", d.reason)
        launched = []
        w.hold(t.task_id, "CAS", d, lambda: launched.append(1) or "Overlord copies. Hog flight tasked.")
        said = w.handle("override")
        self.assertEqual(said, "Enfield 1-1, Warlord, override noted. Hog goes in under an SA-11. Logged.")
        self.assertEqual(said.then, (("Overlord copies. Hog flight tasked.", "overlord"),))
        self.assertEqual(launched, [1])
        self.assertEqual(tasks.get(t.task_id).state, "overridden")
        self.assertEqual(w.debrief.overrides, ["CAS despite: SA-11 covers the target and no SEAD is up"])
        self.assertIn("nothing to override", w.override())                # used up
        w.hold(t.task_id, "CAS", d, lambda: "x")
        clock[0] += 61.0
        self.assertIn("nothing to override", w.override())                # too late

    def test_questions(self):
        w = warlord()
        self.assertEqual(w.handle("say objectives"),
                         "Enfield 1-1, Warlord, objectives: Destroy the SA-11 battery; Hold the bridge.")
        self.assertEqual(w.handle("what's the ROE"), "Enfield 1-1, Warlord, Weapons free north of the river.")
        self.assertIn("Gudauta town", w.handle("say no-strike zones"))
        self.assertIn("Objective Bravo", w.handle("say priorities"))
        self.assertIsNone(w.handle("sing a song"))
        empty = warlord(block=rules.parse_block({}))
        self.assertIn("no objectives written", empty.handle("objectives"))


class FakeJtac:
    def __init__(self):
        self.current, self.calls = None, []

    def wanted(self, t):
        return "target" in t.lower() or "bda" in t.lower()

    def handle(self, t):
        from command.permissions import current
        self.calls.append(current()[0])
        return "Overlord, target Shilka. Grid 38 T KM 12345 67890."


class DarksideWords(unittest.TestCase):
    def setUp(self):
        from common.darkside import Darkside
        self.spoken = []
        self.d = Darkside(FakeJtac(), lambda: {}, self.spoken.append, callsign=lambda: "Enfield 1-1")

    def test_jtac_in_his_words_and_as_darkside(self):
        self.assertEqual(self.d.handle("request target"),
                         "Enfield 1-1, Darkside, target Shilka. Grid 38 T KM 12345 67890.")
        self.assertEqual(self.d.jtac.calls, [DARKSIDE])                    # marking goes out as him
        self.assertEqual(self.d.words("Overlord, red smoke on the target."),
                         "Enfield 1-1, Darkside, red smoke on the target.")
        self.assertEqual(self.d.words("Overlord, BDA, T-72B destroyed."), "Enfield 1-1, Darkside, BDA, T-72B destroyed.")
        self.assertEqual(self.d.words("Overlord copies. Good effect."), "Enfield 1-1, Darkside, good effect.")
        self.d.jtac_speak("Overlord, good effect, T-72B destroyed.")
        self.assertEqual(self.spoken, ["Enfield 1-1, Darkside, good effect, T-72B destroyed."])
        self.assertIsNone(self.d.handle("sing a song"))

    def test_abort_with_nothing(self):
        self.assertIn("no flight under my control", self.d.handle("abort"))


class RolesWorld(unittest.TestCase):
    """Overlord's real CAS mission, Warlord reviewing, Darkside clearing, the guard ENFORCING."""
    def setUp(self):
        from common import overlord as ov
        from common.darkside import Darkside
        from tests.test_overlord import FakeOly, FakeWorld
        self.ov = ov
        self._p = [mock.patch.object(ov, "POLL_S", 0.05), mock.patch.object(ov, "APPEAR_TIMEOUT_S", 2.0),
                   mock.patch.object(ov, "IP_TIMEOUT_S", 0.3)]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])
        was = link._get()
        link.set_provider(None)                                           # DCS's lists decide alive / dead
        self.addCleanup(link.set_provider, was)
        self.world = FakeWorld([])
        self.world.gnd = [("T-72B", 701, *north(-8), 300.0), ("BMP-2", 702, *north(-8.2), 300.0)]
        self.tasks = quiet_tasks()
        self.guard = Guard(self.tasks, enforce=True, printer=lambda s: None)
        self.oly = FakeOly(self.world)
        inner = self.oly.command

        def guarded(n, p):
            ok, why = self.guard(n, p)
            if not ok:
                return False, why
            from command.permissions import current
            self.sent.append((current()[0], n, p.get("ROE")))
            return inner(n, p)
        self.sent = []
        self.oly.command = guarded
        self.spoken, self.log = [], []
        self.warlord = warlord(self.tasks)
        self.d = Darkside(FakeJtac(), self.world.snap, self.spoken.append, callsign=lambda: "Enfield 1-1",
                          oly=self.oly, warlord=self.warlord, tasks=self.tasks,
                          log=lambda who, text: self.log.append((who, text)))
        self.o = ov.Overlord(self.oly, self.world.snap, self.spoken.append, tasks=self.tasks,
                             warlord=self.warlord, darkside=self.d)

    def wait(self, pred, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def state(self):
        t = self.tasks.get("K1")
        return t.state if t else None



class OverlordUnderRoles(RolesWorld):
    def test_cas_cleared_by_darkside_only(self):
        said = self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertIn("Weapons hold until Darkside clears them", said.then[1][0])   # full form (P2-7)
        self.assertTrue(self.wait(lambda: self.state() == "complete"), self.tasks.get("K1"))
        self.assertEqual(self.guard.count, 0, self.guard.recent())       # enforced: nothing refused
        roe = [(who, r) for who, n, r in self.sent if n == "setROE"]
        self.assertEqual(roe[0], (OVERLORD, 4))                           # arrives weapons hold
        self.assertIn((DARKSIDE, 0), roe)                                 # weapons free only from Darkside
        self.assertTrue(all(who == DARKSIDE for who, n, _ in self.sent if n == "attackUnit"))
        states = [h[0] for h in self.tasks.get("K1").history]
        self.assertEqual(states[:5], ["requested", "approved", "en_route", "holding", "cleared"])
        self.assertIn(("ai", "Hog 1: Darkside, Hog 1, two A-10s at the IP, weapons hold."), self.log)
        self.assertTrue(any(t == "Darkside: Hog 1, cleared hot, T-72B." for _, t in self.log), self.log)
        self.assertIn("Enfield 1-1, Darkside, Hog is cleared hot, T-72B, 8 kilometres south of you.", self.spoken)
        self.assertTrue(any("good effect, T-72B destroyed." in s for s in self.spoken), self.spoken)
        self.assertIn("Enfield 1-1, Darkside, area clear. Hog returning to base.", self.spoken)

    def test_danger_close_holds_at_the_ip(self):
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertTrue(self.wait(lambda: self.state() == "en_route"))
        with self.world.lock:                                             # friendlies move in after tasking
            self.world.friendly = [("M-1 Abrams", 6, *north(-7.6), 100.0, "g")]
        orig = self.world.snap
        self.world.snap = lambda: dict(orig(), friendly_units=list(self.world.friendly))
        self.d.snapshot = self.world.snap
        self.o.snapshot = self.world.snap
        self.assertTrue(self.wait(lambda: any("holding Hog at the IP, friendly M-1 Abrams" in s for s in self.spoken)),
                        self.spoken)
        self.assertEqual(self.state(), "holding")
        self.assertFalse(any(n == "attackUnit" for _, n, _ in self.sent))
        self.world.friendly = []                                          # they move off: cleared
        self.assertTrue(self.wait(lambda: self.state() == "complete"))

    def test_abort_and_continue(self):
        self.world.kill_on_attack = False
        self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertTrue(self.wait(lambda: self.state() == "attacking"))
        said = self.d.handle("abort")
        self.assertTrue(said.startswith("Enfield 1-1, Darkside, abort, abort, abort. Hog weapons hold"), said)
        self.assertEqual(self.state(), "holding")
        self.assertEqual(self.sent[-1][:3], (DARKSIDE, "setROE", 4))
        n_attacks = sum(1 for _, n, _ in self.sent if n == "attackUnit")
        time.sleep(0.5)
        self.assertEqual(sum(1 for _, n, _ in self.sent if n == "attackUnit"), n_attacks)   # stays held
        self.assertIn("continuing", self.d.handle("continue"))
        self.assertTrue(self.wait(lambda: sum(1 for _, n, _ in self.sent if n == "attackUnit") > n_attacks))
        self.o.handle("Overlord, cancel CAS")
        self.assertEqual(self.state(), "aborted")
        self.assertEqual(self.guard.count, 0, self.guard.recent())

    def test_warlord_denies_and_override_launches(self):
        self.world.ad = list(SA11)
        said = self.o.handle("Overlord, request CAS 8 km south of me")
        self.assertIsInstance(said, Said)
        self.assertEqual(said, "Nomad, Overlord, copy CAS 8 kilometres south of you. Checking with Warlord.")
        self.assertIn("CAS denied. SA-11 covers the target", said.then[0][0])
        self.assertEqual(said.then[0][1], "warlord")
        self.assertFalse(any(n == "spawnAircrafts" for _, n, _ in self.sent))
        self.assertEqual(self.state(), "denied")
        said = self.warlord.handle("override")
        self.assertIn("override noted. Hog goes in under an SA-11", said)
        self.assertTrue(said.then[0][0].startswith("Nomad, Overlord, Hog flight"), said.then)
        self.assertTrue(any(n == "spawnAircrafts" for _, n, _ in self.sent))
        self.assertTrue(self.wait(lambda: self.state() in ("holding", "cleared", "attacking", "complete")))
        self.assertEqual([h[0] for h in self.tasks.get("K1").history][:4],
                         ["requested", "denied", "overridden", "en_route"])

    def test_warlord_advice_after_overlord(self):
        self.world.gnd = [("T-72B", 701, *north(8), 300.0)]                # north: outside Objective Bravo
        said = self.o.handle("Overlord, request CAS 8 km north of me")
        self.assertIn("Checking with Warlord", said)
        self.assertEqual([who for _, who in said.then], ["warlord", "overlord", "warlord"])
        self.assertIn("outside the priority areas", said.then[2][0])
        self.o.handle("Overlord, cancel CAS")

    def test_sead_still_overlords(self):
        self.world.ad = [("Kub 1S91 str", 501, *north(10), 300.0)]
        self.o.handle("Overlord, request SEAD 10 km north of me")
        self.assertTrue(self.wait(lambda: self.state() == "complete"))
        self.assertEqual(self.guard.count, 0, self.guard.recent())
        self.assertTrue(all(who == OVERLORD for who, n, _ in self.sent if n == "attackUnit"))


class Main(unittest.TestCase):
    def test_wiring(self):
        import main
        self.assertIsNotNone(main.WARLORD)
        self.assertIsNotNone(main.DARKSIDE)
        self.assertEqual(main.JTAC.speak, main.DARKSIDE.jtac_speak)
        if main.OVERLORD is not None:
            self.assertIs(main.OVERLORD.darkside, main.DARKSIDE)
            self.assertIs(main.OVERLORD.warlord, main.WARLORD)

    def test_warlord_without_the_tactical_picture(self):
        import main
        with mock.patch.object(main, "TACTICAL", None):
            self.assertTrue(main.WARLORD.picture_active())                # judged without it, not deferred
        with mock.patch.object(main, "TACTICAL", object()), mock.patch.object(link, "active", lambda: False):
            self.assertFalse(main.WARLORD.picture_active())

    def test_speak_as_says_every_voice_in_order(self):
        import main
        got = []
        with mock.patch.object(main, "_speak_one", lambda said, who: got.append((who, str(said)))):
            main._speak_as(Said("Darkside line.", pre=[("Overlord, contact Darkside.", "overlord")],
                                then=[(Said("Warlord line.", then=[("Overlord line.", "overlord")]), "warlord")]),
                           "darkside")
        self.assertEqual(got, [("overlord", "Overlord, contact Darkside."), ("darkside", "Darkside line."),
                               ("warlord", "Warlord line."), ("overlord", "Overlord line.")])

    def test_role_replies(self):
        import main
        with mock.patch.object(main.state_store, "snapshot", lambda: {}):
            said, who, _ = main.role_reply("Warlord, say objectives")
            self.assertEqual(who, "warlord")
            self.assertIn("Warlord, ", said)
            said, who, _ = main.role_reply("Darkside, abort")
            self.assertEqual(who, "darkside")
            self.assertIn("no flight under my control", said)

    def test_debrief_counts_overrides(self):
        from common.debrief import Debrief
        d = Debrief(log_dir="/nonexistent")
        d.override("CAS despite: SA-11 covers the target")
        self.assertIn("1 Warlord override: CAS despite: SA-11 covers the target.", d.summary())


if __name__ == "__main__":
    unittest.main()
