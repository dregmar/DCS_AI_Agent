"""
common/comms_menu.py
Voice control of the DCS radio (comms) menu, for any aircraft.

With Saved Games\\DCS\\Scripts\\Hooks\\george_comms_hook.lua installed, George reads the menu
(UDP 5011) and drives it (UDP 5012), so you can talk to it by name:
    "radio menu"                  opens it and reads the choices
    "ATC" / "Kobuleti" / "five"   picks an item by name or number while it's open
    "back" / "close menu"
    "radio, ATC, request startup" several levels in one go ("request ...", "ask/tell ..." too)
Without the hook it falls back to keys (\\ and F1..F12) and can't read the menu back.
"""
import re
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import config
from core.state import state_store

MENU_PORT = getattr(config, "COMMS_MENU_PORT", 5011)
CMD_PORT = getattr(config, "COMMS_CMD_PORT", 5012)
LIVE_SEC = 3.0
STEP_SEC = 1.2

KEY_MENU_OPEN_SEC = 20.0     # keys only: treat the menu as open this long after the last key

_NUM_WORDS = {"level": 11, "leven": 11, "elven": 11, "eleventh": 11, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "won": 1, "to": 2,
              "too": 2, "for": 4, "ate": 8}
_STOP_WORDS = {"the", "a", "to", "for", "please", "george", "chief", "radio", "comms", "comm", "com",
               "menu", "and", "then", "request", "requesting", "call", "tell", "ask", "us", "me",
               "with", "on", "of", "go", "select", "option", "item", "number", "okay", "ok"}
_SYNONYMS = {"tower": "atc", "ground": "ground", "controller": "atc", "atc": "atc",
             "awacs": "awacs", "magic": "awacs", "darkstar": "awacs", "overlord": "awacs",
             "tanker": "tanker", "texaco": "tanker", "arco": "tanker", "shell": "tanker",
             "jtac": "jtac", "wingman": "wingman", "flight": "flight", "crew": "crew",
             "start": "startup", "startup": "startup", "start-up": "startup", "rearm": "rearm",
             "refuel": "refuel", "refueling": "refuel", "takeoff": "takeoff", "take": "takeoff",
             "off": "", "inbound": "inbound", "landing": "landing", "land": "landing",
             "taxi": "taxi", "other": "other", "f10": "other"}


def parse_menu(text: str) -> dict:
    """'MENU 1\\nCAPTION x\\nITEM 1 F1. ATC...' -> {"visible", "caption", "items": {n: name}}"""
    lines = text.splitlines()
    out = {"visible": bool(lines) and lines[0].strip() == "MENU 1", "caption": "", "items": {},
           "subs": set()}                    # item numbers that open a submenu ("..." / ">>")
    for line in lines[1:]:
        if line.startswith("CAPTION "):
            out["caption"] = line[8:].strip()
        elif line.startswith("ITEM "):
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1].isdigit():
                out["items"][int(parts[1])] = clean_item(parts[2])
                if "..." in parts[2] or ">>" in parts[2]:
                    out["subs"].add(int(parts[1]))
    return out


def parse_route(text: str) -> list:
    """'ROUTE 0,1000,2000;1,5000,6000' -> [(0, 1000.0, 2000.0), (1, 5000.0, 6000.0)]"""
    out = []
    for part in text[len("ROUTE"):].strip().split(";"):
        bits = part.split(",")
        if len(bits) == 3:
            try:
                out.append((int(bits[0]), float(bits[1]), float(bits[2])))
            except ValueError:
                pass
    return out


def parse_wpnames(text: str) -> list:
    """'WPNAMES\n3\t1000\t2000\tIP' -> [(3, 1000.0, 2000.0, 'IP')] (5 Nov: the 9-line's IP)."""
    out = []
    for line in text.split("\n")[1:]:
        bits = line.split("\t")
        if len(bits) >= 4:
            try:
                out.append((int(bits[0]), float(bits[1]), float(bits[2]), bits[3].strip()))
            except ValueError:
                pass
    return out


