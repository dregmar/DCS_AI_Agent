"""
common/briefing.py - the briefing knowledge base: local search (BM25) over JSON files, answered by
George / the Huey co-pilot on the intercom and by Overlord on the radio.

Sources (data/, reloaded when a file changes; nothing leaves the PC):
    campaign_briefing.json   yours: package priority, ROE, sectors, frequencies, threats ...
                             (write it by hand, or: python briefing_import.py my_briefing.txt)
    mission_briefing.json    written by the program from the loaded DCS mission's briefing
                             (situation, your coalition's task; george_comms_hook.lua sends it)
    apache_procedures.json   AH-64D procedures and how George works
    huey_procedures.json     UH-1H procedures (optional, same format)
    + the crews' checklists (apache/checklists.py, huey/checklists.py), generated at start
    manuals/*.json           DCS module manuals (python manual_import.py <pdf> --aircraft <types>):
                             {"aircraft": [DCS type names], "entries": [... "section": bookmark path]};
                             used only in those aircraft, never by Overlord

File format: {"entries": [{"title": "...", "text": "...", "say": "optional short spoken answer",
              "tags": ["optional", "search", "words"], "aircraft": "AH-64D" | "UH-1H" | "any"}]}
(a bare list of entries also works).

Who sees what: Overlord answers from the campaign and mission briefings only. The co-pilot also uses
the procedures and checklists of the aircraft you're in.

Answering: the best entry's "say" is spoken as written. Otherwise a short text is read out; a long
one goes to the local model with the top extracts ("answer only from these"). Every number in the
model's answer must appear in the extracts, else the extract itself is read out (no invented
frequencies or codes). Nothing found: "That's not in the briefing."
"""
import json
import math
import os
import re
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

FILES = {                       # file -> (source, default aircraft)
    "campaign_briefing.json": ("campaign", "any"),
    "mission_briefing.json": ("mission", "any"),
    "apache_procedures.json": ("procedures", "AH-64D"),
    "huey_procedures.json": ("procedures", "UH-1H"),
}
MANUAL_DIR = "manuals"
BRIEFING_SOURCES = ("campaign", "mission")
RELOAD_CHECK_S = 2.0
MIN_SCORE = 1.2                 # BM25 score below this is "not found"
# ... and the entry must contain at least 2/3 of the question's words ("fire rockets" doesn't
# match "return fire only")
SHORT_TEXT = 240                # entries this short are read out as they are
K1, B = 1.5, 0.75
HOW_TO_BOOST = 1.3
_HOW_TO = re.compile(r"^\s*(?:(?:george|okay|ok|so)[\s,]+)*how (?:do|can|should|to|would)\b", re.I)
_STEPS = re.compile(r"procedure|employment|checklist|start ?up|\bstart\b|launch|recovery|set ?up", re.I)

# ------------------------------------------------------------------ text
_STOP = set("""a an the of to in on at for and or is are was were be been do does did what whats what's
where wheres which who whos when why how our my we us you your me i it its this that these those
there here any some with from by as about please george overlord say tell give today tonight now
current currently again can could would should will shall have has had get got know remind brief
confirm read let lets let's going current correct""".split()) | {"s", "re", "ll"}

