"""'two, go independent' -> Outcast 2 through Olympus. A fake Olympus clones and deletes units in a
fake Export.lua world; nothing leaves this process."""
import threading
import time
import unittest
from unittest import mock

from common import independents as ind, wingmen
from common.olympus_orders import Executor
from tests.test_olympus import snap as base_snap, contact


class FakeOly:
    def __init__(self, world):
        self.world, self.sent, self.up, self.clone_works = world, [], True, True
        self.connected = True

    def connect(self):
        return self.up

    def get(self, path):
        return {}

    def command(self, name, params):
        self.sent.append((name, params))
        w = self.world
        with w.lock:
            if name == "cloneUnits" and self.clone_works:
                src = params["units"][0]
                w.groups += 1
                g = f"Olympus-{w.groups}"
                w.s["olympus_units"].append((g, g + "-1", 9000 + w.groups, src["location"]["lat"],
                                             src["location"]["lng"], 100.0))
            elif name == "deleteUnit":
                gone = [x for x in w.s["wing"] if x[1] == params["ID"]]
                w.s["wing"] = [x for x in w.s["wing"] if x[1] != params["ID"]]
                w.s["flight_alive"] = [a for a in w.s["flight_alive"] if a not in {g[0] for g in gone}]
            elif name == "attackUnit" and w.kill:
                tid = params["targetID"]
                threading.Timer(0.05, w.kill_target, args=(tid,)).start()
        return True, ""


class World:
    def __init__(self):
        self.lock = threading.Lock()
        self.s = base_snap()
        self.s.update({"olympus_units": [], "enemy_ground": [], "air_defence": [], "my_ll": (42.0, 42.0)})
        self.groups, self.kill = 0, True

    def kill_target(self, tid):
        with self.lock:
            self.s["enemy_ground"] = [g for g in self.s["enemy_ground"] if g[1] != tid]
            self.s["contacts"] = [c for c in self.s["contacts"] if c["id"] != tid]

    def snap(self):
        with self.lock:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in self.s.items()}


class _Base(unittest.TestCase):
    def setUp(self):
        self._p = [mock.patch.object(ind, "APPEAR_TIMEOUT_S", 2.0), mock.patch.object(ind, "POLL_S", 0.05),
                   mock.patch.object(ind, "HUNT_IDLE_S", 0.3)]
        for p in self._p:
            p.start()
        self.world = World()
        self.oly = FakeOly(self.world)
        self.said = []
        ex = Executor(self.oly, self.world.snap)
        wingmen.EXECUTOR = ex
        wingmen.INDEPENDENTS = ind.Independents(ex, self.world.snap, self.said.append)
        self.addCleanup(setattr, wingmen, "INDEPENDENTS", None)
        self.addCleanup(setattr, wingmen, "EXECUTOR", None)

    def tearDown(self):
        for p in self._p:
            p.stop()

    def say(self, text):
        return wingmen.handle(text, comms=None, snap=self.world.snap(), speak=self.said.append)

    def wait(self, cond, t=3.0):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    def sent(self, name):
        return [p for n, p in self.oly.sent if n == name]

    def go_independent(self, n=2):
        r = self.say(f"{ind.NUM_SAY[n]}, go independent")
        self.assertIn("going independent", r)
        self.assertTrue(self.wait(lambda: any(f"Outcast {n}, independent" in x for x in self.said)), self.said)