def parse_flight(text: str):
    """'FLIGHT Gunfighter\n1\tGunfighter-1\tPlayer\t1\n2\tGunfighter-2\tHigh\t0'
    -> ('Gunfighter', [(1, 'Gunfighter-1', 'Player', True), (2, 'Gunfighter-2', 'High', False)])"""
    lines = text.split("\n")
    group = lines[0][len("FLIGHT"):].strip()
    units = []
    for line in lines[1:]:
        bits = line.split("\t")
        if len(bits) == 4 and bits[0].strip().isdigit():
            units.append((int(bits[0]), bits[1], bits[2], bits[3].strip() == "1"))
    return group, units


def _item_key(text: str) -> str:
    t = clean_item(text).lower().replace("defense", "defence")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def same_item(menu_name: str, wanted: str) -> bool:
    """'Air Defences' == 'air defenses', 'Wingman 2' == 'wingman 2'."""
    return _item_key(menu_name) == _item_key(wanted)


def clean_item(text: str) -> str:
    t = re.sub(r"^\s*F\d+\.\s*", "", text)
    return t.replace("...", "").replace(">>", "").strip()


# Words that say which branch a request lives under, so "request taxi" finds ATC first
_BRANCH_HINTS = {"atc": {"startup", "taxi", "takeoff", "landing", "inbound", "hover", "abort"},
                 "ground crew": {"rearm", "refuel", "repair", "chocks", "chock", "power", "canopy", "wheel"}}


def _with_hints(words: List[str]) -> List[str]:
    out = list(words)
    for branch, hints in _BRANCH_HINTS.items():
        bw = _words(branch)
        if any(w in hints for w in words) and not all(b in out for b in bw):
            out = bw + out
    return out


def _words(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z0-9\-]+", text.lower()):
        w = _SYNONYMS.get(w, w)
        if w and w not in _STOP_WORDS:
            out.append(w)
    return out


def score(utter_words: List[str], item: str) -> float:
    """Share of the item's words that were said (0..1); 'ATC' said for 'ATC' = 1."""
    # 29 Oct: "Nellis" scored 1 of 4 against "Nellis (327.000 MHz)": frequencies and tags don't count
    bare = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", item)
    iw = _words(bare) or _words(item)
    if not iw:
        return 0.0
    hit = sum(1 for w in iw if w in utter_words or any(u.startswith(w) or w.startswith(u)
                                                           for u in utter_words if len(u) > 3))
    return hit / len(iw)


def best_item(utter_words: List[str], items: Dict[int, str]) -> Tuple[Optional[int], float]:
    best, best_s = None, 0.0
    for n, name in items.items():
        if n >= 11:                     # Previous Menu / Exit are spoken as "back" / "close"
            continue
        s = score(utter_words, name)
        if s > best_s:
            best, best_s = n, s
    return best, best_s


def readout(menu: dict, limit: int = 8) -> str:
    items = [(n, name) for n, name in sorted(menu["items"].items()) if n < 11]
    if not items:
        return "Menu's empty."
    caption = re.sub(r"\([^)]*\)", " ", menu.get("caption") or "")      # "Nellis (327.000 MHz)" has dots
    parts = [p.strip() for p in caption.split(".") if p.strip() and not p.strip().isdigit()]
    head = parts[-1] if parts else "Radio"
    # spoken: without "[N/A]" tags and "(116 MHz)" frequency lists
    say = lambda name: re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)).strip() or name
    listed = ", ".join(f"{n} {say(name)}" for n, name in items[:limit])
    more = f", and {len(items) - limit} more" if len(items) > limit else ""
    return f"{head}: {listed}{more}."


