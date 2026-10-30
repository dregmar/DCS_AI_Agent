"""
common/independents.py - wingmen made independent, so DCS Olympus orders work on them.

DCS ignores AI tasks for a group a player leads (flight 2026-10-20), so Olympus can't task the
wingmen in your own group. "Two, go independent" has Olympus clone that wingman, loadout
included, into an AI group of its own ("Olympus-N"), then removes the original. From then on he
is "Outcast 2", and every order to him goes through Olympus:

    "two, go independent" · "three, go independent" · "flight, go independent"
    "Outcast 2, attack my current target" · "Outcast 3, take out the ZSU at two o'clock"
    "Outcast 2, engage at will"   (weapons free; he takes the most dangerous enemy near him, one
                                   after another, and rejoins when there's nothing left)
    "Outcast 3, RTB"              (lands at the mission's start point)
    "Outcast 2, rejoin" / "follow me" / "hold position" / "go to waypoint 3" / "land next to me"
    "Outcast 2, weapons hold" / "weapons free" / "lase my target" / "stop lasing" / "status"
"Two, ..." keeps working after he's gone independent: it goes to Outcast 2.

It can't be undone: the radio menu no longer controls him. The order of the steps keeps it safe:
clone first (the original stays), then remove the original only once the clone shows up in
Export.lua. Olympus's own "clone and delete" would remove the original even when the clone
fails. Never your own aircraft: only wingmen Export.lua lists in your group, and never your id.

George stays quiet when an order went through (like the other wingman orders); he speaks when
someone goes independent, when an order fails, and when an Outcast is lost.
"""
import re
import threading
import time
from typing import Callable, Dict, List, Optional

from common import geo
from common.olympus_orders import FOLLOW_DEFAULT, ROE

CALLSIGN = "Outcast"
NUM_SAY = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
APPEAR_TIMEOUT_S = 20.0
POLL_S = 3.0
HUNT_RADIUS_M = 6_000              # engage at will: enemies this close to the Outcast
HUNT_IDLE_S = 60.0                 # nothing left for this long: rejoin
CLONE_OFFSET_M = 120.0             # the clone appears this far to the side of the original
CLONE_MATCH_M = 800.0              # ... and must show up within this distance of that spot
AGL_M, SPEED_MPS = 60.0, 45.0      # after the clone: 200 ft above ground, about 90 knots
COST = 10                          # Olympus spawn points per clone (when spawns are restricted)

_NUM = r"(\d|two|to|too|tool|three|tree|four|for|five|six)"
_NUMW = {"two": 2, "to": 2, "too": 2, "tool": 2, "three": 3, "tree": 3, "four": 4, "for": 4,
         "five": 5, "six": 6}
_LEAD = r"^(?:(?:hey|george|okay|ok|roger|uh|um|and|so)[\s,]+)*"
# Whisper's versions of "Outcast"
# heard 22 Oct: "ORGAS 2", "ORCAS 4", "Allcast 4", "All CAS for", "All CAS4", "OCH-3", "for art theory"
_CS = (r"(?:out\s?cast|outcasts|outcaste|out\s?cost|outcost|autocast|art\s?cast|out\s?caste?d?|outkast|orgas|"
       r"orcas|or\s?cast|all\s?cast|all\s?cas|all\s?cass|och|ouch|o\s?cast|oh\s?cast|ocaste|forecast|"
       r"for\s?cast|august)")
_REPEAT = re.compile(r"^((?:(?:hey|george|okay|ok|roger|uh|um|and|so)[\s,]+)*)" + _CS + r"[\s,.]+(?=" + _CS + r")",
                     re.I)                                      # "Ocast, Ocast 2, ..." (23 Oct)
_OUTCAST = re.compile(_LEAD + r"(?:out\s?cast|outcasts|outcaste|out\s?cost|outcost|autocast|art\s?cast|"
                      r"out\s?caste?d?|outkast|orgas|orcas|or\s?cast|all\s?cast|all\s?cas|all\s?cass|och|"
                      # heard 23 Oct: "Ocast 2" (most of the time), "Forecast 2", "August 2"
                      r"ouch|o\s?cast|oh\s?cast|ocaste|forecast|for\s?cast|august)[\s,.-]*" + _NUM + r"(?=\b|[a-z])[\s,.!?]*(.*)$", re.I)
