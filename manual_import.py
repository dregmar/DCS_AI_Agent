"""
manual_import.py - turns a DCS module manual (PDF) into a knowledge-base file the crew voice answers
from: "how do I employ the Maverick?", "how do I align the INS?", "what's the Case III marshal stack?".

    python manual_import.py "C:\\...\\Mods\\aircraft\\FA-18C\\Doc\\DCS FA-18C Early Access Guide EN.pdf" --aircraft FA-18C_hornet
    python manual_import.py "...\\A-10C II Flight Manual EN.pdf" --aircraft A-10C_2 A-10C
    python manual_import.py "...\\Supercarrier Guide.pdf" --aircraft FA-18C_hornet --name supercarrier
    add --dry-run to see what would be kept without writing

The PDF's own bookmarks (outline) split it into sections: each becomes one or more entries titled
with its bookmark path ("Weapons > AGM-65 Maverick > Employment"), cut into ~600-character pieces.
Page headers / footers (lines on many pages), the contents, index and legal pages, and sections that
are mostly short lines (tables, figure labels) are left out.

Writes data/manuals/<name>.json. The program picks it up within a couple of seconds (no restart) and
uses it only while you fly one of the --aircraft types (DCS type names, as the console's [PROFILE]
line prints them). Nothing leaves your PC: keep these files out of anything you share; they are
Eagle Dynamics' text.

Needs pypdf (pip install pypdf).
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "data", "manuals")
PIECE = 600
MIN_WORDS = 10
_SKIP_TITLE = re.compile(r"^(?:table of contents|contents|index|legal|copyright|credits|revision|"
                         r"change ?log|disclaimer|acknowledg\w*|list of (?:figures|tables)|abbreviations?|"
                         r"glossary|dedication|foreword)\b", re.I)
_BULLET = re.compile(r"^\s*(?:[•●▪◦\-–*]|\d{1,2}[.)]|[a-z][.)])\s+")


# ------------------------------------------------------------------ text clean-up (pure)
def _shape(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def furniture(pages: Sequence[str], share: float = 0.3) -> set:
    """Lines (digits ignored) that appear on more than `share` of the pages: headers and footers."""
    if len(pages) < 5:
        return set()
    seen = Counter()
    for p in pages:
        seen.update({_shape(l) for l in p.splitlines() if l.strip()})
    return {s for s, n in seen.items() if n > share * len(pages) and len(s) < 120}


def clean(text: str, drop: set) -> str:
    """Drops header / footer lines, rejoins hyphenated breaks and wrapped lines; keeps a line break
    before bullets and numbered steps."""
    out: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _shape(line) in drop or re.fullmatch(r"(?:\d{1,4}\s*){1,8}", line):
            if out and out[-1] != "":
                out.append("")
            continue
        if out and out[-1] and not _BULLET.match(line):
            if out[-1].endswith("-") and not out[-1].endswith(" -"):
                out[-1] = out[-1][:-1] + line
            else:
                out[-1] += " " + line
        else:
            out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def table_like(text: str) -> bool:
    """Mostly short fragments (tables, figure callouts): poor spoken answers."""
    words = re.findall(r"[A-Za-z]{2,}", text)
    if len(words) < MIN_WORDS:
        return True
    sentences = len(re.findall(r"[a-z]{2,}[.!?](?:\s|$)", text))
    return sentences == 0 and len(words) < 60


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def cut_section(text: str, title: str, next_title: Optional[str]) -> str:
    """The part of `text` from the heading `title` to the heading `next_title` (when found)."""
    low = _norm(text)
    flat = re.sub(r"\s+", " ", text).strip()     # same length as low
    start = low.find(_norm(title)) if title else -1
    if start >= 0:
        flat, low = flat[start + len(_norm(title)):], low[start + len(_norm(title)):]
    if next_title:
        end = low.find(_norm(next_title))
        if end > 0:
            flat = flat[:end]
    return flat.lstrip(" :.-\x01\x02").rstrip(" :-\x01\x02")


# ------------------------------------------------------------------ outline -> entries (pure)
def sections(outline: List[Tuple[int, str, int]], n_pages: int) -> List[Tuple[str, int, int, Optional[str]]]:
    """outline: (depth, title, page) in reading order -> (path, first page, last page, next title).
    A section runs to the page where the next bookmark starts."""
    rows, path = [], []
    for i, (depth, title, page) in enumerate(outline):
        path = path[:depth] + [title.strip()]
        nxt = outline[i + 1] if i + 1 < len(outline) else None
        last = (nxt[2] if nxt else n_pages - 1)
        rows.append((" > ".join(path), page, max(page, last), nxt[1].strip() if nxt else None))
    return rows


def split_pieces(text: str, limit: int = PIECE) -> List[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces, cur = [], ""
    for p in paras:
        while len(p) > limit:                   # a long paragraph: cut at a sentence end
            cut = p.rfind(". ", 0, limit)
            cut = cut + 1 if cut > limit // 3 else limit
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + len(p) + 2 > limit:
            pieces.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        pieces.append(cur)
    return pieces


def build_entries(outline: List[Tuple[int, str, int]], pages: List[str]) -> Tuple[List[dict], Dict[str, int]]:
    drop = furniture(pages)
    stats = Counter()
    entries = []
    for path, first, last, next_title in sections(outline, len(pages)):
        leaf = path.split(" > ")[-1]
        if any(_SKIP_TITLE.match(p) for p in path.split(" > ")):
            stats["skipped: contents / index / legal"] += 1
            continue
        raw = "\n".join(pages[first:last + 1])
        body = clean(raw, drop)
        # line breaks ride through the cut as marks that aren't whitespace
        body = cut_section(body.replace("\n\n", " \x01 ").replace("\n", " \x02 "), leaf, next_title)
        body = re.sub(r" ?\x01 ?", "\n\n", body)
        body = re.sub(r" ?\x02 ?", "\n", body).strip()
        if table_like(body):
            stats["skipped: tables / too short"] += 1
            continue
        tags = sorted({w for w in re.findall(r"[a-z0-9-]+", path.lower()) if len(w) > 2})
        pieces = split_pieces(body)
        for i, piece in enumerate(pieces):
            entries.append({"title": path + (f" ({i + 1})" if len(pieces) > 1 else ""), "text": piece,
                            "tags": tags, "section": path, "page": first + 1})
        stats["sections kept"] += 1
    stats["entries"] = len(entries)
    return entries, dict(stats)


# ------------------------------------------------------------------ the PDF
def read_pdf(path: str):
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("Needs pypdf: pip install pypdf")
    reader = PdfReader(path)
    outline: List[Tuple[int, str, int]] = []

    def walk(items, depth):
        last = None
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(it)
            except Exception:
                continue
            if page is None or page < 0:
                continue
            outline.append((depth, str(it.title or "").strip(), page))
            last = it
        return last

    walk(reader.outline or [], 0)
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    outline.sort(key=lambda r: r[2])               # reading order (stable for the same page)
    return outline, pages


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--aircraft", nargs="+", required=True, help="DCS type names, e.g. FA-18C_hornet A-10C_2")
    ap.add_argument("--name", help="output file name (default: from the PDF name)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    outline, pages = read_pdf(a.pdf)
    if not outline:
        sys.exit("This PDF has no bookmarks (outline); can't split it into sections.")
    entries, stats = build_entries(outline, pages)
    name = a.name or re.sub(r"[^a-z0-9]+", "_", os.path.splitext(os.path.basename(a.pdf))[0].lower()).strip("_")
    print(f"{os.path.basename(a.pdf)}: {len(pages)} pages, {len(outline)} bookmarks")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    for e in entries[:3]:
        print(f"  e.g. {e['title']}: {e['text'][:120]}...")
    if a.dry_run:
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, name + ".json")
    with open(out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"title": os.path.basename(a.pdf), "aircraft": a.aircraft, "entries": entries}, fh,
                  indent=1, ensure_ascii=False)
    os.replace(out + ".tmp", out)
    print(f"Wrote {out} ({len(entries)} entries) for {', '.join(a.aircraft)}.")


if __name__ == "__main__":
    main()
