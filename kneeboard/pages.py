"""
kneeboard/pages.py - which page the kneeboard shows, and the Briefing and Checklist pages (feature 2).

    PageControl          locked: the last page / scroll request; the page applies each one once
    match(text, bare_scroll)  "show the map / briefing / checklist / comms / status", "show the start
                         checklist", "page down / up", "next / previous page", "scroll to the top",
                         "scroll down / up" (in the Apache only after "kneeboard": George's list uses it)
                         -> {"kind": "page", "page": ..., "checklist": ...} | {"kind": "scroll", "by": ...} | None
                         also (30 Oct) "show alerts", "kneeboard, acknowledge (all)" -> {"kind": "ack", "all": ...},
                         "kneeboard, track on / off" -> {"kind": "track", "on": ...}, "kneeboard, zoom in / out"
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

PAGES = ("map", "briefing", "checklist", "comms", "status", "alerts")

_LEAD = r"^(?:(?:george|kneeboard|knee board|hey|okay|ok)[\s,]+)*"
_SHOW = r"(?:show(?: me)?|open|display|bring up|pull up|go to|switch to|page|kneeboard|knee board)"
_PAGE_WORDS = [
    ("map", r"(?:moving |tactical |the )?map"),
    ("briefing", r"(?:mission )?(?:briefing|brief)|manual"),
    ("comms", r"(?:comms?|coms|comes|radio|com) ?(?:log)?|log"),     # 29 Oct: "knee board comes"
    ("status", r"(?:system |systems )?status|systems|stats|that s us"),  # 29 Oct: "Niebord, that's us"
    ("alerts", r"alerts?|warnings?"),
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
                        r"neeboard|niebord|neebord)\b")
# "kneeboard, zoom in / out": the map (only after "kneeboard": "zoom in" alone is George's TADS)
_ZOOM = re.compile(r"^kneeboard[\s,]+(?:map[\s,]+)?zoom (in|out)$")
# "kneeboard, acknowledge" (the newest alert) / "kneeboard, acknowledge all": always after "kneeboard"
_ACK = re.compile(r"^kneeboard[\s,]+(?:acknowledged?|acknowledging)(?:[\s,]+(all|everything|all alerts))?$")
# "kneeboard, track on / off" / "kneeboard, show / hide the track": this session only
_TRACK = re.compile(r"^kneeboard[\s,]+(?:(?:the )?track (on|off)|(show|hide) (?:the |my )?track)$")


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

    def apply(self, cmd: dict) -> None:
        with self._lock:
            self._seq += 1
            self._last = dict(cmd)
            if cmd.get("kind") == "page" and cmd.get("page") == "checklist":
                if cmd.get("checklist") or cmd.get("jet_checklist"):     # a named list replaces both
                    self._checklist, self._jet = cmd.get("checklist"), cmd.get("jet_checklist")

    def chosen_checklist(self, jet: bool = False) -> Optional[str]:
        """The list asked for: the crews' name, or with jet=True the jet list's name (they differ:
        "takeoff checklist" is the Huey's before_takeoff but the Hornet's takeoff)."""
        with self._lock:
            return (self._jet or self._checklist) if jet else self._checklist

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
