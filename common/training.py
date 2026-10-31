"""
common/training.py - the training-mode gate (audit step 7, approved 30 Oct). Feature 8 (Overlord
training targets) plugs into this; this module spawns nothing itself.

Feature 8 needs a Game Master login to Olympus, which can create, change and delete anything on
either side. Everything that login sends passes Training.guard first:

    spawnGroundUnits / spawnHelicopters / spawnAircrafts
        only while training mode is on; only for the enemy coalition; every spawn location within
        TRAINING_MAX_RANGE_KM of you; at most TRAINING_MAX_GROUPS groups and TRAINING_MAX_UNITS units
        alive (decision 7); never with other human players in the mission (decision 8)
    deleteUnit / setROE / setPath
        only for a unit of a tracked training group (never a mission unit, your flight, Overlord's
        flights or an Outcast), training on or off (so clean-up always works)
    anything else
        never (and the client's own allow-list, common/olympus.py, holds only these six)

The Blue commander client (wingmen, Overlord, Outcasts) is untouched and never sends these.

Voice, Overlord only: "Overlord, training mode on / off", "Overlord, clear training targets",
"Overlord, training status". Turning training off clears the targets first (decision 5). They are
also cleared when the program exits, and forgotten when a new mission loads (they went with it).

After a crash (decision 6) the list is on disk (logs/training_groups.json). At the next start, once
the mission briefing says the same mission is running, only units still there under the same group
name, unit name AND id are removed (unit ids restart with each mission, so an id alone could be
someone else); then the file is cleared.

Passwords: the Game Master password lives only in config.py and goes no further than the Olympus
client; nothing here logs, stores or returns it.
"""
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from core.state import enemy_lists_stale

SPAWNS = frozenset({"spawnGroundUnits", "spawnHelicopters", "spawnAircrafts"})
# setAltitudeType / setAltitude (feature 8): helicopter targets hold 150 m above the ground
UNIT_COMMANDS = frozenset({"deleteUnit", "setROE", "setPath", "setAltitudeType", "setAltitude"})
TRAINING_COMMANDS = SPAWNS | UNIT_COMMANDS
_COALITION = {1: "red", 2: "blue"}
_ENEMY = {1: 2, 2: 1}
PLAYERS_FRESH_S = 15.0
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs",
                            "training_groups.json")

# Olympus's site templates (scripts/templates.lua): one "unit" in a spawnGroundUnits order can be a
# whole battery (Patriot site 20 units, SA-10 18, ...), so the unit limit counts the template's units.
DEFAULT_TEMPLATES = os.path.join(os.path.expanduser("~"), "Saved Games", "DCS", "Mods", "Services", "Olympus",
                                 "scripts", "templates.lua")


def template_sizes(path: Optional[str]) -> Optional[Dict[str, int]]:
    """{template name: units} from Olympus's templates.lua, or None when it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            s = f.read()
    except (OSError, TypeError):
        return None
    sizes: Dict[str, int] = {}
    for m in re.finditer(r'-- end of \["([^"]+)"\]', s):            # each template ends with its name
        name = m.group(1)
        if name == "units":
            continue
        seg = s[s.rfind('["%s"]' % name, 0, m.start()):m.start()]
        if '["units"]' in seg:
            sizes[name] = len(re.findall(r'\["dx"\]', seg))
    return sizes


_ON = re.compile(r"\btraining(?: mode)? (?:on|active|start)\b|"
                 r"\b(?:start|begin|enter|enable|turn on) (?:the )?training\b")
# 30 Oct flight: "end the training", "exit training (mode)", "cancel training", "abort training" all missed
_OFF = re.compile(r"\btraining(?: mode)? (?:off|stop|end|over|finished|complete)\b|"
                  r"\b(?:stop|end|exit|leave|cancel|abort|finish|quit|terminate|disable|turn off) "
                  r"(?:the |our |my )?training\b")
_QUESTION = re.compile(r"\?|^(?:how|what|why|when|where|can|could|should|would|is|are|do|does)\b")
_CLEAR = re.compile(r"\b(?:clear|remove|delete) (?:the |all )?training(?: targets?| groups?)?\b")
_STATUS = re.compile(r"\btraining status\b|\bstatus,? training\b")


def _dist_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    from common.overlord import dist_brg
    return dist_brg(a, b)[0]


def _locations(params: Dict[str, Any]) -> List[Tuple[float, float]]:
    """Every lat/lng in a spawn order: its own "location" and each unit's."""
    out = []
    for loc in [params.get("location")] + [u.get("location") for u in params.get("units") or []
                                            if isinstance(u, dict)]:
        if isinstance(loc, dict):
            try:
                out.append((float(loc["lat"]), float(loc["lng"])))
            except (KeyError, TypeError, ValueError):
                out.append((float("nan"), float("nan")))        # unreadable: refused below
    return out


