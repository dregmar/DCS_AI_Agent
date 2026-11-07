"""
common/warlord.py - WARLORD, the mission commander (Phase 2, P2-5).

Overlord asks him before every flight he spawns: review(kind, point, targets, snap) -> Decision.
Rules, in order (the first that applies decides; each has its spoken reason):

    1. no current tactical picture                      -> defer  ("no current picture")
    2. a target / the point inside a no-strike zone     -> deny
    3. CAS under a big SAM, no SEAD up or approved       -> deny   ("recommend SEAD first")
    4. CAS / SEAD with your side's units within danger close of a target (Export's own-side list, T8)
                                                         -> deny
    5. WARLORD_MAX_FLIGHTS tasked flights already up     -> defer
    6. priority areas written and the point in none      -> approve, with advice
    otherwise                                            -> approve

Towards you he only advises (decision 31 Oct): a denial is spoken with its reason and the request is
held HOLD_S seconds; "Warlord, override" launches it, restates the risk and logs the override to the
debrief. "Warlord, say objectives / priorities / ROE / no-strike" reads the hand-written "warlord"
block of data/campaign_briefing.json (command/rules.py).

He never tasks or attacks: his Olympus permission is "stop" only (command/permissions.py). Fixed
wording, no model (D6). Thread-safe: the held request is swapped under a lock; resume() is called
outside it.
"""
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from command import exchange, rules
from command.roles import Said
from command.tasking import TASKS, TaskRegistry
from common.debrief import DEBRIEF

HOLD_S = 60.0
MAX_FLIGHTS = 4
SAID = {"CAS": "CAS", "SEAD": "SEAD", "CAP": "CAP", "ESCORT": "escort"}

_OVERRIDE = re.compile(r"\b(?:override|over\s?ride|overwrite|approve it anyway|go anyway|send it anyway)\b", re.I)
_OBJECTIVES = re.compile(r"\b(?:objectives?|mission goals?|tasking order)\b", re.I)
_PRIORITIES = re.compile(r"\b(?:priorit(?:y|ies)|priority areas?)\b", re.I)
_ROE = re.compile(r"\b(?:roe|r o e|rules of engagement)\b", re.I)
_STATUS = re.compile(r"\b(?:status|sitrep|sit rep|situation report)\b", re.I)
_NO_STRIKE = re.compile(r"\b(?:no[\s-]?strikes?|no[\s-]?fire|restricted)\b", re.I)


@dataclass(frozen=True)
class Decision:
    verdict: str                 # "approve" | "deny" | "defer"
    reason: str = ""             # why (said after "denied." / "stand by on ...:")
    advice: str = ""             # what he recommends, or a note with an approval
    risk: str = ""               # restated on an override
    hard: bool = False           # R1: no-strike / danger close can't be overridden
    rule: str = ""               # which rule decided: no_strike, danger_close, sam, picture, flights, priority


HARD_SAID = {"no_strike": "a no-strike zone", "danger_close": "danger close"}


def _with_caution(caution: str, advice: str) -> str:
    """R6: the caution first, then any other note ("Caution, ZSU-23 near the target. Also, the SA-8 ...")."""
    if not caution:
        return advice
    if not advice:
        return caution
    return f"{caution} Also, {advice[0].lower() + advice[1:]}"


def _say_type(name: str) -> str:
    from common.overlord import say_type
    return say_type(name)


