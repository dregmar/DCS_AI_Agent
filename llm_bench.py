"""
llm_bench.py - compare LLM models on the phrases that actually reach the LLM.

    python llm_bench.py                         qwen2.5:7b against qwen3:8b
    python llm_bench.py qwen3:8b qwen3:4b       any models you've pulled
    python llm_bench.py --runs 3                repeat each phrase (steadier timings)

Close DCS first (or not, to see how the models do with DCS using the GPU). Needs Ollama running
and the models pulled ("ollama pull qwen3:8b"). Nothing is sent to DCS.

Each phrase goes through exactly what the program does with it: the real prompt (George's for the
Apache phrases, the Huey crew's for the Huey ones), the same timeout, then the same checks
(sanitize, and for the Huey the "related words" gate). A phrase passes when the resulting action
is the right one. Most phrases are from your flight logs; some are new free-form wordings the
fast path doesn't catch. Models are run one at a time and unloaded after, so only one is in VRAM.

Phrases the instant matcher (fast path) handles in flight are marked: there a model's answer
doesn't matter. The report is printed and saved to logs/llm_bench_<date>_<time>.txt.
"""
import argparse
import os
import statistics
import sys
import time

import config
from intelligence import llm_client
from intelligence.llm_client import query_qwen, sanitize_result, chat_json, _load_model, prime_prompt, GEORGE_SYSTEM
from huey import commands as huey_commands
from intelligence.fast_path import match_fast

# (profile, phrase, [acceptable results]); a result is a dict of fields that must match
CASES = [
    # --- Huey, from the flight logs
    ("huey", "Set reader frequency 241", [{"action": "RADIO", "mhz": 241.0}]),
    ("huey", "set radio to frequency 232.9", [{"action": "RADIO", "mhz": 232.9}]),
    ("huey", "set radio 124.750", [{"action": "RADIO", "mhz": 124.75}]),
    ("huey", "connect loader", [{"action": "HOOK_LOAD"}]),
    ("huey", "how do I attach cargo?", [{"action": "NONE"}]),
    ("huey", "have it gone cool", [{"action": "NONE"}]),
    ("huey", "a press board", [{"action": "NONE"}]),
    ("huey", "we're point two", [{"action": "NONE"}]),
    ("huey", "mission waypoint", [{"action": "NONE"}]),
    # --- Huey, free-form
    ("huey", "tell the door gunners to open up on anything they see", [{"action": "CREW_ROE", "value": "FREE"}]),
    ("huey", "gunners, only shoot back if they shoot at us", [{"action": "CREW_ROE", "value": "RETURN"}]),
    ("huey", "everybody stop shooting", [{"action": "CREW_ROE", "value": "HOLD"}]),
    ("huey", "how are the engines looking, chief", [{"action": "ENGINE"}, {"action": "STATUS"}]),
    ("huey", "put the landing light on for me", [{"action": "SWITCH", "key": "ldg_light", "value": "ON"}]),
    ("huey", "give me some light, the search light", [{"action": "SWITCH", "key": "search_light", "value": "ON"}]),
    ("huey", "can you hold this attitude for me", [{"action": "AUTOPILOT", "value": "ATTITUDE"}]),
    ("huey", "what's the fuel looking like", [{"action": "FUEL"}]),
    ("huey", "any bad guys around us", [{"action": "CONTACTS"}]),
    ("huey", "where did the cargo go", [{"action": "LOAD_REPORT"}]),
    ("huey", "let's get the before takeoff checks done", [{"action": "CHECKLIST_START", "value": "before_takeoff"}]),
    ("huey", "dispense some countermeasures", [{"action": "FLARE"}]),
    # --- Apache (George), from the logs and free-form
    ("apache", "target radio.", [{"action": "NONE"}]),
    ("apache", "a press board", [{"action": "NONE"}]),
    ("apache", "George, give me the cannon", [{"action": "GEORGE_SET", "setting": "weapon", "value": "GUN"}]),
    ("apache", "set the gun to fifty round bursts", [{"action": "GEORGE_SET", "setting": "gun_burst", "value": "50"}]),
    ("apache", "switch over to the radar guided missiles", [{"action": "GEORGE_SET", "setting": "missile_type", "value": "RF"}]),
    ("apache", "use high explosive rockets", [{"action": "GEORGE_SET", "setting": "rocket_type", "value": "HE"}]),
    ("apache", "have a look around out in front of us", [{"action": "AREA_SEARCH", "value": "FWD AREA"},
                                                         {"action": "PHS_SEARCH"}, {"action": "TADS_SEARCH"}]),
    ("apache", "what's our situation", [{"action": "STATUS"}]),
    ("apache", "you're free to engage, weapons free", [{"action": "GEORGE_SET", "setting": "roe", "value": "FREE"}]),
    ("apache", "turn on the laser", [{"action": "TOGGLE_LASER"}]),
    ("apache", "arm us up", [{"action": "SET_ARMED"}]),
    ("apache", "zoom in on that", [{"action": "ZOOM_IN"}]),
    ("apache", "stop everything, cease", [{"action": "CEASE"}]),
    ("apache", "save that target as a point", [{"action": "STORE_TARGET"}]),
]

HUEY_STATE = "0 ft AGL, 0 kts; rotor 324, torque 20, fuel 1200 lbs; master arm SAFE"
GEORGE_STATE = "weapon NO_WPN, roe HOLD FIRE, TADS FIXED"


