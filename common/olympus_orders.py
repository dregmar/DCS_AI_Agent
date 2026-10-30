"""
common/olympus_orders.py
Wingman orders the DCS radio menu can't give, sent through DCS Olympus (common/olympus.py):

    "two, attack my target" · "three, take out the ZSU at two o'clock" · "flight, hit the trucks"
    "two, follow me" · "three, stay on my left at 200 metres" · "flight, on me"
    "two, go to waypoint 3" · "two, land next to me" · "three, land at waypoint 2"
    "two, weapons hold" · "flight, weapons free" · "three, return fire only"
    "two, lase my target" · "two, lase the tank, code 1688" · "two, stop lasing"
    "two, mark my target with smoke" · "pop smoke" / "smoke on me" (green on you)

Targets come from Export.lua's contacts (DCS object id = Olympus's unit id); wingmen are your
flight's units Export.lua sees (their object ids). George stays quiet when Olympus took the order.
"""
import math
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from common import geo, targets

DEFAULT_LASER_CODE = 1688
SPOT_UPDATE_SEC = 2.0

# DCS AI option ROE values (Olympus passes them through): 0 weapon free, 3 return fire, 4 hold
ROE = {"free": 0, "return": 3, "hold": 4}
# Follow offsets (DCS 'Follow' task: x ahead, y up, z right, metres) by flight position
FOLLOW_DEFAULT = {2: (-60, 0, 60), 3: (-60, 0, -60), 4: (-120, 0, 120)}

_N = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|zero)"
_NUMW = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}

# "atop" / "a tack": Whisper's "attack" (22 Oct)
_ATTACK = re.compile(r"\b(?:attack|atop|a tack|attach|engage|hit|kill|take out|destroy|shoot|light up|go after|get)\b")
_MY_TARGET = re.compile(r"\bmy (?:current |present |)(?:target|tgt|designation)\b")
_ALL = re.compile(r"(?<!ground )(?<!air )\btargets\b|\b(?:everything|all of them|them all|the convoy|the group|what you see)\b")
_FOLLOW = re.compile(r"\b(?:follow me|on me|stay (?:on|with) me|stick with me|tighten up|close up|"
                     r"stay on my (?:left|right|wing)|(?:take|fly) (?:my )?(?:left|right) (?:wing|side))\b")
_SIDE = re.compile(r"\b(left|right)\b")
_DIST = re.compile(r"\b(\d+(?:\.\d+)?) ?(m|meters?|metres?|feet|ft|km|kilometers?|kilometres?|klicks?)\b")
# "three, move in front, 1 kilometer" / "four, move forward 500 metres" / "two, stay behind me"
_FORE = re.compile(r"\b(?:move|go|get|fly|stay|push|take position|position yourself|lead)\b.*?"
                   r"\b(in front|front|ahead|forward|behind|back|trail)\b")
_GOTO = re.compile(r"\b(?:go|proceed|fly|head|move|push|continue) (?:to |on to |for )?(?:waypoint|wp|steerpoint) " + _N + r"\b")
_LAND_ME = re.compile(r"\bland (?:next to me|by me|beside me|with me|here|on me|near me)\b")
_LAND_WP = re.compile(r"\bland (?:at |on )?(?:waypoint|wp) " + _N + r"\b")
_ROE_HOLD = re.compile(r"\b(?:weapons hold|weapons tight|hold (?:your )?fire|hold weapons)\b")
_ROE_FREE = re.compile(r"\b(?:weapons free|free fire|fire at will|cleared hot|weapons hot)\b")
_ROE_RETURN = re.compile(r"\b(?:return fire|weapons return)\b")
# Whisper: "stop blazing", "stop losing", "stop lase in"
_LASE_STOP = re.compile(r"\bstop (?:the )?(?:lasing|lazing|blazing|losing|lacing|lasering|laser|lase|lays?ing)\b|"
                        r"\b(?:laser off|cease lasing|kill the laser|laser stop)\b")
