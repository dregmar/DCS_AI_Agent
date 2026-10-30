"""Fixes from the LLM bench (Oct 2026): phrases the fast path missed or got wrong, and LLM answers
that named the wrong radio."""
import unittest

from tests import fakes  # noqa: F401
from huey import commands
from intelligence.fast_path import match_fast


def m(text):
    r = commands.match(text)
    return r and {k: v for k, v in r.items() if k not in ("speech", "source")}


class TestHueyFastPath(unittest.TestCase):
    def test_radio_without_its_name(self):
        self.assertEqual(m("set radio 124.750"), {"action": "RADIO", "key": "vhf_am", "mhz": 124.75})
        self.assertEqual(m("Set reader frequency 241"), {"action": "RADIO", "key": "uhf", "mhz": 241.0})
        self.assertEqual(m("set radio to frequency 232.9"), {"action": "RADIO", "key": "uhf", "mhz": 232.9})
        self.assertEqual(m("radio 40.5"), {"action": "RADIO", "key": "vhf_fm", "mhz": 40.5})
        self.assertEqual(m("radio 110.5"), {"action": "RADIO", "key": "vhf_nav", "mhz": 110.5})
        self.assertIsNone(m("radio 500"))                       # no radio has that: LLM / say again

    def test_cargo(self):
        self.assertEqual(m("connect loader"), {"action": "HOOK_LOAD"})
        self.assertEqual(m("where did the cargo go"), {"action": "LOAD_REPORT"})
        self.assertIsNone(m("how do I attach cargo?"))            # a question: nothing pressed
        self.assertIsNone(m("can I drop the load here"))
        self.assertEqual(m("hook the load"), {"action": "HOOK_LOAD"})
        self.assertEqual(m("where's the load"), {"action": "LOAD_REPORT"})

    def test_crew_and_autopilot(self):
        self.assertEqual(m("gunners, only shoot back if they shoot at us"),
                         {"action": "CREW_ROE", "seats": ["left", "right"], "value": "RETURN"})
        self.assertEqual(m("can you hold this attitude for me"), {"action": "AUTOPILOT", "value": "ATTITUDE"})


class TestHueySanitize(unittest.TestCase):
    def s(self, res, text):
        r = commands.sanitize(res, text)
        return {k: v for k, v in r.items() if k not in ("speech", "source")}

    def test_wrong_radio_is_corrected(self):
        self.assertEqual(self.s({"action": "RADIO", "key": "vhf_am", "mhz": 241}, "set reader frequency 241"),
                         {"action": "RADIO", "key": "uhf", "mhz": 241.0})
        self.assertEqual(self.s({"action": "RADIO", "key": "uhf", "mhz": 124.75}, "set radio 124.750"),
                         {"action": "RADIO", "key": "vhf_am", "mhz": 124.75})
        self.assertEqual(self.s({"action": "RADIO", "mhz": 30.5}, "radio thirty decimal five"),
                         {"action": "RADIO", "key": "vhf_fm", "mhz": 30.5})
        self.assertEqual(self.s({"action": "RADIO", "key": "uhf", "mhz": 999}, "radio 999")["action"], "NONE")

    def test_autopilot_value_wordings(self):
        self.assertEqual(self.s({"action": "AUTOPILOT", "value": "ATTITUDE HOLD"}, "hold this attitude"),
                         {"action": "AUTOPILOT", "value": "ATTITUDE"})


class TestApacheFastPath(unittest.TestCase):
    def test_bursts_plural(self):
        r = match_fast("set the gun to fifty round bursts", {"targets": []})
        self.assertEqual((r["action"], r["setting"], r["value"]), ("GEORGE_SET", "gun_burst", "50"))
        r = match_fast("burst 20", {"targets": []})
        self.assertEqual((r["setting"], r["value"]), ("gun_burst", "20"))


if __name__ == "__main__":
    unittest.main()
