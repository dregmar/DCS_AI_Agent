"""Audit step 7 (approved 30 Oct): the training-mode gate. The Game Master client can only spawn
enemy units near you while training is on, alone in the mission, within the limits, and can only touch
the units it spawned. Uses the real Olympus client with a fake HTTP layer, so the allow-list and the
guard run exactly as on the PC. No real login, no DCS."""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import fakes  # noqa: F401
from common import olympus, training
from common.training import Training, TRAINING_COMMANDS

ME = (42.0, 42.0)
SECRET = "gm-secret-pw-123"


class FakeHTTP:
    """Olympus as far as the client needs: every GET / PUT answers 200; PUTs are recorded."""
    def __init__(self):
        self.sent = []

    def __call__(self, req, timeout):
        if req.get_method() == "PUT":
            self.sent.append(json.loads(req.data))
        http = self

        class R:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


def north(km):
    return (ME[0] + km * 1000 / 111_320.0, ME[1])


def spawn(n=2, km=5, coalition="red"):
    lat, lon = north(km)
    return {"coalition": coalition, "units": [{"unitType": "T-72B", "location": {"lat": lat, "lng": lon}}
                                              for _ in range(n)]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.snap = {"coalition": 2, "my_ll": ME, "self_id": 100, "olympus_enemy": [], "enemy_lists_age": 0.5}
        self.http = FakeHTTP()
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "logs", "training_groups.json")
        self.mission = "Op Training Day"
        self.printed = []
        self.t = self.make()

    def tearDown(self):
        self.dir.cleanup()

    def make(self, enabled=False, voice_switch=True, client=True, path="default", templates="sizes",
             olympus_enabled=lambda: True):
        def make_client(guard):
            if not client:
                return None
            return olympus.Olympus("http://localhost:4512", "George", SECRET, opener=self.http,
                                   allowed=TRAINING_COMMANDS, guard=guard)
        return Training(lambda: self.snap, make_client, mission_name=lambda: self.mission, enabled=enabled,
                        voice_switch=voice_switch, max_groups=6, max_units=24, max_range_km=50,
                        path=self.path if path == "default" else path, printer=self.printed.append,
                        templates={"SA-11 SAM Battery": 12, "Patriot site": 20} if templates == "sizes" else templates,
                        olympus_enabled=olympus_enabled)

    def sent(self):
        return [next(iter(c)) for c in self.http.sent]

    def alone(self, t=None):
        (t or self.t).note_players("PLAYERS 1 sp")

    def track(self, t=None, group="Olympus-7", ids=(701, 702)):
        t = t or self.t
        t.add_group(group, [(f"{group}-{i}", uid) for i, uid in enumerate(ids, 1)], "two T-72s")
        self.snap["olympus_enemy"] += [(group, f"{group}-{i}", uid, *north(5), 300) for i, uid in enumerate(ids, 1)]


