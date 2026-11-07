"""
common/jtac.py - Overlord as JTAC (feature 9): a target talk-on ready for the CDU / UFC.

    "Overlord, request target"            the most dangerous enemy ground unit near you (big radar SAMs
    "... request target north of me"      are left out and called as threats instead), in a direction,
    "... request target, armour"          or of a kind
    "Overlord, next target"               another one
    "Overlord, say again target"          repeats it
    "Overlord, mark target (red smoke)"   smoke on it through Olympus
    "... lase the target (code 1688)"     a laser on it (3 Nov) from the nearest JTAC (a unit named JTAC / FAC /
    "... laser off"                       observer / recon / SOF, 8 km) or drone (20 km slant) that has terrain line
                                          of sight to it (common/jtac_sight.py, george_comms_hook.lua). Kept on
                                          a moving target; off when it's destroyed, gone or out of sight.
    "Overlord, BDA"                       destroyed / still active / can't assess

    -> "Overlord, target ZSU-23-4 Shilka. Grid 40 Romeo, Delta Quebec, 1 2 3 4 5, 6 7 8 9 0.
        Elevation 1,240 feet. 12 miles north east of you. Threats: SA-8, 2 miles north of the target."

Coordinates per aircraft (common/aircraft.py target_coords): MGRS, 10 digits for the A-10C's CDU; lat/long
in degrees and decimal minutes (3 decimals) for the Hornet's UFC. Elevation is the target's own
altitude from Export.lua (a ground unit sits on the ground). The current target is also "my target"
for "Outcast 2, attack / lase my target" and "Overlord, CAS on my target" (main.py).
When the target is destroyed Overlord says so on his own ("good effect").
Thread-safe: orders come on the voice thread, tick() on the profile-watch thread.
"""
import re
import threading
from typing import Callable, List, Optional

from common import aircraft
from common.overlord import (dist_brg, compass, say_type, ground_near, radars_near, is_big_sam,
                             parse_direction, target_state)
from common.position import to_mgrs, NATO
from core.state import stale_reason

FT_PER_M = 3.28084
THREAT_RADIUS_M = 10_000            # air defence this close to the target is called as a threat
DIR_CONE_DEG = 35.0

_REQUEST = re.compile(r"\b(?:request(?:ing)?|give me|need|say|send)\s+(?:a\s+|the\s+|me\s+)?(?:new\s+)?"
                      r"(?:target|tasking|task)\b|\b(?:any|got)\s+(?:a\s+)?targets?\b|^\s*target\s*\??$|"
                      r"\b(?:request(?:ing)?|give me|need)\s+(?:a\s+)?talk[ -]?on\b", re.I)
_NEXT = re.compile(r"\b(?:next|another|new|different|follow[ -]?on)\s+target\b", re.I)
_REPEAT = re.compile(r"\b(?:say again|repeat)\b.*\b(?:target|grid|coordinates|position)\b|"
                     r"\b(?:say|repeat)\s+(?:the\s+)?(?:grid|coordinates)\b", re.I)
# 28 Oct: "Overlord, smoke on my target" went to the router, which made it a SEAD request
_MARK = re.compile(r"\bmark\b.*\btarget\b|\bmark (?:it|with smoke)\b|"
                   r"\bsmoke (?:on )?(?:the |my |our |that )?target\b|\b(?:request(?:ing)?|need) (?:\w+ )?smoke\b", re.I)
# 3 Nov: "mark the target with laser" / "lase the target" gave red smoke: a laser mark is its own order
_LASE = re.compile(r"\b(?:lase|lased|lasing|laze|lazing|lays|lace|lacing|laser|designate|sparkle)\b", re.I)
_LASE_ON = re.compile(r"\b(?:target|it|that|mark|code|on)\b", re.I)
_LASE_STOP = re.compile(r"\b(?:stop|cease|kill|end|terminate|cancel)\s+(?:the\s+)?(?:lasing|lazing|laser|lase|sparkle)\b|"
                        r"\blaser\s+(?:off|stop)\b", re.I)