class Warlord:
    def __init__(self, snapshot: Callable[[], dict], callsign: Callable[[], str] = lambda: "Nomad",
                 block: Optional[Dict] = None, tasks: TaskRegistry = TASKS,
                 picture_active: Optional[Callable[[], bool]] = None, max_flights: int = MAX_FLIGHTS,
                 danger_close_m: float = rules.DANGER_CLOSE_M, clock: Callable[[], float] = time.monotonic,
                 debrief=DEBRIEF):
        self.snapshot, self.callsign, self.tasks = snapshot, callsign, tasks
        self.block = block if block is not None else rules.parse_block({})
        if picture_active is None:
            from tactical import link
            picture_active = link.active
        self.picture_active, self.max_flights, self.danger_close_m = picture_active, max_flights, danger_close_m
        self.caution_m = rules.CAUTION_M                # R6 (WARLORD_CAUTION_M in main.py)
        self._clock, self.debrief = clock, debrief
        self._lock = threading.Lock()
        self._held = None                       # (task_id, kind, Decision, resume, when)
        self._last_hard = None                  # (kind, Decision, when): "nothing to override" says why

    # ------------------------------------------------------------ the rules
    def review(self, kind: str, point, targets: List[dict], snap: dict) -> Decision:
        if not self.picture_active():
            return Decision("defer", "no current tactical picture", rule="picture")
        skip_note = ""
        if kind in ("CAS", "SEAD"):
            # R1: the request inside a no-strike zone (its point, or every target) is a hard denial; single
            # targets inside are skipped after launch (the permission table refuses attacks on them)
            placed = [t for t in targets or [] if t.get("lat") is not None]
            inside = [(t, rules.zone_at(self.block["no_strike"], float(t["lat"]), float(t["lon"]))) for t in placed]
            inside = [(t, z) for t, z in inside if z]
            pz = rules.zone_at(self.block["no_strike"], float(point[0]), float(point[1])) if point is not None else None
            if pz or (placed and len(inside) == len(placed)):
                z = pz or inside[0][1]
                return Decision("deny", f"the target is inside the {z['name']} no-strike zone",
                                "Pick another target.", f"a strike inside the {z['name']} no-strike zone",
                                hard=True, rule="no_strike")
            if inside:
                t, z = inside[0]
                more = f" and {len(inside) - 1} more" if len(inside) > 1 else ""
                skip_note = f"the {_say_type(t['type'])}{more} in the {z['name']} no-strike zone will be skipped."
        if kind == "CAS" and point is not None:
            from common.overlord import big_sams_near
            sams = big_sams_near(snap, (float(point[0]), float(point[1])))
            sead_up = any(t.kind == "SEAD" for t in self.tasks.active())
            if sams and not sead_up:
                sam = _say_type(sams[0]["type"])
                return Decision("deny", f"{sam} covers the target and no SEAD is up", "Recommend SEAD first.",
                                f"Hog goes in under an {sam}", rule="sam")
        if kind in ("CAS", "SEAD"):
            for t in targets or []:
                if t.get("lat") is None:
                    continue
                near = rules.friendlies_near(snap, float(t["lat"]), float(t["lon"]), self.danger_close_m)
                if near:
                    f = near[0]
                    return Decision("deny", f"friendly {_say_type(f['type'])} {f['d']:.0f} metres from the target",
                                    "Danger close.", f"danger close, friendlies {f['d']:.0f} metres away",
                                    hard=True, rule="danger_close")
        up = len([t for t in self.tasks.active() if t.owner != "PILOT"])    # R3: your Outcasts don't count
        if up >= self.max_flights:
            return Decision("defer", f"{up} flights already up", rule="flights")
        caution = self._caution(point, targets) if kind == "CAS" else ""
        areas = self.block["priority_areas"]
        if areas and point is not None and not rules.zone_at(areas, float(point[0]), float(point[1])):
            names = ", ".join(a["name"] for a in areas[:2])
            note = f"this is outside the priority areas. {names} come first."
            return Decision("approve", "", _with_caution(caution, (skip_note[0].upper() + skip_note[1:] + " Also, "
                                                                   + note) if skip_note else note), rule="priority")
        return Decision("approve", "", _with_caution(caution, skip_note),
                        rule="no_strike" if skip_note else "caution" if caution else "")

    def _caution(self, point, targets: List[dict]) -> str:
        """R6: MANPADS, AAA or short-range IR SAMs within WARLORD_CAUTION_M of a CAS target, from the picture's
        threat list: "Caution, ZSU-23 and Igla near the target." Not a denial. '' when none (or no picture)."""
        from tactical import link
        spots = [(float(t["lat"]), float(t["lon"])) for t in targets or [] if t.get("lat") is not None]
        if point is not None:
            spots.append((float(point[0]), float(point[1])))
        names, seen = [], set()
        for lat, lon in spots[:8]:
            for r in link.threats_near(lat, lon, self.caution_m) or []:
                if r.get("category") in ("MANPADS", "AAA", "IR SAM") and r["id"] not in seen:
                    seen.add(r["id"])
                    n = _say_type(r["type"])
                    if n not in names:
                        names.append(n)
        if not names:
            return ""
        said = names[0] if len(names) == 1 else ", ".join(names[:2]) + " and " + names[2] if len(names) >= 3 \
            else " and ".join(names)
        return f"Caution, {said} near the target."

    # ------------------------------------------------------------ what he says
    def _lead(self) -> str:
        return exchange.lead(self.callsign(), "Warlord")

    def approve_text(self, kind: str) -> str:
        """His answer to Overlord in the full form ("Overlord, Warlord, CAS approved."); '' when short."""
        return "" if exchange.short() else f"Overlord, Warlord, {SAID.get(kind, kind)} approved."

    def deny_text(self, kind: str, d: Decision) -> str:
        advice = f" {d.advice}" if d.advice else ""
        tail = " This one can't be overridden." if d.hard else " Say Warlord, override, to go anyway."
        return f"{self._lead()}{SAID.get(kind, kind)} denied. {d.reason[0].upper() + d.reason[1:]}.{advice}{tail}"

    def defer_text(self, kind: str, d: Decision, wait_s: Optional[float] = None) -> str:
        """R7: with wait_s the request is kept and rechecked, so you needn't ask again."""
        if wait_s:
            mins = max(1, round(wait_s / 60))
            return (f"{self._lead()}stand by on {SAID.get(kind, kind)}: {d.reason}. I'll keep it for "
                    f"{mins} minute{'s' if mins != 1 else ''} and call you when it's approved.")
        return f"{self._lead()}stand by on {SAID.get(kind, kind)}: {d.reason}. Request again shortly."

    def now_approved_text(self, kind: str) -> str:
        """R7: a deferred request that now passes."""
        return f"{self._lead()}{SAID.get(kind, kind)} now approved."

    def expired_text(self, kind: str, reason: str) -> str:
        """R7: the wait ran out."""
        return f"{self._lead()}{SAID.get(kind, kind)} request dropped, still {reason}. Request again when ready."

    def advice_text(self, kind: str, d: Decision) -> str:
        if d.advice.startswith("Caution, "):              # R6: "CAS approved. Caution, ZSU-23 near the target."
            return (f"{self._lead()}{d.advice[0].lower() + d.advice[1:]}" if exchange.short()
                    else f"{self._lead()}{SAID.get(kind, kind)} approved. {d.advice}")
        return (f"{self._lead()}note, {d.advice}" if exchange.short()
                else f"{self._lead()}{SAID.get(kind, kind)} approved. Note, {d.advice}")

    # ------------------------------------------------------------ deny / override
    def hold(self, task_id: str, kind: str, d: Decision, resume: Callable[[], str]) -> None:
        """A denied request, kept HOLD_S seconds for "Warlord, override". resume() launches it and
        returns what Overlord says. A hard denial (R1) is never held: note_hard() instead."""
        if d.hard:
            self.note_hard(kind, d)
            return
        with self._lock:
            self._held = (task_id, kind, d, resume, self._clock())

    def note_hard(self, kind: str, d: Decision) -> None:
        """A hard denial: remembered only to say why there's nothing to override."""
        with self._lock:
            self._held = None
            self._last_hard = (kind, d, self._clock())

    def held(self) -> Optional[tuple]:
        with self._lock:
            h = self._held
        if h is None or self._clock() - h[4] > HOLD_S:
            return None
        return h

    def override(self) -> str:
        with self._lock:
            h, self._held = self._held, None
        if h is None or self._clock() - h[4] > HOLD_S:
            with self._lock:
                last = self._last_hard
            if last is not None and self._clock() - last[2] <= HOLD_S:
                return (f"{self._lead()}nothing to override. The last denial was "
                        f"{HARD_SAID.get(last[1].rule, 'a hard rule')} and can't be overridden.")
            return f"{self._lead()}nothing to override."
        task_id, kind, d, resume, _ = h
        self.tasks.set_state(task_id, "overridden", f"pilot override: {d.reason}")
        self.debrief.override(f"{SAID.get(kind, kind)} despite: {d.reason}")
        try:
            launched = resume() or ""
        except Exception as e:                   # never lose the reply over a failed launch
            print(f"[WARLORD] resume failed: {type(e).__name__}: {e}")
            launched = "Overlord, unable to launch."
        risk = f" {d.risk[0].upper() + d.risk[1:]}." if d.risk else ""
        then = [(launched, "overlord")] if launched else []
        return Said(f"{self._lead()}override noted.{risk} Logged.", then=then)

    def status(self) -> str:
        """R2: flights up against the cap, a denial waiting for an override, overrides this mission."""
        live = [t for t in self.tasks.active() if t.owner != "PILOT"]
        parts = [f"{len(live)} of {self.max_flights} flights up" + (": " + ", ".join(
            f"{t.flight} {SAID.get(t.kind, t.kind)}" for t in live[:4]) if live else "")]
        yours = [t for t in self.tasks.active() if t.owner == "PILOT"]
        if yours:
            parts.append("yours: " + ", ".join(t.flight for t in yours[:4]))
        h = self.held()
        if h is not None:
            left = max(0, int(HOLD_S - (self._clock() - h[4])))
            parts.append(f"{SAID.get(h[1], h[1])} denial waiting for an override, {left} seconds left")
        waiting = getattr(self, "waiting_lines", lambda: [])()
        parts.extend(waiting)
        n = len(getattr(self.debrief, "overrides", []) or [])
        if n:
            parts.append(f"{n} override{'s' if n != 1 else ''} this mission")
        return f"{self._lead()}" + ". ".join(p[0].upper() + p[1:] if i else p for i, p in enumerate(parts)) + "."

    def caution_text(self, who: str, why: str, blocked: bool = False) -> str:
        """R3: one of your own attack orders breaks a mission rule."""
        return f"{self._lead()}caution. {who}'s target is {why}." + (" Attack not sent." if blocked else "")

    def held_left(self) -> Optional[Tuple[str, float]]:
        """(task_id, seconds left) of the denial waiting for an override, or None (kneeboard)."""
        h = self.held()
        return None if h is None else (h[0], max(0.0, HOLD_S - (self._clock() - h[4])))

    # ------------------------------------------------------------ "Warlord, ..."
    def handle(self, rest: str) -> Optional[str]:
        """What Warlord says to the rest of "Warlord, ...", or None if it isn't for him (radio checks
        and the bare name are main.py's)."""
        t = rest or ""
        b = self.block
        if _OVERRIDE.search(t):
            return self.override()
        if _STATUS.search(t):
            return self.status()
        if _OBJECTIVES.search(t):
            if not b["objectives"]:
                return f"{self._lead()}no objectives written for this campaign."
            return f"{self._lead()}objectives: " + "; ".join(b["objectives"][:4]) + "."
        if _NO_STRIKE.search(t):
            if not b["no_strike"]:
                return f"{self._lead()}no no-strike zones written."
            return f"{self._lead()}no-strike: " + ", ".join(z["name"] for z in b["no_strike"][:5]) + "."
        if _PRIORITIES.search(t):
            if not b["priority_areas"]:
                return f"{self._lead()}no priority areas written. Everything is open."
            return f"{self._lead()}priorities: " + ", ".join(z["name"] for z in b["priority_areas"][:5]) + "."
        if _ROE.search(t):
            return self._lead() + (b["roe"] if b["roe"] else "no ROE written. Briefed ROE applies.")
        return None