class CommsMenu:
    def __init__(self, speak: Callable[[str], None], press_keys: Optional[Callable] = None,
                 step_sec: float = STEP_SEC):
        """press_keys(list of key names) -> bool, used only when the hook isn't running."""
        self.speak = speak
        self.press_keys = press_keys
        self.step_sec = step_sec
        self._lock = threading.Lock()
        self._menu = {"visible": False, "caption": "", "items": {}}
        self._rx = 0.0
        self._key_menu_t = -1e9              # keys only: when we last pressed a menu key
        self.last_items: List[str] = []      # the page where select_path didn't find its item
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ------------------------------------------------------------ link
    def update(self, menu: dict) -> None:
        with self._lock:
            self._menu, self._rx = menu, time.monotonic()

    def snapshot(self) -> Tuple[dict, float]:
        with self._lock:
            return dict(self._menu, items=dict(self._menu["items"])), self._rx

    def live(self) -> bool:
        _, rx = self.snapshot()
        return bool(rx) and time.monotonic() - rx <= LIVE_SEC

    def is_open(self) -> bool:
        if self.live():
            return self.snapshot()[0]["visible"]
        return time.monotonic() - self._key_menu_t < KEY_MENU_OPEN_SEC

    def blocks_llm(self) -> bool:
        """True while the menu is up and driven by keys: a misheard "F11" must not reach the
        LLM and come back as some other command (seen: "F level" -> pitot heat)."""
        return not self.live() and self.is_open()

    def _send(self, cmd: str) -> None:
        try:
            self.sock.sendto(cmd.encode(), ("127.0.0.1", CMD_PORT))
            # GROUP / UNIT are repeated every 10 s for the hook: logged only when they change
            # (28 Oct: "UNIT RedArcher" filled the log)
            kind = cmd.split(" ", 1)[0]
            if cmd.startswith("LOS\t"):               # line-of-sight queries every 2 s while lasing: not logged
                return
            if kind in ("GROUP", "UNIT"):
                if getattr(self, "_said_ident", {}).get(kind) == cmd:
                    return
                self._said_ident = dict(getattr(self, "_said_ident", {}), **{kind: cmd})
            print(f"[COMMS OUT] {cmd}")
        except OSError as e:
            print(f"[COMMS ERROR] {e}")

    def _wait_change(self, before: dict) -> dict:
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.step_sec:
            menu, rx = self.snapshot()
            if rx > t0 and (menu["visible"] != before["visible"] or menu["caption"] != before["caption"]
                            or menu["items"] != before["items"]):
                time.sleep(0.15)             # let the new page finish drawing
                return self.snapshot()[0]
            time.sleep(0.03)
        return self.snapshot()[0]

    # ------------------------------------------------------------ actions
    def open(self) -> str:
        if not self.live():
            return self._keys(["\\"], opening=True, said="Radio menu.")
        menu, _ = self.snapshot()
        if not menu["visible"]:
            self._send("OPEN")
            menu = self._wait_change(menu)
        return readout(menu) if menu["visible"] else "Radio menu didn't open."

    def close(self) -> str:
        if not self.live():
            return self._keys(["f12"], closing=True, said="Menu closed.")
        menu, _ = self.snapshot()
        if menu["visible"]:
            self._send("CLOSE")
            self._wait_change(menu)
        return "Menu closed."

    def back(self) -> str:
        if not self.live():
            return self._keys(["f11"], said="Back.")
        menu, _ = self.snapshot()
        if not menu["visible"]:
            return "Menu's not open."
        self._send("BACK")
        return readout(self._wait_change(menu))

    def select(self, n: int) -> str:
        if not 1 <= n <= 12:
            return "No such item."
        if not self.live():
            return self._keys([f"f{n}"], closing=(n == 12), said=f"F{n}.")
        menu, _ = self.snapshot()
        if not menu["visible"]:
            return "Menu's not open."
        name = menu["items"].get(n)
        if name is None:
            return f"No item {n}."
        self._send(f"SELECT {n}")
        new = self._wait_change(menu)
        if not new["visible"]:
            return f"{name}."
        if new["items"] == menu["items"] and new["caption"] == menu["caption"]:
            return f"{name}, no change."
        return readout(new)

    def select_path(self, path: List[str]) -> Tuple[bool, str]:
        """Opens the menu and chooses items BY NAME, one level per entry (the F-numbers move:
        DCS hides options already in effect). (True, "") when the last item was chosen;
        (False, why) otherwise, with the menu closed again. Blocking (about a second)."""
        if not self.live():
            return False, "the radio menu link isn't running"
        menu, _ = self.snapshot()
        if menu["visible"]:
            self._send("CLOSE")
            menu = self._wait_change(menu)
        self._send("OPEN")
        menu = self._wait_change(menu)
        if not menu["visible"]:
            return False, "the radio menu didn't open"
        for i, want in enumerate(path):
            n = next((k for k, name in sorted(menu["items"].items()) if k < 11 and same_item(name, want)), None)
            if n is None:
                self.last_items = [name for k, name in sorted(menu["items"].items()) if k < 11]
                self._send("CLOSE")
                self._wait_change(menu)
                return False, f"no {want} in the menu"
            self._send(f"SELECT {n}")
            new = self._wait_change(menu)
            if i == len(path) - 1:
                if new["visible"] and new["items"] != menu["items"]:      # opened a page, not an order
                    self._send("CLOSE")
                    self._wait_change(new)
                    return False, f"{want} isn't an order"
                if new["visible"]:
                    self._send("CLOSE")
                    self._wait_change(new)
                return True, ""
            if not new["visible"] or new["items"] == menu["items"]:
                if new["visible"]:
                    self._send("CLOSE")
                    self._wait_change(new)
                return False, f"{want} didn't open"
            menu = new
        return True, ""

    def map_tree(self, root_rx: str = r"wingman|flight|element", max_depth: int = 4,
                 max_pages: int = 60) -> dict:
        """Walks the menu branches whose name matches root_rx and returns the tree:
        {"caption", "items": {n: {"name", "sub": {...}}}}. Only enters submenus (items ending in
        "..."), so no order is ever sent; closes the menu at the end. Blocking (several seconds)."""
        if not self.live():
            raise RuntimeError("the radio menu link isn't running")
        menu, _ = self.snapshot()
        if menu["visible"]:
            self._send("CLOSE")
            menu = self._wait_change(menu)
        self._send("OPEN")
        menu = self._wait_change(menu)
        if not menu["visible"]:
            raise RuntimeError("the radio menu didn't open")
        pages = [0]

        def walk(page: dict, depth: int, branch_rx) -> dict:
            """Items are followed by NAME: DCS re-sorts the ATC list by distance while you fly
            (28 Oct), so an F-number can point at another item a moment later. Only an item the
            CURRENT page marks as a submenu is ever selected."""
            node = {"caption": page["caption"], "items": {}}
            for first_n, name in sorted(page["items"].items()):   # listed as the page first showed them
                n = next((k for k, v in page["items"].items() if v == name), None)
                if n is None or first_n >= 11:               # gone, or F11 previous / F12 exit
                    continue
                entry = {"name": name}
                if (n in page.get("subs", ()) and depth < max_depth and pages[0] < max_pages
                        and (branch_rx is None or re.search(branch_rx, name.lower()))):
                    self._send(f"SELECT {n}")
                    new = self._wait_change(page)
                    pages[0] += 1
                    if not new["visible"]:
                        raise RuntimeError(f"'{name}' closed the menu")
                    if new["items"] != page["items"]:
                        entry["sub"] = walk(new, depth + 1, None)
                        self._send("BACK")
                        back = self._wait_change(new)
                        if back["items"] != page["items"]:
                            if sorted(back["items"].values()) != sorted(page["items"].values()):
                                raise RuntimeError(f"lost my place in the menu after '{name}'")
                            page = back                      # same page, new order
                node["items"][first_n] = entry
            return node

        try:
            return walk(menu, 0, root_rx)
        finally:
            self._send("CLOSE")
            self._wait_change(self.snapshot()[0])

    def by_words(self, text: str, open_first: bool = False, min_score: float = 0.5) -> Optional[str]:
        """Walks the menu by the words said, one level per match. None = nothing matched."""
        if not self.live():
            return None
        words = _with_hints(_words(text))
        if not words:
            return None
        menu, _ = self.snapshot()
        if not menu["visible"]:
            if not open_first:
                return None
            self._send("OPEN")
            menu = self._wait_change(menu)
            if not menu["visible"]:
                return "Radio menu didn't open."
        chosen = []
        for _ in range(5):
            n, s = best_item(words, menu["items"])
            choices = [k for k in menu["items"] if k < 11]
            if (n is None or s < min_score) and chosen and len(choices) == 1:
                n, s = choices[0], 1.0           # only one way on (e.g. a single airfield)
            if n is None or s < min_score:
                break
            name = menu["items"][n]
            chosen.append(name)
            for w in _words(name):              # don't use the same words twice
                if w in words:
                    words.remove(w)
            self._send(f"SELECT {n}")
            menu = self._wait_change(menu)
            if not menu["visible"]:
                return ", ".join(chosen) + "."
        if not chosen:
            return None if not open_first else ("Not in the menu. " + readout(menu))
        return readout(menu)

    def _keys(self, keys, opening=False, closing=False, said="") -> str:
        if self.press_keys is None:
            return "Radio menu link isn't running."
        if not self.press_keys(keys):
            return "DCS not in focus."
        self._key_menu_t = -1e9 if closing else time.monotonic()
        return said


