"""
core/console_log.py
Mirrors everything printed to the console into  logs/george_YYYYMMDD_HHMMSS.log  in the
project folder, with a timestamp on each line. Writing happens on a background thread, so a
slow disk can never stall the PTT, telemetry or George threads. Keeps the newest 20 logs.

Call start_console_log() once, first thing in main.
"""
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
KEEP_LOGS = 20

_q: "queue.Queue[str]" = queue.Queue()
_started = False


class _Tee:
    """Wraps a console stream: writes go to the console unchanged and are copied to the log."""

    def __init__(self, stream):
        self._stream = stream
        self._partial = {}          # per thread, so two threads printing at once don't merge
        self._lock = threading.Lock()

    def write(self, text):
        try:
            self._stream.write(text)
        except Exception:
            pass
        tid = threading.get_ident()
        with self._lock:
            buf = self._partial.get(tid, "") + text
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                _q.put(line)
            if buf:
                self._partial[tid] = buf
            else:
                self._partial.pop(tid, None)
        return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _writer(path: str) -> None:
    with open(path, "a", encoding="utf-8", buffering=1) as f:
        while True:
            line = _q.get()
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            f.write(f"{stamp}  {line}\n")


def _prune() -> None:
    try:
        logs = sorted(n for n in os.listdir(LOG_DIR) if n.startswith("george_") and n.endswith(".log"))
        for old in logs[:-KEEP_LOGS]:
            os.remove(os.path.join(LOG_DIR, old))
    except OSError:
        pass


def start_console_log() -> str:
    """Start mirroring stdout/stderr to a new log file. Returns its path."""
    global _started
    if _started:
        return ""
    _started = True
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, time.strftime("george_%Y%m%d_%H%M%S.log"))
    threading.Thread(target=_writer, args=(path,), daemon=True, name="console-log").start()
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    # Route logging (e.g. log.exception in the George driver) to the console, and so to the log.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="[%(name)s] %(levelname)s %(message)s")
    _prune()
    print(f"[LOG] Console output is also saved to {path}")
    return path
