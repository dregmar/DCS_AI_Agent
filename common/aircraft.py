"""
common/aircraft.py - settings that depend on what you fly (by DCS type name, Export's "ac").

Helicopters keep the values everything was built with (Outcasts at 60 m above the ground and
45 m/s, distances in kilometres). Jets get jet values: Outcasts and the escort at altitude and
jet speed, wider formation, distances in (nautical) miles, joker / bingo fuel calls from Overlord.

    profile(snap)            -> the Profile for the aircraft in snap ("aircraft")
    say_dist(metres, snap)   -> "12 miles" / "12 kilometres"
    say_alt(metres, snap)    -> "angels 18" (jets, thousands of feet) / "500 feet"

To tune a type, add or change its entry in PROFILES; unknown types fall back to HELO or JET by
name. config.AIRCRAFT_OVERRIDES = {"FA-18C_hornet": {"reserve_lbs": 3500}} changes single values.
"""
from dataclasses import dataclass, field, replace
from typing import Dict, Optional, Tuple

import config

M_PER_NM = 1852.0
FT_PER_M = 3.28084


@dataclass(frozen=True)
class Profile:
    name: str
    kind: str                                   # "helo" or "jet"
    imperial: bool                              # nm / feet in speech (else km)
    outcast_alt_m: float                        # Outcast altitude after the clone
    outcast_alt_type: str                       # "AGL" or "ASL"
    outcast_speed_mps: float
    follow: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)  # n -> (x ahead, y up, z right) m
    escort_follow: bool = False                 # escort flies formation on you (jets) instead of orbiting
    escort_offset: Tuple[float, float, float] = (-600.0, 150.0, 600.0)
    escort_back_m: Optional[float] = None       # escort spawn distance behind you (None: the kind's own)
    fuel_calls: bool = False                    # Overlord's joker / bingo calls
    burn_lbs_hr: float = 0.0                    # cruise burn, for fuel needed to get home
    cruise_kts: float = 0.0
    reserve_lbs: float = 0.0                    # what you want left on landing
    joker_margin_lbs: float = 0.0               # joker = bingo + this
    air_warn_m: Optional[float] = None          # Overlord's pop-up call range (None: 40 km)
    air_close_m: Optional[float] = None         # ... and "bandit, closing" (None: 15 km)
    target_coords: str = "mgrs"                 # JTAC talk-on: "mgrs" (A-10C CDU, helicopters) or "ddm" (lat/long)
    target_grid_digits: int = 4                 # MGRS digits per coordinate (4 = 10 m, 5 = 1 m)

    def follow_offset(self, n: int) -> Tuple[float, float, float]:
        """Formation slot for wingman n: even numbers right, odd left, further out each pair."""
        if n in self.follow:
            return self.follow[n]
        x, _, z = self.follow.get(2, (-60.0, 0.0, 60.0))
        k = max(1, n // 2)
        return (x * k, 0.0, abs(z) * k * (1 if n % 2 == 0 else -1))


HELO = Profile("helicopter", "helo", False, 60.0, "AGL", 45.0,
               follow={2: (-60, 0, 60), 3: (-60, 0, -60), 4: (-120, 0, 120)})

JET = Profile("jet", "jet", True, 6000.0, "ASL", 220.0,
              follow={2: (-150, 0, 300), 3: (-150, 0, -300), 4: (-300, 0, 600)},
              escort_follow=True, escort_back_m=12_000.0,
              fuel_calls=True, burn_lbs_hr=4500.0, cruise_kts=420.0, reserve_lbs=2000.0, joker_margin_lbs=1500.0,
              air_warn_m=50 * 1852.0, air_close_m=20 * 1852.0, target_coords="ddm")

PROFILES: Dict[str, Profile] = {
    "A-10C": replace(JET, name="A-10C", outcast_alt_m=1500.0, outcast_alt_type="ASL", outcast_speed_mps=150.0,
                     follow={2: (-120, 0, 250), 3: (-120, 0, -250), 4: (-240, 0, 500)},
                     escort_offset=(-500.0, 300.0, 500.0), escort_back_m=10_000.0,
                     burn_lbs_hr=2600.0, cruise_kts=300.0, reserve_lbs=1500.0, joker_margin_lbs=1000.0,
                     target_coords="mgrs", target_grid_digits=5),
    "FA-18C_hornet": replace(JET, name="F/A-18C", outcast_alt_m=6000.0, outcast_speed_mps=230.0,
                             burn_lbs_hr=4500.0, cruise_kts=420.0, reserve_lbs=3000.0, joker_margin_lbs=1500.0),
}
PROFILES["A-10C_2"] = replace(PROFILES["A-10C"], name="A-10C II")
PROFILES["A-10A"] = replace(PROFILES["A-10C"], name="A-10A")

# DCS helicopter type names (and prefixes) that keep the helicopter profile
_HELO_PREFIX = ("AH-64", "UH-1", "Mi-8", "Mi-24", "Ka-50", "SA342", "OH58", "OH-58", "CH-47", "UH-60", "AH-1")


def by_type(aircraft: str) -> Profile:
    a = aircraft or ""
    p = PROFILES.get(a)
    if p is None:
        p = HELO if (not a or a.startswith(_HELO_PREFIX)) else JET
    over = (getattr(config, "AIRCRAFT_OVERRIDES", {}) or {}).get(a)
    return replace(p, **over) if over else p


def profile(snap: Optional[dict]) -> Profile:
    return by_type((snap or {}).get("aircraft") or "")


def say_dist(metres: float, snap: Optional[dict] = None, prof: Optional[Profile] = None) -> str:
    """'12 miles' (nautical, jets) / '12 kilometres' (helicopters). One decimal below 10."""
    p = prof or profile(snap)
    v, unit = ((metres / M_PER_NM, "miles") if p.imperial else (metres / 1000.0, "kilometres"))
    s = f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else f"{round(v)}"
    return f"{s} {unit[:-1] if s == '1' else unit}"


def say_alt(metres: float, snap: Optional[dict] = None, prof: Optional[Profile] = None) -> str:
    """Jets: 'angels 18' (thousands of feet; below 1,000 ft: '800 feet'). Helicopters: '500 feet'."""
    p = prof or profile(snap)
    ft = metres * FT_PER_M
    if p.kind == "jet" and ft >= 1000:
        return f"angels {round(ft / 1000)}"
    return f"{round(ft, -2):.0f} feet"
