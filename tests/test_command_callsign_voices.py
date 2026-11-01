"""Phase 2, P2-2 and P2-3: your callsign from the mission (Unit:getCallsign() through the comms hook,
fallback NOMAD), and Warlord's / Darkside's voices and wake words. Nothing is played."""
import os
import unittest
from unittest import mock

import numpy as np

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from audio import tts
from command import roles, wake
from core import state as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Spoken(unittest.TestCase):
    def test_forms(self):
        for raw, said in [("Enfield11", "Enfield 1-1"), ("Colt12", "Colt 1-2"), ("Uzi 3 1", "Uzi 3-1"),
                          ("Uzi3-1", "Uzi 3-1"), ("Springfield101", "Springfield 10-1"), ("NOMAD", "Nomad"),
                          ("enfield", "Enfield"), ("101", "101"), ("  Pontiac 21 ", "Pontiac 2-1"),
                          ("Dodge", "Dodge"), ("", "")]:
            self.assertEqual(roles.spoken(raw), said, raw)

    def test_parse(self):
        self.assertEqual(roles.parse_callsign("CALLSIGN Enfield11\tAerial-1-1"), ("Enfield11", "Aerial-1-1"))
        self.assertEqual(roles.parse_callsign("CALLSIGN Enfield11"), ("Enfield11", ""))
        self.assertIsNone(roles.parse_callsign("CALLSIGN "))
        self.assertIsNone(roles.parse_callsign("FUEL 0.5 100 name x"))
        self.assertIsNone(roles.parse_callsign(None))


class Callsign(unittest.TestCase):
    def snap(self, **kw):
        s = {"callsign_raw": "Enfield11", "callsign_unit": "Aerial-1-1", "callsign_age": 3.0, "my_unit": "Aerial-1-1"}
        s.update(kw)
        return s

    def test_from_dcs(self):
        self.assertEqual(roles.callsign(self.snap()), ("Enfield 1-1", "DCS"))
        self.assertEqual(roles.callsign(self.snap(my_unit=None)), ("Enfield 1-1", "DCS"))
        self.assertEqual(roles.callsign_link(self.snap())["detail"], "Enfield 1-1 (from DCS: Enfield11)")

    def test_fallbacks(self):
        for snap, why in [(self.snap(callsign_raw=None), "no callsign"), (self.snap(callsign_age=120.0), "old"),
                          (self.snap(my_unit="Other-1"), "not your unit")]:
            said, where = roles.callsign(snap)
            self.assertEqual(said, "Nomad")
            self.assertIn(why, where)
        self.assertEqual(roles.callsign({}, "VIPER 1")[0], "Viper 1")       # OPERATOR_CALLSIGN as set
        self.assertEqual(roles.callsign({}, "")[0], "Nomad")
        self.assertIsNone(roles.callsign_link({})["ok"])

    def test_state_store(self):
        s = st.ApacheStateStore()
        self.assertIsNone(s.snapshot()["callsign_raw"])
        s.set_callsign("Enfield11", "Aerial-1-1")
        snap = s.snapshot()
        self.assertEqual((snap["callsign_raw"], snap["callsign_unit"]), ("Enfield11", "Aerial-1-1"))
        self.assertLess(snap["callsign_age"], 1.0)
        s.set_callsign(None)                                               # a new mission
        self.assertIsNone(s.snapshot()["callsign_age"])


