"""Overlord: "Overlord, ..." requests only, SEAD through Olympus spawnAircrafts + attackUnit.
A fake Olympus client and a fake DCS (the snapshot) stand in; nothing leaves this process."""
import threading
import time
import unittest
from unittest import mock

from common import overlord as ov

ME = (42.0, 42.0)


class FakeOly:
    def __init__(self, world, mission=None, up=True):
        self.world, self.up, self.sent = world, up, []
        self.mission = mission or {"mission": {"commandModeOptions": {
            "commandMode": "Blue commander", "restrictSpawns": True, "spawnPoints": {"blue": 100}}}}

    def connect(self):
        return self.up

    def get(self, path):
        return self.mission

    def command(self, name, params):
        self.sent.append((name, params))
        self.world.on_command(name, params)
        return True, ""


class FakeWorld:
    """What Export.lua would show: radars and Olympus flights."""
    def __init__(self, radars):
        self.lock = threading.Lock()
        self.ad = list(radars)
        self.oly = [("Olympus-1", "Olympus-1-1", 900, 41.0, 42.0, 6100)]   # someone else's flight
        self.gnd, self.air = [], []
        self.me = ME
        self.kill_on_attack = True
        self.groups = 1

    def on_command(self, name, p):
        with self.lock:
            if name == "spawnAircrafts":
                u = p["units"][0]["location"]
                self.groups += 1
                g, base = f"Olympus-{self.groups}", 1000 * (self.groups - 1)
                self.oly += [(g, g + "-1", base + 1, u["lat"], u["lng"], 6100),
                             (g, g + "-2", base + 2, u["lat"], u["lng"], 6100)]
            elif name == "attackUnit" and self.kill_on_attack:
                threading.Timer(0.05, self.kill, args=(p["targetID"],)).start()

    def kill(self, oid):
        with self.lock:
            self.ad = [a for a in self.ad if a[1] != oid]
            self.gnd = [a for a in self.gnd if a[1] != oid]
            self.air = [a for a in self.air if a[1] != oid]

    def snap(self):
        with self.lock:
            return {"my_ll": self.me, "hdg": 0.0, "route": [], "air_defence": list(self.ad),
                    "olympus_units": list(self.oly), "enemy_ground": list(self.gnd),
                    "enemy_air": list(self.air)}


def north(km):
    return (ME[0] + km * 1000 / 111_320.0, ME[1])


RADARS = [("Kub 1S91 str", 501, *north(10.5)), ("Kub 2P25 ln", 502, *north(10.2)),
          ("ZSU-23-4 Shilka", 503, *north(9)), ("Strela-10M3", 504, *north(10)),
          ("Kub 1S91 str", 505, *north(60))]


class _Base(unittest.TestCase):
    def setUp(self):
        self._p = [mock.patch.object(ov, "POLL_S", 0.1), mock.patch.object(ov, "APPEAR_TIMEOUT_S", 2.0)]
        for p in self._p:
            p.start()
        self.world = FakeWorld(RADARS)
        self.oly = FakeOly(self.world)
        self.said = []
        self.o = ov.Overlord(self.oly, self.world.snap, self.said.append)

    def tearDown(self):
        for p in self._p:
            p.stop()

    def wait(self, cond, t=5.0):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()