# ------------------------------------------------------------------ phrases
_OPEN = re.compile(r"^(?:open |show )?(?:the )?(?:radio|comms?|com|communications?) menu$|^(?:radio|comms)$")
_CLOSE = re.compile(r"^(?:close|exit|cancel|hide)(?: the)?(?: radio| comms?)?(?: menu)?$")
_BACK = re.compile(r"^(?:back|go back|previous|previous menu|back up)$")
# "F5", "option 5", and what Whisper makes of "F six" / "F eleven": "AP6", "ef 6", "F level"
_NUMBER = re.compile(r"^(?:f|f-|ef|eff|af|ap|at|if|of|s|x|half|option|item|number|select|press f?)?\s*-?\s*(\d{1,2}|"
                     + "|".join(_NUM_WORDS) + r")$")
_PREFIX = re.compile(r"^(?:radio|comms|comm|com)\b[\s,]*(.+)$")
_REQUEST = re.compile(r"^(?:request|requesting|ask|tell|call)\b")


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\b(?:george|chief|please|okay|ok)\b", " ", t)
    t = re.sub(r"[^a-z0-9\- ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def handle(menu: CommsMenu, text: str) -> Optional[str]:
    """What George says if this utterance was for the radio menu, else None."""
    t = _norm(text)
    if not t:
        return None
    if _OPEN.match(t):
        return menu.open()
    is_open = menu.is_open()
    if is_open:
        if _CLOSE.match(t):
            return menu.close()
        if _BACK.match(t):
            return menu.back()
        m = _NUMBER.match(t)
        if m:
            tok = m.group(1)
            return menu.select(int(tok) if tok.isdigit() else _NUM_WORDS[tok])
        said = menu.by_words(t)
        if said is not None:
            return said
    m = _PREFIX.match(t)
    if m and not re.match(r"^(?:check|checks|frequencies)$", m.group(1)):
        if menu.live():
            return menu.by_words(m.group(1), open_first=True)
    if _REQUEST.match(t) and menu.live():
        return menu.by_words(t, open_first=True, min_score=0.5)
    return None


def parse_fuel(text: str):
    """'FUEL 0.84210 1157.0 player AH-64D-1' -> (0.8421, 1157.0, 'AH-64D-1', 'player'), or None.
    The source says how the hook found the aircraft: "player" (DCS's own player unit) or "name"."""
    m = re.match(r"FUEL (-?[\d.]+) (-?[\d.]+)(?: (player|name))? ?(.*)$", text.strip())
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2)), m.group(4).strip(), m.group(3) or ""
    except ValueError:
        return None