class TestFence(_Base):
    def test_everything_refused_while_off(self):
        self.alone()
        c = self.t.client()
        self.assertEqual(c.command("spawnGroundUnits", spawn()), (False, "training mode is off"))
        self.assertEqual(self.http.sent, [])

    def test_allow_list(self):
        """The GM client never sends anything but the six training commands; Blue's list is unchanged."""
        self.t.on = True
        self.alone()
        c = self.t.client()
        for name in ("attackUnit", "spawnNavyUnits", "explosion", "cloneUnits", "setCommandModeOptions"):
            self.assertFalse(c.command(name, {"ID": 1})[0], name)
        self.assertEqual(self.http.sent, [])
        blue = olympus.Olympus("http://localhost:4512", "George", "x", opener=self.http)
        self.assertEqual(blue.allowed, frozenset(olympus.ALLOWED))
        self.assertFalse(blue.allowed & {"spawnGroundUnits", "spawnHelicopters"})
        self.assertIsNone(blue.guard)

    def test_players(self):
        self.t.on = True
        c = self.t.client()
        self.assertIn("can't tell who else", c.command("spawnGroundUnits", spawn())[1])   # no PLAYERS yet
        self.t.note_players("PLAYERS 3 mp")
        self.assertEqual(c.command("spawnGroundUnits", spawn())[1], "other players are in the mission")
        self.t.note_players("PLAYERS 1 mp")                  # hosting alone
        self.assertTrue(c.command("spawnGroundUnits", spawn())[0])
        with mock.patch.object(self.t, "clock", lambda: time.monotonic() + 60):
            self.assertIn("can't tell", c.command("spawnGroundUnits", spawn())[1])      # stale

    def test_coalition_and_range(self):
        self.t.on = True
        self.alone()
        c = self.t.client()
        self.assertIn("must be red", c.command("spawnGroundUnits", spawn(coalition="blue"))[1])
        self.assertIn("must be red", c.command("spawnGroundUnits", spawn(coalition=""))[1])
        self.assertIn("more than 50 km", c.command("spawnGroundUnits", spawn(km=60))[1])
        self.assertEqual(c.command("spawnGroundUnits", {"coalition": "red", "units": []})[1], "no spawn location")
        bad = spawn()
        bad["units"][0]["location"] = {"lat": "x", "lng": 1}
        self.assertIn("more than 50 km", c.command("spawnGroundUnits", bad)[1])          # unreadable: refused
        self.assertTrue(c.command("spawnHelicopters", spawn(km=49))[0])
        self.snap["coalition"] = 1                          # you're red: targets are blue
        self.assertTrue(c.command("spawnGroundUnits", spawn(coalition="blue"))[0])
        self.snap["coalition"] = 0
        self.assertIn("coalition isn't known", c.command("spawnGroundUnits", spawn())[1])

    def test_limits(self):
        self.t.on = True
        self.alone()
        c = self.t.client()
        for g in range(5):
            self.track(group=f"Olympus-{g}", ids=(100 * g + 1, 100 * g + 2))
        self.assertTrue(c.command("spawnGroundUnits", spawn(n=4))[0])                  # 6th group, 14 units
        self.track(group="Olympus-9", ids=(901,))
        self.assertIn("limit is 6", c.command("spawnGroundUnits", spawn(n=1))[1])
        self.t.forget()
        for g in range(2):
            self.track(group=f"Olympus-{g}", ids=tuple(range(100 * g + 1, 100 * g + 11)))   # 20 units
        self.assertIn("limit of 24", c.command("spawnGroundUnits", spawn(n=5))[1])
        self.assertTrue(c.command("spawnGroundUnits", spawn(n=4))[0])

    def test_only_tracked_units(self):
        """Delete / ROE / path: never a mission unit, you, an Overlord flight or an Outcast."""
        c = self.t.client()
        self.track()
        for uid in (5, 100, 1001, 2001):
            for name in ("deleteUnit", "setROE", "setPath"):
                self.assertIn("isn't a training target", c.command(name, {"ID": uid})[1])
        self.assertEqual(c.command("deleteUnit", {"ID": "x"})[1], "no unit id")
        self.assertTrue(c.command("setROE", {"ID": 701, "ROE": 4})[0])        # works with training off too
        self.assertEqual(self.sent(), ["setROE"])

    def test_no_explosions(self):
        """Olympus's delete detonates 250 kg on the unit when explosion is set (and always on a player)."""
        c = self.t.client()
        self.track()
        self.assertEqual(c.command("deleteUnit", {"ID": 701, "explosion": True, "explosionType": "normal"}),
                         (False, "no explosions"))
        self.assertTrue(c.command("deleteUnit", {"ID": 701, "explosion": False, "explosionType": ""})[0])

    def test_site_templates_count_their_units(self):
        self.t.on = True
        self.alone()
        c = self.t.client()
        lat, lon = north(5)
        site = lambda kind: {"coalition": "red", "units": [{"unitType": kind, "location": {"lat": lat, "lng": lon}}]}
        self.assertEqual(self.t.units_in(site("Patriot site"), ground=True), 20)
        self.assertTrue(c.command("spawnGroundUnits", site("SA-11 SAM Battery"))[0])        # 12 of 24
        self.track(group="Olympus-1", ids=tuple(range(1, 13)))
        self.assertIn("limit of 24", c.command("spawnGroundUnits", site("Patriot site"))[1])   # 12 + 20
        self.assertEqual(self.t.units_in(site("T-72B"), ground=True), 1)

    def test_templates_unreadable_refuses_ground_only(self):
        t = self.make(templates=None)
        t.on = True
        self.alone(t)
        c = t.client()
        self.assertIn("site templates", c.command("spawnGroundUnits", spawn())[1])
        self.assertTrue(c.command("spawnHelicopters", spawn())[0])

    def test_template_file_parsed(self):
        """Olympus's templates.lua as on the PC (CRLF, keys in any order, an end marker per template)."""
        lua = ('templates = \r\n{\r\n    ["SA-11 SAM Battery"] = \r\n    {\r\n        ["country"] = 0,\r\n'
               '        ["type"] = "vehicle",\r\n        ["units"] = \r\n        {\r\n'
               '            [1] = \r\n            {\r\n                ["dx"] = 0,\r\n            }, -- end of [1]\r\n'
               '            [2] = \r\n            {\r\n                ["dx"] = 5,\r\n            }, -- end of [2]\r\n'
               '        }, -- end of ["units"]\r\n    }, -- end of ["SA-11 SAM Battery"]\r\n'
               '    ["Hawk SAM Battery"] = \r\n    {\r\n        ["units"] = \r\n        {\r\n'
               '            [1] = \r\n            {\r\n                ["dx"] = 1,\r\n            }, -- end of [1]\r\n'
               '        }, -- end of ["units"]\r\n    }, -- end of ["Hawk SAM Battery"]\r\n}\r\n')
        path = os.path.join(self.dir.name, "templates.lua")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(lua)
        self.assertEqual(training.template_sizes(path), {"SA-11 SAM Battery": 2, "Hawk SAM Battery": 1})
        self.assertIsNone(training.template_sizes(os.path.join(self.dir.name, "missing.lua")))

    def test_broken_guard_refuses(self):
        c = olympus.Olympus("http://localhost:4512", "George", SECRET, opener=self.http,
                            allowed=TRAINING_COMMANDS, guard=lambda n, p: 1 / 0)
        self.assertEqual(c.command("deleteUnit", {"ID": 1}), (False, "guard error ZeroDivisionError"))
        self.assertEqual(self.http.sent, [])


