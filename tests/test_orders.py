"""Audit step 6: explicit order outcomes (common/orders.py) for wingman, Outcast and Overlord orders,
logged and shown in the kneeboard comms log, with the spoken answers unchanged."""
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from common import orders, wingmen, overlord as ov, independents as ind
from common.orders import ORDERS, OrderLog
from tests import test_independents as ti, test_overlord as to
from tests.test_olympus import OlympusCase, FakeOlympus


class Recorder:
    """Swaps ORDERS' printer and keeps what was recorded from here on."""

    def __enter__(self):
        self.t0 = time.time()
        self._p = mock.patch.object(ORDERS, "printer", lambda s: None)
        self._p.start()
        return self

    def __exit__(self, *a):
        self._p.stop()

    def since(self):
        """Only what this test recorded (ORDERS is shared by the whole run)."""
        return [r for r in ORDERS.recent(1000) if r.t >= self.t0]



def last(who):
    """The newest result for who (other tests' background threads may record for others)."""
    return [r for r in ORDERS.recent(1000) if r.who == who][-1]


class TestRegistry(unittest.TestCase):
    def test_record_and_callback(self):
        lines, got = [], []
        log = OrderLog(printer=lines.append)
        log.on_result = got.append
        r = log.record("Two", "attack", "submitted", "attackUnit")
        self.assertEqual(r.text(), "Two: attack, submitted (attackUnit)")
        self.assertEqual(lines, ["[ORDER] Two: attack, submitted (attackUnit)"])
        self.assertEqual(got, [r])
        with self.assertRaises(ValueError):
            log.record("Two", "attack", "done")

    def test_callback_error_doesnt_break_the_order(self):
        lines = []
        log = OrderLog(printer=lines.append)
        log.on_result = lambda r: 1 / 0
        log.record("Two", "follow", "submitted")
        self.assertIn("comms log", lines[-1])

    def test_outcome(self):
        self.assertEqual(orders.outcome([("attackUnit", True, ""), ("attackUnit", True, "")], ""),
                         ("submitted", "attackUnit"))
        self.assertEqual(orders.outcome([("setPath", True, ""), ("landAtPoint", False, "HTTP 500")], ""),
                         ("failed", "landAtPoint: HTTP 500"))
        self.assertEqual(orders.outcome([], "Unable, I can't see three."), ("failed", "Unable, I can't see three"))
        self.assertEqual(orders.outcome([], "Overlord, unable, no spawn points."),
                         ("failed", "Overlord, unable, no spawn points"))
        self.assertIsNone(orders.outcome([], "Nobody's lasing."))
        self.assertIsNone(orders.outcome([], ""))

    def test_capture_is_per_thread_and_nests(self):
        import threading
        with orders.capture() as outer:
            with orders.capture() as inner:
                orders.note_command("smoke", True)
            t = threading.Thread(target=orders.note_command, args=("setROE", True))
            t.start()
            t.join()
        orders.note_command("followUnit", True)                 # nothing capturing: ignored
        self.assertEqual(inner, [("smoke", True, "")])
        self.assertEqual(outer, [("smoke", True, "")])


class TestWingmanOrders(OlympusCase):
    """The real Olympus client against the fake core.dll: its results feed the outcome."""

    def test_submitted_and_wording_unchanged(self):
        with Recorder():
            self.assertEqual(self.say("two, attack the ZSU"), "")
            r = last("Two")
        self.assertEqual((r.who, r.order, r.status, r.reason), ("Two", "attack", "submitted", "attackUnit"))

    def test_refused_command_failed(self):
        with Recorder(), mock.patch.object(FakeOlympus, "do_PUT", lambda h: h._reply(500)):
            said = self.say("two, follow me")
            r = last("Two")
        self.assertTrue(said.startswith("Unable"), said)
        self.assertEqual((r.who, r.order, r.status), ("Two", "follow", "failed"))
        self.assertIn("HTTP 500", r.reason)

    def test_radio_menu(self):
        class Menu:
            last_items = []

            def __init__(self, ok):
                self.ok = ok

            def select_path(self, path):
                return (True, "") if self.ok else (False, "no Wingman 2 in the menu")
        with Recorder():
            self.assertEqual(wingmen.give_order(Menu(True), self.snap, 2, ["Engage", "My Target"]), "")
            a = last("Two")
            wingmen.give_order(Menu(False), self.snap, 2, ["Engage", "My Target"])
            b = last("Two")
        self.assertEqual((a.who, a.order, a.status, a.reason), ("Two", "Engage / My Target", "submitted", "radio menu"))
        self.assertEqual(b.status, "failed")
        self.assertIn("no Wingman 2 in the menu", b.reason)


