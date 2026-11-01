"""
tactical/queries.py - spoken answers about the tactical picture, from fixed templates (design section 10,
decisions D1 and D6).

    q = match(text)                   -> "picture" | "threats" | "unknowns" | "jtac" | "stale" | "changes" | None
    answer(q, pic, ctx)               -> what to say

Questions (whole sentences only, after an optional "George" / "crew" / "kneeboard"; after "Overlord," the
same questions are answered in Overlord's voice by main.py):
    "tactical picture", "ground picture", "what's out there"
    "what threats are known", "threat summary", "what threats are there"
    "where are the unknown contacts", "what can't George place"
    "what has JTAC reported", "JTAC report", "what's the JTAC target"
    "which contacts are stale", "stale contacts", "what's old"
    "what's changed", "anything new", "any changes"
Not here, on purpose: "Overlord, picture" (his air picture), "what do you see" / George's target list
(D1: his own contacts come from his own list), "priority threat" / "where's the threat" (George's fast
path), "status" / "sitrep".

D1: these answers describe the whole picture (DCS's world scan, simulator truth), so they say "picture",
never "I see". D6: every word comes from the picture; no model. An empty list is said as "nothing
listed", not "no threats" when the picture is stale.
Pure module: the wording helpers come in through Say.
"""
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from tactical.models import Affiliation, Category, PictureEvent, PictureSnapshot, Status, Track
from tactical.sources import dist_m, true_bearing

_LEAD = r"^(?:(?:george|crew|kneeboard|hey|okay|ok|say|give me|tell me)[\s,]+)*"
_Q = [
    ("picture", r"(?:(?:the|a) )?(?:tactical|ground) picture(?: please)?|what s out there|what is out there"),
    ("threats", r"(?:what|which) threats(?: (?:are|do we have|have we got))?(?: (?:known|there|out there|around))?|"
                r"(?:the |a )?(?:known )?threats? summary|(?:summari[sz]e |list )(?:the )?(?:known )?threats|"
                r"known threats|(?:any|what) threats|threats"),
    ("unknowns", r"(?:where are |what are |any |list )?(?:the )?unknown contacts?|"
                 r"what can t (?:you|george) (?:identify|place)|unidentified contacts?"),
    ("jtac", r"what (?:has|did) (?:the )?jtac report(?:ed)?|(?:the )?jtac report|what s the jtac target|"
             r"what is the jtac target"),
    ("stale", r"(?:which|what|any) (?:contacts?|tracks?|targets?) (?:are |is )?(?:stale|old|lost|out of date)|"
              r"stale contacts?|what s (?:stale|old)"),
    ("changes", r"what s changed(?: recently)?|what has changed(?: recently)?|anything new|any changes|"
                r"what s new(?: in the picture)?|picture changes"),
]
_RX = [(k, re.compile(_LEAD + "(?:" + p + r")(?:\s+(?:please|now))?$")) for k, p in _Q]