class TestVoiceAndClearing(_Base):
    def test_on_off_clears_first(self):
        self.alone()
        self.assertEqual(self.t.handle("training mode on"), "Instructor, training mode on. Training targets allowed.")
        self.track()
        self.track(group="Olympus-8", ids=(801,))
        self.assertEqual(self.t.handle("training status"), "Instructor, training mode on, 2 groups, 3 units alive.")
        self.assertEqual(self.t.handle("training mode off"), "Instructor, 2 training groups removed. Training mode off.")
        self.assertEqual(sorted(c["deleteUnit"]["ID"] for c in self.http.sent), [701, 702, 801])
        self.assertFalse(self.t.on)
        self.assertEqual(self.t.handle("training status"), "Instructor, training mode off.")

    def test_clear(self):
        self.assertEqual(self.t.handle("clear training targets"), "Instructor, no training targets.")
        self.track()
        self.assertEqual(self.t.handle("clear the training targets"), "Instructor, 1 training group removed.")
        self.assertFalse(self.t.has_groups())

    def test_refusals(self):
        self.alone()
        """1 Nov (one training mode): training turns on anyway (the instructor's exercises work) and
        says why there are no training targets; only the config switch refuses."""
        self.alone()
        t = self.make(client=False)
        self.assertEqual(t.handle("training mode on"),
                         "Instructor, training mode on. Training targets unavailable, no Game Master login in config.")
        self.assertTrue(t.on)
        t = self.make(voice_switch=False)
        self.assertEqual(t.handle("training mode on"), "Instructor, unable, training mode is switched off in config.")
        self.assertFalse(t.on)
        t = self.make()
        t.note_players("PLAYERS 2 mp")
        self.assertEqual(t.handle("training mode on"),
                         "Instructor, training mode on. Training targets unavailable, other players are in the mission.")
        self.assertTrue(t.on)
        self.assertEqual(t.spawn_refusal({"coalition": "red"}), "other players are in the mission")   # gate unchanged
        t = self.make(olympus_enabled=lambda: False)
        self.assertIn("Olympus is turned off in config", t.handle("training mode on"))

    def test_flown_phrases_to_end_and_start(self):
        """30 Oct flight: none of these ended training; "cancel / abort training" went to Overlord's cancel."""
        self.alone()
        for said in ("exit training mode", "exit training", "end the training", "cancel training",
                     "abort training", "stop training", "training mode off"):
            self.t.on = True
            self.assertTrue(self.t.wanted(said), said)
            self.assertIn("training mode off", self.t.handle(said), said)
            self.assertFalse(self.t.on, said)
        for said in ("start training mode", "turn on training", "training mode on"):
            self.t.on = False
            self.assertTrue(self.t.wanted(said), said)
            self.t.handle(said)
            self.assertTrue(self.t.on, said)
        self.assertFalse(self.t.wanted("how do I end the training?"))      # a question: the briefing
        self.assertTrue(self.t.wanted("remove training groups"))

    def test_not_training_phrases(self):
        for rest in ("requesting SEAD", "status", "cancel all", "picture"):
            self.assertFalse(self.t.wanted(rest), rest)

    def test_dead_groups_pruned_only_with_fresh_lists(self):
        self.track()
        self.snap["olympus_enemy"] = []
        self.snap["enemy_lists_age"] = 12.0               # stale: can't tell
        self.assertEqual(self.t.counts(), (1, 2))
        self.snap["enemy_lists_age"] = 0.5
        self.assertEqual(self.t.counts(), (0, 0))
        self.assertIn("destroyed", self.printed[-1])

    def test_new_mission_forgets(self):
        self.track()
        self.t.forget()
        self.assertFalse(self.t.has_groups())
        self.assertEqual(self.http.sent, [])                # nothing deleted: they went with the mission

    def test_shutdown_clears(self):
        self.track()
        self.t.shutdown()
        self.assertEqual(self.sent(), ["deleteUnit", "deleteUnit"])
        self.t.shutdown()                                   # atexit after the normal exit: nothing more
        self.assertEqual(len(self.http.sent), 2)