class HookLua(unittest.TestCase):
    def test_callsign_sender(self):
        with open(os.path.join(ROOT, "dcs_scripts", "george_comms_hook.lua"), encoding="utf-8") as f:
            s = f.read()
        self.assertIn("local CALLSIGN_BODY = [==[", s)
        self.assertIn('return "CALLSIGN " .. cs .. "\\t" .. un', s)
        self.assertIn("    send_callsign(now)\n", s)
        self.assertIn("gch.want_unit, gch.fuel_t, gch.callsign_t = unit, 0, 0", s)      # asked again for a new unit
        from tests.test_tactical_sides import balance
        self.assertEqual(balance(s), 0)
        body = s[s.index("local CALLSIGN_BODY = [==[") + 26:s.index("]==]", s.index("local CALLSIGN_BODY"))]
        self.assertEqual(balance(body), 0)

    def test_comms_menu_takes_it(self):
        with open(os.path.join(ROOT, "common", "comms_menu.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index('if text.startswith("CALLSIGN "):')
        self.assertLess(i, src.index('if text.startswith("FUEL "):'))
        self.assertIn("state_store.set_callsign(*got)", src[i:i + 400])


class Wake(unittest.TestCase):
    def test_names(self):
        for text, want in [("Warlord, radio check", (roles.WARLORD, "radio check")),
                           ("War lord, override", (roles.WARLORD, "override")),
                           ("Hey Warlords, how do you read", (roles.WARLORD, "how do you read")),
                           ("Darkside, request target", (roles.DARKSIDE, "request target")),
                           ("Dark side radio check", (roles.DARKSIDE, "radio check")),
                           ("Dark site, BDA", (roles.DARKSIDE, "BDA")),
                           ("Darkslide, abort", (roles.DARKSIDE, "abort"))]:
            self.assertEqual(wake.addressed(text), want, text)

    def test_not_them(self):
        from common import overlord as ov
        for text in ("Overlord, picture", "Two, rejoin", "Water, check", "darken the sight", "Workload status",
                     "Dart board", ""):
            self.assertIsNone(wake.addressed(text), text)
        for text in ("Warlord, radio check", "Darkside, request target"):
            self.assertIsNone(ov.addressed(text), text)                      # Overlord doesn't take them


class FakeChunk:
    def __init__(self, n):
        self.audio_int16_bytes = np.zeros(n, dtype=np.int16).tobytes()


class FakeVoice:
    def __init__(self, rate):
        self.config = type("C", (), {"sample_rate": rate})()
        self.said = []

    def synthesize(self, text):
        self.said.append(text)
        yield FakeChunk(800)


class Voices(unittest.TestCase):
    def setUp(self):
        self.v = {k: FakeVoice(r) for k, r in ((tts.GEORGE, 16000), (tts.OVERLORD, 22050),
                                               (tts.WARLORD, 22051), (tts.DARKSIDE, 22052))}
        self._p = [mock.patch.dict(tts.VOICES, self.v),
                   mock.patch.object(tts.sd, "play", lambda a, samplerate, **k: None, create=True),
                   mock.patch.object(tts.sd, "stop", lambda: None, create=True),
                   mock.patch.object(tts.time, "sleep", lambda s: None)]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    def test_own_voices(self):
        tts._play("Warlord, CAS approved.", tts.WARLORD)
        tts._play("Darkside, cleared hot.", tts.DARKSIDE)
        self.assertEqual(self.v[tts.WARLORD].said, ["Warlord, CAS approved."])
        self.assertEqual(self.v[tts.DARKSIDE].said, ["Darkside, cleared hot."])

    def test_missing_file_uses_overlords_voice(self):
        with mock.patch.dict(tts.VOICES, {tts.WARLORD: None, tts.DARKSIDE: None}):
            tts._play("Warlord, CAS approved.", tts.WARLORD)
            tts._play("Darkside, cleared hot.", tts.DARKSIDE)
        self.assertEqual(self.v[tts.OVERLORD].said, ["Warlord, CAS approved.", "Darkside, cleared hot."])
        with mock.patch.dict(tts.VOICES, {tts.WARLORD: None, tts.OVERLORD: None}):
            tts._play("Warlord, CAS approved.", tts.WARLORD)
        self.assertEqual(self.v[tts.GEORGE].said, ["Warlord, CAS approved."])   # and then George's

    def test_queue(self):
        with mock.patch.object(tts, "start_speech_worker", lambda: None):
            tts.speak_warlord("Warlord, go ahead.")
            tts.speak_darkside("Darkside, go ahead.")
            items = [tts._queue.get_nowait() for _ in range(2)]
        self.assertEqual([i[4] for i in items], ["warlord", "darkside"])

    def test_loader_is_optional(self):
        with mock.patch.object(tts.os.path, "exists", lambda p: False):
            self.assertIsNone(tts._load_role(tts.WARLORD, "WARLORD_MODEL_PATH", "en_US-norman-medium.onnx"))


class MainReplies(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main

    def reply(self, text, raw="Enfield11"):
        with mock.patch.object(self.main.state_store, "snapshot",
                               lambda: {"callsign_raw": raw, "callsign_unit": "", "callsign_age": 1.0}):
            return self.main.role_reply(text)

    def test_radio_checks_in_their_own_voices(self):
        self.assertEqual(self.reply("Warlord, radio check"),
                         ("Enfield 1-1, Warlord, loud and clear.", "warlord", "warlord"))
        self.assertEqual(self.reply("Dark side, how do you read"),
                         ("Enfield 1-1, Darkside, loud and clear.", "darkside", "darkside"))
        self.assertEqual(self.reply("Darkside", raw=None), ("Nomad, Darkside, go ahead.", "darkside", "darkside"))

    def test_until_their_steps(self):
        said, who, _ = self.reply("Darkside, sing a song")                 # nothing he does (P2-4 on)
        self.assertEqual(who, "darkside")
        self.assertIn("say again", said)
        said, who, _ = self.reply("Warlord, approve CAS")
        self.assertIn("through Overlord", said)
        self.assertIsNone(self.reply("Overlord, picture"))
        with mock.patch.object(self.main, "COMMAND_ROLES_ON", False):
            self.assertIsNone(self.reply("Warlord, radio check"))

    def test_shared_reply_takes_them_first_and_jets_keep_them(self):
        m = self.main
        with mock.patch.object(m.state_store, "profile", lambda: "other"):
            said, who, src = m.shared_reply("Warlord, radio check")
        self.assertEqual((who, src), ("warlord", "warlord"))
        self.assertTrue(said.endswith("Warlord, loud and clear."))

    def test_speak_as_uses_the_voice(self):
        m = self.main
        got = []
        with mock.patch.object(m, "speak_warlord", lambda t, priority=None: got.append(("w", t))), \
                mock.patch.object(m, "speak_darkside", lambda t, priority=None: got.append(("d", t))):
            m._speak_as("Warlord, go ahead.", "warlord")
            m._speak_as("Darkside, go ahead.", "darkside")
        self.assertEqual(got, [("w", "Warlord, go ahead."), ("d", "Darkside, go ahead.")])

    def test_stt_prompt_and_status(self):
        m = self.main
        self.assertIn("Warlord, Darkside", m.stt_prompt())
        links = {l["name"]: l for l in m.kneeboard_status()["links"]}
        self.assertIn("Pilot callsign", links)
        with mock.patch.dict(tts.VOICES, {tts.WARLORD: None}):
            row = {l["name"]: l for l in m.kneeboard_status()["links"]}["Voices (Piper)"]
        self.assertIn("warlord missing, uses overlord's voice", row["detail"])

    def test_router_knows_them(self):
        from intelligence import router
        self.assertIn('"Warlord, radio check"', router.ROUTER_SYSTEM)
        self.assertTrue(router.plausible("dark side radio check", "Darkside, radio check")[0])
        self.assertFalse(router.plausible("radio check", "Warlord, radio check")[0])

    def test_kneeboard_labels(self):
        with open(os.path.join(ROOT, "kneeboard", "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('warlord: "Warlord", darkside: "Darkside"', html)


if __name__ == "__main__":
    unittest.main()
