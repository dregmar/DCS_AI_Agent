"""
common/nine_line.py - Darkside's CAS 9-line (5 Nov 2026; Andre's choices: no readback by default,
NINE_LINE_READBACK to require one; IP = a mission waypoint named IP or Alpha, else computed; spoken to you,
NINE_LINE_AI_TO_AI to brief the Hog flights too).

    1 IP            a waypoint on your route named "IP" (first) or "Alpha" (comms hook WPNAMES), or a waypoint
                    you named in flight ("mark Alpha"), within IP_MAX_M of the target; else computed: IP_M out,
                    away from known air defence, else towards you (common/overlord.cas_ip, as the Hogs use)
    2 heading       IP to target, magnetic (Export.lua's variation), else said as true
    3 distance      IP to target, in your aircraft's units
    4 elevation     the target's, feet MSL
    5 description   "two CHAP T90Ms": the same type within GROUP_M of the target counted
    6 location      MGRS at your aircraft's precision, or lat / long (jets), as the talk-on
    7 mark          laser and code if a JTAC or drone can see it (terrain line of sight), else smoke if
                    Olympus is up, else none
    8 friendlies    the nearest within FRIENDS_M of the target: type, direction, metres; "none within 3 km";
                    "unknown" when nobody reports them
    9 egress        away from the threats, else back towards the IP
    remarks         danger close, threats, a no-strike zone close by (or the target inside one)

Fixed templates only, built from measured data; nothing invented. Says "unknown" where the data is missing.
"""
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from common import aircraft
from common.overlord import cas_ip, compass, dist_brg, say_type

READBACK = False                    # NINE_LINE_READBACK (main.py)
AI_TO_AI = False                    # NINE_LINE_AI_TO_AI (main.py)
IP_MAX_M = 40_000                   # a named IP farther than this from the target isn't used
GROUP_M = 300.0                     # the same type this close to the target counts in line 5
FRIENDS_M = 3_000.0                 # line 8 looks this far
ZONE_NOTE_M = 3_000.0               # a no-strike zone edge this close is a remark
FT_PER_M = 3.28084

