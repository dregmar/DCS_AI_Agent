"""
llm_cache_check.py - does Ollama reuse the cached prompt between commands?

    python llm_cache_check.py

Takes about half a minute with Ollama running (DCS can be closed). Nothing is sent to DCS.

The program keeps George's / the Huey crew's instructions identical on every call so Ollama can
reuse them, but in the 18 Oct flights Ollama read the whole prompt every time (~1,000-1,450
tokens, 1.2-1.7 s). This sends George's real prompt a few times in different ways and prints how
many tokens Ollama had to read each time. If the 2nd and 3rd calls read only a few dozen tokens,
the cache works for that way of asking. Judge by the TIME: Ollama 0.40 counts the cached
tokens too (18 Oct: 1,398 tokens in 6.96 s, then 1,400 in 0.21 s = cache working).
"""
import json
import urllib.request

import config
from intelligence import ollama_chat
from intelligence.llm_client import GEORGE_SYSTEM

STATE = ("STATE:\nTelemetry link: LIVE\nAltitude 1200 ft, airspeed 90 kts, pitch 2.0, roll 0.0\n"
         "Master Arm SAFE, WAS NONE, sight TADS\nGeorge's target list: [None detected]\n"
         "Priority threat: None; TADS masked: False; target out of range: False\n"
         "George's wheel settings (tracked from our own key presses, may drift): weapon NO_WPN, roe HOLD\n")


def ask(payload: dict) -> dict:
    req = urllib.request.Request(ollama_chat.OLLAMA_CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def variant(name: str, tweak) -> list:
    reads = []
    for said in ("status", "arm us up", "zoom in"):
        # own first line per variant, so each one starts cold (not from the previous one's cache)
        msgs = [{"role": "system", "content": f"[cache test: {name}]\n" + GEORGE_SYSTEM},
                {"role": "user", "content": STATE + "PILOT: " + said}]
        p = ollama_chat.build_payload(msgs, config.LLM_MODEL, 60, 0.1)
        tweak(p)
        r = ask(p)
        reads.append((r.get("prompt_eval_count") or 0, (r.get("prompt_eval_duration") or 0) / 1e9))
    line = ", ".join(f"{n} tokens {s:.2f}s" for n, s in reads)
    # judged by time: Ollama 0.40 counts cached tokens too, so the count stays the same
    works = reads[1][1] < reads[0][1] / 3 and reads[2][1] < reads[0][1] / 3
    print(f"  {name:34s} {line}   -> cache {'WORKS' if works else 'not used'}")
    return reads


def main():
    try:
        with urllib.request.urlopen(ollama_chat.OLLAMA_BASE + "/api/version", timeout=5) as r:
            print("Ollama", json.loads(r.read().decode()).get("version"), "| model", config.LLM_MODEL)
    except Exception as e:
        print(f"Can't reach Ollama at {ollama_chat.OLLAMA_BASE}: {e}")
        return
    print("Prompt reading for 3 commands in a row (1st reads everything; then it should be fast):")
    variant("as the program asks", lambda p: None)
    variant("without JSON format", lambda p: p.pop("format", None))
    variant("without think:false", lambda p: p.pop("think", None))
    variant("without /no_think in the prompt", lambda p: p["messages"][0].update(
        content=p["messages"][0]["content"].replace("\n/no_think", "")))
    print("\nSend this output to Claude.")


if __name__ == "__main__":
    main()
