"""
instructor/history.py - the pilot's results on disk (proposal section 7).

    data/instructor/<pilot>/evaluations.jsonl         one Evaluation per line, appended, never rewritten
    data/instructor/<pilot>/sessions/<date>_<ex>.json.gz   raw samples of that attempt (for re-scoring and
                                                       the after-action replay), kept RAW_DAYS days
    data/instructor/<pilot>/targets.jsonl             training-target kills (1 Nov, one training mode):
                                                       what, seconds after the spawn, aircraft, mission,
                                                       time. Not scored yet (a later engagement exercise).

The append log is the truth. A line that doesn't parse is skipped and counted. Writes happen on the
evaluator's thread, outside the in-memory lock; only the list of recent results is locked.
"""
import gzip
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

KEEP_RECENT = 200


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "default").strip("._") or "default"


class History:
    def __init__(self, base_dir: Optional[str], pilot: str = "default", keep_raw: bool = True,
                 raw_days: float = 30.0, clock=time.time, printer=print):
        """base_dir None = in memory only (tests)."""
        self.pilot = _safe(pilot)
        self.dir = os.path.join(base_dir, self.pilot) if base_dir else None
        self.keep_raw, self.raw_days, self.clock, self.printer = keep_raw, float(raw_days), clock, printer
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._recent: List[Dict[str, Any]] = []
        self._kills: List[Dict[str, Any]] = []
        self.bad_lines = 0
        self._load()
        self._load_kills()
        self.prune_raw()

    @property
    def log_path(self) -> Optional[str]:
        return os.path.join(self.dir, "evaluations.jsonl") if self.dir else None

    def _load(self) -> None:
        if not self.log_path or not os.path.exists(self.log_path):
            return
        rows = []
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    self.bad_lines += 1
        with self._lock:
            self._recent = rows[-KEEP_RECENT:]
        if self.bad_lines:
            self.printer(f"[INSTRUCTOR] {self.bad_lines} unreadable line(s) in {self.log_path} skipped.")

    def append(self, evaluation: Dict[str, Any], samples: Optional[List[Dict[str, Any]]] = None) -> None:
        with self._lock:
            self._recent.append(evaluation)
            del self._recent[:-KEEP_RECENT]
        if not self.dir:
            return
        with self._write_lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(evaluation, separators=(",", ":"), sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                if self.keep_raw and samples is not None:
                    raw_dir = os.path.join(self.dir, "sessions")
                    os.makedirs(raw_dir, exist_ok=True)
                    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.clock()))
                    path = os.path.join(raw_dir, f"{stamp}_{_safe(evaluation.get('exercise_id', 'x'))}.json.gz")
                    with gzip.open(path, "wt", encoding="utf-8") as f:
                        json.dump({"evaluation": evaluation, "samples": samples}, f, separators=(",", ":"))
            except OSError as e:
                self.printer(f"[INSTRUCTOR] Couldn't save the result: {type(e).__name__}: {e}")

    @property
    def kills_path(self) -> Optional[str]:
        return os.path.join(self.dir, "targets.jsonl") if self.dir else None

    def _load_kills(self) -> None:
        if not self.kills_path or not os.path.exists(self.kills_path):
            return
        rows = []
        with open(self.kills_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        with self._lock:
            self._kills = rows[-KEEP_RECENT:]

    def add_kill(self, row: Dict[str, Any]) -> None:
        """One training target destroyed (Spawner's watch thread; the file write is outside _lock)."""
        with self._lock:
            self._kills.append(dict(row))
            del self._kills[:-KEEP_RECENT]
        if not self.dir:
            return
        with self._write_lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self.kills_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            except OSError as e:
                self.printer(f"[INSTRUCTOR] Couldn't save the kill: {type(e).__name__}: {e}")

    def kills(self, n: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._kills[-n:]]

    def recent(self, exercise_id: Optional[str] = None, n: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._recent if exercise_id is None or r.get("exercise_id") == exercise_id]
        return [dict(r) for r in rows[-n:]]

    def prune_raw(self) -> int:
        """Raw session files older than raw_days are deleted (decision 8). Returns how many."""
        if not self.dir:
            return 0
        raw_dir = os.path.join(self.dir, "sessions")
        if not os.path.isdir(raw_dir):
            return 0
        cutoff = self.clock() - self.raw_days * 86400.0
        n = 0
        for name in os.listdir(raw_dir):
            if not name.endswith(".json.gz"):
                continue
            p = os.path.join(raw_dir, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
        if n:
            self.printer(f"[INSTRUCTOR] Removed {n} raw session file(s) older than {self.raw_days:g} days.")
        return n