class Training:
    def __init__(self, snapshot: Callable[[], dict], make_client: Callable[..., Any],
                 mission_name: Callable[[], str] = lambda: "", enabled: Optional[bool] = None,
                 voice_switch: Optional[bool] = None, max_groups: Optional[int] = None,
                 max_units: Optional[int] = None, max_range_km: Optional[float] = None,
                 path: Optional[str] = "config", clock: Callable[[], float] = time.monotonic,
                 printer: Callable[[str], None] = print, templates: Any = "config"):
        """make_client(guard) -> an Olympus client logged in as Game Master, or None when there's
        no Game Master password in config (called once, on first use). templates: {name: units} of
        Olympus's site templates ("config": read templates.lua; None: unreadable, ground refused)."""
        self.snapshot, self._make_client, self.mission_name = snapshot, make_client, mission_name
        self.on = bool(getattr(config, "TRAINING_MODE", False) if enabled is None else enabled)
        self.voice_switch = bool(getattr(config, "TRAINING_VOICE_SWITCH", True) if voice_switch is None
                                 else voice_switch)
        self.max_groups = int(max_groups if max_groups is not None else getattr(config, "TRAINING_MAX_GROUPS", 6))
        self.max_units = int(max_units if max_units is not None else getattr(config, "TRAINING_MAX_UNITS", 24))
        self.max_range_km = float(max_range_km if max_range_km is not None
                                  else getattr(config, "TRAINING_MAX_RANGE_KM", 50))
        self.path = getattr(config, "TRAINING_LIST_PATH", DEFAULT_PATH) if path == "config" else path
        self.clock, self.printer = clock, printer
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._groups: List[dict] = []          # {"group": name or None, "units": [(name, id)], "requested": n, "what": str}
        self._client, self._client_made = None, False
        self.on_destroyed: Optional[Callable[[List[str]], None]] = None   # kneeboard alert
        self._players: Optional[Tuple[int, bool, float]] = None
        self._recover: Optional[dict] = self._load()
        if templates == "config":
            templates = template_sizes(getattr(config, "OLYMPUS_TEMPLATES_PATH", None) or DEFAULT_TEMPLATES)
        self.templates: Optional[Dict[str, int]] = templates

    # ------------------------------------------------------------ the Game Master client
    def client(self):
        with self._lock:
            if self._client_made:
                return self._client
            self._client_made = True
        c = self._make_client(self.guard)
        with self._lock:
            self._client = c
        return c

    # ------------------------------------------------------------ who's connected
    def note_players(self, text: str) -> None:
        """'PLAYERS 3 mp' from george_comms_hook.lua."""
        m = re.match(r"PLAYERS (\d+) (mp|sp)", (text or "").strip())
        if m:
            with self._lock:
                self._players = (int(m.group(1)), m.group(2) == "mp", self.clock())

    def others_present(self) -> Optional[bool]:
        """True with other people connected, False alone, None when the hook hasn't said lately."""
        with self._lock:
            p = self._players
        if p is None or self.clock() - p[2] > PLAYERS_FRESH_S:
            return None
        return p[1] and p[0] > 1

    # ------------------------------------------------------------ the fence
    def guard(self, name: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        if name not in TRAINING_COMMANDS:
            return False, f"{name} isn't a training command"
        if name in UNIT_COMMANDS:
            try:
                uid = int(params.get("ID"))
            except (TypeError, ValueError):
                return False, "no unit id"
            if not self._is_tracked(uid):
                return False, f"unit {uid} isn't a training target"
            if name == "deleteUnit" and params.get("explosion"):
                # Olympus "deletes" with a 250 kg explosion when asked (and always for a player's unit)
                return False, "no explosions"
            return True, ""
        why = self.spawn_refusal(params, ground=(name == "spawnGroundUnits"))
        return (why is None), (why or "")

    def units_in(self, params: Dict[str, Any], ground: bool) -> Optional[int]:
        """Real units a spawn order makes: a ground template counts all its units. None = can't tell."""
        entries = [u for u in params.get("units") or [] if isinstance(u, dict)]
        if not ground:
            return max(1, len(entries))
        if self.templates is None:
            return None
        return max(1, sum(self.templates.get(str(u.get("unitType")), 1) for u in entries))

    def spawn_refusal(self, params: Dict[str, Any], ground: bool = True) -> Optional[str]:
        """Why this spawn isn't allowed, or None."""
        if not self.on:
            return "training mode is off"
        others = self.others_present()
        if others is None:
            return "I can't tell who else is in the mission (copy the new comms hook)"
        if others:
            return "other players are in the mission"
        snap = self.snapshot()
        enemy = _COALITION.get(_ENEMY.get(snap.get("coalition")))
        if enemy is None:
            return "your coalition isn't known yet"
        coal = str(params.get("coalition") or "").lower()
        if coal != enemy:
            return f"training targets must be {enemy}, not {coal or 'unspecified'}"
        pts = _locations(params)
        if not pts:
            return "no spawn location"
        me = snap.get("my_ll")
        if not me:
            return "your position isn't known"
        far = max((_dist_m((me[0], me[1]), p) for p in pts), default=0.0)
        if not far <= self.max_range_km * 1000:                    # NaN (unreadable) is refused too
            return f"spawn point more than {self.max_range_km:.0f} km from you"
        new = self.units_in(params, ground)
        if new is None:
            return "I can't read Olympus's site templates, so I can't count the units"
        groups, units = self.counts(snap)
        if groups + 1 > self.max_groups:
            return f"already {groups} training groups, the limit is {self.max_groups}"
        if units + new > self.max_units:
            return f"{units} training units alive, {new} more would pass the limit of {self.max_units}"
        return None

    # entries are compared by identity: two identical pending requests are still two groups
    def _has(self, entry: dict) -> bool:
        return any(g is entry for g in self._groups)

    def _remove(self, entry: dict) -> None:
        self._groups = [g for g in self._groups if g is not entry]

    def has_groups(self) -> bool:
        with self._lock:
            return bool(self._groups)

    def _is_tracked(self, uid: int) -> bool:
        with self._lock:
            return any(int(i) == uid for g in self._groups for _, i in g["units"])

    # ------------------------------------------------------------ the tracked groups
    def add_group(self, group: Optional[str], units: List[Tuple[str, int]], what: str,
                  requested: Optional[int] = None) -> dict:
        """Feature 8: a training group it spawned (units [(unit name, id)] once Export.lua shows it;
        until then group None and the requested unit count, so the limits already count it).
        Returns the entry (for bind / drop); entry["ended"] says why it left the list."""
        entry = {"group": group, "units": [(str(n), int(i)) for n, i in units],
                 "requested": int(requested if requested is not None else len(units)), "what": str(what),
                 "ended": None}
        with self._lock:
            self._groups.append(entry)
        self._save()
        return entry

    def bind(self, entry: dict, group: str, units: List[Tuple[str, int]]) -> bool:
        """The spawned group appeared in DCS: its name and unit ids. False if the entry already left the
        list (cleared, or a new mission, before it appeared)."""
        with self._lock:
            if entry.get("ended") or not self._has(entry):
                return False
            entry["group"], entry["units"] = str(group), [(str(n), int(i)) for n, i in units]
        self._save()
        return True

    def drop(self, entry: dict, reason: str) -> None:
        """A spawn that never appeared: out of the list (nothing to delete)."""
        with self._lock:
            self._remove(entry)
            entry["ended"] = entry.get("ended") or reason
        self._save()

    def discard(self, group: str, units: List[Tuple[str, int]], what: str) -> None:
        """A group that appeared after its request was cleared: remove it from DCS too."""
        entry = self.add_group(group, units, what)
        self._delete([entry])

    @staticmethod
    def new_groups(snap: dict, known: set) -> Dict[str, List[Tuple[str, int]]]:
        """Feature 8: enemy-side Olympus groups that weren't there before a spawn."""
        out: Dict[str, List[Tuple[str, int]]] = {}
        for u in snap.get("olympus_enemy") or []:
            if u[0] not in known:
                out.setdefault(str(u[0]), []).append((str(u[1]), int(u[2])))
        return out

    def counts(self, snap: Optional[dict] = None) -> Tuple[int, int]:
        """(groups, units) alive. Groups whose units are all gone (fresh lists) are dropped."""
        snap = self.snapshot() if snap is None else snap
        fresh = not enemy_lists_stale(snap) and snap.get("enemy_lists_age") is not None
        alive_ids = {int(u[2]) for u in snap.get("olympus_enemy") or []}
        gone = []
        groups = units = 0
        with self._lock:
            for g in self._groups:
                if g["group"] and g["units"]:
                    n = sum(1 for _, i in g["units"] if i in alive_ids) if fresh else len(g["units"])
                    if n == 0:
                        gone.append(g)
                        continue
                else:
                    n = g["requested"]                          # spawned, not seen yet
                groups += 1
                units += n
            for g in gone:
                self._remove(g)
                g["ended"] = "destroyed"
        if gone:
            self.printer(f"[TRAINING] {len(gone)} training group(s) destroyed: "
                         + ", ".join(str(g["group"]) for g in gone))
            self._save()
            if self.on_destroyed is not None:
                try:
                    self.on_destroyed([str(g.get("what") or g["group"]) for g in gone])
                except Exception as e:
                    self.printer(f"[TRAINING] alert: {type(e).__name__}: {e}")
        return groups, units

    def clear(self) -> Tuple[int, str]:
        """Removes every tracked group. (groups removed, why not all) - why is '' when all went."""
        with self._lock:
            groups = list(self._groups)
        if not groups:
            return 0, ""
        return self._delete(groups)

    def _delete(self, groups: List[dict]) -> Tuple[int, str]:
        c = self.client()
        if c is None:
            return 0, "no Game Master login in config"
        removed, why = 0, ""
        for g in groups:
            ok_all = True
            for _, uid in g["units"]:
                ok, err = c.command("deleteUnit", {"ID": uid, "explosion": False, "explosionType": "",
                                                   "immediate": True})
                if not ok:
                    ok_all, why = False, err
            if ok_all:
                removed += 1
                with self._lock:
                    self._remove(g)
                    g["ended"] = g.get("ended") or "cleared"
        self.printer(f"[TRAINING] Cleared {removed} of {len(groups)} training group(s)"
                     + (f"; {why}" if why else "."))
        self._save()
        return removed, why

    def forget(self) -> None:
        """A new mission loaded: its training groups went with the old one."""
        with self._lock:
            for g in self._groups:
                g["ended"] = g.get("ended") or "forgotten"
            had, self._groups, self._recover = len(self._groups), [], None
        if had:
            self.printer(f"[TRAINING] New mission: forgot {had} training group(s).")
        self._save()

    def shutdown(self) -> None:
        """Program exit: remove the training targets (best effort)."""
        try:
            if self.has_groups():
                self.clear()
        except Exception as e:
            self.printer(f"[TRAINING] Clean-up at exit failed: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ voice (Overlord)
    def wanted(self, rest: str) -> bool:
        """A training order (never a question: "how do I end the training?" goes to the briefing)."""
        t = (rest or "").lower().strip()
        if _QUESTION.search(t):
            return False
        return bool(_ON.search(t) or _OFF.search(t) or _CLEAR.search(t) or _STATUS.search(t))

    def handle(self, rest: str) -> Optional[str]:
        t = (rest or "").lower()
        if _CLEAR.search(t):
            return self._say_clear()
        if _OFF.search(t):
            return self.set_mode(False)
        if _ON.search(t):
            return self.set_mode(True)
        if _STATUS.search(t):
            return self.status_text()
        return None

    def set_mode(self, on: bool) -> str:
        if on:
            if self.on:
                return "Overlord, training mode is already on."
            if not self.voice_switch:
                return "Overlord, unable, training mode is switched off in config."
            if self.client() is None:
                return "Overlord, unable, no Game Master login in config."
            others = self.others_present()
            if others is None:
                return "Overlord, unable, I can't tell who else is in the mission. Copy the new comms hook."
            if others:
                return "Overlord, unable, other players are in the mission."
            self.on = True
            self.printer("[TRAINING] Training mode on.")
            return "Overlord, training mode on. Training targets allowed."
        if not self.on and not self.has_groups():
            return "Overlord, training mode is already off."
        removed, why = self.clear()
        self.on = False
        self.printer("[TRAINING] Training mode off.")
        if why:
            return f"Overlord, training mode off. Unable to remove all training targets, {why}."
        if removed:
            return f"Overlord, {removed} training group{'s' if removed != 1 else ''} removed. Training mode off."
        return "Overlord, training mode off."

    def _say_clear(self) -> str:
        if not self.has_groups():
            return "Overlord, no training targets."
        removed, why = self.clear()
        if why:
            return f"Overlord, {removed} training group{'s' if removed != 1 else ''} removed. Unable to remove the rest, {why}."
        return f"Overlord, {removed} training group{'s' if removed != 1 else ''} removed."

    def status_text(self) -> str:
        if not self.on:
            return "Overlord, training mode off."
        groups, units = self.counts()
        return (f"Overlord, training mode on, {groups} group{'s' if groups != 1 else ''}, "
                f"{units} unit{'s' if units != 1 else ''} alive.")

    def link(self) -> dict:
        """Kneeboard Status page row (never creates the client just to show it)."""
        name = "Training (Game Master)"
        if not getattr(config, "OLYMPUS_GM_PASSWORD", ""):
            return {"name": name, "ok": False if self.on else None,
                    "detail": ("on" if self.on else "off") + "; no Game Master login in config"}
        parts, ok = ["on" if self.on else "off"], None
        with self._lock:
            c = self._client if self._client_made else None
        if c is not None and hasattr(c, "status"):
            st = c.status()
            if self.on:
                ok = bool(st.get("connected"))
            parts.append("GM connected" if st.get("connected") else
                         "GM not connected" + (f": {st['last_error']}" if st.get("last_error") else ""))
        groups, units = self.counts()
        if groups:
            parts.append(f"{groups} group(s), {units} unit(s) alive")
        return {"name": name, "ok": ok, "detail": "; ".join(parts)}

    # ------------------------------------------------------------ the list on disk (decision 6)
    def _save(self) -> None:
        if not self.path:
            return
        mission = self.mission_name() or ""                # outside our lock (the briefing has its own)
        with self._lock:
            data = {"_note": "Training targets the program spawned (audit step 7). Removed at the next "
                             "start if the same mission is still running; safe to delete.",
                    "mission": mission,
                    "groups": [{"group": g["group"], "units": [list(u) for u in g["units"]], "what": g["what"]}
                               for g in self._groups if g["group"] and g["units"]]}
        with self._save_lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1)
                os.replace(tmp, self.path)
            except Exception as e:
                self.printer(f"[TRAINING] Couldn't save {self.path}: {type(e).__name__}: {e}")

    def _load(self) -> Optional[dict]:
        if not self.path or not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if data.get("groups") else None
        except Exception as e:
            self.printer(f"[TRAINING] Couldn't read {self.path}: {type(e).__name__}: {e}")
            return None

    def recover(self) -> Optional[int]:
        """After a crash: once the briefing says which mission is running and the enemy lists are
        fresh, removes the saved groups' units that are still there (same group, unit name and id).
        Returns how many units were removed, or None while it's still waiting (or nothing to do)."""
        saved = self._recover
        if not saved:
            return None
        mission = self.mission_name() or ""
        snap = self.snapshot()
        if not mission or snap.get("enemy_lists_age") is None or enemy_lists_stale(snap):
            return None                                 # not sure yet: wait
        self._recover = None
        if saved.get("mission") != mission:
            self.printer(f"[TRAINING] Saved training targets were for '{saved.get('mission')}', "
                         f"not this mission: nothing removed.")
            self._save()
            return 0
        present = {(str(u[0]), str(u[1]), int(u[2])) for u in snap.get("olympus_enemy") or []}
        found = []
        for g in saved.get("groups") or []:
            units = [(str(n), int(i)) for n, i in g.get("units") or [] if (g.get("group"), str(n), int(i)) in present]
            if units:
                found.append({"group": g["group"], "units": units, "requested": len(units),
                              "what": g.get("what", "")})
        if not found:
            self.printer("[TRAINING] No saved training targets still in this mission.")
            self._save()
            return 0
        with self._lock:
            self._groups.extend(found)
        removed, why = self.clear()
        n = sum(len(g["units"]) for g in found)
        self.printer(f"[TRAINING] Removed {n} training unit(s) left from the last run"
                     + (f" ({why})" if why else "."))
        return n if not why else 0
