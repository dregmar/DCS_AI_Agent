"""
kneeboard/pages.py - which page the kneeboard shows, and the Briefing and Checklist pages (feature 2).

    PageControl          locked: the last page / scroll request; the page applies each one once
    match(text, bare_scroll)  "show the map / briefing / checklist / comms / status", "show the start
                         checklist", "page down / up", "next / previous page", "scroll to the top",
                         "scroll down / up" (in the Apache only after "kneeboard": George's list uses it)
                         -> {"kind": "page", "page": ..., "checklist": ...} | {"kind": "scroll", "by": ...} | None
                         also (30 Oct) "show alerts", "kneeboard, acknowledge (all)" -> {"kind": "ack", "all": ...},
                         "kneeboard, track on / off" -> {"kind": "track", "on": ...}, "kneeboard, zoom in / out"
                         also (Instructor Mode) "show the instructor / scores / training"
                         also (tactical picture, T5) "kneeboard, contact 42" / "kneeboard, show contact
                         tango four two" -> {"kind": "contact", "n": 42}; "kneeboard, close contact" -> n None
    build_briefing(kb, aircraft)                     the Briefing page's data
    build_checklist(runner, lists, aircraft, chosen) the Checklist page's data (the running list with
                                                     its current step, done items, and what George sees)

Voice commands are whole utterances only ("show the map", optionally after "kneeboard" or "George"),
so they can't take an order meant for anyone else. They are answered by the page changing, not by a
voice. Pure functions apart from PageControl; called from the kneeboard's HTTP thread and the voice
thread.
"""
import re
import threading
from typing import Dict, List, Optional

PAGES = ("map", "briefing", "checklist", "comms", "status", "alerts", "instructor")

_LEAD = r"^(?:(?:george|kneeboard|knee board|hey|okay|ok)[\s,]+)*"
_SHOW = r"(?:show(?: me)?|open|display|bring up|pull up|go to|switch to|page|kneeboard|knee board)"
_PAGE_WORDS = [
    ("map", r"(?:moving |tactical |the )?(?:map|mup|mop)"),          # 31 Oct: "kneeboard Mup", "kneeboard, mop"
    ("briefing", r"(?:mission )?(?:briefing|brief)|manual"),
    ("comms", r"(?:comms?|coms|comes|radio|com) ?(?:log)?|log"),     # 29 Oct: "knee board comes"
    ("status", r"(?:system |systems )?status|systems|stats|that s us"),  # 29 Oct: "Niebord, that's us"
    ("alerts", r"alerts?|warnings?"),
    ("instructor", r"instructor|instructors|training|scores?|score ?card"),     # Instructor Mode tab (Phase 2)
]
_PAGE_RX = [(p, re.compile(_LEAD + _SHOW + r"[\s,]+(?:the |my )?(?:" + w + r")(?:\s+(?:page|tab))?$"))
            for p, w in _PAGE_WORDS]
_CHECKLIST_RX = re.compile(_LEAD + _SHOW + r"[\s,]+(?:the |my )?(?:(.+?)\s+)?check ?lists?(?:\s+(?:page|tab))?$")
_SCROLL = [
    (1, re.compile(_LEAD + r"page down$|^(?:kneeboard[\s,]+)?next page$")),
    (-1, re.compile(_LEAD + r"page up$|^(?:kneeboard[\s,]+)?(?:previous|last) page$")),
    (0, re.compile(_LEAD + r"scroll (?:to )?(?:the )?top$|^(?:kneeboard[\s,]+)?top of (?:the )?page$")),
    (99, re.compile(_LEAD + r"scroll (?:to )?(?:the )?(?:bottom|end)$")),
]


# How Whisper heard "kneeboard" in flight (29 Oct): "knee board", "new board", "Niebord"...
_KNEEBOARD = re.compile(r"\b(?:knee ?board|kneeboards?|new ?board|nie ?bord|nee ?bord|nee ?board|knee ?bored|"
                        r"neeboard|niebord|neebord|new ?word|new ?ward|knee ?word)\b")   # 30 Oct: "new word, show status"
# "kneeboard, zoom in / out": the map (only after "kneeboard": "zoom in" alone is George's TADS)
_ZOOM = re.compile(r"^kneeboard[\s,]+(?:map[\s,]+)?zoom (in|out)$")
# "kneeboard, acknowledge" (the newest alert) / "kneeboard, acknowledge all": always after "kneeboard"
_ACK = re.compile(r"^kneeboard[\s,]+(?:acknowledged?|acknowledging)(?:[\s,]+(all|everything|all alerts))?$")
# "kneeboard, track on / off" / "kneeboard, show / hide the track": this session only
_TRACK = re.compile(r"^kneeboard[\s,]+(?:(?:the )?track (on|off)|(show|hide) (?:the |my )?track)$")


