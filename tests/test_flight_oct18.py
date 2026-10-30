"""Apache flight 18 Oct 2026: what the LLM answered, and what George does with it now."""
import unittest
from collections import deque

from tests import fakes  # noqa: F401
from intelligence import llm_client
from intelligence.llm_client import sanitize_result, trim_history
from intelligence.fast_path import match_fast


def run(raw, text):
    action, idx, speech, setting, value, targets = sanitize_result(raw, 3, text)
    return action, setting, value, speech


class TestFlightReplay(unittest.TestCase):
    def test_misheard_phrases_do_nothing(self):
        self.assertEqual(run({"action": "CEASE", "speech": "See you well."}, "See you well!")[0], "NONE")
        self.assertEqual(run({"action": "GEORGE_SET", "setting": "weapon", "value": "GUN"}, "A few will")[0], "NONE")
        self.assertEqual(run({"action": "GEORGE_SET", "setting": "weapon", "value": "GUN"},
                             "Switch left, MFD to video.")[0], "NONE")

    def test_none_answers_dont_invent_facts(self):
        self.assertEqual(run({"action": "NONE", "speech": "Objective is to secure the area and provide support."},
                             "What's our objective?")[3], "Unable.")
        self.assertEqual(run({"action": "NONE", "speech": "Master Arm is DISARMED, weapons on HOLD."},
                             "what is our current power setting.")[3], "Unable.")
        self.assertEqual(run({"action": "NONE", "speech": "See you again."}, "See you again.")[3], "Say again.")
        self.assertEqual(run({"action": "NONE", "speech": "Copy."}, "nice shot")[3], "Copy.")

    def test_real_commands_still_pass(self):
        self.assertEqual(run({"action": "GEORGE_SET", "setting": "weapon", "value": "GUN"},
                             "George, give me the cannon")[:3], ("GEORGE_SET", "weapon", "GUN"))
        self.assertEqual(run({"action": "GEORGE_SET", "setting": "roe", "value": "FREE"},
                             "you're free to engage")[:3], ("GEORGE_SET", "roe", "FREE"))
        self.assertEqual(run({"action": "CONSENT_TO_FIRE"}, "light him up, fire")[0], "CONSENT_TO_FIRE")
        self.assertEqual(run({"action": "AREA_SEARCH", "value": "FWD AREA"},
                             "have a look around out in front of us")[0], "AREA_SEARCH")
        self.assertEqual(run({"action": "STATUS"}, "what is our current position.")[0], "STATUS")
        self.assertEqual(run({"action": "SELECT_TARGET_INDEX", "target_index": 2}, "lock the second one")[0],
                         "SELECT_TARGET_INDEX")

    def test_fast_path_additions(self):
        snap = {"targets": []}
        r = match_fast("Go weapons hot", snap)
        self.assertEqual((r["action"], r["setting"], r["value"]), ("GEORGE_SET", "roe", "FREE"))
        self.assertEqual(match_fast("Zoom.", snap)["action"], "ZOOM_IN")
        self.assertEqual(match_fast("fuel", snap)["action"], "STATUS")
        self.assertEqual(match_fast("go hot", snap)["action"], "SET_ARMED")      # unchanged


class TestHistoryTrim(unittest.TestCase):
    def test_trimmed_in_batches(self):
        h = deque()
        lengths = []
        for i in range(12):
            h.append({"role": "user", "content": str(i)})
            trim_history(h)
            lengths.append(len(h))
        self.assertEqual(lengths, [1, 2, 3, 4, 5, 6, 7, 8, 4, 5, 6, 7])
        self.assertEqual(h[0]["content"], "5")

    def test_remember_turn_leaves_out_unused_fields(self):
        llm_client.CONVERSATION_HISTORY.clear()
        llm_client.remember_turn("status", "STATUS", None, "Stand by.")
        self.assertEqual(llm_client.CONVERSATION_HISTORY[-1]["content"],
                         '{"action": "STATUS", "speech": "Stand by."}')
        llm_client.CONVERSATION_HISTORY.clear()


class TestFuel(unittest.TestCase):
    def test_fuel_units(self):
        import importlib, sys
        sys.modules.setdefault("sounddevice", type(sys)("sounddevice"))
        try:
            main = importlib.import_module("main")
        except Exception as e:                      # main needs the audio stack; skip without it
            self.skipTest(f"main.py not importable here: {e}")
        snap = {"telemetry_age": 0.1}
        self.assertEqual(main.fuel_report(dict(snap, fuel_raw=(1100.0, 0.0))), "Fuel 2430 pounds.")
        self.assertEqual(main.fuel_report(dict(snap, fuel_raw=(0.62, 0.0))), "Fuel 62 percent.")
        # 22 Oct, two external tanks: 2.49 x a full internal load (1,140 kg)
        self.assertEqual(main.fuel_report(dict(snap, aircraft="AH-64D_BLK_II", fuel_raw=(2.4919, 0.0))),
                         "Fuel 6260 pounds.")
        self.assertEqual(main.fuel_report(dict(snap, fuel_raw=None)), "No fuel data.")


if __name__ == "__main__":
    unittest.main()
