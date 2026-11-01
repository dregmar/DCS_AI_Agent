"""Hand-made tracks and George entries for the tactical tests (no DCS data needed)."""
import math

from tactical.models import (Affiliation, Basis, Category, Position, Report, SourceMark, Status, Track, Velocity,
                             existence_conf, identity_conf)

ME = (42.0, 42.0)
KM_LAT = 1000 / 111_320.0
KM_LON = 1000 / (111_320.0 * math.cos(math.radians(42.0)))


def track(tid, typ, km_north=0.0, km_east=0.0, status=Status.ACTIVE, aff=Affiliation.HOSTILE,
          cat=Category.GROUND, vel=None, alt=300.0, reason="in the DCS world scan"):
    pos = Position(ME[0] + km_north * KM_LAT, ME[1] + km_east * KM_LON, alt, Basis.WORLD, 100.0, 1000.0)
    v = Velocity(vel[0], vel[1], None, Basis.INFERRED, 99.0, 100.0) if vel else None
    return Track(track_id=tid, key="dcs:" + tid[1:], generation=0, category=cat, affiliation=aff, type_name=typ,
                 display=typ, position=pos, velocity=v, first_seen_mono=1000.0, last_seen_mono=1000.0,
                 last_update_mono=1000.0, sources=(SourceMark("world.gnd", Basis.WORLD, 1000.0, 1000.0, 1),),
                 exist_conf=existence_conf(status), ident_conf=identity_conf(Basis.WORLD), status=status,
                 status_reason=reason)


def entry(i, name, klass=None):
    """A George list entry as tactical/sources.py george_reports makes it."""
    is_class = klass is not None or not name
    return Report(source="george.list", basis=Basis.SENSOR, key=None, relation=None, category=Category.UNKNOWN,
                  type_name=None if is_class else name, klass=klass, coalition=None, position=None,
                  seen_model=None, seen_mono=1000.0, age_known=True, generation=0, flags=frozenset({"no_position"}),
                  extra=(("complete", True), ("list_index", i), ("name", name)))


ATTRS = {
    "T-72B": {"Tanks", "Armored vehicles", "HeavyArmoredUnits"},
    "T-80UD": {"Tanks", "Armored vehicles", "HeavyArmoredUnits"},
    "BMP-2": {"IFV", "Armored vehicles", "Infantry carriers"},
    "Osa 9A33 ln": {"SAM TR", "SR SAM", "Air Defence", "RADAR_BAND1_FOR_ARM"},
    "ZSU-23-4 Shilka": {"AAA", "Mobile AAA", "Air Defence"},
    "Ural-375": {"Trucks", "Unarmed vehicles"},
    "MiG-29S": {"Fighters", "Planes", "Air"},
}


def attrs(name):
    return frozenset(ATTRS.get(name, ()))