_CODE = re.compile(r"\bcode (\d) ?(\d) ?(\d) ?(\d)\b|\bcode (\d{4})\b", re.I)
DEFAULT_LASER_CODE = 1688
GROUND_LASER_RANGE_M = 8_000       # a ground JTAC this close (slant) can lase (JTAC_GROUND_LASER_M)
AIR_LASER_RANGE_M = 20_000         # a drone or airborne FAC (JTAC_DRONE_LASER_M)
LOST_SIGHT_CHECKS = 2              # sight checks in a row without line of sight: laser off
MAX_ASKED = 6                      # the nearest in range asked about each lase
_BDA = re.compile(r"\bbda\b|\bb d a\b|\bbattle damage\b|\bdamage assessment\b|\bis (?:it|the target) (?:dead|destroyed)\b",
                  re.I)
_COLORS = ("red", "green", "white", "orange", "blue")


# 4 Nov flight: Whisper heard "lase target" as "layers / please / nears my / least target". Only in a JTAC
# request, and only straight before "target" / "it", these are read as "lase".
_LASE_HEARD = re.compile(r"\b(?:layers?|please|nears|least|vase|phase|faze|lease[ds]?|lays|lace[ds]?|laze[ds]?|leis|lees|"
                         r"lies|leaves|blaze|lane|lanes|lasers)\b(?=\s+(?:on\s+)?(?:the\s+|my\s+|that\s+|our\s+)?"
                         r"(?:target|it)\b)", re.I)


def heard(t: str) -> str:
    """The JTAC request with Whisper's versions of "lase" put right, and its commas / full stops taken out
    (5 Nov: "request, target" and "follow on, target" got "say again"). Question marks stay."""
    t = re.sub(r"\s+", " ", re.sub(r"[,.;:!]+", " ", t or "")).strip()
    return _LASE_HEARD.sub("lase", t)


_LASE_QUESTION = re.compile(r"\?|\b(?:what|what's|whats|which|tell me|confirm|say again)\b|\bcodes\b", re.I)


def lase_wanted(t: str) -> bool:
    """"lase the target", "mark it with laser", "laser on, code 1688", or just "lase" / "sparkle"; never a
    question ("what's the laser code?" is the briefing's)."""
    t = (t or "").strip()
    if not _LASE.search(t) or _LASE_QUESTION.search(t):
        return False
    return bool(_LASE_ON.search(t) or len(re.findall(r"[a-z']+", t.lower())) <= 2)


def laser_range(o) -> float:
    return AIR_LASER_RANGE_M if o.air else GROUND_LASER_RANGE_M


def say_source(o) -> str:
    """"the MQ-9 Reaper" for a drone, "JTAC Hammer" for a named unit."""
    return f"the {say_type(o.type)}" if o.why == "uav" else o.label


def _say_digits(s: str) -> str:
    return " ".join(s)


def say_grid(lat: float, lon: float, digits: int) -> str:
    gzd, sq, e, n = to_mgrs(lat, lon, digits)
    return f"Grid {gzd[:-1]} {NATO[gzd[-1]]}, {NATO[sq[0]]} {NATO[sq[1]]}, {_say_digits(e)}, {_say_digits(n)}."


def ddm(v: float, pos: str, neg: str, decimals: int = 3) -> str:
    d = abs(v)
    deg = int(d)
    mins = round((d - deg) * 60, decimals)
    if mins >= 60:
        deg, mins = deg + 1, 0.0
    return f"{pos if v >= 0 else neg} {deg} degrees {mins:0{3 + decimals}.{decimals}f}"


def say_ddm(lat: float, lon: float) -> str:
    """'North 25 degrees 36.452, East 56 degrees 48.123.' (Hornet UFC precise entry)."""
    return f"{ddm(lat, 'North', 'South')}, {ddm(lon, 'East', 'West')}."


def text_coords(lat: float, lon: float) -> str:
    """For the log / kneeboard: both forms."""
    gzd, sq, e, n = to_mgrs(lat, lon, 5)
    return f"{gzd} {sq} {e} {n} | {ddm(lat, 'N', 'S')}' {ddm(lon, 'E', 'W')}'"