# Called with each "BRIEF ..." message (the loaded mission's briefing) and "TYPES ..." message
# (every unit type's DCS attributes): set by main.py
ON_BRIEF = None
ON_TYPES = None
ON_AIRBASES = None
ON_PLAYERS = None          # "PLAYERS n mp|sp": people connected (common/training.py refuses multiplayer)
ON_LOADED = None            # "LOADED n": a mission was loaded or restarted (unit ids start again)
ON_EVENTS = None            # "EVENTS\n<kind;id;name;type;killer id;name;type>...": DCS unit deaths (tactical I-T4)
ON_JTACS = None             # "JTACS\n...": your side's laser sources (common/jtac_sight.py, 3 Nov)
ON_LOS = None               # "LOS <req> <target id>\n...": a line-of-sight answer (common/jtac_sight.py)


def listen(menu: CommsMenu) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", MENU_PORT))
    print(f"[COMMS] Listening on UDP port {MENU_PORT}...")
    announced = False
    last_route = None
    last_flight = None
    last_brief = None
    asked = (None, 0.0)                     # (group, when) we last asked the hook for
    asked_unit = (None, 0.0)                # (unit, when): whose fuel the hook reports
    while True:
        try:
            data, _ = s.recvfrom(65535)
            text = data.decode("utf-8", errors="replace")
            # Report YOUR group (Export.lua knows it), not the mission's "Player" unit's group
            snap = state_store.snapshot()
            mine = snap.get("my_group")
            if mine and mine != snap.get("flight_group") and (asked[0] != mine or time.monotonic() - asked[1] > 10):
                menu._send(f"GROUP {mine}")
                asked = (mine, time.monotonic())
            unit = snap.get("my_unit")
            if unit and (asked_unit[0] != unit or time.monotonic() - asked_unit[1] > 10):
                menu._send(f"UNIT {unit}")
                asked_unit = (unit, time.monotonic())
            if text.startswith("LOADED"):             # mission (re)loaded: its flights and clones are gone
                if ON_LOADED is not None:
                    ON_LOADED()
                continue
            if text.startswith("JTACS"):              # laser sources (george_comms_hook.lua, 3 Nov)
                if ON_JTACS is not None:
                    ON_JTACS(text)
                continue
            if text.startswith("LOS "):               # a line-of-sight answer for the JTAC's laser
                if ON_LOS is not None:
                    ON_LOS(text)
                continue
            if text.startswith("EVENTS\n"):           # DCS says units died (george_comms_hook.lua, T8)
                if ON_EVENTS is not None:
                    ON_EVENTS(text)
                continue
            if text.startswith("PLAYERS"):            # who's connected (george_comms_hook.lua, audit step 7)
                if ON_PLAYERS is not None:
                    ON_PLAYERS(text)
                continue
            if text.startswith("CALLSIGN "):          # your callsign from the mission (hook, P2-2)
                from command.roles import parse_callsign
                got = parse_callsign(text)
                if got is not None:
                    state_store.set_callsign(*got)
                continue
            if text.startswith("FUEL "):              # your fuel straight from DCS (george_comms_hook.lua)
                got = parse_fuel(text)
                if got is not None:
                    state_store.set_dcs_fuel(*got)
                continue
            if text.startswith("AIRBASES"):           # airfields, FARPs, ships (george_comms_hook.lua)
                if ON_AIRBASES is not None:
                    ON_AIRBASES(text)
                continue
            if text.startswith("TYPES"):              # unit types and attributes (george_comms_hook.lua)
                if ON_TYPES is not None:
                    ON_TYPES(text)
                continue
            if text.startswith("BRIEF"):              # the mission briefing (george_comms_hook.lua)
                if text != last_brief and ON_BRIEF is not None:
                    ON_BRIEF(text)
                    last_brief = text
                continue
            if text.startswith("FLIGHT"):             # your flight (george_comms_hook.lua)
                group, units = parse_flight(text)
                if (group, units) != last_flight:
                    state_store.set_flight(group, units)
                    print(f"[FLIGHT] {group}: " + ", ".join(
                        f"{n} {name} ({'you' if me else skill})" for n, name, skill, me in units))
                    last_flight = (group, units)
                continue
            if text.startswith("WPNAMES"):            # named waypoints (george_comms_hook.lua, 5 Nov)
                names = parse_wpnames(text)
                if names != state_store.snapshot().get("route_names"):
                    state_store.set_route_names(names)
                    if names:
                        print("[NAV] Named waypoints: " + ", ".join(f"{n} {nm}" for n, _, _, nm in names[:8]))
                continue
            if text.startswith("ROUTE"):              # the mission route (george_comms_hook.lua)
                route = parse_route(text)
                if route != last_route:
                    state_store.set_mission_route(route)
                    print(f"[NAV] Mission route: {len(route)} points "
                          f"(waypoints {', '.join(str(p[0]) for p in route[:12])}).")
                    last_route = route
                continue
            menu.update(parse_menu(text))
            if not announced:
                announced = True
                print("[COMMS] Radio menu link is live.")
        except Exception as e:
            print(f"[COMMS ERROR] {type(e).__name__}: {e}")
            time.sleep(0.2)


def start(menu: CommsMenu) -> threading.Thread:
    t = threading.Thread(target=listen, args=(menu,), daemon=True, name="comms-menu")
    t.start()
    return t
