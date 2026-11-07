"""
common/darkside.py - DARKSIDE, the JTAC / FAC (Phase 2, P2-4 and P2-6).

P2-4: what common/jtac.py does (request / next / repeat target, mark, BDA, "good effect") in Darkside's
voice and words: "Enfield 1-1, Darkside, T-72B, grid ...". jtac.py stays the target engine underneath;
marking goes out as DARKSIDE (command/permissions.py lets only him and the pilot mark).

P2-6: terminal control of Overlord's CAS flights, and of nothing else. Overlord's CAS flight arrives
weapons hold and flies to an IP; Overlord's CAS loop (common/overlord.py) asks clearance(m, target,
snap) there, and Darkside clears it hot only when all of these hold:

    the target is alive in the tactical picture (T7; DCS's lists when there is no picture)
    no friendly ground / naval unit within DARKSIDE_DANGER_CLOSE_M of it (Export's own-side list, T8)
    it isn't inside a no-strike zone (Warlord's block)
    the pilot hasn't said "Darkside, abort" (until "Darkside, continue")

Then he sends weapons free and the attack himself (clear()). "Darkside, abort" holds every CAS flight
at once (weapons hold); Overlord's loop takes it back to the IP. He doesn't ask the pilot before
clearing (Andre, 1 Nov).

AI-to-AI calls (check-in, "cleared hot") go to the comms log only, as "ai" lines (log callback; P2-7
adds the spoken option). Thread-safe: the flights he controls and the abort set are under one lock, never held
while sending a command or speaking.
"""
import re
import threading
from typing import Callable, Dict, List, Optional, Set, Tuple

from command import exchange, rules
from command.permissions import acting_as
from command.roles import DARKSIDE, Said
from command.tasking import TASKS, TaskRegistry

ROE_FREE, ROE_HOLD = 0, 4

_ABORT = re.compile(r"\b(?:abort|abort abort|cease|stop the attack|knock it off|hold fire|weapons hold)\b", re.I)
_CONTINUE = re.compile(r"\b(?:continue|resume|press|cleared hot|carry on)\b", re.I)
_LEAD = re.compile(r"^\s*Overlord(?:\s+copies)?[,.]\s*", re.I)
# 9-line (5 Nov, common/nine_line.py): "request nine-line", your readback, your "IN" call
_NINE = re.compile(r"\b(?:9|nine)[ -]?lines?\b", re.I)
_READBACK = re.compile(r"\b(?:read ?back|readback|reading back)\b", re.I)
_IN = re.compile(r"^(?:[a-z]+ \d(?:[ -]?\d)?,? )?(?:in|inbound)(?: hot)?(?:,? (?:from|heading)\b.*|"
                 r",? (?:from )?(?:the )?(?:north|south|east|west)\w*.*)?$", re.I)