# phrase -> canonical token (applied to documents and questions alike)
_PHRASES = [
    (r"rules? of engagement|engagement rules|\broe'?s\b", " roe "),
    (r"area of operations?|\baos?\b|operating area", " ao "),
    (r"time on target|\btots?\b", " tot "),
    (r"push time|push", " push "),
    (r"frequenc(?:y|ies)|\bfreqs?\b|channel(?:s)?|\bpresets?\b", " freq "),
    (r"call ?signs?", " callsign "),
    (r"air ?defen[cs]es?|\bsams?\b|surface to air|triple a|\baaa\b|anti ?aircraft|\bmanpads?\b", " airdefence "),
    (r"priorit(?:y|ies|ise|ize|ized|ised)|primary target|main effort", " priority "),
    (r"objectives?|tasking|\btasks?\b|\bmissions?\b|\bgoals?\b|\baims?\b", " objective "),
    (r"threats?|enemy|enemies|hostiles?|opfor|red forces", " threat "),
    (r"friendl(?:y|ies)|blue forces|own forces", " friendly "),
    (r"forward arming and refuel(?:l)?ing point|\bfarps?\b", " farp "),
    (r"\bdivert\b|\balternate\b", " divert "),
    (r"code ?words?", " codeword "),
    (r"laser codes?|\bprf\b", " lasercode "),
    (r"start ?up|starting|\bstart\b", " start "),
    (r"shut ?down", " shutdown "),
    (r"take ?off", " takeoff "),
    (r"hell ?fires?|\bmissiles?\b", " hellfire "),
    (r"\bguns?\b|cannon|30 ?mm|chain gun", " gun "),
    (r"\brockets?\b|hydras?|ffars?", " rocket "),
    (r"\bcase (?:3|three|iii)\b", " casethree "),        # carrier recoveries (manuals write "Case III")
    (r"\bcase (?:2|two|ii)\b", " casetwo "),
    (r"\bcase (?:1|one|i)\b", " caseone "),
]
_PHRASES = [(re.compile(p, re.I), r) for p, r in _PHRASES]


def _norm(text: str) -> str:
    t = " " + (text or "").lower().replace("’", "'") + " "
    for rx, rep in _PHRASES:
        t = rx.sub(rep, t)
    return t


