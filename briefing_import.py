"""
briefing_import.py - turns a campaign briefing written as plain text (.txt or .md) into
data/campaign_briefing.json, the file George, the Huey co-pilot and Overlord answer from.

    python briefing_import.py my_briefing.txt            replaces the entries
    python briefing_import.py more_notes.md --append     adds to them

Headings start a new entry: markdown headings ("## Rules of engagement"), short lines ending with
a colon ("PACKAGE PRIORITY:"), or short ALL-CAPS lines. Long sections are split into paragraphs.
To have an answer spoken exactly as you want it, edit the JSON afterwards and add "say".
The program picks the file up within a couple of seconds; no restart needed.
"""
import argparse
import json
import os
import re
import sys

from common.briefing import split_paragraphs

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "campaign_briefing.json")
_HEADING = re.compile(r"^\s*(?:#{1,6}\s*(?P<md>.+?)\s*#*\s*$|(?P<colon>[^.!?:]{2,60}):\s*$|"
                      r"(?P<caps>[A-Z0-9][A-Z0-9 /&,()'-]{2,60}))\s*$")


def sections(text: str):
    title, buf = "Briefing", []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m and line.strip():
            if "".join(buf).strip():
                yield title, "\n".join(buf)
            title = (m.group("md") or m.group("colon") or m.group("caps")).strip().rstrip(":")
            title = title.title() if title.isupper() else title
            buf = []
        else:
            buf.append(line)
    if "".join(buf).strip():
        yield title, "\n".join(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    with open(a.file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    entries = []
    for title, body in sections(text):
        parts = split_paragraphs(body)
        for i, p in enumerate(parts):
            entries.append({"title": title + (f" ({i + 1})" if len(parts) > 1 else ""), "text": p,
                            "tags": [w for w in re.findall(r"[a-z]+", title.lower()) if len(w) > 2]})
    data = {"campaign": os.path.splitext(os.path.basename(a.file))[0], "entries": []}
    if a.append and os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            data = json.load(fh)
    data.setdefault("entries", []).extend(entries)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print(f"{len(entries)} entries {'added to' if a.append else 'written to'} {a.out}:")
    for e in entries:
        print(f"  {e['title']}: {e['text'][:70]}{'...' if len(e['text']) > 70 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
