"""
apache/eufd.py
The pilot's Up-Front Display (EUFD) from DCS-BIOS: warnings, cautions and advisories.

The top of the EUFD is three 18-character columns: WARNINGS | CAUTIONS | ADVISORIES. Below them
are the radios and the fuel / time lines. The rows George treats as the message area, and the
column positions, are in config (APACHE_EUFD_*) and are checked with apache_probe.py.
"""
import re
from typing import Dict, List, Optional

import config

COLUMNS = getattr(config, "APACHE_EUFD_COLUMNS", [(0, 18), (19, 37), (38, 56)])
WCA_ROWS = getattr(config, "APACHE_EUFD_WCA_ROWS", 6)        # top N lines are the message area
KINDS = ("warning", "caution", "advisory")
_RADIOISH = re.compile(r"\d{2,3}\.\d|[~*]|\bVHF\b|\bUHF\b|\bFM[12]\b|\bHF\b")


def messages(lines: List[Optional[str]]) -> Optional[Dict[str, List[str]]]:
    """{'warning': [...], 'caution': [...], 'advisory': [...]} or None when no EUFD data."""
    rows = [l for l in lines[:WCA_ROWS] if l is not None]
    if not rows:
        return None
    out = {k: [] for k in KINDS}
    for line in rows:
        for kind, (a, b) in zip(KINDS, COLUMNS):
            text = re.sub(r"\s+", " ", line[a:b]).strip(" |:-")
            if len(text) >= 3 and not _RADIOISH.search(text) and text not in out[kind]:
                out[kind].append(text)
    return out


def speakable(text: str) -> str:
    """'ENG1 OIL PSI LO' -> 'engine 1 oil pressure low'."""
    t = " " + text.upper() + " "
    for a, b in ((r"\bENG ?(\d)\b", r"engine \1"), (r"\bENG\b", "engine"), (r"\bPSI\b", "pressure"),
                 (r"\bLO\b", "low"), (r"\bHI\b", "high"), (r"\bTEMP\b", "temperature"),
                 (r"\bXMSN\b", "transmission"), (r"\bHYD\b", "hydraulic"), (r"\bGEN\b", "generator"),
                 (r"\bPRI\b", "primary"), (r"\bUTIL\b", "utility"), (r"\bOVSP\b", "overspeed"),
                 (r"\bRTR\b", "rotor"), (r"\bNR\b", "rotor RPM"), (r"\bFLT\b", "flight"),
                 (r"\bOUT\b", "out"), (r"\bFAIL\b", "fail")):
        t = re.sub(a, b, t)
    return re.sub(r"\s+", " ", t).strip().lower()


class EufdMonitor:
    """New warnings / cautions read out once each (until they clear). Advisories stay quiet."""

    def __init__(self, speak, settle_sec: float = 5.0, burst_limit: int = 6):
        self.speak = speak
        self.settle_sec, self.burst_limit = settle_sec, burst_limit
        self._seen = None
        self._since = None

    def check(self, lines: List[Optional[str]], now: float) -> None:
        msgs = messages(lines)
        if msgs is None:
            self._seen, self._since = None, None
            return
        current = {(k, m) for k in ("warning", "caution") for m in msgs[k]}
        if self._seen is None:
            self._seen, self._since = current, now
            return
        new = current - self._seen
        self._seen = current
        if not new or now - self._since < self.settle_sec or len(new) >= self.burst_limit:
            return
        for kind, text in sorted(new, key=lambda kt: (KINDS.index(kt[0]), kt[1])):
            self.speak(f"{'Warning' if kind == 'warning' else 'Caution'}, {speakable(text)}.")
