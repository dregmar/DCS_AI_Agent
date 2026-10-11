"""Engagement callouts from stores and contacts (no keys involved)."""
import unittest

from tests.fakes import state_store, send_telemetry, DEFAULT_CONTACTS
from intelligence.engagement_monitor import EngagementMonitor


class Callouts(unittest.TestCase):
    def test_missiles_kills_and_shoot_list(self):
        contacts = [dict(c) for c in DEFAULT_CONTACTS]
        state_store.set_george_list(["ZSU-23-4", "LARC-V", "LARC-V", "T-72B"], True)
        send_telemetry(contacts, ammo=(8, 0, 300))
        state_store.set_engagement([1, 4])                 # ZSU first, then the T-72
        said = []
        mon = EngagementMonitor(said.append)

        def step(ammo):
            send_telemetry(contacts, ammo=ammo)
            mon.check()

        step((8, 0, 300))
        step((7, 0, 300))                                  # missile away
        contacts = [c for c in contacts if c["id"] != 13]  # the ZSU is gone
        for _ in range(4):
            step((7, 0, 300))
        step((6, 0, 300))
        contacts = [c for c in contacts if c["id"] != 15]  # the T-72 is gone
        for _ in range(4):
            step((6, 0, 300))
        step((6, 0, 280))
        step((6, 0, 260))                                  # one burst, one callout
        self.assertEqual(said, ["Missile away, 7 remaining.", "Target destroyed. Next target, T-72B.",
                                "Missile away, 6 remaining.", "Target destroyed. Shoot list complete.",
                                "Firing gun."])

    def test_no_kill_call_without_an_engagement(self):
        contacts = [dict(c) for c in DEFAULT_CONTACTS]
        state_store.set_engagement([])
        said = []
        mon = EngagementMonitor(said.append)
        send_telemetry(contacts)
        mon.check()
        contacts = contacts[1:]
        for _ in range(4):
            send_telemetry(contacts)
            mon.check()
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