class Jtac:
    def __init__(self, snapshot: Callable[[], dict], oly=None, speak: Optional[Callable[[str], None]] = None,
                 laser=None):
        """oly: common.olympus.Olympus (marking), or None. speak: Overlord's unprompted voice.
        laser: common.olympus_orders.Executor (lase_from / lase_stop), or None."""
        self.snapshot, self.oly, self.speak, self.laser = snapshot, oly, speak, laser
        self.sight = None                           # common.jtac_sight.SightService (main.py)
        self.extra_wanted: Callable[[str], bool] = lambda t: False   # Darkside's 9-line phrases (main.py)
        self.notify = speak                         # "lost sight, laser off" (Darkside's voice in main.py)
        self._lasing = None                         # the Observer whose laser is on
        self._blind = 0                             # sight checks in a row without line of sight
        self._lock = threading.Lock()
        self.current: Optional[dict] = None
        self._given: List[int] = []                 # ids already given this sortie ("next target")
        self._reported_dead = None

    # ------------------------------------------------------------ what was said
    def wanted(self, rest: str) -> bool:
        t = heard(rest)
        return bool(self.extra_wanted(t) or _REQUEST.search(t) or _NEXT.search(t) or _BDA.search(t) or _MARK.search(t)
                    or _LASE_STOP.search(t) or lase_wanted(t)
                    or (_REPEAT.search(t) and self.current is not None))

    def handle(self, rest: str) -> Optional[str]:
        """What Overlord says (the text after "Overlord,"), or None if it isn't a JTAC request."""
        t = heard(rest)
        if _BDA.search(t):
            return self.bda()
        if _LASE_STOP.search(t):
            return self.laser_off()
        if lase_wanted(t):                          # before smoke: "mark the target with laser"
            return self.lase(t)
        if _MARK.search(t):
            return self.mark(t)
        if _REPEAT.search(t) and self.current is not None:
            return self.describe(self.current, self.snapshot())
        if _NEXT.search(t):
            return self.request(t, skip_given=True)
        if _REQUEST.search(t):
            return self.request(t)
        return None

    # ------------------------------------------------------------ choosing
    def candidates(self, snap: dict, text: str = "") -> List[dict]:
        me = snap.get("my_ll")
        if not me:
            return []
        prof = aircraft.profile(snap)
        radius = 37_000 if prof.kind == "jet" else 12_000
        cands = self._ground(snap, (me[0], me[1]), radius)      # big radar SAMs are left out
        d = parse_direction(text.lower())
        if d is not None:
            cands = [c for c in cands
                     if abs((dist_brg((me[0], me[1]), (c["lat"], c["lon"]))[1] - d[0] + 180) % 360 - 180) <= DIR_CONE_DEG]
        from common import targets
        kind = targets.wanted_kind(text)
        if kind is not None:
            from common.unit_types import TYPES
            cands = [c for c in cands if re.search(kind[1], c["type"].lower()) or TYPES.matches_spoken(c["type"], text.lower())]
        return cands                                             # most dangerous first, then nearest

    @staticmethod
    def _ground(snap: dict, here, radius: float) -> List[dict]:
        """R4: hostile ground tracks the picture holds ACTIVE (never a stale or lost one, never a neutral),
        most dangerous first like ground_near; without a current picture, Export's list as before."""
        from tactical import link
        pic = link.hostile_ground_near(here[0], here[1], radius)
        if pic is None:
            cands = ground_near(snap, here, radius)
            alts = {int(g[1]): float(g[4]) for g in snap.get("enemy_ground") or [] if len(g) >= 5}
            for c in cands:
                c["alt"] = alts.get(c["id"])
            return cands
        from common.overlord import threat_weight
        out = [dict(r, w=threat_weight(r["type"]), src="enemy_ground") for r in pic
               if r["id"] is not None and not is_big_sam(r["type"])]
        out.sort(key=lambda r: (-r["w"], r["d"]))
        return out

    def request(self, text: str = "", skip_given: bool = False) -> str:
        snap = self.snapshot()
        if not snap.get("my_ll"):
            return "Overlord, unable, I don't have your position."
        why = stale_reason(snap)                 # no target from a frozen picture (audit step 3)
        if why:
            return f"Overlord, unable, {why}. Say again in a moment."
        cands = self.candidates(snap, text)
        with self._lock:
            given = set(self._given) | ({self.current["id"]} if skip_given and self.current else set())
        if skip_given:
            cands = [c for c in cands if c["id"] not in given]
        if not cands:
            return "Overlord, negative, no " + ("more " if skip_given else "") + "targets for you."
        tgt = dict(cands[0], src="enemy_ground")
        with self._lock:
            self.current = tgt
            if tgt["id"] not in self._given:
                self._given.append(tgt["id"])
            self._reported_dead = None
        print(f"[JTAC] Target {tgt['type']} (id {tgt['id']}): {text_coords(tgt['lat'], tgt['lon'])}"
              + (f", elevation {tgt['alt'] * FT_PER_M:.0f} ft" if tgt.get("alt") is not None else ""))
        return self.describe(tgt, snap)

    @staticmethod
    def _live(tgt: dict) -> dict:
        """The target where it is now: the tactical picture follows a moving unit (step T7); without a
        picture, the position copied when it was assigned."""
        from tactical import link
        from tactical.models import dcs_key
        pos = link.position(dcs_key(tgt.get("id")))
        if pos is None:
            return tgt
        return dict(tgt, lat=pos[0], lon=pos[1], alt=pos[2] if pos[2] is not None else tgt.get("alt"))

    def describe(self, tgt: dict, snap: dict) -> str:
        tgt = self._live(tgt)
        prof = aircraft.profile(snap)
        parts = [f"Overlord, target {say_type(tgt['type'])}."]
        if prof.target_coords == "ddm":
            parts.append(say_ddm(tgt["lat"], tgt["lon"]))
        else:
            parts.append(say_grid(tgt["lat"], tgt["lon"], prof.target_grid_digits))
        if tgt.get("alt") is not None:
            parts.append(f"Elevation {int(round(tgt['alt'] * FT_PER_M, -1)):,} feet.")
        me = snap.get("my_ll")
        if me:
            d, brg = dist_brg((me[0], me[1]), (tgt["lat"], tgt["lon"]))
            parts.append(f"{aircraft.say_dist(d, snap).capitalize()} {compass(brg)} of you.")
        threats = self.threats(tgt, snap)
        if threats:
            parts.append("Threats: " + "; ".join(threats) + ".")
        return " ".join(parts)

    @staticmethod
    def _raw_threats(snap: dict, here) -> List[dict]:
        """Without a current picture: Export's lists, as before R4."""
        from common.unit_types import TYPES
        rows = radars_near(snap, here, THREAT_RADIUS_M) + [
            g for g in ground_near(snap, here, THREAT_RADIUS_M) if TYPES.is_air_defence(g["type"]) or g["w"] >= 90]
        for g in snap.get("enemy_ground") or []:             # big SAMs ground_near leaves out
            if is_big_sam(str(g[0])) and int(g[1]) not in {r["id"] for r in rows}:
                d, _ = dist_brg(here, (float(g[2]), float(g[3])))
                if d <= THREAT_RADIUS_M:
                    rows.append({"type": g[0], "id": int(g[1]), "lat": float(g[2]), "lon": float(g[3]), "d": d})
        return rows

    def threats(self, tgt: dict, snap: dict) -> List[str]:
        """Air defence near the target (SAMs, AAA), nearest first, up to two."""
        from tactical import link
        here = (tgt["lat"], tgt["lon"])
        seen, out = set(), []
        pic = link.threats_near(here[0], here[1], THREAT_RADIUS_M)       # R4: the picture's threat list
        if pic is not None:
            rows = [r for r in pic if re.search(r"SAM|AAA|MANPADS", r.get("category") or "")]
        else:
            rows = self._raw_threats(snap, here)
        for r in sorted(rows, key=lambda r: dist_brg(here, (r["lat"], r["lon"]))[0]):
            if r["id"] == tgt["id"] or r["id"] in seen:
                continue
            seen.add(r["id"])
            d, brg = dist_brg(here, (r["lat"], r["lon"]))
            where = "at the target" if d < 300 else f"{aircraft.say_dist(d, snap)} {compass(brg)} of the target"
            out.append(f"{say_type(r['type'])}, {where}")
            if len(out) == 2:
                break
        return out

    # ------------------------------------------------------------ mark, BDA
    def mark(self, text: str) -> str:
        with self._lock:
            tgt = self.current
        if tgt is None:
            return "Overlord, no target to mark. Request a target first."
        if self.oly is None or not getattr(self.oly, "connect", lambda: False)():
            return "Overlord, unable to mark, Olympus isn't running."
        tgt = self._live(tgt)                        # smoke where it is now, not where it was assigned
        color = next((c for c in _COLORS if re.search(r"\b" + c + r"\b", text.lower())), "red")
        ok, why = self.oly.command("smoke", {"color": color, "location": {"lat": tgt["lat"], "lng": tgt["lon"]}})
        if not ok:
            return f"Overlord, unable to mark, {why}."
        return f"Overlord, {color} smoke on the target."

    def candidates_in_range(self, tgt: dict) -> Optional[list]:
        """Laser sources within their range of the target (horizontal, first cut), nearest first, as
        (Observer, metres); None if DCS hasn't sent the JTAC list."""
        obs = self.sight.observers() if self.sight is not None else None
        if obs is None:
            return None
        out = []
        for o in obs:
            d = dist_brg((o.lat, o.lon), (tgt["lat"], tgt["lon"]))[0]
            if d <= laser_range(o):
                out.append((o, d))
        out.sort(key=lambda od: od[1])
        return out[:MAX_ASKED]

    def where(self, target_id) -> Optional[tuple]:
        """For the laser tracker (every 2 s): where the target is now; None once it's destroyed, gone, or
        the JTAC has lost sight of it (then he says so)."""
        with self._lock:
            tgt, src = self.current, self._lasing
        if tgt is None or tgt.get("id") != target_id:
            return None
        snap = self.snapshot()
        if target_state(snap, tgt) == "dead":
            return None
        from command.rules import position_of
        pos = position_of(snap, target_id)
        if pos is None or src is None or self.sight is None:
            return pos
        got = self.sight.query(target_id, [src.name], timeout=1.0)
        if got is None:                             # no answer: keep lasing, ask again next time
            return pos
        if not got.found:                           # DCS has no such unit any more: destroyed before the picture
            with self._lock:                        # marks it (4 Nov: that was called "lost sight"); the kill
                if self._lasing is src:             # call follows from tick()
                    self._lasing, self._blind = None, 0
            print(f"[JTAC] Target {target_id} no longer in DCS: laser off.")
            return None
        row = got.rows.get(src.name)
        down = row is not None and row[0] == "gone"
        blind = row is None or row[0] != "1" or row[1] > laser_range(src)
        with self._lock:
            if self._lasing is not src:
                return pos
            self._blind = self._blind + 1 if blind else 0
            off = down or self._blind >= LOST_SIGHT_CHECKS
            if off:
                self._lasing, self._blind = None, 0
        if not off:
            return pos
        who = say_source(src)
        why = f"{who} is down" if down else f"{who} lost sight of the target"
        print(f"[JTAC] {why}: laser off.")
        if self.notify is not None:
            self.notify(f"Overlord, {why}, laser off.")
        return None

    def laser_pick(self, tgt: dict):
        """The nearest JTAC / drone in range with line of sight to tgt: ((Observer, metres), "") or
        (None, why not - a sentence ending with a full stop). Asks DCS (up to 1.5 s): not on the main thread."""
        cands = self.candidates_in_range(tgt)
        if cands is None:
            return None, "DCS hasn't sent the JTAC list. Is the comms hook up to date?"
        if not cands:
            return None, (f"no JTAC within {GROUND_LASER_RANGE_M / 1000:g} km or drone within "
                          f"{AIR_LASER_RANGE_M / 1000:g} km of the target. Say mark target for smoke.")
        got = self.sight.query(tgt["id"], [o.name for o, _ in cands])
        if got is None:
            return None, "no line of sight answer from DCS."
        if not got.found:
            return None, "DCS can't find the target."
        pick = next(((o, d) for o, d in cands
                     if got.rows.get(o.name, ("0", 0.0))[0] == "1" and got.rows[o.name][1] <= laser_range(o)), None)
        if pick is not None:
            return pick, ""
        live = [o for o, _ in cands if got.rows.get(o.name, ("gone", 0.0))[0] != "gone"]
        if not live:
            return None, "no JTAC or drone left near the target."
        if len(live) == 1:
            return None, f"{say_source(live[0])} has no line of sight to the target."
        return None, f"none of the {len(live)} JTACs in range can see the target."

    def lase(self, text: str) -> str:
        with self._lock:
            tgt = self.current
        if tgt is None:
            return "Overlord, no target to lase. Request a target first."
        if self.laser is None or self.oly is None or not getattr(self.oly, "connect", lambda: False)():
            return "Overlord, unable to lase, Olympus isn't running."
        snap = self.snapshot()
        if target_state(snap, tgt) == "dead":
            return "Overlord, the target's destroyed. Request next target."
        tgt = self._live(tgt)
        pick, why = self.laser_pick(tgt)
        if pick is None:
            return f"Overlord, unable to lase, {why}"
        m = _CODE.search(text or "")
        code = int("".join(g for g in m.groups() if g)) if m else DEFAULT_LASER_CODE
        self.laser_off(quiet=True)
        src, d = pick
        with self._lock:
            self._lasing, self._blind = src, 0
        why = self.laser.lase_from(src.id, tgt, code, where=self.where)
        if why:
            with self._lock:
                self._lasing = None
            return f"Overlord, unable to lase, {why[len('Unable, '):] if why.startswith('Unable, ') else why}"
        brg = dist_brg((tgt["lat"], tgt["lon"]), (src.lat, src.lon))[1]
        return (f"Overlord, laser on the target, code {_say_digits(str(code))}, from {say_source(src)}, "
                f"{aircraft.say_dist(d, snap)} {compass(brg)} of the target.")

    def laser_off(self, quiet: bool = False) -> str:
        with self._lock:
            src, self._lasing = self._lasing, None
        if src is None or self.laser is None:
            return "" if quiet else "Overlord, no laser on."
        on = src.id in self.laser.lasing()
        why = self.laser.lase_stop([{"id": src.id}]) if on else ""
        if quiet:
            return ""
        if why:
            return f"Overlord, {why[0].lower() + why[1:]}"
        return "Overlord, laser off."

    def bda(self) -> str:
        snap = self.snapshot()
        with self._lock:
            tgt = self.current
        if tgt is None:
            return "Overlord, no target assigned."
        why = stale_reason(snap)
        if why:
            return f"Overlord, unable to assess, {why}."
        st = target_state(snap, tgt)
        if st == "dead":
            with self._lock:
                self._reported_dead = tgt["id"]
            return f"Overlord, BDA, {say_type(tgt['type'])} destroyed. Good effect. Request next target when ready."
        if st == "alive":
            return f"Overlord, BDA, {say_type(tgt['type'])} still active."
        return "Overlord, unable to assess, the target's out of my picture."

    def active(self) -> bool:
        with self._lock:
            return self.current is not None

    def my_target(self) -> Optional[dict]:
        """The current target while it's alive (for "attack / lase / CAS on my target")."""
        with self._lock:
            tgt = self.current
        if tgt is None or target_state(self.snapshot(), tgt) == "dead":
            return None
        return dict(self._live(tgt), jtac=True)

    def tick(self) -> Optional[str]:
        """Calls the target destroyed once, on his own."""
        with self._lock:
            tgt, said = self.current, self._reported_dead
        if tgt is None or said == tgt["id"] or self.speak is None:
            return None
        if target_state(self.snapshot(), tgt) != "dead":
            return None
        with self._lock:
            self._reported_dead = tgt["id"]
        print(f"[JTAC] Target {tgt['id']} {tgt['type']} called destroyed: {self._why_dead(tgt)}")
        text = f"Overlord, good effect, {say_type(tgt['type'])} destroyed."
        self.speak(text)
        return text

    @staticmethod
    def _why_dead(tgt: dict) -> str:
        """For the log (4 Nov: an SA-11 called destroyed came back 10 minutes later): what decided it."""
        from tactical import link
        from tactical.models import dcs_key
        p = link._get()
        pic = p.picture() if p is not None else None
        if pic is None:
            return "Export's lists (no current picture)"
        t = next((x for x in pic.tracks if x.key == dcs_key(tgt.get("id"))), None)
        if t is None:
            return "not in the picture; Export's lists"
        return f"picture track {t.track_id} {t.status.value}: {t.status_reason}"

    def reset(self) -> None:
        with self._lock:
            self.current, self._given, self._reported_dead = None, [], None
