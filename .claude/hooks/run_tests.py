"""Stop hook for DCS George AI (Claude Code hook).

When Claude is about to finish and .py / data files changed since the last passing run
(after_edit.py marks them), run `python -m unittest discover tests -b` at below-normal priority.
- Pass: the mark is cleared, and the test count is shown.
- Fail: Claude is told to keep going, with the end of the output. After 3 failed runs in a row it
  is allowed to stop, so a test it can't fix doesn't loop forever; the mark stays.
- DCS.exe running: skipped (DCS performance first); the mark stays for the next stop.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
DUE = os.path.join(STATE, "tests_due")
FAILS = os.path.join(STATE, "fail_count")
MAX_FAILS = 3
TIMEOUT_S = 540
TAIL_CHARS = 6000


def say(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def dcs_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DCS.exe", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "dcs.exe" in out.lower()


def read_int(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_int(path, n):
    os.makedirs(STATE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(n))


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    try:
        json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        pass
    if not os.path.exists(DUE):
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    if dcs_running():
        say({"systemMessage": "DCS is running: tests skipped. They run at the next stop after DCS closes."})
        return 0

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    flags = 0x00004000 if os.name == "nt" else 0  # BELOW_NORMAL_PRIORITY_CLASS
    try:
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "tests", "-b"],
                           cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=TIMEOUT_S, creationflags=flags)
        out, ok = (p.stdout or "") + (p.stderr or ""), p.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = f"Test run took longer than {TIMEOUT_S} s and was stopped.", False

    m = re.search(r"Ran (\d+) tests? in ([\d.]+)s", out)
    ran = f"{m.group(1)} tests in {float(m.group(2)):.0f} s" if m else "tests"

    if ok:
        remove(DUE)
        remove(FAILS)
        say({"systemMessage": f"Tests passed: {ran} (python -m unittest discover tests -b)."})
        return 0

    fails = read_int(FAILS) + 1
    write_int(FAILS, fails)
    tail = out[-TAIL_CHARS:]
    if fails >= MAX_FAILS:
        remove(FAILS)
        say({"systemMessage": f"Tests still failing after {MAX_FAILS} tries ({ran}). "
                              "Claude stopped; tell it what to do next."})
        return 0
    say({"decision": "block",
         "reason": (f"`python -m unittest discover tests -b` failed ({ran}, try {fails} of "
                    f"{MAX_FAILS}). Fix the failures, or if a failure isn't caused by this change "
                    "(e.g. the known flaky test_driver burst test), say so to Andre.\n\n" + tail)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
