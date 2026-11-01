"""
intelligence/router.py - lets the model work out orders that no fixed phrase caught.

When nothing matches what the pilot said (or Overlord / an Outcast only got "say again"), the
crew's model may answer
    {"action": "ROUTE", "command": "Outcast 2, engage air defences"}
i.e. rewrite it into one of the standard orders below. That text then goes through the normal
handlers exactly as if the pilot had said it, so the model can't invent an action we don't have:
if the handlers don't take it, the pilot hears "say again".

  * Plausibility gate: the rewrite must be addressed to someone the pilot named (the number of
    an Outcast, something like "Overlord", a waypoint name he said, ...).
  * Confirmation: orders that are hard to undo (go independent, spawn an Overlord flight, attack,
    weapons free, delete) are read back first: "Confirm, Outcast 2, engage air defences?" and
    only run on "affirm".
  * Learning: a rewrite that worked is saved in data/learned_phrases.json: the whole utterance
    ("ocast 2 destroy your defence" -> "Outcast 2, engage air defences") and short mis-heard
    words that became one of our keywords ("ocast" -> "outcast"). Next time the fixed phrases
    catch it at once, without the model. The file can be edited or deleted by hand.

Runs on the voice-worker thread; the lock is for the learned-phrase file and the pending
confirmation (tests and the file watcher may touch them from elsewhere).
"""

import difflib
import json
import os
import re
import threading
import time
from typing import Callable, Optional, Tuple

# The part of the crew's system prompt that teaches the ROUTE answer. It is fixed text (so the
# prompt stays in Ollama's cache); who and what exists right now goes in the STATE line instead
# (routing_state below).
ROUTE_PROMPT = """
Orders for someone else (an independent wingman, a wingman, Overlord), or for the shared
navigation features, are not your own actions. For those answer
{"action": "ROUTE", "command": "<the order in standard words>", "speech": ""}
with the command in exactly one of these forms (keep the pilot's numbers, names, directions):
- Independent wingmen, callsign "Outcast N" (STATE lists them; speech recognition often mishears
  Outcast as Ocast, Forecast, August, Allcast...): "Outcast N, attack my target" |
  "Outcast N, attack target 3" | "Outcast N, engage at will" | "Outcast N, engage air defences" |
  "Outcast N, rejoin" | "Outcast N, hold position" | "Outcast N, RTB" | "Outcast N, go to waypoint 3" |
  "Outcast N, land next to me" | "Outcast N, weapons hold" | "Outcast N, weapons free" |
  "Outcast N, lase my target" | "Outcast N, stop lasing" | "Outcast N, status"
- Wingmen in the flight: "two, ..." / "three, ..." / "flight, ..." with the same orders, plus
  "two, go independent" | "flight, cover me" | "flight, engage bandits"
- Overlord (C2 on the radio): "Overlord, requesting SEAD|CAS|CAP|escort <where>" where <where> is
  "on my target", "north of me", "10 km east of me", "over waypoint 4", "over Alpha" or nothing |
  "Overlord, picture" | "Overlord, status" | "Overlord, cancel SEAD|CAS|CAP|escort|all" |
  "Overlord, request target" | "Overlord, next target" | "Overlord, mark target" | "Overlord, BDA"
- Named waypoints (Alpha..Zulu, Objective): "Mark our current position as waypoint Bravo" |
  "Set grid <the grid he said> as waypoint Charlie" (only when he said a grid) | "Where is Bravo" | "Delete waypoint Bravo" |
  "List waypoints"
- Reports: "What's our grid" | "Say bullseye" | "Nearest friendly airfield" | "Nearest FARP" | "Debrief"
Keep the pilot's own place: "up north" / "to the west" -> "north of me" / "west of me"; "my target"
only if he said target; never add a waypoint, name, grid or number he didn't say.
Only ROUTE when the pilot clearly meant one of these. Never ROUTE your own crew orders.
"""

