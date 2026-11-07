"""
kneeboard/server.py - the kneeboard's local web server (OpenKneeboard web dashboard tab).

Python's own ThreadingHTTPServer on a daemon thread, bound to 127.0.0.1 only:
    GET /          the page (kneeboard/static/index.html)
    GET /state     one JSON snapshot of every feed plus "comms" (debugging)
    GET /events    Server-Sent Events: one event per feed ("map", "status", "briefing", "checklist",
                   "ui" ...) when its data changes (each checked at its own interval, resent every
                   5 s anyway), and "comms" with new log lines only; "page" carries the version of
                   index.html, and a page older than that reloads itself (31 Oct: OpenKneeboard kept
                   showing the old tabs after an update until its tab was reloaded by hand).
Settings page (S2, 6 Nov 2026; core/settings.py):
    GET  /settings        every editable setting with its value and where it came from, and which changes
                          made this run wait for a restart
    POST /settings        {"name", "value"} -> {"ok", "why", "setting"}
    POST /settings/reset  {"name"}          -> the same
    POST /settings/sample {"path"}          -> plays a line in that voice (S3; one of settings.voices())
Changes need the key made at each program start (header X-KB-Key). The page gets it inside the HTML this
server sends (index.html's __SETTINGS_KEY__); a browser doesn't let other web sites read that page, so they
can't change settings. Requests whose Host isn't 127.0.0.1 / localhost are refused (DNS rebinding).
Each connected page costs one thread that sleeps between checks; building the data is a few
milliseconds, so DCS doesn't notice. Nothing here touches the game or the voice threads.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional, Tuple

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TICK_S = 0.2
RESEND_S = 5.0
VERSION_MARK = b"__PAGE_VERSION__"        # index.html carries this; the served page carries the version
KEY_MARK = b"__SETTINGS_KEY__"            # ... and this: the served page carries the settings key
MAX_BODY = 4096
_LOCAL_HOSTS = ("127.0.0.1", "localhost")



class _QuietServer(ThreadingHTTPServer):
    """OpenKneeboard's WebView drops connections (tab switch, reload): ConnectionAbortedError while the
    request line is read, before our handler runs. That isn't an error; anything else still prints."""
    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