class TestCrashRecovery(_Base):
    def saved(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_saved_and_removed_at_next_start(self):
        self.track()
        self.assertEqual(self.saved()["mission"], "Op Training Day")
        self.assertEqual(len(self.saved()["groups"]), 1)
        # "crash": a new program on the same mission; one unit gone, one id reused by a different unit
        self.snap["olympus_enemy"] = [("Olympus-7", "Olympus-7-1", 701, *north(5), 300),
                                      ("Olympus-3", "Olympus-3-1", 702, *north(5), 300)]
        t2 = self.make()
        self.mission = ""
        self.assertIsNone(t2.recover())                     # mission not known yet: waits
        self.mission = "Op Training Day"
        self.assertEqual(t2.recover(), 1)
        self.assertEqual([c["deleteUnit"]["ID"] for c in self.http.sent], [701])   # not 702: other group
        self.assertEqual(self.saved()["groups"], [])
        self.assertIsNone(t2.recover())                     # done once

    def test_other_mission_nothing_removed(self):
        self.track()
        self.mission = "Some Other Mission"
        t2 = self.make()
        self.assertEqual(t2.recover(), 0)
        self.assertEqual(self.http.sent, [])
        self.assertEqual(self.saved()["groups"], [])

    def test_waits_for_fresh_lists(self):
        self.track()
        t2 = self.make()
        self.snap["enemy_lists_age"] = None                 # old Export.lua: can't check
        self.assertIsNone(t2.recover())
        self.snap["enemy_lists_age"] = 0.5
        self.assertEqual(t2.recover(), 2)

    def test_no_file_when_path_off(self):
        t = self.make(path=None)
        t.add_group("Olympus-1", [("Olympus-1-1", 1)], "x")
        self.assertFalse(os.path.exists(self.path))


class TestNoSecret(_Base):
    def test_password_never_shown(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(training.config, "OLYMPUS_GM_PASSWORD", SECRET):
            self.alone()
            self.t.handle("training mode on")
            c = self.t.client()
            c.command("spawnGroundUnits", spawn(coalition="blue"))
            c.command("deleteUnit", {"ID": 5})
            self.track()
            self.t.handle("training mode off")
            shown = repr(self.t.link()) + repr(c.status()) + "".join(self.printed) + out.getvalue()
            with open(self.path, encoding="utf-8") as f:
                shown += f.read()
        import hashlib
        self.assertNotIn(SECRET, shown)
        self.assertNotIn(hashlib.sha256(SECRET.encode()).hexdigest(), shown)


class TestMainWiring(unittest.TestCase):
    def test_overlord_training_phrases(self):
        main = fakes.pipeline_module().main
        said, who, _ = main.shared_reply("Instructor, training status")
        self.assertEqual((said, who), ("Instructor, training mode off.", "instructor"))
        try:
            said, who, _ = main.shared_reply("Instructor, training mode on")
            self.assertEqual(who, "instructor")
            self.assertTrue(main.TRAINING.on)                  # on, without training targets
            self.assertIn("Training targets unavailable", said)
            self.assertIn("no Game Master login", main.TRAINING.link()["detail"])
        finally:
            main.TRAINING.set_mode(False)
        self.assertFalse(main.TRAINING.on)
        self.assertIsNone(main.TRAINING.path)

    def test_players_message_reaches_the_gate(self):
        from common import comms_menu
        main = fakes.pipeline_module().main
        self.assertEqual(comms_menu.ON_PLAYERS, main.TRAINING.note_players)


if __name__ == "__main__":
    unittest.main()