# Prompt for an aircraft with no crew (no George, no Huey crew): routing only.
ROUTER_SYSTEM = ("You turn a pilot's radio and intercom requests in DCS into standard orders.\n"
                 "Each message starts with STATE, then PILOT: what he said.\n" + ROUTE_PROMPT +
                 '\nOtherwise answer {"action": "NONE", "speech": "Say again."}. Respond STRICTLY in JSON.\n')

LEARNED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "learned_phrases.json")
CONFIRM_TIMEOUT_S = 12.0

SAY_AGAIN = re.compile(r"\bsay again\b", re.I)
# What Overlord or a wingman says when he didn't get it: the model may try to work it out first.
MISSED = re.compile(r"\bsay again\b|\bnot in the briefing\b", re.I)

_NUMWORD = {"two": "2", "to": "2", "too": "2", "three": "3", "tree": "3", "four": "4", "for": "4",
            "five": "5", "six": "6"}
# Orders read back before they run (hard to undo, or they shoot / spend Olympus points).
_RISKY = re.compile(r"\b(?:independent|requesting|attack|engage|weapons free|lase|cancel|delete|clear|"
                    r"rtb|jettison)\b", re.I)
_AFFIRM = re.compile(r"^\s*(?:affirm\w*|a firm|yes|yeah|yep|correct|confirm\w*|that'?s (?:right|correct)|"
                     r"roger|do it|execute|go ahead|send it)\b", re.I)
_NEGATIVE = re.compile(r"^\s*(?:negative|no|nope|cancel|disregard|wrong|abort|belay|never mind)\b", re.I)
# Whisper's versions of a one-word answer (24 Oct: "Afrom." dropped a pending CAS request).
_AFFIRM_WORDS = ("affirm", "affirmative")
_NEGATIVE_WORDS = ("negative", "negatory")

_DIR_WORDS = ("north east", "north west", "south east", "south west", "north", "south", "east", "west")
_NATO = {"alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
         "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
         "uniform", "victor", "whiskey", "xray", "yankee", "zulu", "objective"}

# Words a mis-heard span may be learned as (anything else is only learned as a whole utterance).
KEYWORDS = {"outcast", "overlord", "rejoin", "independent", "waypoint", "bullseye", "sead", "cas",
            "cap", "escort", "rtb", "defences", "lase", "lasing", "objective", "farp", "debrief",
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
            "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
            "uniform", "victor", "whiskey", "xray", "yankee", "zulu"}
# Never learned as a mis-hearing of a keyword (too common: would rewrite ordinary speech).
_STOP = {"a", "an", "the", "to", "too", "two", "for", "four", "of", "on", "in", "at", "my", "me", "you",
         "your", "our", "we", "us", "it", "is", "and", "or", "go", "get", "set", "say", "all", "up",
         "out", "over", "cast", "lord", "way", "point", "target", "fire", "hold", "free", "air"}


def norm(text: str) -> str:
    """Lower case, letters/digits/apostrophes only, single spaces."""
    t = (text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", t)).strip()


# spoken digits for the "no invented numbers" check (31 Oct: "george waypoint alpha" was rewritten
# with the prompt's example grid, "Set grid 38T LM 7218 4018 as waypoint Alpha", and learned)
_DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9", "ten": "10",
                "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
                "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}
_TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fifty": "5", "sixty": "6", "seventy": "7",
         "eighty": "8", "ninety": "9"}


def _spoken_digits(t: str) -> str:
    """Every digit the pilot said, in order ("three eight tango" -> "38", "twenty five" -> "25")."""
    out, ws = [], t.split()
    for i, w in enumerate(ws):
        if w in _TENS:
            nxt = ws[i + 1] if i + 1 < len(ws) else ""
            unit = _DIGIT_WORDS.get(nxt, "")
            out.append(_TENS[w] + ("" if len(unit) == 1 and unit != "0" else "0"))
        elif w in _DIGIT_WORDS and not (i and ws[i - 1] in _TENS and len(_DIGIT_WORDS[w]) == 1
                                        and w not in ("zero", "oh")):
            out.append(_DIGIT_WORDS[w])
        elif w in _NUMWORD:
            out.append(_NUMWORD[w])
        else:
            out.append("".join(ch for ch in w if ch.isdigit()))
    return "".join(out)


