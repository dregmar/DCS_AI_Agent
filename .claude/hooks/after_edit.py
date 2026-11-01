"""PostToolUse hook for DCS George AI (Claude Code hook), after Edit / Write.

- A .py file: compiled in memory (no .pyc written). A syntax error goes back to Claude (exit 2).
  Any .py or data/*.json change marks the test suite as due; the Stop hook (run_tests.py) runs it.
- A Lua file in dcs_scripts/: reminds Claude to copy it to Saved Games, because snapshot.bat copies
  Saved Games -> dcs_scripts and would overwrite the edit.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
DUE = os.path.join(STATE, "tests_due")

LUA_HOME = {
    "export.lua": r"%USERPROFILE%\Saved Games\DCS\Scripts",
    "george_comms_hook.lua": r"%USERPROFILE%\Saved Games\DCS\Scripts\Hooks",
}


def mark_due(rel):
    os.makedirs(STATE, exist_ok=True)
    with open(DUE, "a", encoding="utf-8") as f:
        f.write(rel + "\n")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return 0
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if not path:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root)).replace("\\", "/")
    except ValueError:
        return 0
    if rel.startswith(".."):
        return 0
    low = rel.lower()

    if low.endswith(".py"):
        mark_due(rel)
        try:
            with open(path, "rb") as f:
                compile(f.read(), path, "exec")
        except SyntaxError as e:
            sys.stderr.write(f"Syntax error in {rel} line {e.lineno}: {e.msg}\n")
            return 2
        except OSError:
            pass
        return 0

    if low.startswith("data/") and low.endswith(".json"):
        mark_due(rel)
        try:
            with open(path, "rb") as f:
                json.loads(f.read().decode("utf-8-sig"))
        except ValueError as e:
            sys.stderr.write(f"{rel} is not valid JSON: {e}\n")
            return 2
        except OSError:
            pass
        return 0

    if low.startswith("dcs_scripts/") and low.endswith(".lua"):
        name = os.path.basename(low)
        home = LUA_HOME.get(name, r"%USERPROFILE%\Saved Games\DCS\Scripts")
        msg = (f"You edited {rel}. Copy it to {home} now and tell Andre: snapshot.bat copies "
               "Saved Games -> dcs_scripts and would overwrite this edit. DCS loads it at the next "
               "mission start.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