def tokens(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", _norm(text)):
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        for suf in ("ment", "ing", "ed"):           # "alignment" / "aligning" / "aligned" -> "align"
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[:-len(suf)]
                break
        out.append(w)
        if "-" in w:                                    # "sa-6" also matches "sa 6"
            out.extend(p for p in w.split("-") if p and p not in _STOP)
    return out


# ------------------------------------------------------------------ question detection
_QUESTION = re.compile(r"^(?:(?:george|hey|okay|ok|so|and|uh|um|copilot|co pilot)[\s,]+)*"
                       r"(?:what|what's|whats|where|where's|which|who|who's|when|why|how|is|are|do|does|"
                       r"did|tell me|remind me|brief me|read me|say again the|confirm|any|give me the)\b|\?\s*$",
                       re.I)
_TOPIC = re.compile(r"\b(?:briefing|brief|mission|package|priorit\w*|objectives?|tasking|tasks?|roe|rules? of "
                    r"engagement|engagement rules|sector|ao|area of operations?|expected threats?|briefed threats?|"
                    r"threat level|known threats?|intel\w*|frequenc\w*|"
                    r"freqs?|call ?signs?|campaign|bullseye|code ?words?|joker|divert|alternate|farps?|"
                    r"weather|time on target|tot|push time|procedures?|sop|laser codes?|egress|ingress|"
                    r"holding|friendl\w*|tankers?|awacs|jtac|landing zone|lz|pick ?up|drop ?off|"
                    r"how (?:do|does|can|should|to) (?:i|you|we|the|it|one)|employ\w*|explain)\b", re.I)


# "current mission" / "current objective" said on their own (23 Oct): a question too
_BARE = re.compile(r"^(?:(?:george|okay|ok)[\s,]+)*(?:our |the |current |say )*(?:mission|objective|tasking)s?"
                   r"(?: today)?[.?!]*$", re.I)


def is_question(text: str) -> bool:
    t = (text or "").strip()
    return bool(_QUESTION.search(t)) or bool(_BARE.match(t))


# Questions about the mission as a whole ("what's our current mission?")
_OVERVIEW = {"objective", "situation", "summary", "summarise", "summarize", "overview", "briefing", "plan",
             "job", "doing", "today"}

# Live questions about what George sees now, not the briefing
_LIVE = re.compile(r"\b(?:priority (?:threat|target)|target (?:\d+|one|two|three|four|five)|nearest|closest|"
                   r"(?:my|our|the) target\b|contacts?|in range|on the nose|o'?clock)\b", re.I)


def is_manual_question(text: str) -> bool:
    """Any question that isn't about what's around you now (for aircraft with a manual loaded)."""
    return is_question(text) and not _LIVE.search(text or "")


def is_briefing_question(text: str) -> bool:
    """A question that names a briefing topic ("what's our package priority", "what's the ROE")."""
    return is_question(text) and bool(_TOPIC.search(text or "")) and not _LIVE.search(text or "")


# ------------------------------------------------------------------ entries
def fits(entry_aircraft, aircraft: Optional[str]) -> bool:
    """An entry's aircraft ("any", one name, or a list of DCS type names) against the one you fly."""
    if not aircraft or entry_aircraft in (None, "any"):
        return True
    names = entry_aircraft if isinstance(entry_aircraft, (list, tuple)) else [entry_aircraft]
    # "A-10C" fits "A-10C_2", "AH-64D" fits "AH-64D_BLK_II": DCS adds suffixes to type names
    return any(n == aircraft or n.startswith(aircraft + "_") or aircraft.startswith(n + "_") for n in names)


class Entry:
    __slots__ = ("id", "source", "title", "text", "say", "tags", "aircraft", "toks", "tf", "section")

    def __init__(self, id, source, title, text, say="", tags=(), aircraft="any", section=""):
        self.id, self.source, self.title, self.text = id, source, title or "", text or ""
        self.say, self.tags, self.aircraft = say or "", list(tags or []), aircraft or "any"
        self.section = section or self.title
        self.toks = tokens(self.title) * 2 + tokens(" ".join(self.tags)) * 2 + tokens(self.text)
        tf: Dict[str, int] = {}
        for t in self.toks:                      # counted once here: manuals hold thousands of entries
            tf[t] = tf.get(t, 0) + 1
        self.tf = tf

    def as_dict(self) -> dict:
        d = {"title": self.title, "text": self.text}
        if self.say:
            d["say"] = self.say
        if self.tags:
            d["tags"] = self.tags
        if self.aircraft != "any":
            d["aircraft"] = self.aircraft
        return d


def entries_from_json(data, source: str, aircraft: str, prefix: str) -> List[Entry]:
    rows = data.get("entries", []) if isinstance(data, dict) else data
    out = []
    for i, r in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(r, dict) or not (r.get("text") or r.get("say")):
            continue
        out.append(Entry(f"{prefix}{i}", source, r.get("title", ""), r.get("text") or r.get("say"),
                         r.get("say", ""), r.get("tags") or [], r.get("aircraft") or aircraft,
                         r.get("section", "")))
    return out


def checklist_entries() -> List[Entry]:
    """The crews' checklists as entries ("what's the before take-off checklist")."""
    out = []
    for mod, aircraft, attr in (("apache.checklists", "AH-64D", "APACHE_CHECKLISTS"),
                                ("huey.checklists", "UH-1H", "CHECKLISTS")):
        try:
            lists = getattr(__import__(mod, fromlist=[attr]), attr)
        except Exception as e:
            print(f"[BRIEFING] {mod}: {type(e).__name__}: {e}")
            continue
        for key, (title, items) in lists.items():
            steps = [it[0] for it in items]
            out.append(Entry(f"chk-{aircraft}-{key}", "procedures", f"{title} checklist",
                             ". ".join(steps) + ".", say=f"{title}: " + ", ".join(steps) + ".",
                             tags=["checklist", key.replace("_", " ")], aircraft=aircraft))
    return out


# ------------------------------------------------------------------ mission briefing (from DCS)
def parse_brief_message(text: str) -> Tuple[str, Dict[str, str]]:
    """'BRIEF <mission name>\\n##SITUATION\\n...\\n##TASK\\n...' -> (name, {section: text})."""
    lines = (text or "").split("\n")
    name = lines[0][6:].strip() if lines and lines[0].startswith("BRIEF") else ""
    sections, cur = {}, None
    for line in lines[1:]:
        if line.startswith("##"):
            cur = line[2:].strip().title()
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"
    return name, {k: v.strip() for k, v in sections.items() if v.strip()}


def split_paragraphs(text: str, limit: int = 600) -> List[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[-*•]\s)|\n(?=[A-Z][A-Z /]{3,}:)", text or "") if p.strip()]
    merged = []                     # a fragment ("An alternate 2016:") belongs to what follows it
    for p in paras:
        if merged and (len(merged[-1]) < 60 or merged[-1].endswith(":")):
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    paras = merged
    out = []
    for p in paras:
        while len(p) > limit:                       # long paragraph: cut at a sentence end
            cut = p.rfind(". ", 0, limit)
            cut = cut + 1 if cut > limit // 3 else limit
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


# ------------------------------------------------------------------ the knowledge base
class KnowledgeBase:
    def __init__(self, data_dir: str, extra: Optional[Callable[[], Iterable[Entry]]] = None):
        self.data_dir = data_dir
        self._extra_fn = extra
        self._extra: Optional[List[Entry]] = None
        self._lock = threading.Lock()
        self._mtimes: Dict[str, float] = {}
        self._entries: List[Entry] = []
        self._checked = -1e9
        self._df: Dict[str, int] = {}
        self._avgdl = 1.0
        self._mission = ""                   # the DCS mission's name (mission_briefing.json)
        self._last: Optional[dict] = None    # the last answer's entries, for the kneeboard

    # -------------------------------------------------------------- loading
    def _files(self) -> Dict[str, Tuple[str, str]]:
        """file (relative to data/) -> (source, default aircraft), manuals included."""
        files = dict(FILES)
        try:
            for f in sorted(os.listdir(os.path.join(self.data_dir, MANUAL_DIR))):
                if f.endswith(".json"):
                    files[MANUAL_DIR + "/" + f] = ("manual", "none")
        except OSError:
            pass
        return files

    def _scan(self) -> Dict[str, float]:
        out = {}
        for f in self._files():
            p = os.path.join(self.data_dir, f)
            try:
                out[f] = os.path.getmtime(p)
            except OSError:
                pass
        return out

    def _reload_if_changed(self) -> None:
        now = time.monotonic()
        if now - self._checked < RELOAD_CHECK_S and self._extra is not None:
            return
        self._checked = now
        mt = self._scan()
        if mt == self._mtimes and self._extra is not None:
            return
        if self._extra is None:
            self._extra = list(self._extra_fn()) if self._extra_fn else []
        entries = list(self._extra)
        counts = []
        for f, (source, aircraft) in self._files().items():
            if f not in mt:
                continue
            try:
                with open(os.path.join(self.data_dir, f), encoding="utf-8") as fh:
                    data = json.load(fh)
                if source == "manual":                # the file says which aircraft it is for
                    aircraft = (data.get("aircraft") if isinstance(data, dict) else None) or "none"
                got = entries_from_json(data, source, aircraft, f.split(".")[0].replace("/", "-") + "-")
                if source == "mission":
                    self._mission = str(data.get("mission") or "") if isinstance(data, dict) else ""
                entries += got
                counts.append(f"{f} {len(got)}")
            except Exception as e:
                print(f"[BRIEFING] Can't read {f}: {type(e).__name__}: {e}")
        df: Dict[str, int] = {}
        for e in entries:
            for t in set(e.toks):
                df[t] = df.get(t, 0) + 1
        self._entries, self._df, self._mtimes = entries, df, mt
        self._avgdl = (sum(len(e.toks) for e in entries) / len(entries)) if entries else 1.0
        print(f"[BRIEFING] Knowledge base: {len(entries)} entries ({', '.join(counts) or 'no data files'}"
              f"{', ' + str(len(self._extra)) + ' checklists' if self._extra else ''}).")

    def entries(self) -> List[Entry]:
        with self._lock:
            self._reload_if_changed()
            return list(self._entries)

    def has_manual(self, aircraft: Optional[str]) -> bool:
        """A manual is loaded for this DCS type."""
        return bool(aircraft) and any(e.source == "manual" and fits(e.aircraft, aircraft) for e in self.entries())

    def mission_name(self) -> str:
        with self._lock:
            return self._mission

    def last_answer(self) -> Optional[dict]:
        """{"ids": [entry ids used], "question", "when"} of the last answer, or None (kneeboard)."""
        with self._lock:
            return dict(self._last) if self._last else None

    def _note(self, question: str, entries: List[Entry]) -> None:
        with self._lock:
            self._last = {"ids": [e.id for e in entries], "question": question, "when": time.time()}

    # -------------------------------------------------------------- search
    def search(self, query: str, sources: Optional[Iterable[str]] = None, aircraft: Optional[str] = None,
               k: int = 3) -> List[Tuple[float, Entry]]:
        with self._lock:
            self._reload_if_changed()
            entries, df, avgdl = self._entries, self._df, self._avgdl
        q = [t for t in tokens(query)]
        if not q or not entries:
            return []
        n = len(entries)
        src = set(sources) if sources else None
        scored = []
        for e in entries:
            if src and e.source not in src:
                continue
            if aircraft and not fits(e.aircraft, aircraft):
                continue
            if aircraft is None and e.source == "manual":
                continue                             # manuals only for the aircraft they're for
            tf = e.tf
            dl = len(e.toks) or 1
            s = 0.0
            for t in set(q):
                f = tf.get(t)
                if not f:
                    continue
                idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
                s += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / avgdl))
            if s > 0:
                scored.append((s, e))
        if _HOW_TO.match(query or ""):               # "how do I ...": the steps before the panel description
            scored = [(s * HOW_TO_BOOST if e.source == "manual" and _STEPS.search(e.section) else s, e)
                      for s, e in scored]
        scored.sort(key=lambda x: -x[0])
        return scored[:k]

    # -------------------------------------------------------------- answering
    def answer(self, question: str, speaker: str = "george", aircraft: Optional[str] = None,
               llm: Optional[Callable[[List[dict]], dict]] = None) -> str:
        """What to say. speaker 'overlord' = briefings only, with "Overlord, " in front."""
        over = speaker == "overlord"
        lead = "Overlord, " if over else ""
        q = set(tokens(question))
        if q and q <= _OVERVIEW:                     # "what's our mission?", "what's the situation?"
            return lead + self._overview(question, speaker, llm, over)
        hits = self.search(question, BRIEFING_SOURCES if over else None, None if over else aircraft)
        need = math.ceil(len(q) * 2 / 3)
        hits = [h for h in hits if h[0] >= MIN_SCORE and len(q & set(h[1].toks)) >= need]
        if not hits:
            return lead + ("negative, that's not in the briefing." if over else "That's not in the briefing.")
        top = hits[0][1]
        self._note(question, [top])
        print(f"[BRIEFING] '{question}' -> {top.title or top.id} ({hits[0][0]:.1f})"
              + "".join(f", {e.title or e.id} ({s:.1f})" for s, e in hits[1:]))
        if top.say:
            return lead + top.say
        if len(top.text) <= SHORT_TEXT or llm is None:
            return lead + self._extract(top.text)
        said = self._compose(question, [e for _, e in hits], speaker, llm)
        return lead + (said or self._extract(top.text))

    def _overview(self, question: str, speaker: str, llm, over: bool) -> str:
        """The whole mission: campaign priorities/objectives, then the mission's task and situation.
        Summarised by the model; without it, the task (or the paragraph that gives the goal)."""
        ents = self.entries()
        camp = [e for e in ents if e.source == "campaign" and ({"objective", "priority"} & set(e.toks))]
        task = [e for e in ents if e.source == "mission" and "task" in e.title.lower()]
        rest = [e for e in ents if e.source == "mission" and e not in task]
        picked = (camp + task + rest)[:6]
        if not picked:
            return "negative, that's not in the briefing." if over else "That's not in the briefing."
        print(f"[BRIEFING] '{question}' -> overview of {len(picked)} entries")
        self._note(question, picked)
        if llm is not None:
            said = self._compose(question, picked, speaker, llm)
            if said:
                return said
        if task:                                     # the mission's own task first
            return self._extract(task[0].text)
        if camp:
            return camp[0].say or self._extract(camp[0].text)
        goal = (next((e for e in rest if re.search(r"\b(?:goal|ordered|objectives?)\b", e.text, re.I)), None)
                or next((e for e in rest if re.search(r"\b(?:your task|mission is|you will|your job)\b", e.text,
                                                       re.I)), None) or rest[0])
        return self._extract(goal.text)

    @staticmethod
    def _extract(text: str, limit: int = SHORT_TEXT) -> str:
        t = re.sub(r"\s+", " ", text).strip()
        if len(t) <= limit:
            return t
        cut = t.rfind(". ", 0, limit)
        return t[:cut + 1] if cut > 40 else t[:limit].rsplit(" ", 1)[0] + "."

    @staticmethod
    def _compose(question: str, entries: List[Entry], speaker: str,
                 llm: Callable[[List[dict]], dict]) -> Optional[str]:
        who = ("Overlord, the C2 controller on the radio" if speaker == "overlord"
               else "the co-pilot on the intercom")
        system = (f"You are {who}, answering the pilot's question from the mission briefing or the aircraft manual. "
                  "Use ONLY the extracts. Copy numbers, names, frequencies, codes and times exactly "
                  "as written. One or two short spoken sentences, no lists, no markdown. If the extracts don't "
                  'answer the question, say exactly: Not in the briefing. Reply JSON: {"say": "..."}')
        extracts = "\n".join(f"[{i + 1}] {e.title}: {re.sub(r'\s+', ' ', e.text)[:700]}"
                             for i, e in enumerate(entries))
        try:
            res = llm([{"role": "system", "content": system},
                       {"role": "user", "content": f"EXTRACTS:\n{extracts}\nQUESTION: {question}"}])
        except Exception as e:
            print(f"[BRIEFING] Model unavailable ({type(e).__name__}: {e}); reading the extract.")
            return None
        said = str((res or {}).get("say") or "").strip()
        if not said or "not in the briefing" in said.lower():
            return None
        allowed = set(re.findall(r"\d+(?:\.\d+)?", extracts))
        bad = [n for n in re.findall(r"\d+(?:\.\d+)?", said) if n not in allowed]
        if bad or len(said) > 320:
            print(f"[BRIEFING] Model answer rejected ({'numbers ' + ', '.join(bad) if bad else 'too long'}): '{said}'")
            return None
        return said

    # -------------------------------------------------------------- the DCS mission briefing
    def set_mission_briefing(self, name: str, sections: Dict[str, str]) -> bool:
        """Writes data/mission_briefing.json when the loaded mission's briefing changes."""
        entries = []
        for sec, text in sections.items():
            for i, p in enumerate(split_paragraphs(text)):
                entries.append({"title": f"Mission {sec.lower()}" + (f" ({i + 1})" if i else ""),
                                "text": p, "tags": ["mission", sec.lower(), "briefing"]})
        data = {"mission": name, "entries": entries}
        path = os.path.join(self.data_dir, "mission_briefing.json")
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("mission") == name and old.get("entries") == entries:
                return False
        except Exception:
            pass
        os.makedirs(self.data_dir, exist_ok=True)
        data["received"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        print(f"[BRIEFING] Mission briefing '{name}' saved: {len(entries)} entries "
              f"({', '.join(sections) or 'empty'}).")
        with self._lock:
            self._checked = -1e9                     # reload on the next question
        return True

    def on_brief_message(self, text: str) -> None:
        name, sections = parse_brief_message(text)
        self.set_mission_briefing(name, sections)