def _invented_number(o: str, c: str) -> Optional[str]:
    """A number (or grid part) in the rewrite that the pilot never said, else None."""
    said, ow = _spoken_digits(o), set(o.split())
    for w in c.split():
        d = "".join(ch for ch in w if ch.isdigit())
        if d and w not in ow and d not in said:
            return w
    return None


def _numbers(t: str) -> set:
    return {_NUMWORD.get(w, w) for w in t.split() if w.isdigit() or w in _NUMWORD}


def _like(words, target: str, cutoff: float) -> bool:
    """Some word (or two neighbouring words) of words sounds like target."""
    spans = words + [a + b for a, b in zip(words, words[1:])]
    return any(difflib.SequenceMatcher(None, s, target).ratio() >= cutoff for s in spans)


def _directions(t: str) -> list:
    """Compass directions in normalised text, longest first ("north west" before "west")."""
    t = re.sub(r"\b(north|south)(east|west)\b", r"\1 \2", t)
    found = []
    for d in _DIR_WORDS:
        if re.search(r"\b" + d + r"\b", t) and not any(d in f for f in found):
            found.append(d)
    return found


def repair(original: str, command: str) -> str:
    """Puts back what the model dropped: a direction the pilot gave for an Overlord request
    (24 Oct: "...armour up north" came back as "requesting CAS on my target")."""
    o, c = norm(original), norm(command)
    m = re.match(r"overlord requesting (sead|cas|cap|escort)\b", c)
    dirs = _directions(o)
    if m and dirs and not _directions(c):
        fixed = f"Overlord, requesting {m.group(1).upper()} {dirs[0]} of me"
        print(f"[ROUTER] Kept the direction: '{command}' -> '{fixed}'")
        return fixed
    return command


def plausible(original: str, command: str) -> Tuple[bool, str]:
    """Is the rewrite addressed to someone the pilot actually named, and does it only use
    places he said? (ok, why not)"""
    o, c = norm(original), norm(command)
    ow = o.split()
    if not c or c == o:
        return False, "no change"
    bad = _invented_number(o, re.sub(r"^outcast \d+\b", "", c))   # the callsign has its own check below
    if bad:
        return False, f"{bad} not said"
    # no invented places (24 Oct: "...armour up north" -> "requesting CAS over Alpha")
    for name in (w for w in c.split() if w in _NATO):
        if not _like(ow, name, 0.7):
            return False, f"{name} not said"
    m = re.search(r"\bwaypoint (\d+)\b", c)
    if m and m.group(1) not in _numbers(o) and not re.search(r"\b" + m.group(1) + r"\b", o):
        return False, f"waypoint {m.group(1)} not said"
    for d in _directions(c):
        if d not in _directions(o) and not (d in ("north", "south", "east", "west")
                                            and any(d in x for x in _directions(o))):
            return False, f"{d} not said"
    m = re.match(r"outcast (\d)\b", c)
    if m:
        return (m.group(1) in _numbers(o), f"Outcast {m.group(1)} not named")
    if c.startswith("overlord"):
        ok = _like(ow, "overlord", 0.6) or re.search(r"\b(?:sead|cas|cap|escort|close air|fighter cover|"
                                                    r"picture|bogey dope|bandits)\b", o) is not None
        return ok, "Overlord not named"
    m = re.match(r"(two|three|four|flight)\b", c)
    if m:
        who = m.group(1)
        ok = (who == "flight" and re.search(r"\b(?:flight|everyone|everybody|wingm[ae]n)\b", o) is not None) or \
             (who != "flight" and _NUMWORD[who] in _numbers(o))
        return ok, f"{who} not named"
    named = [w for w in c.split() if w in KEYWORDS - {"waypoint", "outcast", "overlord"}]
    if re.search(r"\b(?:mark|set|where|delete)\b", c) and named:
        return any(_like(ow, n, 0.7) for n in named), "waypoint name not said"
    return True, ""


