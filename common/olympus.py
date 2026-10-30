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
"""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

# spawnAircrafts: Overlord's SEAD flight only (common/overlord.py). No delete / explosion.
# cloneUnits / deleteUnit: "two, go independent" only (common/independents.py), never your aircraft.
ALLOWED = {"spawnAircrafts", "cloneUnits", "deleteUnit", "setAltitudeType", "setSpeed", "attackUnit", "followUnit", "setPath", "landAtPoint", "landAt", "setROE",
           "setReactionToThreat", "changeAltitude", "setAltitude", "fireLaser", "fireInfrared",
           "moveSpot", "deleteSpot", "smoke"}
DATA_PATHS = ("/olympus/mission", "/olympus/bullseyes", "/olympus/airbases")
COMMAND_PATHS = ("/olympus", "/olympus/commands")
RETRY_SEC = 15.0


class OlympusError(Exception):
    pass


class Olympus:
    def __init__(self, url: str = "http://localhost:4512", user: str = "George", password: str = "",
                 timeout: float = 2.0, opener=None):
        self.url = url.rstrip("/")
        self.user = user
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        pw = password or ""
        self._auth_variants = [("sha256", hashlib.sha256(pw.encode()).hexdigest()), ("plain", pw)]
        self._auth: Optional[str] = None            # the Authorization header that works
        self._auth_name = ""
        self._cmd_path: Optional[str] = None
        self._last_try = -1e9
        self._lock = threading.Lock()
        self.last_error = ""
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

    # ------------------------------------------------------------ connection
    def connect(self, force: bool = False) -> bool:
        """Finds a working login. Cheap once connected; retries at most every RETRY_SEC."""
        with self._lock:
            if self._auth and not force:
                return True
            now = time.monotonic()
            if not force and now - self._last_try < RETRY_SEC:
                return False
            self._last_try = now
            try:
                for name, secret in self._auth_variants:
                    auth = self._header(secret)
                    for path in DATA_PATHS:
                        status, _ = self._request("GET", path, None, auth)
                        if status == 200:
                            self._auth, self._auth_name = auth, name
                            print(f"[OLYMPUS] Connected ({name} password, {path}).")
                            return True
                        if status == 404:
                            continue
                self.last_error = "Olympus refused the password (Blue commander?)"
            except OlympusError as e:
                self.last_error = str(e)
            return False

    @property
    def connected(self) -> bool:
        return self._auth is not None

    def get(self, path: str) -> Any:
        if not self.connect():
            raise OlympusError(self.last_error or "Olympus not connected")
        status, body = self._request("GET", path, None, self._auth)
        if status != 200:
            raise OlympusError(f"GET {path}: HTTP {status}")
        try:
            return json.loads(body.decode("utf-8", errors="replace") or "null")
        except ValueError:
            return body

    def command(self, name: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        """PUT {name: params}. (True, '') when Olympus accepted it, else (False, why)."""
        if name not in ALLOWED:
            return False, f"{name} isn't allowed"
        if not self.connect():
            return False, self.last_error or "Olympus not connected"
        paths = [self._cmd_path] if self._cmd_path else list(COMMAND_PATHS)
        last = ""
        for path in paths:
            try:
                status, body = self._request("PUT", path, {name: params}, self._auth)
            except OlympusError as e:
                self._auth = None                    # reconnect next time
                return False, str(e)
            if status == 200:
                if self._cmd_path != path:
                    self._cmd_path = path
                print(f"[OLYMPUS] {name} {json.dumps(params)}")
                return True, ""
            if status in (401, 403):
                self._auth = None
                return False, "Olympus refused the password"
            last = f"HTTP {status}"
        return False, f"Olympus didn't take {name} ({last})"