# "kneeboard, contact 42" / "kneeboard, show track T42" / "kneeboard, contact tango four two": the map's
# detail panel for that track (always after "kneeboard"); "kneeboard, close / hide contact" closes it
_CONTACT = re.compile(r"^kneeboard[\s,]+(?:show |open )?(?:the )?(?:contact|track)(?: number)?[\s,]+(.+)$")
_CONTACT_CLOSE = re.compile(r"^kneeboard[\s,]+(?:close|hide|clear)(?: the)? (?:contact|track|panel)(?: panel)?$")
_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "ate": 8, "nine": 9, "niner": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}


def contact_number(words: str) -> Optional[int]:
    """"42", "t42", "tango 42", "four two", "forty two", "one hundred and five" -> the number, else None."""
    toks = [w for w in re.split(r"[\s,]+", words.strip()) if w and w not in ("tango", "t", "number", "and")]
    if toks and re.fullmatch(r"t\d+", toks[0]):
        toks[0] = toks[0][1:]
    if not toks:
        return None
    if all(t.isdigit() for t in toks):
        return int("".join(toks))
    if all(t in _UNITS for t in toks):                 # spoken digit by digit: "four two"
        return int("".join(str(_UNITS[t]) for t in toks))
    total, cur = 0, 0
    for t in toks:
        if t in _UNITS:
            cur += _UNITS[t]
        elif t in _TEENS:
            cur += _TEENS[t]
        elif t in _TENS:
            cur += _TENS[t]
        elif t == "hundred":
            cur = max(cur, 1) * 100
        elif t.isdigit():
            cur += int(t)
        else:
            return None
    total += cur
    return total if total > 0 else None


def _norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9\s,]", " ", (text or "").lower())
    t = _KNEEBOARD.sub("kneeboard", re.sub(r"\s+", " ", t))
    return re.sub(r"\s+", " ", t).strip(" ,")


# "scroll down / up": George's target list in the Apache, so there only after "kneeboard"
_SCROLL_WORD = re.compile(r"^(kneeboard[\s,]+|knee board[\s,]+)?(?:(?:george|hey|okay|ok)[\s,]+)*scroll (down|up)$")


def match(text: str, bare_scroll: bool = True) -> Optional[dict]:
    """bare_scroll False (the Apache): "scroll down / up" alone is left for George's list;
    "kneeboard, scroll down", "page down" and "next page" still work."""
    t = _norm(text)
    if not t:
        return None
    z = _ZOOM.match(t)
    if z:
        return {"kind": "zoom", "by": -1 if z.group(1) == "in" else 1}
    a = _ACK.match(t)
    if a:
        return {"kind": "ack", "all": bool(a.group(1))}
    tr = _TRACK.match(t)
    if tr:
        return {"kind": "track", "on": (tr.group(1) or tr.group(2)) in ("on", "show")}
    if _CONTACT_CLOSE.match(t):
        return {"kind": "contact", "n": None}
    c = _CONTACT.match(t)
    if c:
        n = contact_number(c.group(1))
        if n is not None:
            return {"kind": "contact", "n": n}
    for page, rx in _PAGE_RX:
        if rx.match(t):
            return {"kind": "page", "page": page}
    m = _CHECKLIST_RX.match(t)
    if m:
        name = None
        if m.group(1):
            from huey.commands import match_checklist
            from common.jet_checklists import match_name
            jet = match_name(m.group(1))             # jet names first: "cold start", "carrier launch"...
            got = match_checklist(m.group(1) + " checklist")
            if jet is None and (not got or got.get("action") != "CHECKLIST_START"):
                return None                      # "show the foo checklist": not a page command
            name = got["value"] if got and got.get("action") == "CHECKLIST_START" else None
            return {"kind": "page", "page": "checklist", "checklist": name, "jet_checklist": jet}
        return {"kind": "page", "page": "checklist", "checklist": None, "jet_checklist": None}
    for by, rx in _SCROLL:
        if rx.match(t):
            return {"kind": "scroll", "by": by}
    m = _SCROLL_WORD.match(t)
    if m and (bare_scroll or m.group(1)):
        return {"kind": "scroll", "by": 1 if m.group(2) == "down" else -1}
    return None