class Router:
    def __init__(self, shared: Callable[[str], Optional[tuple]], speak: Callable[[str, str], None],
                 learned_file: Optional[str] = LEARNED_FILE, clock: Callable[[], float] = time.monotonic,
                 understands: Optional[Callable[[str], Optional[bool]]] = None):
        """shared(text) -> (said, speaker) or None: the shared handlers, without speaking.
        speak(text, speaker): speaker is "george" or "overlord".
        understands(command) -> False when no handler would take it (checked before "Confirm ...?",
        24 Oct: "Confirm, Outcast 2, engage TUNKS, MARTWEST?"), None when it can't tell."""
        self.shared, self.speak, self.file, self.clock = shared, speak, learned_file, clock
        self.understands = understands
        self._recent = None                      # (addressee, when): "outcast 2" / "overlord"
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()       # one file write at a time; never held with _lock
        self._version = 0                        # bumped under _lock on every change to phrases / words
        self._saved_version = 0
        self._pending = None                     # (original, command, expires)
        self.phrases, self.words = {}, {}
        self._load()

    # ------------------------------------------------------------ learned phrases
    def _load(self):
        if not self.file or not os.path.exists(self.file):
            return
        try:
            with open(self.file, encoding="utf-8") as f:
                data = json.load(f)
            self.phrases = {}
            for k, v in (data.get("phrases") or {}).items():
                ok, why = plausible(k, v)
                if ok or why == "no change":
                    self.phrases[norm(k)] = v
                else:                        # learned before a gate existed: never fire it again
                    print(f"[ROUTER] Dropped learned phrase '{k}' -> '{v}': {why}.")
            self.words = {norm(k): norm(v) for k, v in (data.get("words") or {}).items()}
            print(f"[ROUTER] {len(self.phrases)} learned phrases, {len(self.words)} learned words.")
        except Exception as e:
            print(f"[ROUTER] Couldn't read {self.file}: {type(e).__name__}: {e}")

    def _save(self):
        """Writes the learned phrases. Call WITHOUT _lock held (audit step 9: the file was written
        under it, so apply_learned on the voice thread waited on the disk). Copies are taken under
        _lock; writes are serialised on _save_lock and an older copy never overwrites a newer one."""
        if not self.file:
            return
        with self._lock:
            version = self._version
            data = {"_note": "Learned from rewrites that worked. Delete any line that is wrong.",
                    "phrases": dict(self.phrases), "words": dict(self.words)}
        with self._save_lock:
            if version <= self._saved_version:
                return                           # a newer copy is already on disk
            try:
                os.makedirs(os.path.dirname(self.file), exist_ok=True)
                tmp = self.file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1, sort_keys=True)
                os.replace(tmp, self.file)
                self._saved_version = version
            except Exception as e:
                print(f"[ROUTER] Couldn't save {self.file}: {type(e).__name__}: {e}")

    def apply_learned(self, text: str) -> str:
        """The utterance with what was learned applied (unchanged when nothing applies)."""
        key = norm(text)
        with self._lock:
            whole = self.phrases.get(key)
            words = dict(self.words)
        if whole:
            print(f"[ROUTER] Learned: '{text}' -> '{whole}'")
            return whole
        if not words:
            return text
        out = text
        for src in sorted(words, key=len, reverse=True):       # "ok cas" also matches "OK, CAS"
            rx = r"[\s,.!?-]+".join(re.escape(w) for w in src.split())
            out = re.sub(r"(?<![a-z0-9'])" + rx + r"(?![a-z0-9'])", words[src], out, flags=re.I)
        if out != text:
            print(f"[ROUTER] Learned words: '{text}' -> '{out}'")
        return out

    def learn(self, original: str, command: str) -> None:
        o, c = norm(original), command.strip()
        if not o or o == norm(c):
            return
        with self._lock:
            self.phrases[o] = c
            a, b = o.split(), norm(c).split()
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                if op != "replace" or j2 - j1 != 1 or not 1 <= i2 - i1 <= 2:
                    continue
                src, dst = " ".join(a[i1:i2]), b[j1]
                if dst not in KEYWORDS or src in _STOP or any(w.isdigit() for w in a[i1:i2]):
                    continue
                if difflib.SequenceMatcher(None, src.replace(" ", ""), dst).ratio() >= 0.45:
                    self.words[src] = dst
            self._version += 1
        self._save()
        print(f"[ROUTER] Learned '{o}' -> '{c}'.")

    def forget(self, text: str) -> bool:
        with self._lock:
            gone = self.phrases.pop(norm(text), None) is not None
            if gone:
                self._version += 1
        if gone:
            self._save()
        return gone

    # ------------------------------------------------------------ confirmation
    def pending(self) -> Optional[str]:
        with self._lock:
            p = self._pending
        return p[1] if p and p[2] > self.clock() else None

    def answer_pending(self, text: str) -> bool:
        """Call first with every utterance. True if it was the answer to "Confirm, ...?"."""
        with self._lock:
            p, self._pending = self._pending, None
        if not p or p[2] <= self.clock():
            return False
        original, command, _ = p
        who = speaker_for(command)
        said = answer(text)
        if said == "yes":
            self._run(original, command)
            return True
        if said == "no":
            print(f"[ROUTER] '{command}' not confirmed.")
            self.speak("Overlord, copy, disregard." if who == "overlord" else "Copy, disregard.", who)
            return True
        if len(norm(text).split()) <= 2:         # a mis-heard short answer: ask again, keep it
            with self._lock:
                self._pending = p
            print(f"[ROUTER] '{text}': not affirm or negative; asking again.")
            self.speak("Overlord, say again, affirm or negative?" if who == "overlord"
                       else "Say again, affirm or negative?", who)
            return True
        print(f"[ROUTER] '{command}' dropped: '{text}' is a new order.")
        return False                             # something else: handled as a new order

    # ------------------------------------------------------------ follow-ups
    FOLLOW_UP_S = 30.0

    def note_addressee(self, who: str) -> None:
        """Who the pilot just spoke to ("outcast 2", "overlord"): a bare "hold position" a moment later
        is for them (28 Oct: "Outcast 2, whole position" -> say again -> "Hold position." refused)."""
        with self._lock:
            self._recent = (who.lower(), self.clock())

    def _follow_up(self, command: str) -> bool:
        m = re.match(r"(outcast \d|overlord)\b", norm(command))
        with self._lock:
            r = self._recent
        return bool(m and r and r[0] == m.group(1) and self.clock() - r[1] <= self.FOLLOW_UP_S)

    # ------------------------------------------------------------ the rewrite
    def dispatch(self, original: str, command: str) -> bool:
        """The model's rewrite of original. True if it was taken (run, or read back to confirm)."""
        command = repair(original, (command or "").strip().strip('"').strip())
        ok, why = plausible(original, command)
        if not ok and why.endswith("not named") and self._follow_up(command):
            print(f"[ROUTER] '{original}': follow-up to {command.split(',')[0]}.")
            ok, why = True, ""
        if ok and self.understands is not None and self.understands(command) is False:
            ok, why = False, "no handler understands it"
        if not ok:
            print(f"[ROUTER] Rejected '{original}' -> '{command}': {why}.")
            return False
        print(f"[ROUTER] '{original}' -> '{command}'")
        if _RISKY.search(command):
            with self._lock:
                self._pending = (original, command, self.clock() + CONFIRM_TIMEOUT_S)
            self.speak(readback(command), speaker_for(command))
            return True
        return self._run(original, command)

    def _run(self, original: str, command: str) -> bool:
        got = self.shared(command)
        if got is None:
            print(f"[ROUTER] No handler took '{command}'.")
            who = speaker_for(command)
            self.speak("Overlord, say again." if who == "overlord" else "Say again.", who)
            return True
        said, speaker = got
        if said:
            self.speak(said, speaker)
        # only a rewrite that was taken is learned: not "say again", "unable" or a refusal ("negative, no
        # targets for you": 30 Oct, a spawn request rewritten as "request target" was learned)
        # nor "nothing to do" answers (30 Oct: "end the training" -> "cancel all" got "no flights tasked";
        # "mark ground training group with smoke" -> "mark target" got "no target to mark": both learned)
        if (not MISSED.search(said or "") and not re.search(r"\b(?:unable|negative)\b", said or "", re.I)
                and not re.match(r"^\s*(?:overlord,\s*)?(?:no|nothing)\b", said or "", re.I)):
            self.learn(original, command)
        return True


