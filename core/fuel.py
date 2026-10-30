"""
core/fuel.py - fuel from LoGetEngineInfo, in pounds, per aircraft.

Best: straight from DCS through george_comms_hook.lua: Unit:getFuel() (a fraction of a full
internal load, above 1 with external tanks) x the airframe's own fuelMassMax (from_dcs). Right for
any aircraft and loadout. 24 Oct: hook 0.555 x 1,140 kg = 1,395 lb, cockpit 1,380.

Fallback, LoGetEngineInfo (when the hook can't reach the mission scripting):
  * AH-64D: the same fraction of a full internal load (0.5523 a minute after the hook's 0.5553).
    Times the max the hook last gave, else 1,140 kg (the AH-64D's fuelMassMax). Earlier versions
    took it for a 0..1 fraction ("126 percent"), thousands of pounds, then tonnes: all wrong.
  * most others: kilograms.
  * a few: a 0..1 fraction (spoken as percent).
"""

KG_TO_LB = 2.20462
DCS_FUEL_FRESH_SEC = 15.0
APACHE_MAX_KG = 1140.0           # the AH-64D's fuelMassMax as DCS reported it (24 Oct)


def from_dcs(snap: dict):
    """(pounds, with external tanks?) from the comms hook's Unit:getFuel() x fuelMassMax, when it is
    fresh and for your own aircraft; else None (then LoGetEngineInfo below is used)."""
    got = snap.get("dcs_fuel")
    if not got or snap.get("dcs_fuel_age", float("inf")) > DCS_FUEL_FRESH_SEC:
        return None
    frac, max_kg, unit, source = (tuple(got) + ("",))[:4]
    mine = snap.get("my_unit") or ""
    # Export's unit name is sometimes the PILOT's name ("RedArcher", 24 Oct), so a unit DCS itself
    # calls the player's is taken as yours.
    if max_kg <= 0 or frac < 0 or (source != "player" and mine and unit and unit != mine):
        return None
    return frac * max_kg * KG_TO_LB, frac > 1.001


def say_dcs_fuel(lbs: float, external: bool) -> str:
    return f"Fuel {round(lbs, -1):.0f} pounds" + (", with the external tanks." if external else ".")


def fuel_lbs(aircraft: str, internal: float, external: float = 0.0, max_kg: float = None):
    """Pounds, or None when the module gives only a fraction.
    max_kg: the airframe's fuelMassMax when the hook has reported it (makes a fraction exact)."""
    total = (internal or 0.0) + (external or 0.0)
    if (aircraft or "").upper().startswith("AH-64"):
        return total * (max_kg or APACHE_MAX_KG) * KG_TO_LB
    if max_kg and total <= 5.0:                 # a fraction, and we know this airframe's max
        return total * max_kg * KG_TO_LB
    if internal <= 1.5 and (external or 0.0) <= 1.5:
        return None
    return total * KG_TO_LB


def known_max_kg(snap: dict):
    """The airframe's fuelMassMax from the hook's last report (any age), or None."""
    got = snap.get("dcs_fuel")
    if not got or got[1] <= 0 or snap.get("dcs_fuel_age", float("inf")) > 600:   # not another airframe's
        return None
    return got[1]


def say_fuel(aircraft: str, internal: float, external: float = 0.0, max_kg: float = None) -> str:
    lbs = fuel_lbs(aircraft, internal, external, max_kg)
    if lbs is None:
        return f"Fuel {internal * 100:.0f} percent."
    return f"Fuel {round(lbs, -1):.0f} pounds."


def current_lbs(snap: dict):
    """Fuel on board in pounds (DCS reading first, then Export), or None when unknown or a fraction."""
    got = from_dcs(snap)
    if got is not None:
        return got[0]
    raw = snap.get("fuel_raw")
    if not raw:
        return None
    return fuel_lbs(snap.get("aircraft") or "", raw[0], raw[1], known_max_kg(snap))