def normalise(text: str) -> str:
    t = re.sub(r"[^a-z0-9\s,]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip(" ,")


def match(text: str) -> Optional[str]:
    """The question asked, or None. Whole sentences only, so nothing meant for George or a wingman
    is taken."""
    t = normalise(text)
    if not t or len(t.split()) > 9:
        return None
    for key, rx in _RX:
        if rx.match(t):
            return key
    return None


@dataclass(frozen=True)
class Say:
    """Wording, from the program: say_dist(metres) -> "9 kilometres" / "5 miles" (common/aircraft.py),
    say_type(name) -> "SA-8" (common/overlord.py), compass(bearing) -> "north east"."""
    dist: Callable[[float], str]
    type: Callable[[str], str]
    compass: Callable[[float], str]


@dataclass(frozen=True)
class Context:
    me_ll: Optional[Tuple[float, float]]
    say: Say
    jtac: Optional[dict] = None                    # the JTAC's current target (common/jtac.py), or None
    events: Sequence[PictureEvent] = ()            # recent picture events (TacticalService.events)
    stale: bool = False
    stale_reason: str = ""


CHANGES_WINDOW_S = 120.0
MAX_SAID = 3
LOG_WEIGHT = 70                                     # new / lost / back worth reporting from this threat weight


def _air(t: Track) -> bool:
    return t.category in (Category.AIR, Category.AIR_FIXED, Category.AIR_ROTARY)


def worth_reporting(e: PictureEvent, pic: Optional[PictureSnapshot]) -> bool:
    """Is this picture event worth a comms-log line or a "what's changed" mention? The picture as a
    whole: yes. Our own units: no. Enemy destroyed / gone: yes. New / lost / back: only enemy aircraft,
    air defence and threats of weight >= LOG_WEIGHT (trucks appearing as you fly in aren't news)."""
    if e.track_id is None:
        return True
    t = pic.track(e.track_id) if pic is not None else None
    if t is None or t.affiliation == Affiliation.FRIENDLY:
        return False
    if e.kind in ("new", "lost", "reappeared"):
        if t.affiliation == Affiliation.NEUTRAL:        # neutrals coming and going aren't news (T8)
            return False
        threat = next((a for a in pic.threats if a.track_id == t.track_id), None)
        return _air(t) or "air_defence" in t.tags or (threat is not None and threat.weight >= LOG_WEIGHT)
    return True


def _where(t: Track, ctx: Context) -> str:
    if ctx.me_ll is None or t.position is None:
        return ""
    d = dist_m(ctx.me_ll, (t.position.lat, t.position.lon))
    return f"{ctx.say.dist(d)} {ctx.say.compass(true_bearing(ctx.me_ll, (t.position.lat, t.position.lon)))}"


def _ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s} seconds ago" if s < 90 else f"{s // 60} minutes ago"


def _name(t: Track, ctx: Context) -> str:
    return ctx.say.type(t.type_name or t.display or "unknown type")


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _stale_prefix(ctx: Context) -> str:
    return f"Picture stale, {ctx.stale_reason}. " if ctx.stale and ctx.stale_reason else (
        "Picture stale. " if ctx.stale else "")


def _hostile(pic: PictureSnapshot) -> List[Track]:
    return [t for t in pic.tracks if t.affiliation in (Affiliation.HOSTILE, Affiliation.NEUTRAL)]


def _threat_line(a, t: Track, ctx: Context, pic: PictureSnapshot) -> str:
    parts = [_name(t, ctx), a.category]
    where = _where(t, ctx)
    if where:
        parts.append(where)
    if a.inside_ring:
        parts.append("you're inside its approximate ring")
    closing = next((e.fact for e in a.evidence if e.fact.startswith("closing at")), None)
    if closing:
        parts.append(closing)
    if t.status == Status.STALE:
        parts.append("not current")
    elif t.status != Status.ACTIVE:
        parts.append(f"last seen {_ago(_age(t, pic))}")
    return ", ".join(parts)


def _age(t: Track, pic: PictureSnapshot) -> float:
    """Seconds since the track was last seen, by the picture's own clock."""
    return pic.built_mono - t.last_seen_mono


# ------------------------------------------------------------------ the answers
def answer(q: str, pic: Optional[PictureSnapshot], ctx: Context) -> str:
    if pic is None:
        return "No tactical picture yet."
    return {"picture": _picture, "threats": _threats, "unknowns": _unknowns, "jtac": _jtac, "stale": _stale,
            "changes": _changes}[q](pic, ctx)


def _picture(pic: PictureSnapshot, ctx: Context) -> str:
    hostile = [t for t in _hostile(pic) if t.status != Status.GONE]
    current = [t for t in hostile if t.status in (Status.ACTIVE, Status.STALE)]
    old = [t for t in hostile if t.status in (Status.OUT_OF_COVERAGE, Status.LOST)]
    ground = sum(1 for t in current if not _air(t))
    air = sum(1 for t in current if _air(t))
    out = [_stale_prefix(ctx).strip()] if ctx.stale else []
    if not current and not old:
        out.append("Picture: no enemy units in DCS's lists around you.")
    else:
        bits = []
        if ground:
            bits.append(f"{ground} ground")
        if air:
            bits.append(f"{air} air")
        if old:
            bits.append(f"{len(old)} last known only")
        out.append("Picture: " + ", ".join(bits) + ".")
    if pic.threats:
        a = pic.threats[0]
        t = pic.track(a.track_id)
        if t is not None:
            out.append("Top threat " + _threat_line(a, t, ctx, pic) + ".")
    un = list(pic.george_unassociated)
    if len(un) == 1:
        out.append(f"George has a {un[0].name.lower() if un[0].strength == 'class_only' else un[0].name} "
                   f"he can't place.")
    elif un:
        out.append(f"George has {len(un)} entries he can't place.")
    return " ".join(out)


def _threats(pic: PictureSnapshot, ctx: Context) -> str:
    rows = [(a, pic.track(a.track_id)) for a in pic.threats]
    rows = [(a, t) for a, t in rows if t is not None]
    pre = _stale_prefix(ctx)
    if not rows:
        return pre + ("No threats in the picture." if not ctx.stale else "No threats listed.")
    said = [_threat_line(a, t, ctx, pic) for a, t in rows[:MAX_SAID]]
    more = f" {len(rows) - MAX_SAID} more." if len(rows) > MAX_SAID else ""
    return pre + f"{_count(len(rows), 'threat')}. " + ". ".join(s[0].upper() + s[1:] for s in said) + "." + more


def _unknowns(pic: PictureSnapshot, ctx: Context) -> str:
    if not pic.george:
        return "George's list isn't read yet. Open his target list once."
    un = list(pic.george_unassociated)
    if not un:
        return "None. Every entry on George's list is in the picture."
    parts = []
    for a in un[:MAX_SAID]:
        if a.strength == "class_only":
            n = len(a.candidates)
            parts.append(f"number {a.list_index}, {a.name.lower()}, class only, {_count(n, 'unit')} could fit")
        else:
            parts.append(f"number {a.list_index}, {a.name}, no position")
    more = f" {len(un) - MAX_SAID} more." if len(un) > MAX_SAID else ""
    n = len(un)
    return f"{n} {'entry' if n == 1 else 'entries'} George can't place: " + "; ".join(parts) + "." + more


def _jtac(pic: PictureSnapshot, ctx: Context) -> str:
    t = next((t for t in pic.tracks if "jtac_target" in t.tags), None)
    if t is None:
        if not ctx.jtac:
            return "No JTAC target assigned."
        return f"JTAC gave a {ctx.say.type(str(ctx.jtac.get('type') or 'target'))}; it isn't in the picture now."
    head = f"JTAC gave a {_name(t, ctx)}"
    if t.status == Status.ACTIVE:
        state = "still active"
    elif t.status == Status.GONE:
        state = t.status_reason.replace(", cause unknown", "")
    elif t.status == Status.STALE:
        state = "picture stale"
    else:
        state = f"last seen {_ago(_age(t, pic))}"
    where = _where(t, ctx)
    return f"{head}, {state}" + (f", {where}." if where and t.status != Status.GONE else ".")


def _stale(pic: PictureSnapshot, ctx: Context) -> str:
    if ctx.stale:
        return _stale_prefix(ctx) + "Every contact is from the last scan."
    old = [t for t in _hostile(pic) if t.status in (Status.OUT_OF_COVERAGE, Status.LOST)]
    if not old:
        return "All contacts are current."
    old.sort(key=lambda t: t.last_seen_mono, reverse=True)
    parts = []
    for t in old[:MAX_SAID]:
        where = _where(t, ctx)
        parts.append(f"{_name(t, ctx)} last seen {_ago(_age(t, pic))}" + (f", {where}" if where else ""))
    more = f" {len(old) - MAX_SAID} more." if len(old) > MAX_SAID else ""
    return f"{_count(len(old), 'contact')} not current: " + "; ".join(parts) + "." + more


def _changes(pic: PictureSnapshot, ctx: Context) -> str:
    since = pic.built_mono - CHANGES_WINDOW_S
    evs = [e for e in ctx.events if e.mono >= since and worth_reporting(e, pic)]
    if not evs:
        return "No changes in the last 2 minutes."
    texts = []
    for e in evs[-4:][::-1]:                          # newest first
        s = re.sub(r"\s*\(T\d+\)", "", e.text).rstrip(".")
        s = re.sub(r"^New: ", "New ", s)
        texts.append(s)
    more = len(evs) - len(texts)
    return "Last 2 minutes: " + ". ".join(texts) + "." + (f" {more} more on the comms log." if more > 0 else "")
