"""S1 of the kneeboard Settings page (approved 6 Nov 2026): core/settings.py. A value saved in
settings_local.json wins over config.py; resetting it brings config.py's back; only the 37 listed settings
can be saved, each checked; live settings reach the running objects at once. Tests never touch the PC's
settings_local.json (tests/fakes.py: GEORGE_SETTINGS_FILE)."""
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (config overrides, unit types isolated, no settings file)
from core import settings


class Check(unittest.TestCase):
    def test_types_and_limits(self):
        self.assertEqual(settings.check("NINE_LINE_READBACK", "on"), (True, True))
        self.assertEqual(settings.check("NINE_LINE_READBACK", False), (True, False))
        self.assertFalse(settings.check("NINE_LINE_READBACK", "maybe")[0])
        self.assertEqual(settings.check("WARLORD_MAX_FLIGHTS", "3"), (True, 3))
        self.assertEqual(settings.check("WARLORD_MAX_FLIGHTS", 9), (False, "Flights up at once: 1 to 8"))
        self.assertFalse(settings.check("WARLORD_MAX_FLIGHTS", 2.5)[0])
        self.assertFalse(settings.check("WARLORD_MAX_FLIGHTS", True)[0])
        self.assertEqual(settings.check("DIRECT_ORDERS_ZONES", "Block"), (True, "block"))
        self.assertFalse(settings.check("DIRECT_ORDERS_ZONES", "ignore")[0])
        self.assertEqual(settings.check("OPERATOR_CALLSIGN", "Dodge"), (True, "DODGE"))
        self.assertFalse(settings.check("OPERATOR_CALLSIGN", "x")[0])
        self.assertFalse(settings.check("OPERATOR_CALLSIGN", "Dodge'; rm")[0])

    def test_only_the_list(self):
        self.assertEqual(len(settings.SETTINGS), 37)
        self.assertEqual({s.group for s in settings.SETTINGS}, set(settings.GROUPS))
        for name in ("OLYMPUS_PASSWORD", "OLYMPUS_USER", "OLYMPUS_URL", "KNEEBOARD_PORT", "LLM_MODEL", "BASE_DIR",
                     "OLYMPUS_GM_PASSWORD", "JOY_BUTTON"):
            self.assertEqual(settings.check(name, "x"), (False, f"{name} can't be changed here"))

    def test_voices_from_both_folders(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base)
        os.makedirs(os.path.join(base, "voices"))
        for p in ("a.onnx", "a.onnx.json", "voices/b.onnx", "voices/b.onnx.json", "c.onnx", "notes.txt"):
            open(os.path.join(base, p), "w").close()
        got = [os.path.relpath(p, base) for p in settings.voices(base)]
        self.assertEqual(got, ["a.onnx", os.path.join("voices", "b.onnx")])         # c has no .json
        self.assertTrue(settings.check("DARKSIDE_MODEL_PATH", os.path.join(base, "voices", "b.onnx"), base)[0])
        self.assertFalse(settings.check("DARKSIDE_MODEL_PATH", os.path.join(base, "c.onnx"), base)[0])
        self.assertFalse(settings.check("DARKSIDE_MODEL_PATH", "C:\\Windows\\x.onnx", base)[0])


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "settings_local.json")
        self.cfg = types.SimpleNamespace(WARLORD_MAX_FLIGHTS=4, NINE_LINE_READBACK=False)
        self.log = []

    def store(self):
        return settings.SettingsStore(self.path, cfg=self.cfg, printer=self.log.append, base=self.dir)

    def test_layers_save_reset(self):
        s = self.store()
        self.assertEqual((s.get("WARLORD_MAX_FLIGHTS"), s.source("WARLORD_MAX_FLIGHTS")), (4, "config"))
        self.assertEqual((s.get("DEFER_WAIT_S"), s.source("DEFER_WAIT_S")), (300, "default"))
        self.assertEqual(s.get("DEFER_WAIT_S", 120), 120)                    # the caller's default first
        seen = []
        s.bind("WARLORD_MAX_FLIGHTS", seen.append)
        self.assertEqual(s.set_value("WARLORD_MAX_FLIGHTS", 3), (True, ""))
        self.assertEqual((s.get("WARLORD_MAX_FLIGHTS"), s.source("WARLORD_MAX_FLIGHTS")), (3, "saved"))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"WARLORD_MAX_FLIGHTS": 3})
        self.assertIn("[SETTINGS] WARLORD_MAX_FLIGHTS 4 -> 3 (kneeboard)", self.log)
        self.assertEqual(self.store().get("WARLORD_MAX_FLIGHTS"), 3)          # read back at the next start
        self.assertTrue(any("1 saved setting override config.py: WARLORD_MAX_FLIGHTS=3" in m for m in self.log))
        self.assertEqual(s.reset("WARLORD_MAX_FLIGHTS"), (True, ""))
        self.assertEqual(s.get("WARLORD_MAX_FLIGHTS"), 4)
        self.assertEqual(seen, [4, 3, 4])                                     # now, the change, the reset
        self.assertEqual(os.listdir(self.dir), ["settings_local.json"])       # no temp files left

    def test_refused_values_change_nothing(self):
        s = self.store()
        self.assertFalse(s.set_value("WARLORD_MAX_FLIGHTS", 99)[0])
        self.assertFalse(s.set_value("OLYMPUS_PASSWORD", "x")[0])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(s.saved(), {})

    def test_restart_settings_say_so(self):
        s = self.store()
        s.set_value("TACTICAL_ENABLED", False)
        self.assertTrue(self.log[-1].endswith("- applies after a restart"))

    def test_bad_file_ignored(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        s = self.store()
        self.assertEqual(s.saved(), {})
        self.assertIn("unreadable", self.log[0])
        with open(self.path, "w") as f:
            json.dump({"WARLORD_MAX_FLIGHTS": 50, "OLYMPUS_PASSWORD": "x", "NINE_LINE_READBACK": True}, f)
        s = self.store()
        self.assertEqual(s.saved(), {"NINE_LINE_READBACK": True})            # the bad and unknown ones dropped
        self.assertTrue(any("Ignored OLYMPUS_PASSWORD" in m for m in self.log))
        self.assertFalse(any("x" == m for m in self.log))

    def test_memory_only_and_a_failing_bind(self):
        s = settings.SettingsStore(None, cfg=self.cfg, printer=self.log.append)
        s.bind("NINE_LINE_READBACK", lambda v: 1 / 0, apply_now=False)
        self.assertEqual(s.set_value("NINE_LINE_READBACK", True), (True, ""))
        self.assertTrue(s.get("NINE_LINE_READBACK"))
        self.assertTrue(any("Applying NINE_LINE_READBACK failed" in m for m in self.log))
        self.assertEqual(os.listdir(self.dir), [])

    def test_describe(self):
        rows = self.store().describe()
        self.assertEqual(len(rows), 37)
        r = next(x for x in rows if x["name"] == "WARLORD_MAX_FLIGHTS")
        self.assertEqual((r["value"], r["source"], r["lo"], r["hi"], r["live"]), (4, "config", 1, 8, True))
        self.assertEqual(next(x for x in rows if x["name"] == "DIRECT_ORDERS_ZONES")["choices"], ["warn", "block"])

    def test_tests_never_use_the_real_file(self):
        self.assertEqual(os.environ.get("GEORGE_SETTINGS_FILE"), "")
        self.assertIsNone(settings.default_path())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(settings.default_path())                       # unittest loaded: memory only


class LiveInMain(unittest.TestCase):
    """Each live setting reaches the running program at once, and back on reset."""
    def change(self, name, value):
        self.assertEqual(settings.set_value(name, value, "test"), (True, ""))
        self.addCleanup(settings.reset, name, "test")

    def test_roles_and_lists(self):
        import main
        from common import jtac, nine_line
        from command import exchange
        self.change("JTAC_GROUND_LASER_M", 5000)
        self.change("JTAC_DRONE_LASER_M", 30000)
        self.assertEqual((jtac.GROUND_LASER_RANGE_M, jtac.AIR_LASER_RANGE_M), (5000.0, 30000.0))
        self.change("COMMAND_SHORT_FORM", True)
        self.assertTrue(exchange.short())
        self.change("OPERATOR_CALLSIGN", "Dodge")
        self.assertEqual(main._operator_callsign(), "DODGE")
        self.change("TRAINING_MAX_GROUPS", 2)
        self.change("TRAINING_VOICE_SWITCH", False)
        self.assertEqual((main.TRAINING.max_groups, main.TRAINING.voice_switch), (2, False))
        self.change("KNEEBOARD_TRACK", False)
        self.assertFalse(main.TRACK.visible)
        if main.WARLORD is None or main.DARKSIDE is None:
            return
        self.change("WARLORD_MAX_FLIGHTS", 2)
        self.change("WARLORD_CAUTION_M", 3000)
        self.change("DIRECT_ORDERS_ZONES", "block")
        self.change("DARKSIDE_DANGER_CLOSE_M", 1500)
        self.change("DANGER_CLOSE_AGL_M", 500)
        self.change("NINE_LINE_READBACK", True)
        self.change("PERMISSIONS_ENFORCE", False)
        self.assertEqual((main.WARLORD.max_flights, main.WARLORD.caution_m), (2, 3000.0))
        self.assertTrue(main.PERMISSIONS.direct_block)
        self.assertFalse(main.PERMISSIONS.enforce)
        self.assertEqual((main.WARLORD.danger_close_m, main.DARKSIDE.danger_close_m, main._danger_close_m()),
                         (1500.0, 1500.0, 1500.0))
        self.assertEqual(main.DARKSIDE.agl_m, 500.0)
        self.assertTrue(nine_line.READBACK)
        if main.OVERLORD is not None:
            self.change("DEFER_WAIT_S", 120)
            self.assertEqual(main.OVERLORD.defer_wait_s, 120.0)

    def test_back_after_reset(self):
        import main
        from common import jtac
        settings.set_value("JTAC_GROUND_LASER_M", 4000, "test")
        settings.reset("JTAC_GROUND_LASER_M", "test")
        self.assertEqual(jtac.GROUND_LASER_RANGE_M, 8000.0)
        if main.WARLORD is not None:
            self.assertEqual(main.WARLORD.max_flights, 4)

    def test_spoken_gates(self):
        import main
        spoken = []
        with mock.patch.object(main, "_unprompted", lambda text, fn: spoken.append(text)), \
                mock.patch.object(main, "_alert_fuel", lambda text: None):
            self.change("FUEL_CALLS", False)
            main.FUEL_WATCH.speak("Overlord, joker fuel.")
            self.assertEqual(spoken, [])                                      # off: on the Alerts tab only
            settings.set_value("FUEL_CALLS", True, "test")
            main.FUEL_WATCH.speak("Overlord, joker fuel.")
            self.assertEqual(spoken, ["Overlord, joker fuel."])

    def test_hover_level_and_friendlies(self):
        from instructor.session import _hover_level
        from kneeboard.model import friendly_radius_m
        cfg = types.SimpleNamespace(INSTRUCTOR_HOVER_LEVEL=2)
        self.assertEqual(_hover_level(cfg), 2)
        self.change("INSTRUCTOR_HOVER_LEVEL", 4)
        self.assertEqual(_hover_level(cfg), 4)
        self.change("FRIENDLY_SHOW_M", 8000)
        self.assertEqual(friendly_radius_m(), 8000.0)


class CrewGates(unittest.TestCase):
    def test_talkdown_and_flares(self):
        from huey.crew_chief import CrewChief
        c = CrewChief(lambda t: None, lambda: {}, active=lambda: True)
        self.assertTrue(c.talkdown_on())
        called = []
        c.talkdown = type("T", (), {"check": lambda self, snap, now: called.append(1)})()
        c.talkdown_on = lambda: False
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "huey",
                               "crew_chief.py"), encoding="utf-8") as f:
            self.assertIn("if self.talkdown and tel_ok and self.talkdown_on():", f.read())
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "huey",
                               "profile.py"), encoding="utf-8") as f:
            self.assertIn('settings.get("HUEY_AUTO_ARM_FLARES", AUTO_ARM_FLARES)', f.read())


class Voices(unittest.TestCase):
    def test_voices_subfolder_fallback(self):
        import audio.tts as tts
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base)
        os.makedirs(os.path.join(base, "voices"))
        open(os.path.join(base, "voices", "en_US-bryce-medium.onnx"), "w").close()
        import config
        with mock.patch.object(config, "BASE_DIR", base):
            got = tts._voice_path("DARKSIDE_MODEL_PATH", os.path.join(base, "en_US-bryce-medium.onnx"))
        self.assertEqual(got, os.path.join(base, "voices", "en_US-bryce-medium.onnx"))


if __name__ == "__main__":
    unittest.main()
