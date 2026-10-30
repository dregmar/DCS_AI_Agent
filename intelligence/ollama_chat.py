"""
intelligence/ollama_chat.py
One JSON answer from a local Ollama model, through Ollama's own API (/api/chat).

Why not the OpenAI-compatible endpoint any more: Qwen3 is a "thinking" model. By default it writes
a <think> ... </think> block before every answer, which costs seconds, eats the reply's token
budget (the JSON gets cut off) and breaks json.loads. Ollama's own API can switch that off
("think": false). Belt and braces for older Ollama versions: Qwen3's "/no_think" switch goes into
the system prompt, and any <think> block that still comes back is stripped before parsing.

Works the same for non-thinking models (qwen2.5:7b), so switching models is just LLM_MODEL in
config.py.
"""
import threading
import time
import json
import re
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# 127.0.0.1, not "localhost": on Windows "localhost" can try IPv6 (::1) first, where Ollama isn't.
OLLAMA_BASE = "http://127.0.0.1:11434"
OLLAMA_CHAT_URL = OLLAMA_BASE + "/api/chat"
THINKING_FAMILIES = ("qwen3", "deepseek-r1", "qwq", "magistral", "gpt-oss")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def is_thinking_model(model: str) -> bool:
    return model.lower().startswith(THINKING_FAMILIES)


def extract_json(text: str) -> Dict[str, Any]:
    """The JSON object in a model reply, without any <think> block or text around it.
    Raises ValueError if there's no JSON object."""
    t = _THINK_BLOCK.sub("", text or "")
    t = re.sub(r"^.*?</think>", "", t, flags=re.S | re.I)      # a block whose start was cut off
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in model reply: {text[:120]!r}")
    result = json.loads(t[start:end + 1])
    return result if isinstance(result, dict) else {}


def build_payload(messages: List[dict], model: str, max_tokens: int, temperature: float,
                  send_think: bool = True) -> dict:
    msgs = [dict(m) for m in messages]
    thinking = is_thinking_model(model)
    if thinking and msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = msgs[0]["content"].rstrip() + "\n/no_think"
    payload = {
        "model": model,
        "messages": msgs,
        "stream": False,
        "format": "json",
        "keep_alive": -1,               # stay loaded (see llm_client.warmup_llm)
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if thinking and send_think:
        payload["think"] = False
    return payload


def _post(payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(OLLAMA_CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except (socket.timeout, TimeoutError) as e:
        raise TimeoutError(f"LLM request timed out after {timeout:.0f}s") from e
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise TimeoutError(f"LLM request timed out after {timeout:.0f}s") from e
        raise


def prime(system_prompt: str, model: str, timeout: float = 60.0, post=None) -> None:
    """Has Ollama read a system prompt once and keep it in its cache, so the first real command
    doesn't pay for it (about 7 s for George's prompt on the RX 5700 XT, against 0.7 s cached).
    Same payload shape as chat(), so the cached tokens match; the one-token answer is ignored."""
    post = post or _post
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": "STATE: none\nPILOT: radio check"}]
    payload = build_payload(messages, model, 1, 0.1)
    try:
        post(payload, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 400 and "think" in payload:
            post(build_payload(messages, model, 1, 0.1, send_think=False), timeout)
        else:
            raise


_last_lock = threading.Lock()
_LAST: Dict[str, Any] = {}           # the last chat: ok, when (time.time), seconds (kneeboard status page)


def last_call() -> Dict[str, Any]:
    with _last_lock:
        return dict(_LAST)


def _note(ok: bool, secs: float, error: str = "") -> None:
    with _last_lock:
        _LAST.update({"ok": ok, "when": time.time(), "secs": secs, "error": error})


def chat(messages: List[dict], model: str, timeout: float = 8.0, max_tokens: int = 160,
         temperature: float = 0.1, post=None) -> Dict[str, Any]:
    """Sends the chat, returns the parsed JSON object. post(payload, timeout) is for tests."""
    post = post or _post
    payload = build_payload(messages, model, max_tokens, temperature)
    t0 = time.perf_counter()
    try:
        try:
            resp = post(payload, timeout)
        except urllib.error.HTTPError as e:
            # An Ollama too old for the "think" option answers 400: retry without it.
            if e.code == 400 and "think" in payload:
                print("[LLM] This Ollama doesn't accept 'think'; update Ollama for the fastest Qwen3.")
                resp = post(build_payload(messages, model, max_tokens, temperature, send_think=False), timeout)
            else:
                raise
    except Exception as e:
        _note(False, time.perf_counter() - t0, f"{type(e).__name__}")
        raise
    _note(True, time.perf_counter() - t0)
    print(timing_line(resp))
    content = (resp.get("message") or {}).get("content", "")
    return extract_json(content)


def timing_line(resp: dict) -> str:
    """Where the time went, from Ollama's own counters. Ollama 0.40 counts the whole prompt,
    cached part included, so the TIME is what shows how much it really had to read (about 5 ms per
    uncached token on the RX 5700 XT, almost nothing for the cached system prompt)."""
    ns = 1e-9
    read, read_s = resp.get("prompt_eval_count") or 0, (resp.get("prompt_eval_duration") or 0) * ns
    wrote, wrote_s = resp.get("eval_count") or 0, (resp.get("eval_duration") or 0) * ns
    load_s = (resp.get("load_duration") or 0) * ns
    line = f"[LLM] prompt {read} tokens read in {read_s:.2f}s, answer {wrote} tokens in {wrote_s:.2f}s"
    if load_s > 0.5:
        line += f", model load {load_s:.1f}s"
    return line
