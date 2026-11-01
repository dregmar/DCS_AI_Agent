"""Tactical picture step T3: the service thread - publishing, versions, mission loads, failures,
evidence from Olympus attacks, concurrency, and its wiring in main.py (design sections 4, 11 and 12)."""
import threading
import time
import unittest

from tests import fakes  # noqa: F401  (config overrides, unit types isolated)
from tactical.models import Status
from tactical.service import TacticalService
from tactical.tracks import KillHints

KM = 1000 / 111_320.0


class FakeStore:
    """Snapshot-shaped dicts, changed from the test thread while the service reads them."""
    def __init__(self):
        self._lock = threading.Lock()
        self.t = 100.0
        self.age = 0.3
        self.gnd = {}
        self.fail = None
        self.extra = {}

    def put(self, oid, typ, km):
        with self._lock:
            self.gnd[oid] = (typ, oid, 42.0 + km * KM, 42.0, 300.0)

    def drop(self, oid):
        with self._lock:
            self.gnd.pop(oid, None)

    def scan(self, dt=1.0):
        with self._lock:
            self.t += dt

    def snapshot(self):
        if self.fail:
            raise self.fail
        with self._lock:
            snap = {"my_ll": (42.0, 42.0), "t_model": self.t, "scan_t": self.t - self.age,
                    "enemy_lists_age": self.age, "telemetry_age": 0.05, "enemy_ground": list(self.gnd.values()),
                    "contacts": [], "air_defence": [], "enemy_air": [], "olympus_units": [], "olympus_enemy": [],
                    "wing": [], "george_list": [], "george_list_age": float("inf")}
            snap.update(self.extra)
            return snap


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def service(store, **kw):
    clock = Clock()
    return TacticalService(store.snapshot, clock=clock, **kw), clock


