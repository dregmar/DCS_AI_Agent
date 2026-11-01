"""Tactical picture step T4: George's Target List -> tracks as uncertain associations, never merges
(design section 6, tier 2; test plan rows "George association" and "Conflicting identities")."""
import unittest

from tactical import fusion
from tactical.models import Affiliation, BEST_OF_N, CLASS_ONLY, Category, NONE, Status, UNIQUE
from tests.tactical_fixtures import ME, attrs, entry, track


def assoc(entries, tracks, previous=None, me=ME):
    return fusion.associate(entries, tracks, me, attrs=attrs, previous=previous)


class Association(unittest.TestCase):
    def test_unique(self):
        a, = assoc([entry(1, "ZSU-23-4")], [track("T0001", "ZSU-23-4 Shilka", 3), track("T0002", "BMP-2", 4)])
        self.assertEqual((a.strength, a.track_id, a.candidates), (UNIQUE, "T0001", ("T0001",)))
        self.assertEqual(a.reason, "one unit fits")

    def test_two_of_a_type_is_best_of_n_and_never_shared(self):
        tracks = [track("T0001", "T-72B", 5), track("T0002", "T-72B", 3)]
        a1, a2 = assoc([entry(1, "T-72B"), entry(2, "T-72B")], tracks)
        self.assertEqual((a1.strength, a1.track_id), (BEST_OF_N, "T0002"))          # the nearer one
        self.assertEqual(a1.candidates, ("T0002", "T0001"))
        self.assertEqual((a2.strength, a2.track_id), (BEST_OF_N, "T0001"))
        self.assertEqual(a1.reason, "nearest of 2 that fit")

    def test_more_entries_than_units(self):
        a1, a2 = assoc([entry(1, "T-72B"), entry(2, "T-72B")], [track("T0001", "T-72B", 3)])
        self.assertEqual(a1.track_id, "T0001")
        self.assertEqual((a2.strength, a2.track_id, a2.candidates), (NONE, None, ("T0001",)))

    def test_class_only_gives_no_position(self):
        tracks = [track("T0001", "T-72B", 3), track("T0002", "BMP-2", 4), track("T0003", "Ural-375", 2)]
        a, = assoc([entry(1, "ARMORED", klass="ARMORED")], tracks)
        self.assertEqual((a.strength, a.track_id), (CLASS_ONLY, None))
        self.assertEqual(set(a.candidates), {"T0001", "T0002"})                    # not the truck
        g, = assoc([entry(1, "GROUND", klass="GROUND")], tracks)
        self.assertEqual(len(g.candidates), 3)

    def test_nothing_fits(self):
        a, = assoc([entry(1, "BMP-2")], [track("T0001", "T-72B", 3)])
        self.assertEqual((a.strength, a.track_id, a.reason), (NONE, None, "no unit in the picture fits"))
        n, = assoc([entry(1, "", klass=None)], [track("T0001", "T-72B", 3)])
        self.assertEqual(n.strength, NONE)

    def test_name_differs(self):
        a, = assoc([entry(1, "T-80UD")], [track("T0001", "T-72B", 3)])          # George says T-80, DCS T-72B
        self.assertEqual((a.strength, a.track_id), (BEST_OF_N, "T0001"))           # never "unique"
        self.assertEqual(a.reason, "name differs from the DCS type")

    def test_who_can_be_on_the_list(self):
        tracks = [track("T0001", "T-72B", 20),                                      # beyond 15 km
                  track("T0002", "T-72B", 3, status=Status.GONE),
                  track("T0003", "T-72B", 3, status=Status.OUT_OF_COVERAGE),
                  track("T0004", "T-72B", 3, aff=Affiliation.FRIENDLY),
                  track("T0005", "T-72B", 3, status=Status.STALE)]
        a, = assoc([entry(1, "T-72B")], tracks)
        self.assertEqual((a.strength, a.track_id), (UNIQUE, "T0005"))

    def test_sticky_while_free(self):
        near, far = track("T0001", "T-72B", 3), track("T0002", "T-72B", 5)
        a, = assoc([entry(1, "T-72B")], [near, far], previous={(1, "T-72B"): "T0002"})
        self.assertEqual(a.track_id, "T0002")                                       # keeps its unit
        a, = assoc([entry(1, "T-72B")], [near], previous={(1, "T-72B"): "T0002"})
        self.assertEqual(a.track_id, "T0001")                                       # that unit went
        prev = fusion.previous_of(assoc([entry(1, "T-72B"), entry(2, "BMP-2")], [near, far]))
        self.assertEqual(prev, {(1, "T-72B"): "T0001"})

    def test_list_order_decides_and_reorder_keeps_units(self):
        tracks = [track("T0001", "T-72B", 3), track("T0002", "T-72B", 5), track("T0003", "BMP-2", 4)]
        first = assoc([entry(1, "T-72B"), entry(2, "BMP-2"), entry(3, "T-72B")], tracks)
        self.assertEqual([a.track_id for a in first], ["T0001", "T0003", "T0002"])

    def test_air_by_name_and_class(self):
        mig = track("T0001", "MiG-29S", 8, cat=Category.AIR, alt=3000)
        a, = assoc([entry(1, "MiG-29S")], [mig])
        self.assertEqual(a.track_id, "T0001")
        c, = assoc([entry(1, "AIR", klass="AIR")], [mig, track("T0002", "T-72B", 3)])
        self.assertEqual(c.candidates, ("T0001",))

    def test_no_own_position(self):
        a, = assoc([entry(1, "T-72B")], [track("T0001", "T-72B", 30)], me=None)      # no range filter
        self.assertEqual(a.track_id, "T0001")


if __name__ == "__main__":
    unittest.main()