class PageControl:
    """The last request for the page. Each request gets a new seq; the page applies a seq once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._last: dict = {}
        self._checklist: Optional[str] = None
        self._jet: Optional[str] = None
        self._contact: Optional[int] = None

    def apply(self, cmd: dict) -> None:
        with self._lock:
            self._seq += 1
            self._last = dict(cmd)
            if cmd.get("kind") == "contact":
                self._contact = cmd.get("n")
            if cmd.get("kind") == "page" and cmd.get("page") == "checklist":
                if cmd.get("checklist") or cmd.get("jet_checklist"):     # a named list replaces both
                    self._checklist, self._jet = cmd.get("checklist"), cmd.get("jet_checklist")

    def chosen_checklist(self, jet: bool = False) -> Optional[str]:
        """The list asked for: the crews' name, or with jet=True the jet list's name (they differ:
        "takeoff checklist" is the Huey's before_takeoff but the Hornet's takeoff)."""
        with self._lock:
            return (self._jet or self._checklist) if jet else self._checklist

    def contact(self) -> Optional[int]:
        """The track number whose detail panel the map shows ("kneeboard, contact 42"), or None."""
        with self._lock:
            return self._contact

    def clear_contact(self) -> None:
        with self._lock:
            self._contact = None

    def clear_checklist(self) -> None:
        with self._lock:
            self._checklist = self._jet = None

    def view(self) -> dict:
        with self._lock:
            return dict(self._last, seq=self._seq)


# ------------------------------------------------------------------ briefing page
MAX_MANUAL = 30


def build_briefing(kb, aircraft: Optional[str]) -> Dict:
    """Mission briefing (from DCS), your campaign briefing, the manual section the last answer came
    from, then this aircraft's procedures (closed). A manual is thousands of entries, so only the
    section(s) of the last answer are sent. Checklists are left out (they have their own page).
    "last": the entries George or Overlord answered from last, so the page can mark them."""
    from common.briefing import fits
    entries = kb.entries()
    last = kb.last_answer() if hasattr(kb, "last_answer") else None
    hit_ids = set((last or {}).get("ids") or [])
    man_secs = []
    for e in entries:
        if e.id in hit_ids and e.source == "manual" and e.section not in man_secs:
            man_secs.append(e.section)
    groups = {"mission": [], "campaign": [], "manual": [], "procedures": []}
    for e in entries:
        if e.source not in groups or e.id.startswith("chk-"):
            continue
        if e.source == "procedures" and not fits(e.aircraft, aircraft):
            continue
        if e.source == "manual" and (e.section not in man_secs or len(groups["manual"]) >= MAX_MANUAL):
            continue
        groups[e.source].append({"id": e.id, "title": e.title, "text": e.text})
    name = kb.mission_name() if hasattr(kb, "mission_name") else ""
    sections = [
        {"key": "mission", "title": "Mission briefing" + (f": {name}" if name else ""), "entries": groups["mission"]},
        {"key": "campaign", "title": "Campaign briefing", "entries": groups["campaign"]},
        {"key": "manual", "title": "Manual: " + " / ".join(man_secs), "entries": groups["manual"]},
        {"key": "procedures", "title": f"{aircraft} procedures" if aircraft and aircraft != "OTHER" else "Procedures",
         "entries": groups["procedures"], "closed": True},
    ]
    return {"sections": [s for s in sections if s["entries"]], "last": last}


# ------------------------------------------------------------------ checklist page
def build_checklist(runner, lists: Optional[Dict], aircraft: str, chosen: Optional[str] = None,
                    auto_show: bool = True) -> Dict:
    """runner: huey.checklists.ChecklistRunner (or None: no crew in this aircraft). lists: its checklists.
    Shown: the running list; else the one asked for ("show the shutdown checklist"); else the last run.
    Item state: "seen" (George saw it), "pilot" (you said check), "set" (George set it), "skipped",
    "current", "pending". Pending items carry what George can see now (ok True / False, "seen")."""
    if runner is None or not lists:
        return {"aircraft": aircraft, "lists": [], "shown": None, "running": None, "items": [],
                "auto_show": auto_show}
    v = runner.view()
    shown = v["running"] or (chosen if chosen in lists else None) or v["shown"]
    out = {"aircraft": aircraft, "running": v["running"], "shown": shown, "auto_show": auto_show,
           "lists": [{"key": k, "title": t} for k, (t, _) in lists.items()], "items": [], "outcome": None}
    if shown not in lists:
        return out
    title, items = lists[shown]
    out["title"] = title
    this_run = shown == v["shown"] and (v["running"] or v["outcome"])
    results = v["results"] if this_run else []
    if this_run and not v["running"]:
        out["outcome"] = v["outcome"]
    for i, item in enumerate(items):
        row = {"text": item[0], "auto": item[1] is not None}
        if i < len(results):
            row["state"] = results[i]
        else:
            row["state"] = "current" if (v["running"] and i == v["index"]) else "pending"
            if item[1] is not None:
                try:
                    ok, seen = runner.check(item)
                except Exception:
                    ok, seen = None, ""
                row["ok"], row["seen"] = ok, seen
        out["items"].append(row)
    return out