class Publishing(unittest.TestCase):
    def test_nothing_before_the_first_tick(self):
        svc, _ = service(FakeStore())
        self.assertIsNone(svc.picture())
        self.assertEqual(svc.status_link(), {"name": "Tactical picture", "ok": None, "detail": "not started"})

    def test_tick_publishes(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        svc, _ = service(store)
        pic = svc.tick()
        self.assertIs(svc.picture(), pic)
        self.assertEqual([t.display for t in pic.tracks], ["BMP-2"])
        link = svc.status_link()
        self.assertTrue(link["ok"])
        self.assertIn("1 tracks (1 active)", link["detail"])

    def test_version_moves_only_on_change(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        svc, clock = service(store)
        v1 = svc.tick().version
        for _ in range(3):                                  # new scans, nothing moved
            store.scan()
            clock.t += 1.0
            self.assertEqual(svc.tick().version, v1)
        store.put(12, "BMP-2", 3.05)                        # it moved
        store.scan()
        clock.t += 1.0
        self.assertGreater(svc.tick().version, v1)

    def test_mission_loaded_from_another_thread(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        svc, clock = service(store)
        self.assertEqual(svc.tick().generation, 0)
        threading.Thread(target=svc.mission_loaded).start()
        time.sleep(0.05)
        store.gnd.clear()
        store.put(5, "Ural-375", 2)
        clock.t += 1.0
        pic = svc.tick()
        self.assertEqual(pic.generation, 1)
        self.assertEqual([t.track_id for t in pic.tracks], ["T0001"])
        self.assertIn("new_picture", [e.kind for e in svc.events()])

    def test_events_reach_the_callback(self):
        store = FakeStore()
        got = []
        svc, clock = service(store, on_events=got.extend)
        svc.tick()
        store.put(12, "BMP-2", 3)
        store.scan()
        clock.t += 1.0
        svc.tick()
        self.assertEqual([e.text for e in got], ["New: BMP-2 (T0001)."])


class Failures(unittest.TestCase):
    def test_store_error_keeps_the_last_picture(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        svc, clock = service(store)
        pic = svc.tick()
        store.fail = RuntimeError("boom")
        clock.t += 0.25
        self.assertIs(svc.tick(), pic)
        self.assertEqual(svc.errors, 1)
        link = svc.status_link()
        self.assertFalse(link["ok"])
        self.assertIn("RuntimeError: boom", link["detail"])
        store.fail = None
        clock.t += 11.0
        svc.tick()
        self.assertNotIn("error", svc.status_link()["detail"])

    def test_export_stops(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        svc, clock = service(store)
        svc.tick()
        store.age = 8.0                                      # packets carry an 8 s old scan
        clock.t += 1.0
        pic = svc.tick()
        self.assertEqual(pic.tracks[0].status, Status.STALE)
        link = svc.status_link()
        self.assertFalse(link["ok"])
        self.assertIn("picture is 8 seconds old", link["detail"])

    def test_broken_hints_dont_stop_the_picture(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        svc, _ = service(store, hints=lambda snap, attacks: 1 / 0)
        self.assertEqual(len(svc.tick().tracks), 1)
        self.assertIn("hints", svc.last_error)

    def test_old_export_lua(self):
        store = FakeStore()
        store.put(12, "BMP-2", 3)
        store.extra = {"t_model": None, "scan_t": None, "enemy_lists_age": None}
        svc, _ = service(store)
        svc.tick()
        self.assertIn("ages unknown", svc.status_link()["detail"])


class Evidence(unittest.TestCase):
    def test_olympus_attack_names_the_killer(self):
        store = FakeStore()
        store.put(12, "T-72B", 3)
        names = {}
        svc, clock = service(store, hints=lambda snap, attacks: KillHints(
            targeted={k: names.get(a, "an AI flight") for k, a in attacks.items()}))
        svc.tick()
        names[777] = "Outcast 2"
        svc.note_command("attackUnit", {"ID": 777, "targetID": 12})
        svc.note_command("setROE", {"ID": 777, "ROE": 2})            # not an attack
        svc.note_command("attackUnit", {"ID": "x", "targetID": 13})   # unreadable: ignored
        self.assertEqual(svc.attacks(), {"dcs:12": 777})
        store.drop(12)
        for _ in range(3):
            store.scan()
            clock.t += 1.0
            svc.tick()
        self.assertEqual(svc.picture().tracks[0].status_reason, "destroyed by Outcast 2")
        self.assertEqual(svc.events()[-1].text, "T-72B (T0001) destroyed by Outcast 2.")

    def test_old_attacks_expire(self):
        svc, clock = service(FakeStore())
        svc.note_command("attackUnit", {"ID": 1, "targetID": 2})
        clock.t += 601.0
        self.assertEqual(svc.attacks(), {})


class Concurrency(unittest.TestCase):
    def test_readers_while_the_thread_runs(self):
        store = FakeStore()
        for i in range(30):
            store.put(100 + i, "Ural-375", 1 + i * 0.1)
        svc = TacticalService(store.snapshot, interval=0.001).start()
        errors, versions = [], []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    p = svc.picture()
                    if p is not None:
                        versions.append(p.version)
                        len(p.tracks)
                    svc.events()
                    svc.status_link()
            except Exception as e:                           # pragma: no cover - the failure we look for
                errors.append(e)

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for r in readers:
            r.start()
        for i in range(200):                                 # the world changes underneath
            store.put(100 + i % 30, "Ural-375", 1 + (i % 30) * 0.1 + (i % 3) * 0.01)
            if i % 50 == 0:
                svc.mission_loaded()
            store.scan(0.05)
            svc.note_command("attackUnit", {"ID": 1, "targetID": 100 + i % 30})
            time.sleep(0.002)
        stop.set()
        for r in readers:
            r.join()
        svc.stop()
        self.assertEqual(errors, [])
        self.assertEqual(svc.errors, 0, svc.last_error)
        self.assertGreater(svc.ticks, 10)
        self.assertTrue(versions)


class Enrichment(unittest.TestCase):
    """T4: George's list and the threats, worked out once per update for every reader."""
    def _svc(self, store, **kw):
        from tactical.threats import ThreatFns
        fns = ThreatFns(weight=lambda n: {"ZSU-23-4 Shilka": 100, "T-72B": 60}.get(n, 10),
                        ring_m=lambda n: 2500.0 if "ZSU" in n else None)
        return service(store, threat_fns=fns, **kw)

    def test_george_entries_and_threats_in_the_picture(self):
        store = FakeStore()
        store.put(12, "T-72B", 3)
        store.put(13, "ZSU-23-4 Shilka", 2)
        store.put(14, "Ural-375", 1)
        store.extra = {"george_list": ["ZSU-23-4", "ARMORED", "BMP-2"], "george_list_age": 1.0,
                       "george_list_complete": True}
        svc, _ = self._svc(store)
        pic = svc.tick()
        g = {a.list_index: a for a in pic.george}
        self.assertEqual((g[1].strength, pic.track(g[1].track_id).type_name), ("unique", "ZSU-23-4 Shilka"))
        self.assertEqual(g[2].strength, "class_only")
        self.assertEqual(g[3].strength, "none")
        self.assertEqual([a.list_index for a in pic.george_unassociated], [2, 3])
        self.assertIs(pic.track(g[1].track_id).george, g[1])
        self.assertEqual([pic.track(a.track_id).type_name for a in pic.threats], ["ZSU-23-4 Shilka", "T-72B"])
        self.assertTrue(pic.threats[0].inside_ring)

    def test_version_moves_when_the_association_does(self):
        store = FakeStore()
        store.put(12, "T-72B", 3)
        svc, clock = self._svc(store)
        v = svc.tick().version
        store.extra = {"george_list": ["T-72B"], "george_list_age": 1.0}
        clock.t += 0.25
        self.assertGreater(svc.tick().version, v)

    def test_george_ranking_once_per_update(self):
        store = FakeStore()
        calls = []

        def ev(snap):
            calls.append(snap)
            return [{"id": 1, "type": "T-72B"}], {"id": 1, "type": "T-72B"}, {"tads_masked": False}
        svc, clock = self._svc(store, george_eval=ev)
        self.assertIsNone(svc.george_evaluation())
        svc.tick()
        targets, top, constraints, when = svc.george_evaluation()
        self.assertEqual((top["type"], when, len(calls)), ("T-72B", 1000.0, 1))
        targets[0]["type"] = "changed"                       # readers get copies
        self.assertEqual(svc.george_evaluation()[0][0]["type"], "T-72B")

    def test_real_ranking_by_default(self):
        store = FakeStore()
        store.extra = {"targets": [{"id": 1, "type": "ZSU-23-4 Shilka", "dist_m": 2000, "brg": 10, "clock": 12}],
                       "pitch": 0.0, "roll": 0.0, "was_state": "NONE", "master_arm": "DISARMED"}
        svc, _ = self._svc(store)
        svc.tick()
        _, top, constraints, _ = svc.george_evaluation()
        self.assertEqual(top["type"], "ZSU-23-4 Shilka")
        self.assertIn("tads_masked", constraints)


class MainWiring(unittest.TestCase):
    def test_main_has_the_service(self):
        import main
        self.assertIsNotNone(main.TACTICAL)
        names = [l["name"] for l in main.kneeboard_status()["links"]]
        self.assertIn("Tactical picture", names)
        before = main.TACTICAL._loads
        main.TACTICAL.mission_loaded()
        self.assertEqual(main.TACTICAL._loads, before + 1)
        main.TACTICAL._loads = before

    def test_hints_from_george_and_shots(self):
        import main
        snap = {"engagement": [{"index": 1, "type": "T-72B", "contact_id": 15}], "wing": []}
        h = main._tactical_hints(snap, {})
        self.assertEqual(h.targeted, {"dcs:15": "George"})
        self.assertIsNone(h.last_shot_mono)                 # engagement monitor not started in tests
        h = main._tactical_hints({"wing": [("Ford 2", 31, 42.0, 42.0)]}, {"dcs:9": 31, "dcs:10": 99})
        self.assertEqual(h.targeted, {"dcs:9": "Ford 2", "dcs:10": "an AI flight"})

    def test_olympus_hook(self):
        from common import olympus
        seen = []
        oly = olympus.Olympus("http://127.0.0.1:1", "u", "p")
        oly._command = lambda name, params: (True, "")
        old = olympus.ON_COMMAND
        try:
            olympus.ON_COMMAND = lambda n, p: seen.append((n, p))
            oly.command("attackUnit", {"ID": 1, "targetID": 2})
            olympus.ON_COMMAND = lambda n, p: 1 / 0          # a broken listener never breaks the order
            self.assertEqual(oly.command("attackUnit", {"ID": 1, "targetID": 2}), (True, ""))
            oly._command = lambda name, params: (False, "refused")
            olympus.ON_COMMAND = lambda n, p: seen.append((n, p))
            oly.command("attackUnit", {"ID": 1, "targetID": 3})          # not accepted: not noted
        finally:
            olympus.ON_COMMAND = old
        self.assertEqual(seen, [("attackUnit", {"ID": 1, "targetID": 2})])


if __name__ == "__main__":
    unittest.main()