_NUM = {1: "single", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
_IP_NAME = re.compile(r"^\s*(?:ip|i\.p\.)\b", re.I)
_ALPHA_NAME = re.compile(r"^\s*alpha\b", re.I)


@dataclass
class NineLine:
    ip: str
    heading: str
    distance: str
    elevation: str
    description: str
    location: str
    mark: str
    friendlies: str
    egress: str
    remarks: List[str] = field(default_factory=list)
    elevation_ft: Optional[int] = None        # for the readback check
    location_digits: str = ""
    mark_kind: str = "none"                   # "laser" | "smoke" | "none"
    laser_code: int = 1688
    danger_close: bool = False
    no_strike: str = ""                       # the zone the target is inside, if any
    ip_lat: Optional[float] = None            # where line 1 is (the kneeboard map draws it)
    ip_lon: Optional[float] = None
    ip_map: str = "IP"                        # its map label: "IP · wp 3" / "IP Alpha · wp 4" / "IP (computed)"

    def lines(self) -> List[str]:
        return [f"One, {self.ip}.", f"Two, {self.heading}.", f"Three, {self.distance}.", f"Four, {self.elevation}.",
                f"Five, {self.description}.", f"Six, {self.location}.", f"Seven, {self.mark}.",
                f"Eight, {self.friendlies}.", f"Nine, {self.egress}."]

    def text(self) -> str:
        out = "nine-line. " + " ".join(self.lines())
        if self.remarks:
            out += " Remarks: " + " ".join(r if r.endswith(".") else r + "." for r in self.remarks)
        return out


# ------------------------------------------------------------------ pieces
def _ll_of_xz(snap: dict, north_m: float, east_m: float) -> Optional[Tuple[float, float]]:
    """A mission point (DCS x north, z east) as lat / long, offset from your own position."""
    pos, me = snap.get("pos"), snap.get("my_ll")
    if not pos or not me:
        return None
    from common import geo
    return geo.offset_ll(float(me[0]), float(me[1]), north_m - float(pos[0]), east_m - float(pos[1]))


def named_ip(snap: dict, target: Tuple[float, float]) -> Optional[Tuple[Tuple[float, float], str]]:
    """((lat, lon), "IP" / "Alpha" / the waypoint's name) or None."""
    names = snap.get("route_names") or []
    for rx in (_IP_NAME, _ALPHA_NAME):
        for n, x, z, name in names:
            if rx.search(str(name)):
                ll = _ll_of_xz(snap, float(x), float(z))
                if ll is not None and dist_brg(ll, target)[0] <= IP_MAX_M:
                    label = str(name).strip()
                    return ll, f"{label if rx is _IP_NAME else 'IP ' + label}, waypoint {int(n)}"
    try:
        from common.user_waypoints import USER_WAYPOINTS
        for name in ("ip", "alpha"):
            ll = USER_WAYPOINTS.ll(name, snap)
            if ll is not None and dist_brg(ll, target)[0] <= IP_MAX_M:
                return (float(ll[0]), float(ll[1])), name.upper() if name == "ip" else "Alpha"
    except Exception:
        pass
    return None


def magnetic(true_brg: float, snap: dict) -> str:
    mv = snap.get("magvar")
    if isinstance(mv, (int, float)):
        return f"{int(round((true_brg - mv) % 360)) or 360:03d} magnetic"
    return f"{int(round(true_brg % 360)) or 360:03d} true"


def _same_type_count(snap: dict, tgt: dict) -> int:
    from tactical import link
    from common.overlord import ground_near
    rows = link.hostile_ground_near(tgt["lat"], tgt["lon"], GROUP_M)
    if rows is None:
        rows = ground_near(snap, (tgt["lat"], tgt["lon"]), GROUP_M)
    n = sum(1 for r in rows if str(r.get("type")) == str(tgt.get("type")))
    return max(1, n)


def describe(snap: dict, tgt: dict) -> str:
    n = _same_type_count(snap, tgt)
    name = say_type(tgt["type"])
    if n == 1:
        return f"single {name}"
    return f"{_NUM.get(n, str(n))} {name}{'' if name.endswith('s') else 's'}"


def location(snap: dict, tgt: dict) -> Tuple[str, str]:
    """(spoken, its digits for the readback) in the talk-on's format."""
    from common.jtac import say_ddm, say_grid
    prof = aircraft.profile(snap)
    if prof.target_coords == "ddm":
        said = say_ddm(tgt["lat"], tgt["lon"])
    else:
        said = say_grid(tgt["lat"], tgt["lon"], prof.target_grid_digits)
    digits = "".join(re.findall(r"\d", said.split(",", 1)[-1] if prof.target_coords != "ddm" else said))
    return said.rstrip("."), digits


def friendlies(snap: dict, tgt: dict, danger_m: float) -> Tuple[str, bool]:
    from command import rules
    near = rules.friendlies_near(snap, tgt["lat"], tgt["lon"], FRIENDS_M)
    if near is None:
        return "friendlies unknown", False
    if not near:
        return f"no friendlies within {FRIENDS_M / 1000:g} kilometres", False
    f = near[0]
    pos = (f["lat"], f["lon"]) if f.get("lat") is not None else None
    way = f", {compass(dist_brg((tgt['lat'], tgt['lon']), pos)[1])}" if pos else ""
    return f"friendly {say_type(f['type'])}{way}, {f['d']:.0f} metres", f["d"] < danger_m


def egress(snap: dict, tgt: dict, ip: Tuple[float, float]) -> str:
    from tactical import link
    from common.overlord import radars_near
    here = (tgt["lat"], tgt["lon"])
    rows = link.threats_near(here[0], here[1], 15_000)
    if rows is None:
        rows = radars_near(snap, here, 15_000)
    if rows:
        lat0 = math.radians(here[0])
        n = sum(r["lat"] - here[0] for r in rows)
        e = sum((r["lon"] - here[1]) * math.cos(lat0) for r in rows)
        if n or e:
            away = (math.degrees(math.atan2(e, n)) + 180.0) % 360
            return f"egress {compass(away)}, away from the threats"
    return f"egress {compass(dist_brg(here, ip)[1])}, back towards the IP"


def zone_remark(zones: list, tgt: dict) -> Tuple[str, str]:
    """(remark, the zone the target is inside or '')."""
    from command import rules
    inside = rules.zone_at(zones or [], tgt["lat"], tgt["lon"])
    if inside:
        return f"Target is inside the {inside['name']} no-strike zone, not cleared", inside["name"]
    best = None
    for z in zones or []:
        try:
            d, brg = dist_brg((tgt["lat"], tgt["lon"]), (float(z["lat"]), float(z["lon"])))
            edge = d - float(z.get("radius_m", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if edge <= ZONE_NOTE_M and (best is None or edge < best[0]):
            best = (edge, brg, z["name"])
    if best is None:
        return "", ""
    return (f"{best[2]} no-strike zone {max(0, best[0]) / 1000:.1f} kilometres {compass(best[1])} of the target",
            "")


def mark(jtac, tgt: dict) -> Tuple[str, str, int]:
    """(spoken, kind, code): laser if a JTAC / drone sees the target, else smoke with Olympus, else none."""
    from common.jtac import DEFAULT_LASER_CODE, say_source, _say_digits
    if getattr(jtac, "laser", None) is not None and getattr(jtac, "sight", None) is not None:
        try:
            pick, _ = jtac.laser_pick(tgt)
        except Exception:
            pick = None
        if pick is not None:
            return (f"laser, code {_say_digits(str(DEFAULT_LASER_CODE))}, from {say_source(pick[0])}", "laser",
                    DEFAULT_LASER_CODE)
    oly = getattr(jtac, "oly", None)
    if oly is not None and getattr(oly, "connect", lambda: False)():
        return "red smoke", "smoke", DEFAULT_LASER_CODE
    return "no mark", "none", DEFAULT_LASER_CODE


def build(snap: dict, tgt: dict, jtac, zones: list, danger_m: float,
          ip_override: Optional[Tuple[float, float]] = None) -> NineLine:
    """The 9-line for tgt (a live JTAC target dict). ip_override: a Hog flight's own IP."""
    here = (tgt["lat"], tgt["lon"])
    me = snap.get("my_ll")
    ip_map = "IP (computed)"
    if ip_override is not None:
        ip_ll, ip_said = ip_override, None
    else:
        got = named_ip(snap, here)
        ip_ll, ip_said = (got[0], got[1] if got[1].upper().startswith("IP") else f"IP {got[1]}") if got else \
            (cas_ip(snap, here, (me[0], me[1]) if me else None), None)
        if got:
            ip_map = ip_said.replace(", waypoint ", " · wp ")
    d, brg = dist_brg(ip_ll, here)
    if ip_said is None:
        loc_ip, _ = location(snap, {"lat": ip_ll[0], "lon": ip_ll[1]})
        ip_said = f"IP {aircraft.say_dist(dist_brg(here, ip_ll)[0], snap)} {compass(dist_brg(here, ip_ll)[1])} " \
                  f"of the target, {loc_ip}"
    elev_ft = int(round(tgt["alt"] * FT_PER_M, -1)) if tgt.get("alt") is not None else None
    loc_said, loc_digits = location(snap, tgt)
    mark_said, mark_kind, code = mark(jtac, tgt)
    friends, close = friendlies(snap, tgt, danger_m)
    remarks = []
    if close:
        remarks.append("Danger close")
    threats = []
    try:
        threats = jtac.threats(tgt, snap)
    except Exception:
        pass
    if threats:
        remarks.append("Threats: " + "; ".join(threats))
    zone_said, inside = zone_remark(zones, tgt)
    if zone_said:
        remarks.append(zone_said)
    return NineLine(ip=ip_said, heading=magnetic(brg, snap), distance=aircraft.say_dist(d, snap),
                    elevation=f"{elev_ft:,} feet" if elev_ft is not None else "elevation unknown",
                    description=describe(snap, tgt), location=loc_said, mark=mark_said, friendlies=friends,
                    egress=egress(snap, tgt, ip_ll), remarks=remarks, elevation_ft=elev_ft,
                    location_digits=loc_digits, mark_kind=mark_kind, laser_code=code, danger_close=close,
                    no_strike=inside, ip_lat=round(float(ip_ll[0]), 6), ip_lon=round(float(ip_ll[1]), 6),
                    ip_map=ip_map)


def readback_ok(nine: NineLine, said: str) -> Tuple[bool, str]:
    """Lines 4 and 6 read back: the elevation's digits and the location's digits, in order, anywhere in
    what you said (Whisper writes "1,220" or "1 2 2 0"). (ok, what was wrong)."""
    digits = "".join(re.findall(r"\d", said or ""))
    wrong = []
    if nine.elevation_ft is not None and str(nine.elevation_ft) not in digits:
        wrong.append(f"line 4 is {nine.elevation}")
    if nine.location_digits and nine.location_digits not in digits:
        wrong.append(f"line 6 is {nine.location}")
    return (not wrong), ", ".join(wrong)