def run_case(model: str, profile: str, text: str) -> dict:
    """What the program would end up doing with this phrase: a flat dict, plus timing."""
    t0 = time.perf_counter()
    try:
        if profile == "huey":
            raw = chat_json(huey_commands.llm_system(), [],
                            huey_commands.llm_message(HUEY_STATE, None, text), model=model)
            out = huey_commands.sanitize(raw, text)
        else:
            llm_client.CONVERSATION_HISTORY.clear()
            raw, _ = query_qwen(text, GEORGE_STATE, model=model)
            action, idx, speech, setting, value, targets = sanitize_result(raw, 0, text)
            out = {"action": action, "setting": setting, "value": value, "speech": speech}
        out["raw_action"] = str(raw.get("action"))
        out["error"] = ""
    except Exception as e:
        out = {"action": "ERROR", "error": f"{type(e).__name__}: {e}"[:80], "raw_action": ""}
    out["sec"] = time.perf_counter() - t0
    return out


def fast_path(profile: str, text: str) -> str:
    """What the instant phrase matcher does with it in flight ('' = it goes to the LLM)."""
    r = huey_commands.match(text) if profile == "huey" else match_fast(text, {"targets": []})
    return (r or {}).get("action") or ""


def matches(result: dict, expected: list) -> bool:
    for alt in expected:
        ok = True
        for k, v in alt.items():
            got = result.get(k)
            if k == "mhz":
                ok = got is not None and abs(float(got) - v) < 0.001
            else:
                ok = str(got).upper() == str(v).upper()
            if not ok:
                break
        if ok:
            return True
    return False


def bench(model: str, runs: int, log) -> dict:
    log(f"\n=== {model} ===")
    try:
        log(f"loading ... {_load_model(model):.1f} s")
        run_case(model, "huey", "status")                                    # first call is slow
    except Exception as e:
        log(f"cannot use {model}: {type(e).__name__}: {e}  (ollama pull {model}?)")
        return {}
    passed, times, errors = 0, [], 0
    rows = []
    cached = None
    for profile, text, expected in CASES:
        if profile != cached:                     # like the program does on an aircraft change
            prompt = huey_commands.llm_system() if profile == "huey" else GEORGE_SYSTEM
            log(f"reading the {profile} prompt into the cache (cold): {prime_prompt(prompt, profile, model):.1f} s")
            cached = profile
        for _ in range(runs):
            r = run_case(model, profile, text)
            ok = matches(r, expected)
            passed += ok
            errors += bool(r["error"])
            if not r["error"]:
                times.append(r["sec"])
            got = r["action"] + "".join(f" {k}={r[k]}" for k in ("key", "setting", "value", "mhz")
                                        if r.get(k) not in (None, ""))
            note = r["error"] or (f"(model said {r['raw_action']})" if r["raw_action"].upper() != r["action"] else "")
            rows.append((ok, profile, text, got, r["sec"], note))
            fp = fast_path(profile, text)
            if fp:
                note += f" [in flight the fast path does {fp}]"
            log(f"{'PASS' if ok else 'FAIL'}  {r['sec']:4.1f}s  {profile:6s}  {text[:46]:46s} -> {got} {note}")
    try:
        _load_model(model, keep_alive=0)                                         # free the VRAM
    except Exception:
        pass
    total = len(CASES) * runs
    summary = {"model": model, "passed": passed, "total": total, "errors": errors,
               "median": statistics.median(times) if times else float("nan"),
               "p90": sorted(times)[int(0.9 * (len(times) - 1))] if times else float("nan"),
               "max": max(times) if times else float("nan"), "rows": rows}
    log(f"--- {model}: {passed}/{total} right, {errors} errors/timeouts, "
        f"median {summary['median']:.2f} s, 90% under {summary['p90']:.2f} s, slowest {summary['max']:.2f} s")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Compare LLM models on George / Huey phrases")
    ap.add_argument("models", nargs="*", default=["qwen2.5:7b", "qwen3:8b"])
    ap.add_argument("--runs", type=int, default=1)
    a = ap.parse_args()
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", time.strftime("llm_bench_%Y%m%d_%H%M.txt"))
    out = open(path, "w", encoding="utf-8")

    def log(line=""):
        print(line)
        out.write(line + "\n")
        out.flush()

    log(f"LLM bench {time.strftime('%Y-%m-%d %H:%M')}: {len(CASES)} phrases x {a.runs} run(s), "
        f"timeout {config.LLM_TIMEOUT_SEC} s, max tokens {config.LLM_MAX_TOKENS}")
    results = [s for s in (bench(m, a.runs, log) for m in a.models) if s]
    if len(results) > 1:
        log("\n=== Summary ===")
        for s in results:
            log(f"{s['model']:14s} {s['passed']:3d}/{s['total']} right  {s['errors']} errors  "
                f"median {s['median']:.2f} s  p90 {s['p90']:.2f} s")
        log("\nPhrases where the models disagree:")
        for i in range(len(results[0]["rows"])):
            oks = [s["rows"][i][0] for s in results]
            if len(set(oks)) > 1:
                r0 = results[0]["rows"][i]
                log(f"  {r0[2]!r}: " + ", ".join(f"{s['model']} {'PASS' if s['rows'][i][0] else 'FAIL'} "
                                              f"({s['rows'][i][3]})" for s in results))
    out.close()
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    sys.exit(main())
