"""
common/olympus.py
A small client for DCS Olympus (2.0.5), the RTS-style mod: its backend (Mods\\Services\\Olympus\\
bin\\core.dll) runs inside DCS and takes commands over HTTP on localhost:4512.

Used for wingman orders the radio menu can't give: attack a chosen unit, follow me, fly to a
waypoint, land, weapons hold/free, lase, smoke. Only the commands in ALLOWED can be sent, and only
for units of your own flight (common/wingmen.py checks that).

What's known from the install (20 Oct 2026): backend localhost:4512 (Saved Games\\DCS\\Config\\
olympus.json), passwords stored there as sha256 hex, roles Game master / Blue commander / Red
commander, commands such as attackUnit / followUnit / setPath / landAtPoint / setROE / fireLaser /
moveSpot / deleteSpot / smoke, units referenced by their DCS object id. The exact request shape
(paths, Basic auth with the sha256 of the password or the password itself) is found at the first
connection: AUTH_VARIANTS x PATHS are tried and the one that works is kept. olympus_probe.py shows
what was found.

Blocking calls (2 s timeout): call from a worker thread, never from a telemetry loop.

Health (audit step 2, 29 Oct): no lock is held across HTTP (one connection attempt at a time, on its
own lock); status() gives the last error and its age, the last success and failure counts by kind
(unreachable / refused / http) for the kneeboard Status page; every failed command is logged, so
follow-up orders whose result isn't checked (setROE after a launch, Outcast clean-up) leave a trace.
No password or header ever goes into an error, a log line or status().
"""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any, Callable, Dict, Optional, Tuple

# spawnAircrafts: Overlord's SEAD flight only (common/overlord.py). No delete / explosion.
# cloneUnits / deleteUnit: "two, go independent" only (common/independents.py), never your aircraft.
ALLOWED = {"spawnAircrafts", "cloneUnits", "deleteUnit", "setAltitudeType", "setSpeed", "attackUnit", "followUnit", "setPath", "landAtPoint", "landAt", "setROE",
           "setReactionToThreat", "changeAltitude", "setAltitude", "fireLaser", "fireInfrared",
           "moveSpot", "deleteSpot", "smoke"}
DATA_PATHS = ("/olympus/mission", "/olympus/bullseyes", "/olympus/airbases")
COMMAND_PATHS = ("/olympus", "/olympus/commands")
RETRY_SEC = 15.0
# Called with (name, params) after Olympus accepted a command (main.py: the tactical picture records
# who attacked what). None = nobody listens. Must not block: it runs on the caller's thread.
ON_COMMAND = None


class OlympusError(Exception):
    pass