def speaker_for(command: str) -> str:
    """Who answers about this order: Overlord for his own requests, else the crew."""
    return "overlord" if norm(command).startswith("overlord") else "george"


def readback(command: str) -> str:
    """'Overlord, confirm CAS north of you?' / 'Confirm, Outcast 2, engage air defences?'"""
    m = re.match(r"\s*overlord[\s,]+(.*)$", command, re.I)
    if not m:
        return f"Confirm, {command}?"
    rest = re.sub(r"^requesting\s+", "", m.group(1).strip().rstrip(".?!"), flags=re.I)
    rest = re.sub(r"\bmy\b", "your", re.sub(r"\bme\b", "you", rest))
    return f"Overlord, confirm {rest}?"


def bare_answer(text: str) -> Optional[str]:
    """'yes' / 'no' only for "affirm" / "negative" themselves (and Whisper's spellings), not for
    "send it" or "roger", which mean something to George too."""
    words = norm(text).split()
    if not 1 <= len(words) <= 2 or (len(words) == 2 and words != ["a", "firm"]):
        return None
    got = answer(text)
    return got if got and not re.match(r"(?:yes|yeah|yep|correct|roger|do|execute|go|send|no|nope|cancel|"
                                       r"wrong|abort|belay|never|confirm)\b", words[0]) else None