class TestOverlord(_Base):
    def test_wake_word(self):
        for heard in ("Overlord, requesting SEAD", "overload requesting seed", "Hey, over lord. SEAD",
                      "George, Overlord, status"):
            self.assertIsNotNone(ov.addressed(heard), heard)
        for heard in ("two, attack my target", "fuel", "the overlord is"):
            self.assertIsNone(ov.addressed(heard), heard)

    def test_sead_north_of_me(self):
        r = self.o.handle("Overlord, requesting SEAD strike north of my current position")
        self.assertIn("Overlord copies", r)
        self.assertIn("9 kilometres north of you", r)          # the nearest radar that way
        self.assertIn("SA-6", r)
        self.assertIn("Shilka", r)
        self.assertNotIn("Strela", r)                     # IR only: not a HARM target
        name, spawn = self.oly.sent[0]
        self.assertEqual(name, "spawnAircrafts")
        self.assertEqual(spawn["spawnPoints"], 20)
        self.assertEqual(len(spawn["units"]), 2)
        self.assertEqual(spawn["units"][0]["loadout"], ov.SEAD_LOADOUT)
        self.assertTrue(spawn["immediate"])
        self.assertEqual(spawn["airbaseName"], "")
        lat = spawn["units"][0]["location"]["lat"]
        self.assertLess(lat, north(10)[0])               # comes in from your side (south of the point)
        self.assertTrue(self.wait(lambda: any("area clear" in s for s in self.said)), self.said)
        attacks = [p["targetID"] for n, p in self.oly.sent if n == "attackUnit"]
        self.assertEqual(sorted(attacks), [501, 502, 503])
        self.assertTrue(all(p["ID"] == 1001 for n, p in self.oly.sent if n == "attackUnit"))
        self.assertIn(("setROE", {"ID": 1001, "ROE": 0}), self.oly.sent)
        self.assertEqual(self.oly.sent[-1][0], "setPath")  # then home
        self.assertTrue(any("destroyed" in s for s in self.said))

    def test_distance_and_direction(self):
        r = self.o.handle("Overlord, SEAD 60 kilometers north")
        self.assertIn("60 kilometres north of you", r)
        p, where = ov.parse_where("sead 5 miles north east of me", {"my_ll": ME})
        self.assertEqual(where, "5 miles north east of you")
        self.assertGreater(p[0], ME[0])
        self.assertGreater(p[1], ME[1])

    def test_no_radars_no_flight(self):
        r = self.o.handle("Overlord, requesting SEAD south of my position")
        self.assertIn("No air-defence radars", r)
        self.assertEqual(self.oly.sent, [])

    def test_olympus_down(self):
        self.oly.up = False
        self.assertIn("Olympus isn't running", self.o.handle("Overlord, SEAD north of me"))
        self.assertEqual(self.oly.sent, [])

    def test_spawn_points(self):
        self.oly.mission["mission"]["commandModeOptions"]["spawnPoints"]["blue"] = 10
        r = self.o.handle("Overlord, SEAD north of me")
        self.assertIn("spawn points", r)
        self.assertEqual(self.oly.sent, [])

    def test_one_at_a_time_status_cancel(self):
        self.world.kill_on_attack = False
        self.o.handle("Overlord, SEAD north of me")
        self.assertTrue(self.wait(lambda: any(n == "attackUnit" for n, _ in self.oly.sent)))
        self.assertIn("already tasked", self.o.handle("Overlord, SEAD north of me"))
        self.assertIn("Engaging the", self.o.handle("Overlord, status"))
        self.assertIn("cancelled", self.o.handle("Overlord, cancel SEAD"))
        self.assertEqual(self.oly.sent[-1][0], "setPath")
        self.assertIn("no flights", self.o.handle("Overlord, status"))

    def test_flight_lost(self):
        self.world.kill_on_attack = False
        self.o.handle("Overlord, SEAD north of me")
        self.assertTrue(self.wait(lambda: any(n == "attackUnit" for n, _ in self.oly.sent)))
        with self.world.lock:
            self.world.oly = [u for u in self.world.oly if u[0] != "Olympus-2"]
        self.assertTrue(self.wait(lambda: any("is down" in s for s in self.said)), self.said)

    def test_failed_launch(self):
        with mock.patch.object(self.world, "on_command", lambda *a: None):
            self.o.handle("Overlord, SEAD north of me")
            self.assertTrue(self.wait(lambda: any("failed to launch" in s for s in self.said)))

    def test_unknown_request(self):
        self.assertIn("say again", self.o.handle("Overlord, two, rejoin").lower())
        self.assertEqual(self.o.handle("Overlord"), "Overlord, go ahead.")

    def test_waypoint(self):
        snap = {"my_ll": ME, "route": [[3, 1000.0, 2000.0]]}
        with mock.patch.object(ov.geo.FIT, "to_ll", lambda x, z: (42.2, 42.1)):
            p, where = ov.parse_where("sead on waypoint 3", snap)
        self.assertEqual((p, where), ((42.2, 42.1), "waypoint 3"))
        self.assertEqual(ov.parse_where("sead on waypoint 7", snap)[1], "there's no waypoint 7")


def east(km, base=ME):
    return (base[0], base[1] + km * 1000 / (111_320.0 * 0.7431))