class TestIndependents(_Base):
    def test_clone_then_delete_then_follow(self):
        self.go_independent(2)
        names = [n for n, _ in self.oly.sent]
        self.assertEqual(names[:2], ["cloneUnits", "deleteUnit"])          # clone first, then remove
        clone = self.sent("cloneUnits")[0]
        self.assertEqual(clone["units"][0]["ID"], 101)
        self.assertFalse(clone["deleteOriginal"])
        self.assertEqual(self.sent("deleteUnit")[0]["ID"], 101)
        self.assertEqual(self.sent("followUnit")[-1]["targetID"], 100)     # on your wing
        self.assertEqual(self.sent("followUnit")[-1]["ID"], 9001)
        self.assertIn({"ID": 9001, "altitudeType": "AGL"}, self.sent("setAltitudeType"))

    def test_failed_clone_keeps_the_original(self):
        self.oly.clone_works = False
        self.say("three, go independent")
        self.assertTrue(self.wait(lambda: any("couldn't go independent" in x for x in self.said)), self.said)
        self.assertEqual(self.sent("deleteUnit"), [])
        self.assertIsNone(wingmen.INDEPENDENTS.known(3))

    def test_never_your_own_aircraft(self):
        s = self.world.snap()
        r = wingmen.INDEPENDENTS.detach([{"n": 1, "name": "Rotary-2-1", "id": 100, "lat": 42, "lon": 42}], s)
        self.assertIn("Unable", r)
        self.assertEqual(self.oly.sent, [])

    def test_outcast_orders(self):
        self.go_independent(2)
        self.oly.sent.clear()
        self.assertEqual(self.say("Outcast 2, attack my current target"), "")
        self.assertEqual(self.sent("attackUnit")[0]["ID"], 9001)
        self.assertEqual(self.say("outcast two, RTB"), "")
        self.assertEqual(self.sent("landAtPoint")[0]["ID"], 9001)
        self.assertEqual(self.say("Outcast 2, rejoin"), "")
        self.assertEqual(self.say("Outcast 2, weapons hold"), "")
        self.assertEqual(self.sent("setROE")[-1], {"ID": 9001, "ROE": 4})
        self.assertIn("on your wing", self.say("Outcast 2, status"))
        self.assertIn("say again", self.say("Outcast 2, do a barrel roll"))
        # "two, ..." still reaches him
        self.oly.sent.clear()
        self.assertEqual(self.say("two, weapons free"), "")
        self.assertEqual(self.sent("setROE")[-1], {"ID": 9001, "ROE": 0})
        # Outcast 3 isn't independent yet: same as "three, ..." (24 Oct)
        self.assertEqual(ind.parse_outcast("Outcast 3, RTB"), (3, "rtb"))

    def test_engage_at_will(self):
        self.go_independent(2)
        with self.world.lock:
            self.world.s["enemy_ground"] = [("Ural-375", 501, 42.01, 42.0), ("ZSU-23-4 Shilka", 502, 42.012, 42.0),
                                           ("T-72B", 503, 42.3, 42.0)]        # the T-72 is 33 km away
        self.assertEqual(self.say("Outcast 2, engage at will"),
                         "Outcast 2, engaging at will, 2 in range. First, Shilka.")
        self.assertTrue(self.wait(lambda: any("no more targets" in x for x in self.said)), self.said)
        self.assertEqual([p["targetID"] for p in self.sent("attackUnit")], [502, 501])   # Shilka first
        self.assertEqual(self.sent("setROE")[0]["ROE"], 0)
        self.assertEqual(self.sent("followUnit")[-1]["ID"], 9001)                 # rejoined

    def test_flight_order_reaches_outcasts(self):
        for n in (2, 3, 4):
            self.go_independent(n)
        self.oly.sent.clear()
        self.assertEqual(self.say("flight, weapons hold"), "")
        self.assertEqual(sorted(p["ID"] for p in self.sent("setROE")), [9001, 9002, 9003])

    def test_lost(self):
        self.go_independent(2)
        with self.world.lock:
            self.world.s["olympus_units"] = []
        self.assertIn("is down", self.say("Outcast 2, attack my target"))

    def test_heard_as(self):
        for heard, want in (("Outcast 2, attack my current target", (2, "attack my current target")),
                            ("out cast three RTB", (3, "rtb")), ("Outcast to, engage at will", (2, "engage at will")),
                            ("George, outcast four, status", (4, "status"))):
            self.assertEqual(ind.parse_outcast(heard), want, heard)
        self.assertIsNone(ind.parse_outcast("two, attack my target"))
        self.assertTrue(ind.INDEPENDENT.search("go independence"))


class TestOct22(_Base):
    """22 Oct: Outcast 4 was matched to Outcast 2's group and the real wingman was deleted."""

    def test_existing_outcast_is_never_the_new_clone(self):
        self.go_independent(2)
        self.oly.clone_works = False                     # the second clone doesn't appear...
        real = self.world.snap
        hide = [True]

        def first_without_outcast2():                   # ... and one snapshot misses Outcast 2
            s = real()
            if hide[0]:
                hide[0] = False
                s["olympus_units"] = []
            return s
        wingmen.INDEPENDENTS.snapshot = first_without_outcast2
        self.say("four, go independent")
        self.assertTrue(self.wait(lambda: any("couldn't go independent" in x for x in self.said)), self.said)
        self.assertEqual([p["ID"] for p in self.sent("deleteUnit")], [101])    # only Outcast 2's original
        self.assertIsNone(wingmen.INDEPENDENTS.known(4))

    def test_heard_22oct(self):
        for heard, n in (("ORGAS 2, engage at will", 2), ("ORCAS 4, atop my target", 4),
                         ("Allcast 4, engage my target", 4), ("All CAS for, engage my lase target", 4),
                         ("All CAS4, engage at will", 4), ("OCH-3, engage at will", 3)):
            self.assertEqual(ind.parse_outcast(heard)[0], n, heard)

    def test_laser_target_is_an_attack(self):
        self.go_independent(2)
        self.oly.sent.clear()
        self.assertEqual(self.say("Outcast 2, engage my laser target"), "")
        self.assertEqual(self.sent("attackUnit")[0]["ID"], 9001)
        self.assertEqual(self.sent("fireLaser"), [])
        self.oly.sent.clear()
        self.assertEqual(self.say("ORCAS 2, atop my target"), "")
        self.assertEqual(len(self.sent("attackUnit")), 1)

    def test_my_target_falls_back_to_nearest(self):
        self.go_independent(2)
        self.oly.sent.clear()
        with self.world.lock:
            self.world.s["hdg"] = 180.0                  # nothing on the nose
        self.assertEqual(self.say("Outcast 2, attack my current target"), "")
        self.assertEqual(self.sent("attackUnit")[0]["targetID"], 9)          # the Ural at 1.5 km


