"""
common/jtac_sight.py - who can lase, and can they see the target (3 Nov, approved by Andre: name rule,
ground 8 km / drone 20 km slant, terrain line of sight only, built before R4).

george_comms_hook.lua sends every 10 s "JTACS\n<id>\t<unit name>\t<label>\t<type>\t<uav|named>\t<a|g>\t<lat>
\t<lon>\t<alt m>..." - your side's drones (DCS "UAVs" attribute) and units whose unit or group name says
JTAC / FAC / observer / recon / SOF. query() asks it "LOS\t<req>\t<target id>\t<unit name>..." and waits for
"LOS <req> <target id>\n<unit name>\t<1|0|gone>\t<slant m>..." (land.isVisible from each unit's eye to the
target), or "...\nNOTARGET".

Thread-safe: the hook listener thread calls on_jtacs / on_los; query() runs on the command worker or the
laser tracker and waits (never on the main, PTT or telemetry threads). The lock is never held while sending
or waiting.
"""
import itertools
import threading
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

LIST_MAX_AGE_S = 30.0          # the hook sends every 10 s; older than this = the hook has stopped
QUERY_TIMEOUT_S = 1.5
MAX_NAMES = 8                  # the hook reads at most 8 names a query


class Observer(NamedTuple):
    id: int
    name: str                  # the DCS unit name (Unit.getByName in the hook)
    label: str                 # what Darkside calls it: "JTAC Hammer" (unit or group name) or the unit name
    type: str
    why: str                   # "uav" | "named"
    air: bool
    lat: float
    lon: float
    alt: float


class Sight(NamedTuple):
    found: bool                                      # False: DCS can't find the target
    rows: Dict[str, Tuple[str, float]]               # unit name -> ("1" | "0" | "gone", slant metres)


def parse_jtacs(text: str) -> Optional[List[Observer]]:
    if not (text or "").startswith("JTACS"):
        return None
    out = []
    for line in text.split("\n")[1:]:
        f = line.split("\t")
        if len(f) < 9:
            continue
        try:
            out.append(Observer(int(f[0]), f[1], f[2] or f[1], f[3], f[4], f[5] == "a",
                                float(f[6]), float(f[7]), float(f[8])))
        except ValueError:
            continue
    return out


def parse_los(text: str) -> Optional[Tuple[int, Sight]]:
    lines = (text or "").split("\n")
    head = lines[0].split()
    if len(head) < 3 or head[0] != "LOS":
        return None
    try:
        req = int(head[1])
    except ValueError:
        return None
    body = lines[1:]
    if body[:1] == ["NOTARGET"]:
        return req, Sight(False, {})
    rows = {}
    for line in body:
        f = line.split("\t")
        if len(f) >= 3 and f[1] in ("1", "0", "gone"):
            try:
                rows[f[0]] = (f[1], float(f[2]))
            except ValueError:
                continue
    if not rows and body and (body[0] in ("NOSTATE", "NOWORLD") or body[0].startswith("ERR")):
        print(f"[JTAC] Line-of-sight query failed in DCS: {body[0][:120]}")
        return req, None
    return req, Sight(True, rows)


class SightService:
    def __init__(self, send: Callable[[str], None], clock: Callable[[], float] = time.monotonic,
                 timeout: float = QUERY_TIMEOUT_S):
        self.send, self.clock, self.timeout = send, clock, timeout
        self._lock = threading.Lock()
        self._list: Optional[List[Observer]] = None
        self._list_t = 0.0
        self._ids = itertools.count(1)
        self._waiting: Dict[int, list] = {}           # req -> [Event, Sight | None]

    # ------------------------------------------------------------ from the hook (listener thread)
    def on_jtacs(self, text: str) -> None:
        got = parse_jtacs(text)
        if got is None:
            return
        with self._lock:
            first = self._list is None
            self._list, self._list_t = got, self.clock()
        if first:
            print(f"[JTAC] Laser sources from DCS: {', '.join(o.label for o in got) or 'none'}")

    def on_los(self, text: str) -> None:
        got = parse_los(text)
        if got is None:
            return
        req, sight = got
        with self._lock:
            w = self._waiting.get(req)
        if w is not None:
            w[1] = sight
            w[0].set()

    def clear(self) -> None:
        """A new mission: unit ids start again."""
        with self._lock:
            self._list, self._list_t = None, 0.0

    # ------------------------------------------------------------ for the JTAC
    def observers(self) -> Optional[List[Observer]]:
        """Your side's laser sources, or None if the hook hasn't sent a list lately."""
        with self._lock:
            if self._list is None or self.clock() - self._list_t > LIST_MAX_AGE_S:
                return None
            return list(self._list)

    def query(self, target_id: int, names: List[str], timeout: Optional[float] = None) -> Optional[Sight]:
        """Terrain line of sight from each named unit to the target, or None if DCS didn't answer."""
        names = [n for n in names if n and "\t" not in n and "\n" not in n][:MAX_NAMES]
        if not names:
            return Sight(True, {})
        req = next(self._ids)
        w = [threading.Event(), None]
        with self._lock:
            self._waiting[req] = w
        try:
            self.send("\t".join(["LOS", str(req), str(int(target_id))] + names))
            w[0].wait(self.timeout if timeout is None else timeout)
            return w[1]
        except Exception as e:
            print(f"[JTAC] Line-of-sight query failed: {e}")
            return None
        finally:
            with self._lock:
                self._waiting.pop(req, None)
