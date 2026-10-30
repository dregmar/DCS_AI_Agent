"""
common/position.py - "where are we": your grid (MGRS), lat/long and bullseye, for the co-pilot /
George on the intercom and for Overlord on the radio.

    "What's our current grid?" · "say our grid" · "where are we?" · "give me a ten-digit grid"
    "what's our position in lat long?" · "say bullseye" · "Overlord, request our grid"

The grid is worked out here from Export.lua's LatLongAlt (WGS84 -> UTM -> MGRS, the same grid DCS's
F10 map and kneeboard show), so nothing is asked of DCS. Eight digits (10 m) by default; "six",
"eight" or "ten digit" changes that. Bullseye comes from DCS Olympus (/olympus/bullseyes) when it's
running: bearing (true) and range in nautical miles from your coalition's bullseye to you.
"""
import math
import re
from typing import Callable, Optional, Tuple

# ------------------------------------------------------------------ WGS84 -> UTM -> MGRS
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_BANDS = "CDEFGHJKLMNPQRSTUVWXX"           # 8 degrees each from 80 S; X is 12 degrees
_COLS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROWS = "ABCDEFGHJKLMNPQRSTUV"


def utm_zone(lat: float, lon: float) -> int:
    zone = int((lon + 180) // 6) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:          # south-west Norway
        return 32
    if 72 <= lat < 84:                              # Svalbard
        for lo, hi, z in ((0, 9, 31), (9, 21, 33), (21, 33, 35), (33, 42, 37)):
            if lo <= lon < hi:
                return z
    return min(max(zone, 1), 60)


def to_utm(lat: float, lon: float) -> Tuple[int, str, float, float]:
    """(zone, band letter, easting m, northing m)."""
    if not -80 <= lat <= 84:
        raise ValueError("MGRS covers 80 S to 84 N")
    zone = utm_zone(lat, lon)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = _EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (lam - lon0)
    m = _A * ((1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256) * phi
              - (3 * _E2 / 8 + 3 * _E2 ** 2 / 32 + 45 * _E2 ** 3 / 1024) * math.sin(2 * phi)
              + (15 * _E2 ** 2 / 256 + 45 * _E2 ** 3 / 1024) * math.sin(4 * phi)
              - (35 * _E2 ** 3 / 3072) * math.sin(6 * phi))
    east = _K0 * n * (a + (1 - t + c) * a ** 3 / 6
                      + (5 - 18 * t + t ** 2 + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500000.0
    north = _K0 * (m + n * math.tan(phi) * (a ** 2 / 2 + (5 - t + 9 * c + 4 * c ** 2) * a ** 4 / 24
                                             + (61 - 58 * t + t ** 2 + 600 * c - 330 * _EP2) * a ** 6 / 720))
    if lat < 0:
        north += 10000000.0
    band = _BANDS[min(int((lat + 80) // 8), len(_BANDS) - 1)]
    return zone, band, east, north


def to_mgrs(lat: float, lon: float, digits: int = 4) -> Tuple[str, str, str, str]:
    """('38T', 'KM', '1234', '5678') with `digits` per coordinate (1..5)."""
    zone, band, east, north = to_utm(lat, lon)
    col = _COLS[(zone - 1) % 3][int(east // 100000) - 1]
    row = _ROWS[(int(north // 100000) + (5 if zone % 2 == 0 else 0)) % 20]
    e = int(east % 100000) // (10 ** (5 - digits))
    n = int(north % 100000) // (10 ** (5 - digits))
    return f"{zone}{band}", col + row, f"{e:0{digits}d}", f"{n:0{digits}d}"


def from_utm(zone: int, northern: bool, east: float, north: float) -> Tuple[float, float]:
    """UTM -> (lat, lon), WGS84 (Snyder's series; better than a metre)."""
    x, y = east - 500000.0, north if northern else north - 10000000.0
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    n1 = _A / math.sqrt(1 - _E2 * math.sin(phi1) ** 2)
    t1 = math.tan(phi1) ** 2
    c1 = _EP2 * math.cos(phi1) ** 2
    r1 = _A * (1 - _E2) / (1 - _E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * _EP2) * d ** 4 / 24
                                               + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * _EP2 - 3 * c1 ** 2)
                                               * d ** 6 / 720)
    lon = lon0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * _EP2 + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon)


def mgrs_to_ll(gzd: str, square: str, east: str, north: str) -> Tuple[float, float]:
    """('38T', 'LM', '7218', '4018') -> (lat, lon) of the middle of that grid square."""
    zone, band = int(gzd[:-1]), gzd[-1].upper()
    col, row = square[0].upper(), square[1].upper()
    digits = len(east)
    e100 = _COLS[(zone - 1) % 3].index(col) + 1
    n100 = (_ROWS.index(row) - (5 if zone % 2 == 0 else 0)) % 20
    half = 0.5 * 10 ** (5 - digits)                      # the middle of the square named
    e = e100 * 100000 + int(east) * 10 ** (5 - digits) + half
    n_in = n100 * 100000 + int(north) * 10 ** (5 - digits) + half
    # the row letters repeat every 2,000 km: take the cycle that lands in the latitude band
    band_lat = -80 + 8 * _BANDS.index(band) + 4
    northern = band >= "N"
    _, _, _, n_ref = to_utm(max(-79.9, min(83.9, band_lat)), (zone - 1) * 6 - 180 + 3)
    if not northern:
        n_ref = n_ref                                          # to_utm already adds the false northing
    k = round((n_ref - n_in) / 2000000.0)
    return from_utm(zone, northern, e, n_in + k * 2000000.0)


_SPOKEN_DIGIT = {"zero": "0", "oh": "0", "one": "1", "won": "1", "two": "2", "to": "2", "too": "2",
                 "three": "3", "tree": "3", "four": "4", "for": "4", "five": "5", "fife": "5", "six": "6",
                 "seven": "7", "eight": "8", "ate": "8", "nine": "9", "niner": "9"}


def parse_grid(text: str, ref: Optional[Tuple[float, float]] = None):
    """A spoken grid -> (lat, lon, 'MGRS as understood') or None.
    '38T LM 7218 4018', 'thirty eight tango lima mike seven two one eight four zero one eight',
    'Lima Mike 72184018', '721 401' (zone and square taken from `ref`, your position)."""
    t = (text or "").lower().replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    tens = {"ten": 1, "twenty": 2, "thirty": 3, "forty": 4, "fifty": 5, "sixty": 6}
    units = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
    t = re.sub(r"\b(twenty|thirty|forty|fifty|sixty)(?: (one|two|three|four|five|six|seven|eight|nine))?\b",
               lambda m: str(tens[m.group(1)] * 10 + (units[m.group(2)] if m.group(2) else 0)), t)
    letters = {w.lower(): k for k, w in NATO.items()}
    letters.update({"x ray": "X", "xray": "X", "juliett": "J", "alfa": "A"})
    toks = []
    for w in t.split():
        if w in _SPOKEN_DIGIT:
            toks.append(_SPOKEN_DIGIT[w])
        elif w in letters:
            toks.append(letters[w])
        elif w in ("x", "xray"):
            toks.append("X")
        elif w in ("grid", "location", "at", "is", "set", "the", "of", "position", "coordinates", "mgrs",
                   "square", "and", "point", "a"):
            continue
        else:            # "38t", "lm", digits; anything else (a stray word) breaks the grid
            toks.append(w.upper() if re.fullmatch(r"\d{1,2}[a-z]|[a-z]{2}|\d+", w) else "#")
    flat = "".join(toks).split("#")[-1]
    m = re.search(r"(?:(\d{1,2})([C-HJ-NP-X]))?([A-HJ-NP-Z]{2})?(\d{2,10})$", flat)
    if not m or len(m.group(4)) % 2:
        return None
    digits = m.group(4)
    if len(digits) > 10:
        return None
    if ref is None and not (m.group(1) and m.group(3)):
        return None
    if ref is not None:
        gzd0, sq0, _, _ = to_mgrs(ref[0], ref[1], 5)
    gzd = (m.group(1) + m.group(2)) if m.group(1) else gzd0
    sq = m.group(3) or sq0
    h = len(digits) // 2
    try:
        lat, lon = mgrs_to_ll(gzd, sq, digits[:h], digits[h:])
    except (ValueError, IndexError):
        return None
    return lat, lon, f"{gzd} {sq} {digits[:h]} {digits[h:]}"


# ------------------------------------------------------------------ speaking it
NATO = {"A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf",
        "H": "Hotel", "I": "India", "J": "Juliet", "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November",
        "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango", "U": "Uniform",
        "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu"}


def _digits(s: str) -> str:
    return " ".join(s)


def say_mgrs(lat: float, lon: float, digits: int = 4) -> str:
    gzd, sq, e, n = to_mgrs(lat, lon, digits)
    zone, band = gzd[:-1], gzd[-1]
    return f"Grid {zone} {NATO[band]}, {NATO[sq[0]]} {NATO[sq[1]]}, {_digits(e)}, {_digits(n)}."


def mgrs_text(lat: float, lon: float, digits: int = 4) -> str:
    gzd, sq, e, n = to_mgrs(lat, lon, digits)
    return f"{gzd} {sq} {e} {n}"


def say_latlon(lat: float, lon: float) -> str:
    def part(v, pos, neg):
        d = abs(v)
        deg = int(d)
        mins = (d - deg) * 60
        return f"{pos if v >= 0 else neg} {deg} degrees {mins:.1f}"
    return f"{part(lat, 'North', 'South')}, {part(lon, 'East', 'West')}."


def bullseye_call(me: Tuple[float, float], bull: Tuple[float, float]) -> str:
    n = (me[0] - bull[0]) * 111_320.0
    e = (me[1] - bull[1]) * 111_320.0 * math.cos(math.radians((me[0] + bull[0]) / 2))
    brg = math.degrees(math.atan2(e, n)) % 360
    nm = math.hypot(n, e) / 1852.0
    return f"Bullseye {round(brg) % 360:03d} for {round(nm)}."


# ------------------------------------------------------------------ the request
_ASK = re.compile(r"\b(?:what(?:'s| is| are)|whats|say|give(?: me| us)?|read(?: me| us)?|tell(?: me| us)?|"
                  r"request(?:ing)?|confirm|need|report)\b")
_BARE = re.compile(r"^(?:(?:george|copilot|co pilot|okay|ok)\s+)*(?:our |my |current )*"
                   r"(?:grid|position|location|coordinates|bullseye|lat long)$")
# "smoke my position", "land at my position", "hold position": orders, not questions
_ORDER = re.compile(r"\b(?:smoke|mark|store|land|hold|orbit|cas|sead|cap|escort|attack|engage|go to|fly to)\b")
_WHAT = re.compile(r"\b(?:grid|grids|mgrs|m g r s|coordinates?|coords|position|location|lat ?long|latitude|"
                   r"longitude|bullseye|bulls ?eye|bulls)\b")
_WHERE = re.compile(r"\bwhere (?:are we|am i|is my (?:aircraft|helicopter|position))\b")
_BULL = re.compile(r"\bbulls ?eye\b|\bbulls\b")
_LATLON = re.compile(r"\blat ?long|\blatitude|\blongitude|\blat lon\b")
_DIGITS = re.compile(r"\b(six|6|eight|8|ten|10)[\s-]*digits?\b")
_D = {"six": 3, "6": 3, "eight": 4, "8": 4, "ten": 5, "10": 5}


def wanted(text: str) -> bool:
    t = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    if _BARE.match(t) or _WHERE.search(t):
        return True
    return bool(_WHAT.search(t)) and bool(_ASK.search(t)) and not _ORDER.search(t)


def handle(text: str, snap: dict, bullseye: Optional[Callable[[], Optional[Tuple[float, float]]]] = None
           ) -> Optional[str]:
    """What to say, or None if this isn't a position request."""
    t = re.sub(r"[^a-z0-9' -]+", " ", (text or "").lower())
    if not wanted(t):
        return None
    me = snap.get("my_ll")
    if not me or snap.get("telemetry_age", 0.0) > 10.0:
        return "Unable, no position."
    lat, lon = float(me[0]), float(me[1])
    if _BULL.search(t):
        b = bullseye() if bullseye else None
        if b is None:
            return "Unable, no bullseye. Olympus isn't running. " + say_mgrs(lat, lon)
        return bullseye_call((lat, lon), b)
    if _LATLON.search(t):
        return say_latlon(lat, lon)
    m = _DIGITS.search(t)
    return say_mgrs(lat, lon, _D[m.group(1)] if m else 4)


def olympus_bullseye(oly, coalition: str = "blue") -> Optional[Tuple[float, float]]:
    """Your coalition's bullseye from Olympus (/olympus/bullseyes), or None."""
    try:
        if not oly.connect():
            return None
        data = oly.get("/olympus/bullseyes") or {}
    except Exception:
        return None
    rows = data.get("bullseyes", data) if isinstance(data, dict) else {}
    for b in (rows.values() if isinstance(rows, dict) else rows or []):
        if isinstance(b, dict) and str(b.get("coalition", "")).lower() == coalition:
            try:
                return float(b["latitude"]), float(b["longitude"])
            except (KeyError, TypeError, ValueError):
                return None
    return None