def _plain(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[,.;:!?]+", " ", t or "")).strip().lower()


def _say_type(name: str) -> str:
    from common.overlord import say_type
    return say_type(name)


class Darkside:
    def __init__(self, jtac, snapshot: Callable[[], dict], speak: Callable[[str], None],
                 callsign: Callable[[], str] = lambda: "Nomad", oly=None, warlord=None,
                 tasks: TaskRegistry = TASKS, log: Optional[Callable[[str, str], None]] = None,
                 danger_close_m: float = rules.DANGER_CLOSE_M,
                 say_ai: Optional[Callable[[str, str], None]] = None,
                 agl_m: float = rules.DANGER_CLOSE_AGL_M,
                 outcasts: Optional[Callable[[], Dict[int, str]]] = None):
        """log(who, text): the comms log. say_ai(text, speaker): speaks an AI-to-AI call; used only
        with AI_TO_AI_SPOKEN (command/exchange.py)."""
        self.jtac, self.snapshot, self.speak, self.callsign = jtac, snapshot, speak, callsign
        self.oly, self.warlord, self.tasks = oly, warlord, tasks
        self.log = log or (lambda who, text: None)
        self.say_ai = say_ai
        self.danger_close_m = danger_close_m
        self.agl_m = agl_m                                # R6: you / wingmen / Outcasts below this hold the run
        self.outcasts = outcasts or (lambda: {})          # unit id -> "Outcast 2" (main.py)
        self._lock = threading.Lock()
        self._flights: Dict[str, object] = {}            # task_id -> the Overlord Mission he controls
        self._aborted: Set[str] = set()
        self._said: Dict[str, str] = {}                  # task_id -> last "holding because" said
        self._caution: Set[str] = set()
        self._reported: Set[int] = set()                 # kills already called (JTAC tick or CAS loop)
        self._nine = None                                # (target id, NineLine) last given to you
        self._readback_due = False

    # ------------------------------------------------------------ words
    def _lead(self) -> str:
        return exchange.lead(self.callsign(), "Darkside")

    def _ai(self, who: str, text: str, speaker: str) -> None:
        """An AI-to-AI call: always in the comms log; spoken too with AI_TO_AI_SPOKEN."""
        self.log("ai", text)
        if self.say_ai is not None and exchange.ai_spoken():
            self.say_ai(text.split(": ", 1)[-1], speaker)

    def words(self, text: str) -> str:
        """A JTAC line ("Overlord, red smoke on the target.") in Darkside's words."""
        if not text:
            return text
        body = _LEAD.sub("", text.strip())
        return f"{self._lead()}{body[0].lower() + body[1:] if body[:2] != body[:2].upper() else body}"

    def say(self, text: str) -> None:
        self.speak(f"{self._lead()}{text}")

    def notice(self, text: str) -> None:
        """An unprompted JTAC line ("Overlord, JTAC Hammer lost sight of the target, laser off.") in his words."""
        self.speak(self.words(text))

    def jtac_speak(self, text: str) -> None:
        """For Jtac(speak=...): his unprompted "good effect" in Darkside's voice and words, unless the
        CAS loop already called that kill."""
        cur = getattr(self.jtac, "current", None) or {}
        if cur.get("id") is not None and not self._first_report(cur["id"]):
            return
        self.speak(self.words(text))

    def _first_report(self, target_id) -> bool:
        """True the first time a kill is called (1 Nov: the JTAC's target was also Hog's, two calls)."""
        try:
            tid = int(target_id)
        except (TypeError, ValueError):
            return True
        with self._lock:
            if tid in self._reported:
                return False
            if len(self._reported) > 500:
                self._reported.clear()
            self._reported.add(tid)
            return True

    def reset(self) -> None:
        """A new mission: unit ids start again."""
        with self._lock:
            self._reported.clear()
            self._aborted.clear()
            self._said.clear()
            self._caution.clear()
            self._nine, self._readback_due = None, False

    # ------------------------------------------------------------ "Darkside, ..."
    def wanted(self, rest: str) -> bool:
        t = rest or ""
        return bool(_ABORT.search(t) or (_CONTINUE.search(t) and self.has_flights()) or self.nine_wanted(t)
                    or self.jtac.wanted(t))

    def nine_wanted(self, rest: str) -> bool:
        """A 9-line request, its readback, or your IN call (also handed over from Overlord / "JTAC, ...")."""
        t = rest or ""
        return bool(_NINE.search(t) or (_READBACK.search(t) and self._readback_due) or _IN.match(_plain(t)))

    def handle(self, rest: str) -> Optional[str]:
        """What Darkside says to the rest of "Darkside, ..." (or a hand-off from Overlord / "JTAC, ..."),
        or None if it isn't his."""
        t = rest or ""
        if _ABORT.search(t):
            return self.abort()
        if _NINE.search(t):
            return self.nine_line()
        if _READBACK.search(t) and self._readback_due:
            return self.readback(t)
        if _IN.match(_plain(t)):
            return self.in_call()
        if _CONTINUE.search(t) and self.has_flights():
            return self.resume()
        if self.jtac.wanted(t):
            with acting_as(DARKSIDE):                    # marking is his (smoke, laser)
                said = self.jtac.handle(t)
            return self.words(said) if said else None
        return None

    # ------------------------------------------------------------ 9-line (5 Nov)
    def _zones(self) -> list:
        return (self.warlord.block.get("no_strike") if self.warlord is not None else None) or []

    def _target(self):
        """The JTAC's current target, live; None if there isn't one."""
        tgt = getattr(self.jtac, "current", None)
        return self.jtac._live(tgt) if tgt is not None else None

    def nine_line(self) -> str:
        from common import nine_line
        from common.overlord import target_state
        if self._target() is None:                       # no target yet: the JTAC picks one first
            with acting_as(DARKSIDE):
                said = self.jtac.handle("request target")
            if self._target() is None:
                return self.words(said) if said else f"{self._lead()}no targets to brief."
        snap = self.snapshot()
        tgt = self._target()
        if target_state(snap, tgt) == "dead":
            return f"{self._lead()}the {_say_type(tgt['type'])} is destroyed. Request next target."
        nine = nine_line.build(snap, tgt, self.jtac, self._zones(), self.danger_close_m)
        with self._lock:
            self._nine, self._readback_due = (tgt.get("id"), nine), nine_line.READBACK
        self.log("darkside", f"9-line on {tgt.get('type')} {tgt.get('id')}: {nine.text()}")
        tail = " Read back lines 4, 6 and restrictions." if nine_line.READBACK else " Call IN with direction."
        return f"{self._lead()}{nine.text()}{tail}"

    def readback(self, said: str) -> str:
        from common import nine_line
        with self._lock:
            got = self._nine
        if got is None:
            return f"{self._lead()}no nine-line to read back."
        ok, wrong = nine_line.readback_ok(got[1], said)
        if not ok:
            return f"{self._lead()}negative, {wrong}. Read back again."
        with self._lock:
            self._readback_due = False
        return f"{self._lead()}readback correct. Call IN with direction."

    def in_call(self) -> str:
        """Your IN call: the mark from the 9-line goes down, then cleared hot, or abort with the reason."""
        from common import nine_line
        from common.overlord import target_state
        from command import rules
        with self._lock:
            got, due = self._nine, self._readback_due
        tgt = self._target()
        if tgt is None or got is None or got[0] != tgt.get("id"):
            return f"{self._lead()}no nine-line on this target. Say Darkside, request nine-line."
        if due:
            return f"{self._lead()}read back lines 4, 6 and restrictions first."
        snap = self.snapshot()
        if target_state(snap, tgt) == "dead":
            return f"{self._lead()}abort, the {_say_type(tgt['type'])} is destroyed. Request next target."
        zone = rules.zone_at(self._zones(), tgt["lat"], tgt["lon"])
        if zone:
            return f"{self._lead()}abort, the target is inside the {zone['name']} no-strike zone."
        nine = got[1]
        parts = []
        with acting_as(DARKSIDE):                        # marking is his
            if nine.mark_kind == "laser":
                said = self.jtac.lase(f"code {nine.laser_code}")
                parts.append("laser on" if "laser on" in said else said.replace("Overlord, ", "").rstrip("."))
            elif nine.mark_kind == "smoke":
                said = self.jtac.handle("mark target")
                parts.append("smoke out" if said and "smoke" in said and "unable" not in said.lower()
                             else (said or "no smoke").replace("Overlord, ", "").rstrip("."))
        near = rules.friendlies_near(snap, tgt["lat"], tgt["lon"], self.danger_close_m)
        hot = "cleared hot"
        if near:
            hot += f", danger close, friendly {_say_type(near[0]['type'])} {near[0]['d']:.0f} metres"
        parts.append(hot)
        text = ". ".join(p[0].upper() + p[1:] if i else p for i, p in enumerate(parts))
        return f"{self._lead()}{text}."

    def ip_points(self) -> List[dict]:
        """For the kneeboard map (5 Nov): the 9-line's IP while its target is still the JTAC's, and each Hog
        flight's IP. [{"lat", "lon", "label", "kind": "nine" | "flight", "tlat", "tlon"}]"""
        out = []
        with self._lock:
            got = self._nine
            flights = list(self._flights.values())
        cur = getattr(self.jtac, "current", None) or {}
        if got is not None and got[0] == cur.get("id") and got[1].ip_lat is not None:
            n = got[1]
            out.append({"lat": n.ip_lat, "lon": n.ip_lon, "label": n.ip_map, "kind": "nine",
                        "tlat": cur.get("lat"), "tlon": cur.get("lon")})
        for m in flights:
            ip = getattr(m, "ip", None)
            if ip:
                out.append({"lat": round(float(ip[0]), 6), "lon": round(float(ip[1]), 6),
                            "label": f"{m.cs} IP", "kind": "flight", "tlat": None, "tlon": None})
        return out

    def brief_flight(self, m) -> None:
        """NINE_LINE_AI_TO_AI: the 9-line to a Hog flight at the IP, and its readback (comms log, spoken)."""
        from common import nine_line
        from common.overlord import target_state
        snap = self.snapshot()
        tgt = next((t for t in getattr(m, "targets", []) or [] if t.get("lat") is not None
                    and target_state(snap, t) != "dead"), None)
        if tgt is None:
            return
        tgt = self.jtac._live(tgt) if hasattr(self.jtac, "_live") else tgt
        nine = nine_line.build(snap, tgt, self.jtac, self._zones(), self.danger_close_m,
                               ip_override=getattr(m, "ip", None))
        out = f"Darkside: {m.cs} 1, Darkside, {nine.text()}"
        back = f"{m.cs} 1: Darkside, {m.cs} 1, {nine.elevation}, {nine.location}. Ready."
        self.log("ai", out)
        self.log("ai", back)
        if self.say_ai is not None:
            self.say_ai(out.split(": ", 1)[-1], "darkside")
            self.say_ai(back.split(": ", 1)[-1], "george")

    # ------------------------------------------------------------ CAS flights (P2-6)
    def register(self, m) -> None:
        with self._lock:
            self._flights[m.task_id] = m

    def unregister(self, m) -> None:
        with self._lock:
            self._flights.pop(m.task_id, None)
            self._aborted.discard(m.task_id)
            self._said.pop(m.task_id, None)

    def has_flights(self) -> bool:
        with self._lock:
            return bool(self._flights)

    def aborted(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._aborted

    def clearance(self, m, target: dict, snap: dict) -> Tuple[bool, str]:
        """(cleared, why not) for Overlord's CAS flight m on target."""
        if self.aborted(m.task_id):
            return False, "abort called, waiting for continue"
        from tactical import link
        from tactical.models import dcs_key
        from common.overlord import target_state
        st = link.status(dcs_key(target.get("id")))
        if st is None:
            st = target_state(snap, target)
        if st != "alive":
            return False, f"the {_say_type(target['type'])} isn't confirmed in the picture"
        lat, lon = float(target["lat"]), float(target["lon"])
        if self.warlord is not None:
            z = rules.zone_at(self.warlord.block["no_strike"], lat, lon)
            if z:
                return False, f"the target is inside the {z['name']} no-strike zone"
        near = rules.friendlies_near(snap, lat, lon, self.danger_close_m)
        if near:
            f = near[0]
            return False, f"friendly {_say_type(f['type'])} {f['d']:.0f} metres from the target"
        try:                                              # R6: your own aircraft low near the target
            outcasts = self.outcasts() or {}
        except Exception:
            outcasts = {}
        low = rules.own_low_near(snap, lat, lon, self.danger_close_m, self.agl_m, outcasts,
                                 rules.alt_of(snap, target.get("id")))
        if low:
            f = low[0]
            who = "you're" if f["who"] == "you" else f"{f['who']} is"
            return False, f"{who} {f['d']:.0f} metres from the target"
        with self._lock:
            first = near is None and m.task_id not in self._caution
            if first:
                self._caution.add(m.task_id)
        if first:
            self.log("darkside", "Caution: friendly positions not reported (older Export.lua); "
                                 "danger close can't be checked.")
        return True, ""

    def check_in(self, m) -> None:
        self._ai("ai", f"{m.cs} 1: Darkside, {m.cs} 1, {m.kind.desc} at the IP, weapons hold.", "george")
        from common import nine_line
        if nine_line.AI_TO_AI:
            try:
                self.brief_flight(m)
            except Exception as e:                       # never hold up the CAS loop
                print(f"[DARKSIDE] 9-line to {m.cs} failed: {e}")

    def clear(self, m, target: dict) -> bool:
        """Cleared hot: weapons free and the attack, sent as Darkside. False if Olympus refused."""
        self.tasks.set_state(m.task_id, "cleared", f"cleared hot on {_say_type(target['type'])}")
        if not self.attack(m, target):
            return False
        self._ai("ai", f"Darkside: {m.cs} 1, cleared hot, {_say_type(target['type'])}.", "darkside")
        with self._lock:
            self._said.pop(m.task_id, None)
        if exchange.short():
            self.say(f"{m.cs} cleared hot.")
        else:
            self.say(f"{m.cs} is cleared hot, {_say_type(target['type'])}, {m.where}.")
        return True

    def attack(self, m, target: dict) -> bool:
        """The next target, already cleared (after a kill)."""
        with acting_as(DARKSIDE, m.task_id):
            ok1, why1 = self.oly.command("setROE", {"ID": m.lead_id, "ROE": ROE_FREE})
            ok2, why2 = self.oly.command("attackUnit", {"ID": m.lead_id, "targetID": target["id"]})
        if not (ok1 and ok2):
            self.hold(m, f"Olympus refused the attack: {why1 or why2}")
            return False
        self.tasks.set_state(m.task_id, "attacking", _say_type(target["type"]))
        return True

    def hold(self, m, why: str) -> None:
        """Weapons hold; the flight goes back to the IP (Overlord's loop moves it)."""
        with acting_as(DARKSIDE, m.task_id):
            self.oly.command("setROE", {"ID": m.lead_id, "ROE": ROE_HOLD})
        self.tasks.set_state(m.task_id, "holding", why)

    def withheld(self, m, why: str) -> None:
        """Not cleared: said once per reason (not after an abort: the abort reply said it)."""
        if self.aborted(m.task_id):
            return
        with self._lock:
            if self._said.get(m.task_id) == why:
                return
            self._said[m.task_id] = why
        self.say(f"holding {m.cs} at the IP, {why}.")

    def good_effect(self, m, what: str, left: int, target_id=None) -> None:
        if target_id is not None and not self._first_report(target_id):
            return
        self.say(f"good effect, {what} destroyed." + (f" {left} to go." if left and not exchange.short() else ""))

    def area_clear(self, m) -> None:
        self.say(f"area clear. {m.cs} returning to base." if not exchange.short() else f"area clear, {m.cs} RTB.")

    def abort(self) -> str:
        with self._lock:
            flights = list(self._flights.values())
            self._aborted.update(m.task_id for m in flights)
        if not flights:
            return f"{self._lead()}no flight under my control."
        for m in flights:
            if m.lead_id is not None:
                self.hold(m, "abort called by the pilot")
        names = " and ".join(m.cs for m in flights)
        return (f"{self._lead()}abort, abort, abort. {names} weapons hold, back to the IP. "
                f"Say Darkside, continue, when ready.")

    def resume(self) -> str:
        with self._lock:
            names = [m.cs for m in self._flights.values() if m.task_id in self._aborted]
            self._aborted.clear()
        if not names:
            return f"{self._lead()}nothing on hold."
        return f"{self._lead()}copy, continuing. {' and '.join(names)} cleared again from the IP."