INDEPENDENT = re.compile(r"\b(?:go(?:ing)?\s+|be\s+|become\s+)?(?:independent|independence|independant|"
                         r"in\s?dependent)\b")
_RTB = re.compile(r"\b(?:rtb|r t b|return to base|go home|head home|go back to base|bingo|winchester)\b")
_HUNT = re.compile(r"\b(?:engage at will|engaged at will|fire at will|free hunt|go hunting|hunt|"
                   r"clear the area|engage (?:targets|anything|everything) (?:of opportunity|you see)|"
                   r"targets of opportunity)\b")
# "re-transformation" / "re-drying": Whisper's "rejoin formation" / "rejoin" (23 Oct)
_REJOIN = re.compile(r"\b(?:rejoin(?:ing)?|re join|join up|form up|come back|return to me|back on me|"
                     r"re ?transformation|re ?formation|re ?drying|re ?dry|re ?joy(?:ning)?)\b")
# "engage air defences" / "destroy the defences" / "take out the SAMs": hunt, air defence only
_AD = re.compile(r"\b(?:air ?defen[cs]es?|(?:your |the |their )?defen[cs]es?|sams?|sam sites?|triple a|aaa|"
                 r"anti ?aircraft|radars?|flak|zsus?|shilkas?)\b")
_AD_VERB = re.compile(r"\b(?:engage|destroy|attack|take out|suppress|kill|hit|clear|go after|hunt|sead)\b")
_HOLD = re.compile(r"\b(?:(?:hold|whole|hole|old) (?:position|here)|hover|stay (?:here|there|put)|hold at)\b")
_STATUS = re.compile(r"\b(?:status|report|say state|where are you|sitrep)\b")


def parse_outcast(text: str):
    """(n, rest) when the utterance starts with "Outcast <n>", else None."""
    t = re.sub(r"\s+", " ", (text or "").lower().replace("’", "'")).strip()
    m = _OUTCAST.match(_REPEAT.sub(r"\1", t))
    if not m:
        return None
    tok = m.group(1)
    return (int(tok) if tok.isdigit() else _NUMW[tok]), m.group(2).strip()


def understands(rest: str) -> bool:
    """Would an Outcast take this order (what follows "Outcast N,")? Checked before the router
    asks "Confirm ...?" for it."""
    t = re.sub(r"[^a-z0-9' ]+", " ", (rest or "").lower()).strip()
    if not t:
        return False
    if (_STATUS.search(t) or (_AD.search(t) and _AD_VERB.search(t)) or _HUNT.search(t) or _RTB.search(t)
            or _REJOIN.search(t) or _HOLD.search(t) or INDEPENDENT.search(t)):
        return True
    from common import olympus_orders
    return olympus_orders.parse(t) is not None


def _dist_brg(a, b):
    from common.overlord import dist_brg, compass
    d, brg = dist_brg(a, b)
    return d, compass(brg)


