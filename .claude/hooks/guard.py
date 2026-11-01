"""PreToolUse guard for DCS George AI (Claude Code hook).

Blocks (exit 2, reason on stderr):
- any edit or write of config.py or data/unit_types.json;
- shell commands that name config.py or mention a password, so the Olympus passwords can't be
  printed by a script that opens the file itself (the Read / Edit deny rules only cover Claude's
  own file tools and commands like cat);
- running main.py (it presses keys and talks to DCS).
Everything else: exit 0 (the normal permission rules apply).
"""
import json
import os
import re
import sys

PROTECTED_FILES = {
    "config.py": "config.py is Andre's file (it holds the Olympus passwords) and is never edited "
                 "by Claude. For a new setting, use getattr(config, NAME, default) and tell Andre "
                 "the line to add.",
    "data/unit_types.json": "data/unit_types.json is learned from DCS on Andre's PC and is never "
                            "edited by Claude.",
}
SHELL_BLOCKS = [
    (re.compile(r"(?<![\w.])config\.py\b", re.I),
     "config.py holds Andre's passwords: don't open it from a command. "
     "Use `python .claude/tools/show_config.py [NAME ...]` to see settings."),
    (re.compile(r"passw(or)?d|PASSWD|sha256\(", re.I),
     "Commands that mention passwords are blocked (secrets stay in config.py)."),
    (re.compile(r"\b(?:python3?|py)(?:\.exe)?\s+(?:-\S+\s+)*[\"']?(?:[\w.:~-]*[\\/])*main\.py\b", re.I),
     "Don't run main.py: it presses keys with pynput and talks to DCS. Andre runs it."),
]


def _rel(path, root):
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        rel = os.path.relpath(p, r)
    except ValueError:  # another drive on Windows
        return None
    return rel.replace("\\", "/")


def check(data, root):
    """Return a reason to block, or None."""
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        rel = _rel(path, root)
        if rel is not None and rel.lower() in PROTECTED_FILES:
            return PROTECTED_FILES[rel.lower()]
        return None
    if tool in ("Bash", "PowerShell"):
        cmd = ti.get("command") or ""
        for rx, why in SHELL_BLOCKS:
            if rx.search(cmd):
                return why
    return None


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    why = check(data, root)
    if why:
        sys.stderr.write("Blocked by .claude/hooks/guard.py: " + why + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
