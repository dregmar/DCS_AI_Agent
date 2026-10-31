"""Spoken text -> main.handle_command -> George / DCS-BIOS -> what George says.
main.py's own driver is shut down at import and replaced with one on a FakeKeyboard, DCS-BIOS
sends are captured, and the LLM is disabled, so nothing leaves this process."""
import threading
import time
import unittest

from tests.fakes import (FakeGeorge, FakeKeyboard, wait_until, send_telemetry, DEFAULT_CONTACTS,
                         state_store)
import main                                   # noqa: E402
from core.george_interface import GeorgeInterface
from config import BIOS_OUTPUTS

main.george.shutdown()                        # the real-keyboard driver never gets a command

SPOKEN = []
BIOS_SENT = []
main.speak_george_radio = lambda text, **k: SPOKEN.append(text)
OVERLORD_SPOKEN = []
main.speak_overlord = lambda text, **k: OVERLORD_SPOKEN.append(text)
main.cmd_sender.send_command = lambda ident, arg: (BIOS_SENT.append(f"{ident} {arg}"), True)[1]


def _no_llm(*a, **k):
    raise RuntimeError("LLM disabled in tests")


main.query_qwen = _no_llm
main.READBACK_TIMEOUT_SEC = 0.4
main.WAS_READBACK_SEC = 0.4


class Pipeline(unittest.TestCase):
    def setUp(self):
        state_store.update_george_ui({"visible": False})
        state_store.set_george_list([], False)
        state_store.set_engagement([])
        send_telemetry(DEFAULT_CONTACTS)
        self.fg = FakeGeorge()
        main.george = GeorgeInterface(FakeKeyboard(self.fg), on_event=main._george_event,
                                      ui_source=state_store.george_ui_snapshot,
                                      list_sink=state_store.set_george_list,
                                      focus_check=lambda: True)
        SPOKEN.clear()
        BIOS_SENT.clear()
        time.sleep(0.4)

    def tearDown(self):
        main.george.shutdown()
        self.fg.stop()
        time.sleep(0.4)

    def say(self, text, until=None, timeout=8.0):
        SPOKEN.clear()
        main.handle_command(text, time.monotonic())
        if until:
            wait_until(lambda: any(until in s for s in SPOKEN), timeout)
        return list(SPOKEN)

    def test_search_reads_list_with_clock_positions(self):
        out = self.say("search", until="Target list")
        self.assertIn("Target list: 1 ZSU-23-4 at 12 o'clock, 2 LARC-V at 2 o'clock, 3 LARC-V at 3 o'clock, "
                      "4 T-72B at 10 o'clock, and 2 more.", out)
        self.assertEqual(self.say("where is target 4"), ["Target 4, T-72B, 10 o'clock, 5.2 kilometers."])

    def test_urgent_lane_through_the_real_handlers(self):
        """Step 5: "cease fire" is taken by the fast path (never the model) on the urgent lane; a bare
        "abort" has no fixed George handler, so it falls back to the queue (and the model)."""
        from unittest import mock
        from core import lanes

        def model(*a, **k):                   # what ollama_chat.chat does first on the urgent lane
            main.LANES.before_model()
            raise RuntimeError("LLM disabled in tests")

        fresh = lanes.Lanes()
        with mock.patch.object(main, "LANES", fresh), mock.patch.object(main, "query_qwen", model):
            self.assertTrue(main.run_urgent("George, cease fire", time.monotonic(), None))
            self.assertFalse(main.run_urgent("Abort", time.monotonic(), None))
        self.assertFalse(fresh.handler._lock.locked())

    def test_expired_fire_order_is_not_fired(self):
        SPOKEN.clear()
        main.handle_command("send it", time.monotonic() - 5.0)
        self.assertEqual(SPOKEN, ["Fire order expired, say again."])

    def test_master_arm_presses_once_and_reads_the_light(self):
        addr, mask, shift = BIOS_OUTPUTS["master_arm"]

        def light(on):
            state_store.mark_bios_frame()
            state_store.update_cockpit_switches(addr, (1 << shift) if on else 0)
        light(False)
        threading.Timer(0.1, lambda: light(True)).start()
        self.assertEqual(self.say("master arm on"), ["Master arm on."])
        self.assertEqual(BIOS_SENT, ["PLT_MASTER_ARM_BTN 1", "PLT_MASTER_ARM_BTN 0"])
        BIOS_SENT.clear()
        self.say("master arm on")
        self.assertEqual(BIOS_SENT, [])               # already armed: a press would SAFE it

    def test_action_missiles_uses_your_weapon_switch(self):
        state_store.update_wpn_page(was="GUN", sight="HMD")
        threading.Timer(0.1, lambda: state_store.update_wpn_page(was="MSL")).start()
        self.assertEqual(self.say("action missiles"), ["Your missiles."])
        self.assertEqual(BIOS_SENT, ["PLT_CYCLIC_WEAPON_LR 0", "PLT_CYCLIC_WEAPON_LR 1"])

    def test_status(self):
        out = self.say("status")
        self.assertEqual(len(out), 1)
        self.assertIn("George on no weapon, weapons hold.", out[0])


if __name__ == "__main__":
    unittest.main()