# 28 Oct: small.en wrote "Outcast 2 lays my target"
_LASE = re.compile(r"\b(?:lase|laser|designate|spot|lays|laze|lace|lase up)\b")
_CODE = re.compile(r"\bcode (\d) ?(\d) ?(\d) ?(\d)\b|\bcode (\d{4})\b")
_SMOKE_ME = re.compile(r"\b(?:pop smoke|smoke on me|smoke (?:my position|the lz|here)|mark (?:my position|the lz|me))\b")
_SMOKE_TGT = re.compile(r"\b(?:smoke|mark)\b")
_COLORS = ("green", "red", "white", "orange", "blue")


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUMW[tok]


def parse(rest: str) -> Optional[Tuple[str, dict]]:
    """What follows the recipient -> (kind, params) or None. rest is lower-case, no punctuation."""
    t = rest
    if _LASE_STOP.search(t):
        return "lase_stop", {}
    # "attack target 3" / "engage target number two": an entry of George's target list
    m = re.search(r"\btarget (?:number )?(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", t)
    if _ATTACK.search(t) and m:
        return "attack", {"which": "list", "n": _num(m.group(1)), "text": t}
    # "engage my laser target" / "attack the lased target" (22 Oct): an attack on my target, not a lase
    if _ATTACK.search(t) and re.search(r"\b(?:my |the )?(?:laser|lased|lase|lasing|spotted) target\b", t):
        return "attack", {"which": "mine", "text": "my target"}
    color = next((c for c in _COLORS if re.search(r"\b" + c + r"\b", t)), None)
    if _SMOKE_ME.search(t):
        return "smoke_me", {"color": color or "green"}
    if _SMOKE_TGT.search(t) and (_MY_TARGET.search(t) or targets.described(t) or re.search(r"\btarget\b", t)):
        return "smoke_target", {"color": color or "red", "text": t}
    if _LASE.search(t) and (_MY_TARGET.search(t) or targets.described(t) or re.search(r"\b(?:target|it|that)\b", t)):
        m = _CODE.search(t)
        code = int("".join(g for g in m.groups() if g)) if m else DEFAULT_LASER_CODE
        return "lase", {"code": code, "text": t}
    from common.user_waypoints import NAME, canon        # named waypoints: "go to waypoint Alpha"
    m = re.search(r"\bland\s+(?:at|on|by)\s+(?:the\s+)?(?:waypoint\s+|point\s+)?" + NAME + r"\b", t)
    if m:
        return "land_named", {"name": canon(m.group(1))}
    m = re.search(r"\b(?:go|proceed|fly|head|move|push|continue|hold|orbit|take position)\s+(?:to|at|over|on)\s+"
                  r"(?:the\s+)?(?:waypoint\s+|point\s+)?" + NAME + r"\b", t)
    if m:
        return "goto_named", {"name": canon(m.group(1))}
    m = _LAND_WP.search(t)
    if m:
        return "land_wp", {"wp": _num(m.group(1))}
    if _LAND_ME.search(t):
        return "land_me", {}
    m = _GOTO.search(t)
    if m:
        return "goto_wp", {"wp": _num(m.group(1))}
    d = _DIST.search(t)
    dist = None
    if d:
        unit = d.group(2)
        dist = float(d.group(1)) * (0.3048 if unit in ("feet", "ft") else
                                    1000.0 if unit.startswith(("km", "kilo", "klick")) else 1.0)
    f = _FORE.search(t)
    if f:
        return "follow", {"side": None, "dist": dist, "fore": -1 if f.group(1) in ("behind", "back", "trail") else 1}
    if _FOLLOW.search(t):
        side = _SIDE.search(t)
        return "follow", {"side": side.group(1) if side else None, "dist": dist}
    if _ROE_HOLD.search(t):
        return "roe", {"roe": "hold"}
    if _ROE_RETURN.search(t):
        return "roe", {"roe": "return"}
    if _ROE_FREE.search(t):
        return "roe", {"roe": "free"}
    if _ATTACK.search(t):
        if _MY_TARGET.search(t):
            return "attack", {"which": "mine", "text": t}
        if targets.described(t):
            return "attack", {"which": "described", "text": t}
        if _ALL.search(t):
            return "attack", {"which": "all", "text": t}
    return None