class Kneeboard:
    def __init__(self, map_fn: Callable[[], dict], status_fn: Callable[[], dict], log,
                 host: str = "127.0.0.1", port: int = 5000,
                 feeds: Optional[Dict[str, Tuple[Callable[[], dict], float]]] = None, settings=None,
                 sample: Optional[Callable[[str], None]] = None):
        """feeds: more pages' data, {event name: (function, seconds between checks)}.
        settings: core.settings.SettingsStore for the Settings page, or None (no /settings)."""
        self.map_fn, self.status_fn, self.log = map_fn, status_fn, log
        self.settings = settings
        self.sample = sample                              # sample(path): play a voice (S3; main.py)
        self.key = secrets.token_urlsafe(18)              # a new one every program start
        self._restart_lock = threading.Lock()
        self._restart: set = set()                        # restart-only settings changed this run
        self.feeds: Dict[str, Tuple[Callable[[], dict], float]] = {"map": (map_fn, TICK_S), "status": (status_fn, 0.5),
                                                                  "page": (self.page_version, 2.0)}
        self.feeds.update(feeds or {})
        self._ver_lock = threading.Lock()
        self._ver_key = None
        self._ver = ""
        self.host, self.port = host, port
        self.httpd: Optional[ThreadingHTTPServer] = None

    # ------------------------------------------------------------ server
    def start(self) -> "Kneeboard":
        kb = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):          # keep the console for flying
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    if path == "/settings":
                        kb._settings_get(self)
                    elif path in ("/", "/index.html", "/kneeboard"):
                        kb._file(self, "index.html", "text/html; charset=utf-8")
                    elif path == "/state":
                        got = {name: fn() for name, (fn, _) in kb.feeds.items()}
                        got["comms"] = kb.log.since(0)
                        body = json.dumps(got).encode()
                        kb._send(self, 200, "application/json", body)
                    elif path == "/events":
                        kb._events(self)
                    else:
                        kb._send(self, 404, "text/plain", b"not found")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                try:
                    if path in ("/settings", "/settings/reset", "/settings/sample"):
                        kb._settings_post(self, reset=path.endswith("/reset"), sample=path.endswith("/sample"))
                    else:
                        kb._send(self, 404, "text/plain", b"not found")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.httpd = _QuietServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="kneeboard-http").start()
        print(f"[KNEEBOARD] http://{self.host}:{self.port}/  (add it as a web dashboard tab in OpenKneeboard)")
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()

    @staticmethod
    def _send(h, code: int, ctype: str, body: bytes) -> None:
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(body)

    def _file(self, h, name: str, ctype: str) -> None:
        with open(os.path.join(STATIC, name), "rb") as f:
            body = f.read()
        if name == "index.html":
            body = body.replace(VERSION_MARK, self._version_of(body).encode()).replace(KEY_MARK, self.key.encode())
        self._send(h, 200, ctype, body)

    # ------------------------------------------------------------ Settings page (S2)
    def _local(self, h) -> bool:
        host = (h.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name in _LOCAL_HOSTS

    def _json(self, h, code: int, obj) -> None:
        self._send(h, code, "application/json", json.dumps(obj).encode())

    def _settings_view(self) -> dict:
        rows = self.settings.describe()
        with self._restart_lock:
            waiting = sorted(self._restart)
        return {"settings": rows, "restart": waiting}

    def _settings_get(self, h) -> None:
        if self.settings is None:
            return self._send(h, 404, "text/plain", b"not found")
        if not self._local(h):
            return self._json(h, 403, {"ok": False, "why": "not a local request"})
        self._json(h, 200, self._settings_view())

    def _settings_post(self, h, reset: bool, sample: bool = False) -> None:
        if self.settings is None:
            return self._send(h, 404, "text/plain", b"not found")
        try:
            n = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self._json(h, 413, {"ok": False, "why": "request too large"})
        raw = h.rfile.read(n) if n else b""
        if not self._local(h):
            return self._json(h, 403, {"ok": False, "why": "not a local request"})
        if not hmac.compare_digest((h.headers.get("X-KB-Key") or "").encode(), self.key.encode()):
            return self._json(h, 403, {"ok": False, "why": "the page is out of date: reload it"})
        if sample:
            return self._sample(h, raw)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            name = str(body["name"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return self._json(h, 400, {"ok": False, "why": "bad request"})
        if reset:
            ok, why = self.settings.reset(name, "kneeboard")
        elif "value" not in body:
            return self._json(h, 400, {"ok": False, "why": "no value"})
        else:
            ok, why = self.settings.set_value(name, body["value"], "kneeboard")
        row = next((r for r in self.settings.describe() if r["name"] == name), None)
        if ok and row is not None and not row["live"]:
            with self._restart_lock:
                self._restart.add(name)
        self._json(h, 200 if ok else 400, {"ok": ok, "why": why, "setting": row})

    def _sample(self, h, raw: bytes) -> None:
        """Plays a line in one of the voices on the list (never any other file)."""
        from core import settings as settings_mod
        try:
            path = str(json.loads(raw.decode("utf-8") or "{}")["path"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return self._json(h, 400, {"ok": False, "why": "bad request"})
        known = {os.path.normcase(os.path.abspath(p)): p for p in settings_mod.voices(getattr(self.settings, "base", None))}
        real = known.get(os.path.normcase(os.path.abspath(path)))
        if real is None:
            return self._json(h, 400, {"ok": False, "why": "not one of the voices"})
        if self.sample is None:
            return self._json(h, 400, {"ok": False, "why": "no speech here"})
        threading.Thread(target=self.sample, args=(real,), daemon=True, name="kb-voice-sample").start()
        self._json(h, 200, {"ok": True, "why": ""})

    @staticmethod
    def _version_of(body: bytes) -> str:
        return hashlib.sha1(body).hexdigest()[:12]

    def page_version(self) -> dict:
        """{"v": version of index.html on disk}: re-read only when its size or time changes."""
        path = os.path.join(STATIC, "index.html")
        try:
            st = os.stat(path)
        except OSError:
            return {"v": ""}
        key = (st.st_mtime_ns, st.st_size)
        with self._ver_lock:
            if key != self._ver_key:
                with open(path, "rb") as f:
                    self._ver = self._version_of(f.read())
                self._ver_key = key
            return {"v": self._ver}

    # ------------------------------------------------------------ SSE
    def _events(self, h) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "keep-alive")
        h.end_headers()
        last = {name: None for name in self.feeds}
        sent_t = {name: 0.0 for name in self.feeds}
        checked = {name: -1e9 for name in self.feeds}
        log_id = 0

        def emit(event: str, data: str) -> None:
            h.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())

        first = self.log.since(0)
        if first:
            emit("comms", json.dumps(first))
            log_id = first[-1]["id"]
        while True:
            now = time.monotonic()
            for name, (fn, every) in self.feeds.items():
                if now - checked[name] < every - 1e-3:
                    continue
                checked[name] = now
                try:
                    data = json.dumps(fn(), separators=(",", ":"))
                except Exception as e:
                    data = json.dumps({"error": f"{type(e).__name__}: {e}"})
                if data != last[name] or now - sent_t[name] > RESEND_S:
                    emit(name, data)
                    last[name], sent_t[name] = data, now
            new = self.log.since(log_id)
            if new:
                emit("comms", json.dumps(new))
                log_id = new[-1]["id"]
            h.wfile.flush()
            time.sleep(TICK_S)
