"""
kneeboard/server.py - the kneeboard's local web server (OpenKneeboard web dashboard tab).

Python's own ThreadingHTTPServer on a daemon thread, bound to 127.0.0.1 only:
    GET /          the page (kneeboard/static/index.html)
    GET /state     one JSON snapshot of every feed plus "comms" (debugging)
    GET /events    Server-Sent Events: one event per feed ("map", "status", "briefing", "checklist",
                   "ui" ...) when its data changes (each checked at its own interval, resent every
                   5 s anyway), and "comms" with new log lines only.
Each connected page costs one thread that sleeps between checks; building the data is a few
milliseconds, so DCS doesn't notice. Nothing here touches the game or the voice threads.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional, Tuple

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TICK_S = 0.2
RESEND_S = 5.0


class Kneeboard:
    def __init__(self, map_fn: Callable[[], dict], status_fn: Callable[[], dict], log,
                 host: str = "127.0.0.1", port: int = 5000,
                 feeds: Optional[Dict[str, Tuple[Callable[[], dict], float]]] = None):
        """feeds: more pages' data, {event name: (function, seconds between checks)}."""
        self.map_fn, self.status_fn, self.log = map_fn, status_fn, log
        self.feeds: Dict[str, Tuple[Callable[[], dict], float]] = {"map": (map_fn, TICK_S), "status": (status_fn, 0.5)}
        self.feeds.update(feeds or {})
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
                    if path in ("/", "/index.html", "/kneeboard"):
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

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
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
            self._send(h, 200, ctype, f.read())

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