def _kind_word(text: str) -> str:
    """'tanks' / 'trucks' ... the word the pilot used for the kind of target."""
    kind = targets.wanted_kind(text)
    m = re.search(kind[0], text.lower()) if kind else None
    w = m.group(0).strip() if m else "targets"
    return w if w.endswith("s") or w in ("armour", "armor", "infantry", "artillery", "triple a", "aaa", "flak") else w + "s"


class Executor:
    """Runs parsed orders through Olympus. One per program (main.py); laser spots are tracked in a
    daemon thread so a moving target stays lased."""

    def __init__(self, oly, snapshot: Callable[[], dict], george_top: Callable[[dict], Optional[dict]] = None):
        self.oly = oly
        self.snapshot = snapshot
        self.george_top = george_top or (lambda snap: None)
        self._spots: Dict[int, dict] = {}          # wingman object id -> {"spot", "target", "code"}
        self._lock = threading.Lock()
        self._thread = None

    # ------------------------------------------------------------ helpers
    @staticmethod
    def wing_ids(snap: dict) -> Dict[str, int]:
        return {str(w[0]): int(w[1]) for w in snap.get("wing") or [] if int(w[1]) >= 0}

    @staticmethod
    def _loc(lat: float, lon: float) -> dict:
        return {"lat": lat, "lng": lon}

    @staticmethod
    def _list_target(snap: dict, n: int) -> Optional[dict]:
        """Entry n of George's target list, as an Export.lua contact (it has the object id)."""
        rows = snap.get("targets") or []
        if not 1 <= n <= len(rows):
            return None
        e = rows[n - 1]
        cid = e.get("contact_id")
        for c in snap.get("contacts") or []:
            if cid is not None and c.get("id") == cid:
                return c
        km = (e.get("dist_m") or 0) / 1000.0 or None
        return targets.my_target(snap, {"type": e.get("type"), "dist_km": km})

    def _target_for(self, snap: dict, p: dict) -> Optional[dict]:
        if p.get("which") == "list":
            return self._list_target(snap, p["n"])
        if p.get("which") == "described" or (p.get("text") and targets.described(p["text"])
                                             and not _MY_TARGET.search(p["text"])):
            tgt = targets.by_description(snap, p["text"])
            if tgt is None and not targets.has_clock(p["text"]):     # not on your sensors: all DCS lists
                tgt = targets.by_description_wide(snap, p["text"])
                if tgt is not None:
                    print(f"[OLYMPUS] '{p['text']}': none in your contacts; {tgt['type']} "
                          f"{tgt['dist_m'] / 1000:.1f} km away from the mission's units")
            return tgt
        top = self.george_top(snap)
        if top and top.get("jtac"):     # Overlord's JTAC target: its own id and position
            return top
        tgt = targets.my_target(snap, top)
        if tgt is None:                 # nothing George or the nose can name (22 Oct): the nearest enemy
            tgt = targets.nearest(snap)
            if tgt is not None:
                print(f"[OLYMPUS] 'my target': none on the nose, taking the nearest, {tgt.get('type')}")
        return tgt

    # ------------------------------------------------------------ run
    def run(self, kind: str, params: dict, wingmen: List[dict], who_say: str) -> str:
        """wingmen: [{'n', 'name', 'id'}] already checked (alive AI of your flight).
        '' = done (quiet); otherwise what to say."""
        snap = self.snapshot()
        geo.FIT.add_snapshot(snap)
        if kind == "smoke_me":
            ll = snap.get("my_ll")
            if not ll:
                return "Unable, no position."
            return self._send("smoke", {"color": params["color"], "location": self._loc(*ll)})
        if kind == "smoke_target":
            tgt = self._target_for(snap, params)
            if tgt is None:
                return "No target to mark."
            return self._send("smoke", {"color": params["color"], "location": self._loc(tgt["lat"], tgt["lon"])})
        if kind == "attack":
            if params["which"] == "list":
                tgt = self._target_for(snap, params)
                if tgt is None:
                    return f"No target {params['n']} on George's list."
                out = [self._send("attackUnit", {"ID": w["id"], "targetID": tgt["id"]}) for w in wingmen]
                return next((o for o in out if o), "")
            if params["which"] == "all" or (len(wingmen) > 1 and params["which"] != "mine"):
                tgts = targets.for_flight(snap, len(wingmen), params.get("text", ""))
                if not tgts:
                    return "No targets in range."
                out = []
                for i, w in enumerate(wingmen):
                    t = tgts[i % len(tgts)]
                    out.append(self._send("attackUnit", {"ID": w["id"], "targetID": t["id"]}))
                return next((o for o in out if o), "")
            tgt = self._target_for(snap, params)
            if tgt is None:
                if targets.wanted_kind(params.get("text", "")):
                    from common import aircraft
                    return f"No {_kind_word(params['text'])} within {aircraft.say_dist(targets.WIDE_RANGE_M, snap)}."
                return "No target. Put it on the nose or tell me what it is."
            out = [self._send("attackUnit", {"ID": w["id"], "targetID": tgt["id"]}) for w in wingmen]
            err = next((o for o in out if o), "")
            if not err and tgt.get("wide"):              # you may not see it: say what and where
                from common.overlord import say_type, compass
                from common import aircraft
                return (f"{who_say}, engaging {say_type(tgt['type'])}, {aircraft.say_dist(tgt['dist_m'], snap)} "
                        f"{compass(tgt['brg'])} of you.")
            return err
        if kind == "follow":
            me = snap.get("self_id", -1)
            if me is None or me < 0:
                return "Unable, I can't see our own aircraft."
            out = []
            for i, w in enumerate(wingmen):
                from common import aircraft
                x, y, z = aircraft.profile(snap).follow_offset(w["n"])
                if params.get("fore"):                  # ahead of / behind you, side by side
                    x = params["fore"] * (params.get("dist") or 300.0)
                    z = 0.0 if len(wingmen) == 1 else (60.0 * (i // 2 + 1)) * (1 if i % 2 == 0 else -1)
                elif params.get("side") or params.get("dist"):
                    d = params.get("dist") or abs(z) or 60
                    z = d if (params.get("side") or ("right" if z > 0 else "left")) == "right" else -d
                    x = -min(60.0, d / 3)
                out.append(self._send("followUnit", {"ID": w["id"], "targetID": me,
                                                     "offsetX": x, "offsetY": y, "offsetZ": z}))
            return next((o for o in out if o), "")
        if kind in ("goto_wp", "land_wp"):
            p = next((q for q in snap.get("route") or [] if int(q[0]) == params["wp"]), None)
            if p is None:
                return f"No waypoint {params['wp']}."
            ll = geo.FIT.to_ll(p[1], p[2])
            if ll is None:
                return "Unable, I can't place that waypoint yet."
            cmd = "setPath" if kind == "goto_wp" else "landAtPoint"
            body = (lambda w: {"ID": w["id"], "path": [self._loc(*ll)]}) if kind == "goto_wp" else \
                   (lambda w: {"ID": w["id"], "location": self._loc(*ll)})
            out = [self._send(cmd, body(w)) for w in wingmen]
            return next((o for o in out if o), "")
        if kind in ("goto_named", "land_named"):
            from common.user_waypoints import USER_WAYPOINTS
            ll = USER_WAYPOINTS.ll(params["name"], snap)
            if ll is None:
                return f"No waypoint {params['name']}." if params["name"] != "Objective" else "No objective set."
            if kind == "goto_named":
                out = [self._send("setPath", {"ID": w["id"], "path": [self._loc(*ll)]}) for w in wingmen]
            else:
                out = [self._send("landAtPoint", {"ID": w["id"], "location": self._loc(*ll)}) for w in wingmen]
            return next((o for o in out if o), "")
        if kind == "land_me":
            ll = snap.get("my_ll")
            if not ll:
                return "Unable, no position."
            out = []
            for i, w in enumerate(wingmen):
                n, e = geo.body_to_ne(snap.get("hdg") or 0.0, -30.0 * i, 60.0 * (i + 1) * (1 if i % 2 == 0 else -1))
                out.append(self._send("landAtPoint", {"ID": w["id"], "location": self._loc(*geo.offset_ll(ll[0], ll[1], n, e))}))
            return next((o for o in out if o), "")
        if kind == "roe":
            out = [self._send("setROE", {"ID": w["id"], "ROE": ROE[params["roe"]]}) for w in wingmen]
            return next((o for o in out if o), "")
        if kind == "lase":
            if len(wingmen) != 1:
                return "One lases at a time. Say two or three."
            tgt = self._target_for(snap, params)
            if tgt is None:
                return "No target to lase."
            w = wingmen[0]
            self.lase_stop([w])                           # one spot per wingman
            said = self._send("fireLaser", {"ID": w["id"], "code": params["code"],
                                            "location": self._loc(tgt["lat"], tgt["lon"])})
            if said:
                return said
            with self._lock:      # its spot id is looked up by the tracker (Olympus lists it a bit later)
                self._spots[w["id"]] = {"spot": None, "target": tgt["id"], "code": params["code"],
                                        "since": time.monotonic()}
            self._start_tracker()
            return ""
        if kind == "lase_stop":
            return self.lase_stop(wingmen)
        return "Say again the order."

    def _send(self, name: str, body: dict) -> str:
        ok, why = self.oly.command(name, body)
        return "" if ok else f"Unable, {why}."

    # ------------------------------------------------------------ laser
    def _find_spot(self, source_id: int) -> Optional[int]:
        """The newest active spot of this wingman, from Olympus's data (/olympus/spots)."""
        for path in ("/olympus/spots", "/olympus/mission"):
            try:
                data = self.oly.get(path)
            except Exception:
                continue
            spots = data.get("spots", data) if isinstance(data, dict) else None
            if not isinstance(spots, dict):
                continue
            ids = [int(k) for k, s in spots.items() if isinstance(s, dict) and str(k).lstrip("-").isdigit()
                   and int(s.get("sourceUnitID", -1)) == source_id and s.get("active", True)]
            if ids:
                return max(ids)
        return None

    def lasing(self) -> List[int]:
        with self._lock:
            return list(self._spots)

    def lase_stop(self, wingmen: List[dict]) -> str:
        said = ""
        for w in wingmen:
            with self._lock:
                s = self._spots.pop(w["id"], None)
            if s and s["spot"] is None:
                s["spot"] = self._find_spot(w["id"])
            if s and s["spot"] is not None:
                said = self._send("deleteSpot", {"spotID": s["spot"]}) or said
            elif s:
                said = "Unable, I can't find that laser to switch it off."
        return said

    def _start_tracker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._track, daemon=True, name="olympus-laser")
        self._thread.start()

    def _track(self) -> None:
        last: Dict[int, Tuple[float, float]] = {}
        while True:
            time.sleep(SPOT_UPDATE_SEC)
            with self._lock:
                spots = dict(self._spots)
            if not spots:
                return
            contacts = {c.get("id"): c for c in self.snapshot().get("contacts") or []}
            for wid, s in spots.items():
                if s["spot"] is None:
                    spot = self._find_spot(wid)
                    if spot is None:
                        if time.monotonic() - s.get("since", 0) > 15 and not s.get("warned"):
                            s["warned"] = True
                            print("[OLYMPUS] Laser on, but Olympus doesn't list its spot: it won't follow "
                                  "the target, and 'stop lasing' can't switch it off.")
                        continue
                    with self._lock:
                        if wid in self._spots:
                            self._spots[wid]["spot"] = spot
                    print(f"[OLYMPUS] Laser spot {spot} for unit {wid}.")
                    s["spot"] = spot
                c = contacts.get(s["target"])
                if c is None:                             # destroyed or out of sight: laser off
                    print(f"[OLYMPUS] Target {s['target']} gone, laser {s['spot']} off.")
                    self.lase_stop([{"id": wid}])
                    continue
                pos = (c["lat"], c["lon"])
                if last.get(wid) and math.dist(last[wid], pos) < 1e-5:
                    continue
                last[wid] = pos
                self._send("moveSpot", {"spotID": s["spot"], "location": self._loc(*pos)})
