"""Show DCS George AI settings from config.py with secrets redacted.

    python .claude/tools/show_config.py              every setting
    python .claude/tools/show_config.py OLYMPUS JOY  settings whose names contain these words

Values of names with a PASSWORD / PASS / PW / SECRET / TOKEN / HASH / AUTH part, or API_KEY,
are shown as <redacted>,
and so is any other value that contains one of those (e.g. a password copied into a second name
or a dict).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET_PARTS = {"PASSWORD", "PASSWD", "PASS", "PW", "PWD", "SECRET", "TOKEN", "APIKEY", "HASH",
                "AUTH", "CREDENTIAL", "CREDENTIALS"}


def is_secret(name):
    """OLYMPUS_GM_PASSWORD, *_API_KEY, *_TOKEN: yes. PTT_KEY, LLM_MAX_TOKENS: no."""
    up = name.upper()
    return "API_KEY" in up or any(part in SECRET_PARTS for part in up.split("_"))


def settings(module):
    out = {}
    for name in dir(module):
        if name.startswith("_") or not name.isupper():
            continue
        out[name] = getattr(module, name)
    return out


def redact(values):
    secrets = {str(v) for n, v in values.items()
               if is_secret(n) and isinstance(v, str) and len(v) >= 4}
    shown = {}
    for name, v in values.items():
        text = repr(v)
        if is_secret(name):
            shown[name] = "<redacted>" if v not in ("", None) else text
        elif any(s in text for s in secrets):
            shown[name] = "<redacted>"
        else:
            shown[name] = text
    return shown


def main(argv):
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    import config  # noqa: E402  (Andre's file; only names and safe values are printed)
    shown = redact(settings(config))
    words = [w.upper() for w in argv]
    for name in sorted(shown):
        if words and not any(w in name for w in words):
            continue
        print(f"{name} = {shown[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