class TestMoreFlights(_Base):
    def test_cas_on_a_point(self):
        self.world.gnd = [("ZU-23 Emplacement", 601, *east(5)), ("T-72B", 602, *east(5.5)),
                          ("Ural-375", 603, *east(4.8)), ("Kub 1S91 str", 604, *east(5.2)),
                          ("BMP-2", 605, *east(20))]
        r = self.o.handle("Overlord, requesting close air support 5 kilometres east of me")
        self.assertIn("Hog flight, two A-10s, tasked CAS 5 kilometres east of you", r)
        self.assertIn("Caution", r)                       # the SA-6 radar is left to SEAD
        spawn = self.oly.sent[0][1]
        self.assertEqual(spawn["units"][0]["unitType"], "A-10C_2")
        self.assertTrue(self.wait(lambda: any("area clear" in x for x in self.said)), self.said)
        attacks = [p["targetID"] for n, p in self.oly.sent if n == "attackUnit"]
        self.assertEqual(attacks, [601, 602, 603])        # AAA first, tank, truck; not the SA-6 or the far BMP

    def test_cas_on_my_target(self):
        self.world.gnd = [("T-72B", 701, *east(3)), ("BMP-2", 702, *east(3.2))]
        self.o.my_target = lambda snap: {"id": 702, "type": "BMP-2", "lat": east(3.2)[0], "lon": east(3.2)[1]}
        r = self.o.handle("Overlord, CAS on my target")
        self.assertIn("CAS your target", r)
        self.assertTrue(self.wait(lambda: sum(n == "attackUnit" for n, _ in self.oly.sent) >= 2))
        first = next(p for n, p in self.oly.sent if n == "attackUnit")
        self.assertEqual(first["targetID"], 702)
        self.o.my_target = lambda snap: None
        self.o.cancel([], True)

    def test_cas_nothing_there(self):
        self.assertIn("No enemy ground units", self.o.handle("Overlord, CAS north of me"))
        self.assertEqual(self.oly.sent, [])

    def test_cap_engages_bandit_and_returns(self):
        r = self.o.handle("Overlord, requesting CAP over waypoint 3")
        self.assertIn("there's no waypoint 3", r)
        r = self.o.handle("Overlord, requesting CAP 20 kilometres north of me")
        self.assertIn("Eagle flight, two Eagles, CAP 20 kilometres north of you", r)
        self.assertTrue(self.wait(lambda: any(n == "setPath" for n, _ in self.oly.sent)))
        with self.world.lock:
            self.world.air = [("MiG-29S", 801, *north(45), 7000), ("Su-25T", 802, *north(150), 3000)]
        self.assertTrue(self.wait(lambda: any("splash one MiG-29S" in x for x in self.said)), self.said)
        self.assertTrue(any("committing on MiG-29S, 45 kilometres north of you" in x for x in self.said), self.said)
        attacks = [p["targetID"] for n, p in self.oly.sent if n == "attackUnit"]
        self.assertEqual(attacks, [801])                    # the far Su-25 is outside the 40 km radius
        self.assertEqual(self.oly.sent[-1][0], "setPath")   # back to the station
        self.assertIn("Eagle flight on task", self.o.handle("Overlord, status"))
        self.o.cancel([], True)

    def test_escort_follows_and_engages(self):
        r = self.o.handle("Overlord, requesting fighter escort")
        self.assertIn("Shield flight, two Vipers, escorting you", r)
        self.assertTrue(self.wait(lambda: any(n == "setPath" for n, _ in self.oly.sent)))
        paths = lambda: [p["path"][0] for n, p in self.oly.sent if n == "setPath"]
        with self.world.lock:
            self.world.me = north(8)                        # you move 8 km
        self.assertTrue(self.wait(lambda: len(paths()) >= 2))
        self.assertAlmostEqual(paths()[-1]["lat"], north(8)[0], places=4)
        with self.world.lock:
            self.world.air = [("Mi-24V", 901, *north(20), 300)]
        self.assertTrue(self.wait(lambda: any("splash one Mi-24V" in x for x in self.said)), self.said)
        self.o.cancel(["ESCORT"])

    def test_cancel_which(self):
        self.world.kill_on_attack = False
        self.world.gnd = [("T-72B", 701, *east(3))]
        self.o.handle("Overlord, SEAD north of me")
        self.o.handle("Overlord, CAS 3 km east of me")
        self.assertIn("say which", self.o.handle("Overlord, cancel"))
        self.assertIn("Hog flight, mission cancelled", self.o.handle("Overlord, cancel CAS"))
        self.assertIn("Weasel", self.o.handle("Overlord, status"))
        self.assertIn("Weasel flight, mission cancelled", self.o.handle("Overlord, cancel all"))

    def test_questions_go_to_the_briefing(self):
        asked = []
        self.o.ask = lambda q: asked.append(q) or "Overlord, package priority is the bridge."
        self.assertIn("bridge", self.o.handle("Overlord, what's our package priority today?"))
        self.assertIn("No enemy ground units", self.o.handle("Overlord, can I get CAS on my position?"))
        self.assertIn("say again", self.o.handle("Overlord, two, rejoin"))
        self.assertEqual(len(asked), 1)

    def test_out_of_window_is_unknown(self):
        far = {"type": "T-72B", "id": 5, "lat": north(50)[0], "lon": ME[1], "src": "enemy_ground"}
        self.assertEqual(ov.target_state({"my_ll": ME, "enemy_ground": []}, far), "unknown")
        near = dict(far, lat=north(5)[0])
        self.assertEqual(ov.target_state({"my_ll": ME, "enemy_ground": []}, near), "dead")
        full = [("BMP-2", 100 + i, *north(1)) for i in range(40)]
        self.assertEqual(ov.target_state({"my_ll": ME, "enemy_ground": full}, near), "unknown")