class Independents:
    def __init__(self, executor, snapshot: Callable[[], dict], speak: Callable[[str], None]):
        self.ex, self.snapshot, self.speak = executor, snapshot, speak
        self._lock = threading.Lock()
        self._by_n: Dict[int, dict] = {}          # n -> {"group", "name" (original), "state"}
        self._hunt: Dict[int, threading.Event] = {}

    # ------------------------------------------------------------ registry
    def reset(self) -> None:
        """A new mission: the Outcasts of the last one are gone."""
        with self._lock:
            hunts, self._hunt = dict(self._hunt), {}
            self._by_n.clear()
        for ev in hunts.values():
            ev.set()

    def known(self, n: int) -> Optional[dict]:
        with self._lock:
            r = self._by_n.get(n)
            return dict(r) if r else None

    def numbers(self) -> List[int]:
        with self._lock:
            return sorted(n for n, r in self._by_n.items() if r["state"] == "up")

    def _unit(self, snap: dict, n: int) -> Optional[tuple]:
        r = self.known(n)
        if not r or not r.get("group"):
            return None
        units = sorted((u for u in snap.get("olympus_units") or [] if u[0] == r["group"]), key=lambda u: str(u[1]))
        return units[0] if units else None

    def _claimed(self) -> set:
        """Groups already taken: every Outcast's, and Overlord's flights (on the shared client)."""
        with self._lock:
            mine = {r["group"] for r in self._by_n.values() if r.get("group")}
        if hasattr(self.ex.oly, "claimed"):
            mine |= self.ex.oly.claimed()
        return mine

    def _say(self, n: int) -> str:
        return f"{CALLSIGN} {n}"

    # ------------------------------------------------------------ going independent
    def detach(self, wing: List[dict], snap: dict) -> str:
        """wing: [{'n', 'name', 'id', 'lat', 'lon'}] (alive AI wingmen of your flight, seen by Export.lua).
        Starts one thread per wingman; returns what George says now."""
        if not self.ex.oly.connect():
            return "Unable, Olympus isn't running."
        me = snap.get("self_id", -1)
        go, already = [], []
        for w in wing:
            if w.get("id") is None or w["id"] == me or w.get("name") == snap.get("my_unit"):
                continue                                         # never your own aircraft
            r = self.known(w["n"])
            if r and r["state"] in ("cloning", "up"):
                already.append(w["n"])
                continue
            with self._lock:
                self._by_n[w["n"]] = {"group": None, "name": w["name"], "state": "cloning"}
            go.append(w)
            threading.Thread(target=self._detach_one, args=(w,), daemon=True,
                             name=f"independent-{w['n']}").start()
        if not go:
            if already:
                return f"{self._say(already[0])} is already independent."
            return "Unable, I can't see that wingman."
        who = " and ".join(NUM_SAY.get(w["n"], str(w["n"])).capitalize() for w in go)
        return f"{who}, going independent. Stand by."

    def _detach_one(self, w: dict) -> None:
        n = w["n"]
        try:
            ok = self._clone(w)
        except Exception as e:
            print(f"[OUTCAST] {n}: {type(e).__name__}: {e}")
            ok = False
        if not ok:
            with self._lock:
                self._by_n.pop(n, None)
            self.speak(f"{NUM_SAY.get(n, n)} couldn't go independent. Still in your flight.".capitalize())

    def _clone(self, w: dict) -> bool:
        n = w["n"]
        snap = self.snapshot()
        known = {u[0] for u in snap.get("olympus_units") or []}
        hdg = snap.get("hdg") or 0.0
        side = CLONE_OFFSET_M if n % 2 == 0 else -CLONE_OFFSET_M
        dn, de = geo.body_to_ne(hdg, 0.0, side)
        lat, lon = geo.offset_ll(w["lat"], w["lon"], dn, de)
        ok, why = self.ex.oly.command("cloneUnits", {
            "units": [{"ID": w["id"], "location": {"lat": lat, "lng": lon}}],
            "deleteOriginal": False, "coalition": "blue", "spawnPoints": COST})
        if not ok:
            print(f"[OUTCAST] {n}: cloneUnits refused: {why}")
            return False
        # 22 Oct: Outcast 4 was matched to Outcast 2's group (Olympus-1) and the real AH-64D-3 was
        # deleted with no clone. Groups that are already ours never count, and the clone must be
        # close to where it was asked to appear.
        mine = self._claimed()
        group, deadline = None, time.monotonic() + APPEAR_TIMEOUT_S
        while time.monotonic() < deadline and group is None:
            time.sleep(1.0)
            best = None
            for u in self.snapshot().get("olympus_units") or []:
                if u[0] in known or u[0] in mine:
                    continue
                d, _ = _dist_brg((lat, lon), (float(u[3]), float(u[4])))
                if d < CLONE_MATCH_M and (best is None or d < best[0]):
                    best = (d, u[0])
            group = best[1] if best else None
        if group is None:
            print(f"[OUTCAST] {n}: no clone appeared in {APPEAR_TIMEOUT_S:.0f} s; the original stays")
            return False
        # The clone is there: now remove the original (an AI unit: Olympus just removes it)
        self.ex.oly.command("deleteUnit", {"ID": w["id"], "explosion": False, "explosionType": "",
                                           "immediate": True})
        with self._lock:
            self._by_n[n] = {"group": group, "name": w["name"], "state": "up"}
        if hasattr(self.ex.oly, "claim"):
            self.ex.oly.claim(group)
        unit = self._unit(self.snapshot(), n)
        uid = int(unit[2]) if unit else None
        print(f"[OUTCAST] {w['name']} is now {self._say(n)} ({group}, id {uid})")
        if uid is not None:
            from common import aircraft                     # helicopter or jet height and speed
            p = aircraft.profile(self.snapshot())
            for name, body in (("setAltitudeType", {"ID": uid, "altitudeType": p.outcast_alt_type}),
                               ("setAltitude", {"ID": uid, "altitude": p.outcast_alt_m}),
                               ("setSpeed", {"ID": uid, "speed": p.outcast_speed_mps})):
                self.ex.oly.command(name, body)
            self._follow(n, uid)
        self.speak(f"{self._say(n)}, independent, on your wing.")
        return True

    def _follow(self, n: int, uid: int) -> str:
        me = self.snapshot().get("self_id", -1)
        if me is None or me < 0:
            return "Unable, I can't see our own aircraft."
        from common import aircraft
        x, y, z = aircraft.profile(self.snapshot()).follow_offset(n)
        ok, why = self.ex.oly.command("followUnit", {"ID": uid, "targetID": me, "offsetX": x, "offsetY": y,
                                                     "offsetZ": z})
        return "" if ok else f"Unable, {why}."

    # ------------------------------------------------------------ orders
    def order(self, n: int, rest: str) -> str:
        """An order to Outcast n. '' = done (George stays quiet), else what to say."""
        r = self.known(n)
        if r is None:
            return f"{self._say(n)} isn't independent. Say {NUM_SAY.get(n, n)}, go independent."
        if r["state"] == "cloning":
            return f"{self._say(n)} is still joining. Stand by."
        if r["state"] == "lost":
            return f"{self._say(n)} is down."
        snap = self.snapshot()
        unit = self._unit(snap, n)
        if unit is None:
            with self._lock:
                self._by_n[n]["state"] = "lost"
            from common.debrief import DEBRIEF
            DEBRIEF.loss(self._say(n))
            return f"{self._say(n)} is down."
        uid = int(unit[2])
        t = re.sub(r"[^a-z0-9' ]+", " ", (rest or "").lower()).strip()
        if not t:
            return f"{self._say(n)}, go ahead."
        if _STATUS.search(t):
            return self._status(n, unit, snap)
        self._stop_hunt(n)                       # any other order ends "engage at will"
        if _AD.search(t) and _AD_VERB.search(t) and not re.search(r"\b(?:my|the) target\b|o'?clock", t):
            return self._start_hunt(n, uid, air_defence=True)
        if _HUNT.search(t):
            return self._start_hunt(n, uid)
        if _RTB.search(t):
            home = self._home(snap)
            if home is None:
                return "Unable, I don't know where home is."
            ok, why = self.ex.oly.command("landAtPoint", {"ID": uid, "location": {"lat": home[0], "lng": home[1]}})
            return "" if ok else f"Unable, {why}."
        if _REJOIN.search(t):
            return self._follow(n, uid)
        if _HOLD.search(t):
            ok, why = self.ex.oly.command("setPath", {"ID": uid, "path": [{"lat": float(unit[3]),
                                                                         "lng": float(unit[4])}]})
            return "" if ok else f"Unable, {why}."
        from common import olympus_orders
        parsed = olympus_orders.parse(t)
        if parsed is None:
            return f"{self._say(n)}, say again."
        kind, params = parsed
        said = self.ex.run(kind, params, [{"n": n, "name": self._say(n), "id": uid}], self._say(n))
        return said

    def order_all(self, rest: str) -> List[str]:
        """'flight, ...': the same order to every Outcast. What each said ('' = done)."""
        return [self.order(n, rest) for n in self.numbers()]

    def _home(self, snap: dict):
        for p in snap.get("route") or []:
            if len(p) >= 3 and int(p[0]) == 0:
                ll = geo.FIT.to_ll(float(p[1]), float(p[2]))
                if ll:
                    return ll
        route = snap.get("route") or []
        if route:
            return geo.FIT.to_ll(float(route[0][1]), float(route[0][2]))
        return None

    def _status(self, n: int, unit: tuple, snap: dict) -> str:
        me = snap.get("my_ll")
        hunting = n in self._hunt and not self._hunt[n].is_set()
        doing = ", engaging at will" if hunting else ""
        if not me:
            return f"{self._say(n)}, up{doing}."
        d, way = _dist_brg((me[0], me[1]), (float(unit[3]), float(unit[4])))
        from common import aircraft
        wing_m = 1500 if aircraft.profile(snap).kind == "jet" else 400
        where = "on your wing" if d < wing_m else f"{aircraft.say_dist(d, snap)} {way} of you"
        return f"{self._say(n)}, {where}{doing}."

    # ------------------------------------------------------------ engage at will
    def _stop_hunt(self, n: int) -> None:
        with self._lock:
            ev = self._hunt.pop(n, None)
        if ev:
            ev.set()

    def _start_hunt(self, n: int, uid: int, air_defence: bool = False) -> str:
        """Says at once what he's going after, or that there's nothing near him (24 Oct: the
        order was silent for a minute, then "no more air defences")."""
        snap = self.snapshot()
        unit = self._unit(snap, n)
        cands = self._hunt_targets(snap, unit, air_defence) if unit is not None else []
        what = "air defences" if air_defence else "targets"
        if not cands:
            from common import aircraft
            return f"{self._say(n)}, no {what} within {aircraft.say_dist(HUNT_RADIUS_M, snap)} of me."
        ok, why = self.ex.oly.command("setROE", {"ID": uid, "ROE": ROE["free"]})
        if not ok:
            return f"Unable, {why}."
        ev = threading.Event()
        with self._lock:
            self._hunt[n] = ev
        threading.Thread(target=self._hunt_loop, args=(n, ev, air_defence), daemon=True,
                         name=f"outcast-{n}-hunt").start()
        from common.overlord import say_type
        more = f", {len(cands)} in range" if len(cands) > 1 else ""
        return (f"{self._say(n)}, {'going after the air defences' if air_defence else 'engaging at will'}"
                f"{more}. First, {say_type(cands[0]['type'])}.")

    def _hunt_targets(self, snap: dict, unit: tuple, air_defence: bool) -> list:
        """Enemies within HUNT_RADIUS_M of the Outcast, most dangerous first."""
        from common.overlord import ground_near, radars_near
        here = (float(unit[3]), float(unit[4]))
        cands = ground_near(snap, here, HUNT_RADIUS_M)
        # ground_near leaves the big radar SAMs out; an Apache can take them up close
        cands += [r for r in radars_near(snap, here, HUNT_RADIUS_M)
                  if r["id"] not in {c["id"] for c in cands}]
        if air_defence:                 # "engage air defences": SAMs, radars, AAA, MANPADS only
            from common.unit_types import TYPES
            cands = [c for c in cands if TYPES.is_air_defence(c["type"]) or c["w"] >= 90]
        cands.sort(key=lambda c: (-c["w"], c["d"]))
        return cands

    def _hunt_loop(self, n: int, stop: threading.Event, air_defence: bool = False) -> None:
        from common.overlord import target_state, say_type
        current, idle_since = None, time.monotonic()
        while not stop.is_set():
            snap = self.snapshot()
            unit = self._unit(snap, n)
            if unit is None:
                with self._lock:
                    if n in self._by_n:
                        self._by_n[n]["state"] = "lost"
                from common.debrief import DEBRIEF
                DEBRIEF.loss(self._say(n))
                self.speak(f"{self._say(n)} is down.")
                return
            uid = int(unit[2])
            if current and target_state(snap, current) == "dead":
                print(f"[OUTCAST] {n}: {current['type']} destroyed")
                from common.debrief import DEBRIEF
                DEBRIEF.kill(self._say(n), say_type(current["type"]))
                current = None
            if current is None:
                cands = self._hunt_targets(snap, unit, air_defence)
                if cands:
                    current, idle_since = cands[0], time.monotonic()
                    ok, why = self.ex.oly.command("attackUnit", {"ID": uid, "targetID": current["id"]})
                    if not ok:
                        self.speak(f"{self._say(n)}, unable, {why}.")
                        return
                    print(f"[OUTCAST] {n}: engaging {say_type(current['type'])}")
                elif time.monotonic() - idle_since > HUNT_IDLE_S:
                    self.speak(f"{self._say(n)}, no more {'air defences' if air_defence else 'targets'}, rejoining.")
                    self._follow(n, uid)
                    with self._lock:
                        if self._hunt.get(n) is stop:
                            self._hunt.pop(n, None)
                    return
            stop.wait(POLL_S)