class NotingOly(ti.FakeOly):
    """test_independents' fake Olympus, noting results as the real client does."""

    def command(self, name, params):
        ok, why = super().command(name, params)
        orders.note_command(name, ok, why)
        return ok, why


class TestOutcastOrders(ti._Base):
    def setUp(self):
        super().setUp()
        self.oly = NotingOly(self.world)
        wingmen.EXECUTOR.oly = self.oly
        self.rec = Recorder().__enter__()
        self.addCleanup(self.rec.__exit__)

    def outcast(self, n=2):
        return [r for r in self.rec.since() if r.who == f"Outcast {n}"]

    def test_independent_accepted_then_completed(self):
        self.go_independent(2)
        self.assertTrue(self.wait(lambda: any(r.status == "completed" for r in self.outcast())))
        got = [(r.order, r.status) for r in self.outcast()]
        self.assertIn(("go independent", "accepted"), got)
        self.assertLess(got.index(("go independent", "accepted")), got.index(("go independent", "completed")))

    def test_independent_failed_with_reason(self):
        self.oly.clone_works = False
        self.say("three, go independent")
        self.assertTrue(self.wait(lambda: any(r.status == "failed" for r in self.outcast(3))))
        self.assertEqual(self.outcast(3)[-1].reason, "no clone appeared")

    def test_outcast_order_submitted(self):
        self.go_independent(2)
        self.assertEqual(self.say("Outcast 2, attack my current target"), "")
        r = self.outcast()[-1]
        self.assertEqual((r.order, r.status, r.reason), ("attack my current target", "submitted", "attackUnit"))


class TestOverlordFlights(to._Base):
    def setUp(self):
        super().setUp()
        self.rec = Recorder().__enter__()
        self.addCleanup(self.rec.__exit__)

    def weasel(self):
        return [r.status for r in self.rec.since() if r.who == "Weasel flight"]

    def test_launch_task_cancel(self):
        self.world.kill_on_attack = False
        said = self.o.handle("Overlord, SEAD north of me")
        self.assertTrue(said.startswith("Overlord copies."), said)           # unchanged
        self.assertTrue(self.wait(lambda: "submitted" in self.weasel()))
        self.assertIn("cancelled", self.o.handle("Overlord, cancel SEAD"))
        self.assertEqual(self.weasel()[-3:], ["accepted", "submitted", "completed"])
        self.assertEqual(last("Weasel flight").reason, "cancelled, returning to base")

    def test_cancel_while_being_tasked(self):
        """The race behind an intermittent test_cancel_which failure: "cancel" lands between _join's
        cancel check and state = "engaging". The flight must stay cancelled and go home."""
        self.world.kill_on_attack = False
        real = self.oly.command

        def command(name, params):
            if name == "setROE":
                self.o.cancel(["SEAD"])
            return real(name, params)
        with mock.patch.object(self.oly, "command", command):
            self.o.handle("Overlord, SEAD north of me")
            self.assertTrue(self.wait(lambda: "completed" in self.weasel()))
            time.sleep(0.3)
        self.assertIn("no flights", self.o.handle("Overlord, status"))
        self.assertNotIn("submitted", self.weasel())
        self.assertEqual([n for n, _ in self.oly.sent if n == "attackUnit"], [])
        self.assertEqual(self.oly.sent[-1][0], "setPath")                     # sent home

    def test_failed_to_launch(self):
        with mock.patch.object(self.world, "on_command", lambda *a: None):
            self.o.handle("Overlord, SEAD north of me")
            self.assertTrue(self.wait(lambda: "failed" in self.weasel()))
        self.assertIn("failed to launch", last("Weasel flight").reason)

    def test_olympus_down(self):
        self.oly.up = False
        self.assertEqual(self.o.handle("Overlord, SEAD north of me"), "Overlord, unable, Olympus isn't running.")
        self.assertEqual(last("Weasel flight").status, "failed")


class TestCommsLog(unittest.TestCase):
    def test_line_of_kind_order(self):
        main = fakes.pipeline_module().main
        from kneeboard.comms_log import CommsLog
        log = CommsLog()
        with mock.patch.object(main, "COMMS_LOG", log):
            main.order_to_comms_log(orders.OrderResult("Two", "follow", "failed", "follow: HTTP 500"))
        e = log.since(0)[0]
        self.assertEqual((e["who"], e["text"]), ("order", "Two: follow, failed (follow: HTTP 500)"))


def tearDownModule():
    wingmen.EXECUTOR = None
    wingmen.INDEPENDENTS = None


if __name__ == "__main__":
    unittest.main()