class TestFlightOct21(_Base):
    """From the 21 Oct flight."""

    def test_unit_order_doesnt_change_the_lead(self):
        flip = [False]
        real = self.world.snap

        def shuffled():
            s = real()
            flip[0] = not flip[0]
            if flip[0]:
                s["olympus_units"] = list(reversed(s["olympus_units"]))   # Export.lua: no fixed order
            return s
        self.o.snapshot = shuffled
        self.o.handle("Overlord, requesting CAP")
        self.assertTrue(self.wait(lambda: any(n == "setPath" for n, _ in self.oly.sent)))
        with self.world.lock:
            self.world.air = [("Mi-8MT", 801, *north(11), 300)]
        self.assertTrue(self.wait(lambda: any("splash" in x for x in self.said)), self.said)
        commits = [x for x in self.said if "committing" in x]
        self.assertEqual(len(commits), 1, self.said)
        self.assertEqual({p["ID"] for n, p in self.oly.sent if n == "attackUnit"}, {1001})
        self.o.cancel([], True)

    def test_heard_as(self):
        self.assertEqual(ov.addressed("or what I request in SEAD"), "requesting SEAD")
        self.assertIsNone(ov.addressed("or what about the fuel"))

    def test_direction_without_distance_finds_the_nearest(self):
        self.world.gnd = [("ZBD04A", 701, *north(5)), ("ZBD04A", 702, *north(5.5)), ("ZTZ96B", 703, *east(4))]
        r = self.o.handle("Overlord, request, CAS, to my north")
        self.assertIn("tasked CAS 5 kilometres north of you", r)
        self.assertNotIn("703", str([p for n, p in self.oly.sent if n == "attackUnit"]))
        self.o.cancel([], True)

    def test_no_place_means_nearest(self):
        r = self.o.handle("Overlord, requesting SEAD")
        self.assertIn("9 kilometres north of you", r)
        self.o.cancel([], True)

    def test_my_target_falls_back_to_nearest(self):
        self.world.gnd = [("ZTZ96B", 703, *east(4))]
        self.o.my_target = lambda snap: None
        r = self.o.handle("Overlord, request, CAS on my target")
        self.assertIn("CAS the nearest enemy, 4 kilometres east of you", r)
        self.o.cancel([], True)


class TestPicture(_Base):
    def test_clean_and_groups(self):
        self.assertEqual(self.o.handle("Overlord, what's the current picture?"), "Overlord, picture clean.")
        with self.world.lock:
            self.world.air = [("Mi-24V", 1, *north(25), 150), ("Mi-24V", 2, *north(26), 150),
                              ("MiG-29S", 3, *east(60), 7000)]
        said = self.o.handle("Overlord, picture")
        self.assertEqual(said, "Overlord, picture, 2 groups. 2 ship Mi-24V, 25 kilometres north of you, "
                               "500 feet. single MiG-29S, 60 kilometres east of you, 23000 feet.")
        self.assertIn("2 groups", self.o.handle("Overlord, bogey dope"))