class Olympus:
    def __init__(self, url: str = "http://localhost:4512", user: str = "George", password: str = "",
                 timeout: float = 2.0, opener=None, allowed=None,
                 guard: Optional[Callable[[str, Dict[str, Any]], Tuple[bool, str]]] = None):
        """allowed: the commands this client may send (default ALLOWED, the Blue commander's list).
        guard(name, params) -> (ok, why): must approve every command (the training client's fence,
        common/training.py); None = no extra check."""
        self.url = url.rstrip("/")
        self.allowed = frozenset(ALLOWED if allowed is None else allowed)
        self.guard = guard
        self.user = user
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        pw = password or ""
        self._auth_variants = [("sha256", hashlib.sha256(pw.encode()).hexdigest()), ("plain", pw)]
        self._auth: Optional[str] = None            # the Authorization header that works
        self._auth_name = ""
        self._cmd_path: Optional[str] = None
        self._last_try = -1e9
        self._lock = threading.Lock()              # state only: never held across HTTP
        self._connecting = threading.Lock()        # one connection attempt at a time
        self.last_error = ""
        self._error_at: Optional[float] = None     # time.monotonic() of last_error
        self._ok_at: Optional[float] = None        # last request Olympus answered 200
        self._fails = Counter()                    # unreachable / refused / http
        self._claimed = set()          # Olympus-N groups already ours (Overlord flights, Outcasts)

    def claim(self, group: str) -> None:
        with self._lock:
            self._claimed.add(group)

    def reset_claims(self) -> None:
        """A new mission: Olympus starts its group names again."""
        with self._lock:
            self._claimed.clear()

    def claimed(self) -> set:
        with self._lock:
            return set(self._claimed)

    # ------------------------------------------------------------ http
    def _header(self, secret: str) -> str:
        return "Basic " + base64.b64encode(f"{self.user}:{secret}".encode()).decode()

    def _request(self, method: str, path: str, body: Optional[dict], auth: Optional[str]) -> Tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with self._opener(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if hasattr(e, "read") else b""
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise OlympusError(f"Olympus not reachable ({getattr(e, 'reason', e)})") from e

    # ------------------------------------------------------------ health
    def _ok(self) -> None:
        with self._lock:
            self._ok_at = time.monotonic()

    def _fail(self, kind: str, why: str, what: str = "", always: bool = False, drop: bool = False) -> None:
        """Records a failure; logs it (always for commands, else only when the reason changes)."""
        with self._lock:
            new = why != self.last_error
            self.last_error, self._error_at = why, time.monotonic()
            self._fails[kind] += 1
            if drop:
                self._auth = None                    # reconnect next time
        if always or new:
            print(f"[OLYMPUS] {what + ' failed: ' if what else ''}{why}")

    def status(self) -> Dict[str, Any]:
        """For the Status page: connected, last error and its age, last success age, failure counts."""
        now = time.monotonic()
        with self._lock:
            return {"connected": self._auth is not None, "login": self._auth_name if self._auth else "",
                    "last_error": self.last_error,
                    "error_age": None if self._error_at is None else now - self._error_at,
                    "ok_age": None if self._ok_at is None else now - self._ok_at,
                    "fails": dict(self._fails)}

    # ------------------------------------------------------------ connection
    def connect(self, force: bool = False) -> bool:
        """Finds a working login. Cheap once connected; retries at most every RETRY_SEC. Another
        thread already connecting: False at once (force waits for it)."""
        with self._lock:
            if self._auth and not force:
                return True
            if not force and time.monotonic() - self._last_try < RETRY_SEC:
                return False
        if not self._connecting.acquire(blocking=force):
            return False
        try:
            with self._lock:                         # re-check: the other attempt may have finished
                if self._auth and not force:
                    return True
                if not force and time.monotonic() - self._last_try < RETRY_SEC:
                    return False
                self._last_try = time.monotonic()
            try:
                for name, secret in self._auth_variants:
                    auth = self._header(secret)
                    for path in DATA_PATHS:
                        status, _ = self._request("GET", path, None, auth)
                        if status == 200:
                            with self._lock:
                                self._auth, self._auth_name = auth, name
                                self._ok_at = time.monotonic()
                            print(f"[OLYMPUS] Connected ({name} password, {path}).")
                            return True
                self._fail("refused", "Olympus refused the password (Blue commander?)")
            except OlympusError as e:
                self._fail("unreachable", str(e))
            return False
        finally:
            self._connecting.release()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._auth is not None

    def _login(self) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            return self._auth, self._cmd_path

    def get(self, path: str) -> Any:
        if not self.connect():
            raise OlympusError(self.last_error or "Olympus not connected")
        auth, _ = self._login()
        try:
            status, body = self._request("GET", path, None, auth)
        except OlympusError as e:
            self._fail("unreachable", str(e), f"GET {path}", drop=True)
            raise
        if status in (401, 403):
            self._fail("refused", "Olympus refused the password", f"GET {path}", drop=True)
            raise OlympusError("Olympus refused the password")
        if status != 200:
            self._fail("http", f"GET {path}: HTTP {status}")
            raise OlympusError(f"GET {path}: HTTP {status}")
        self._ok()
        try:
            return json.loads(body.decode("utf-8", errors="replace") or "null")
        except ValueError:
            return body

    def command(self, name: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        """PUT {name: params}. (True, '') when Olympus accepted it, else (False, why). Failures are
        logged here, so callers that don't check the result still leave a trace. The result is also
        noted for common/orders.py (an order's outcome, audit step 6)."""
        ok, why = self._command(name, params)
        from common import orders
        orders.note_command(name, ok, why)
        if ok and ON_COMMAND is not None:          # the tactical picture notes who attacked what
            try:
                ON_COMMAND(name, params)
            except Exception as e:
                print(f"[OLYMPUS] ON_COMMAND: {type(e).__name__}: {e}")
        return ok, why

    def _command(self, name: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        if name not in self.allowed:
            return False, f"{name} isn't allowed"
        if self.guard is not None:
            try:
                ok, why = self.guard(name, params)
            except Exception as e:                   # a broken guard refuses, never lets through
                ok, why = False, f"guard error {type(e).__name__}"
            if not ok:
                print(f"[OLYMPUS] {name} refused: {why}")
                return False, why
        if not self.connect():
            why = self.last_error or "Olympus not connected"
            print(f"[OLYMPUS] {name} not sent: {why}")
            return False, why
        auth, cmd_path = self._login()
        paths = [cmd_path] if cmd_path else list(COMMAND_PATHS)
        last = ""
        for path in paths:
            try:
                status, body = self._request("PUT", path, {name: params}, auth)
            except OlympusError as e:
                self._fail("unreachable", str(e), name, always=True, drop=True)
                return False, str(e)
            if status == 200:
                with self._lock:
                    self._cmd_path = path
                    self._ok_at = time.monotonic()
                print(f"[OLYMPUS] {name} {json.dumps(params)}")
                return True, ""
            if status in (401, 403):
                self._fail("refused", "Olympus refused the password", name, always=True, drop=True)
                return False, "Olympus refused the password"
            last = f"HTTP {status}"
        why = f"Olympus didn't take {name} ({last})"
        self._fail("http", why, name, always=True)
        return False, why