class TestOct23(_Base):
    """23 Oct: 'Ocast 2' (and others) went to George; 'engage air defences' wasn't an Outcast order."""

    def test_heard_23oct(self):
        for heard, n, rest in (("Ocast 2, destroy your defence", 2, "destroy your defence"),
                               ("Forecast 2", 2, ""), ("August 2, status", 2, "status"),
                               ("Ocast, Ocast 2, engage, air defences", 2, "engage, air defences"),
                               ("Ocast 2, engage my target", 2, "engage my target")):
            self.assertEqual(ind.parse_outcast(heard), (n, rest), heard)

    def test_rejoin_soundalikes(self):
        self.go_independent(2)
        for heard in ("Ocast 2, re-transformation", "Allcast 2, re-drying", "Ocast 2, rejoin"):
            self.oly.sent.clear()
            self.assertEqual(self.say(heard), "", heard)
            self.assertEqual(self.sent("followUnit")[0]["ID"], 9001, heard)

    def test_engage_air_defences_only(self):
        self.go_independent(2)
        with self.world.lock:
            self.world.s["enemy_ground"] = [("Ural-375", 501, 42.01, 42.0), ("ZSU-23-4 Shilka", 502, 42.012, 42.0),
                                           ("T-72B", 504, 42.011, 42.0), ("Rapier_fsa_blindfire_radar", 503, 42.013, 42.0)]
        self.oly.sent.clear()
        self.assertEqual(self.say("Ocast 2, engage air defences"),
                         "Outcast 2, going after the air defences, 2 in range. First, Shilka.")
        self.assertTrue(self.wait(lambda: any("no more air defences" in x for x in self.said)), self.said)
        self.assertEqual(sorted(p["targetID"] for p in self.sent("attackUnit")), [502, 503])   # not the truck or tank


class TestOct24(_Base):
    """Cases from the 24 Oct flights."""
    def test_outcast_go_independent_before_he_is(self):
        r = self.say("Outcast 2, go independent.")
        self.assertIn("going independent", r)
        self.assertTrue(self.wait(lambda: any("Outcast 2, independent" in x for x in self.said)), self.said)

    def test_hunt_with_nothing_near_says_so(self):
        self.go_independent(2)
        with self.world.lock:
            self.world.s["enemy_ground"] = [("T-72B", 503, 42.3, 42.0)]          # 33 km away
        self.oly.sent.clear()
        self.assertEqual(self.say("Outcast 2, go kill the air defences"),
                         "Outcast 2, no air defences within 6 kilometres of me.")
        self.assertEqual(self.sent("setROE"), [])

    def test_tanks_not_on_your_sensors(self):
        self.go_independent(2)
        with self.world.lock:
            self.world.s["contacts"] = []
            self.world.s["enemy_ground"] = [("Ural-375", 501, 42.01, 42.0), ("T-72B", 503, 42.06, 41.94),
                                           ("T-72B", 504, 41.95, 42.0)]
        self.oly.sent.clear()
        r = self.say("Outcast 2, go destroy the tanks")
        self.assertEqual(self.sent("attackUnit")[-1]["targetID"], 504)                # nearest tank
        self.assertIn("engaging T-72B", r)
        r = self.say("Ocast 2, engage enemy tanks, north-west")
        self.assertEqual(self.sent("attackUnit")[-1]["targetID"], 503)                # the one north-west
        self.assertIn("north west of you", r)
        with self.world.lock:
            self.world.s["enemy_ground"] = [("Ural-375", 501, 42.01, 42.0)]
        self.assertEqual(self.say("Outcast 2, engage armor"), "No armor within 15 kilometres.")

    def test_understands(self):
        self.assertTrue(ind.understands("engage air defences"))
        self.assertTrue(ind.understands("go destroy the tanks"))
        self.assertTrue(ind.understands("come back to me"))
        self.assertFalse(ind.understands("engage TUNKS, MARTWEST"))
        self.assertFalse(ind.understands("do a barrel roll"))