def answer(text: str) -> Optional[str]:
    """'yes', 'no' or None for a reply to "Confirm ...?", allowing for Whisper's spellings."""
    if _AFFIRM.search(text):
        return "yes"
    if _NEGATIVE.search(text):
        return "no"
    words = norm(text).split()
    if not 1 <= len(words) <= 2:
        return None
    joined = "".join(words)
    best = lambda opts: max(difflib.SequenceMatcher(None, joined, o).ratio() for o in opts)
    yes, no = best(_AFFIRM_WORDS), best(_NEGATIVE_WORDS)
    if max(yes, no) < 0.6:
        return None
    return "yes" if yes > no else "no"


def routing_state(outcasts, wingmen, waypoints, olympus_up: bool) -> str:
    """The STATE part the model needs for ROUTE: who exists right now. Kept short."""
    parts = ["Outcasts: " + (", ".join(f"Outcast {n}" for n in outcasts) or "none")]
    if wingmen:
        parts.append("Wingmen: " + ", ".join(wingmen))
    if waypoints:
        parts.append("Named waypoints: " + ", ".join(waypoints))
    parts.append("Olympus " + ("up" if olympus_up else "down"))
    return " | ".join(parts)


def stt_hints(outcasts, waypoints) -> str:
    """Words for the end of Whisper's prompt (the end counts most and is never cut off)."""
    words = [f"Outcast {n}" for n in outcasts] + [f"waypoint {w}" for w in waypoints]
    return (" " + ", ".join(words) + ".") if words else ""
