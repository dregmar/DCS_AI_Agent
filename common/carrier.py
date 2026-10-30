"""
common/carrier.py - Supercarrier by voice (feature 10), and keeping our voices quiet during recovery.

Supercarrier's Marshal / Approach / Paddles live in the DCS radio menu, and DCS's own voices answer
them. Saying the call picks the matching menu item; we say nothing ourselves:

    "Marshal, inbound" / "mother, 401 inbound"      ATC > CVN-71 ... > Inbound
    "established" · "commencing" · "check in"       (after Inbound the menu's main page IS the
    "platform" · "request azimuth" · "ball" ...      carrier page: "Arrival. Carrier approach ...")
    "abort inbound"

Items are found BY NAME on whatever page is showing (they change with each step and DCS re-sorts
the ATC list by distance). A call that isn't on the page selects nothing: the crew voice reads what
IS there, and the carrier's frequency when the carrier itself isn't in the ATC list.

Seen in DCS (28 Oct, Hornet, CVN-71, Case II/III): Inbound, Request Azimuth -> Abort Inbound,
ESTABLISHED, COMMENCING, Request Azimuth -> Abort Inbound, CHECK IN, Platform, Request Azimuth.
Later items (ball, Clara ...) are matched by their words when they appear.

Quiet window: from Inbound until you're stopped on deck (or Abort Inbound, a new mission, 45 min),
our UNPROMPTED speech (callouts, Overlord's calls, Outcast reports, fuel calls) is held, and played
afterwards (anything held over 60 s is dropped). Answers to what you ask still play.
"""
import re
import threading
import time
from typing import Callable, List, Optional, Tuple

from common.comms_menu import clean_item

CARRIER_ITEM = re.compile(r"\bcvn\b|\bcvn-?\d|carrier|stennis|roosevelt|lincoln|washington|truman|forrestal|"
                          r"tarawa|kuznetsov|vinson|nimitz|eisenhower|reagan|ford\b", re.I)
_CARRIER_PAGE = re.compile(r"carrier|arrival|marshal|case\s*[123i]", re.I)
_ADDRESS = re.compile(r"^\s*(?:(?:hey|okay|ok|george|uh|um)[\s,.]+)*(?:marshal+|marshall|mother|approach|paddles|"
                      r"tower|boss|strike|carrier|the boat)\b[\s,.!?]*", re.I)
_SIDE = re.compile(r"^\s*(?:\d{3}|\d \d \d|hornet|[a-z]+ \d(?:[ -]\d)?)[\s,.]+", re.I)     # "401," / "Enfield 1-1,"

# (key, what the pilot says, what the menu item says)
CALLS: List[Tuple[str, re.Pattern, re.Pattern]] = [
    ("abort inbound", re.compile(r"\babort(?:ing)?\s+(?:the\s+)?inbound\b|\bcancel(?:ling)?\s+inbound\b", re.I),
     re.compile(r"abort", re.I)),
    ("inbound", re.compile(r"\binbound\b", re.I), re.compile(r"^inbound\b", re.I)),
    ("established", re.compile(r"\bestablished\b|\bin the stack\b", re.I), re.compile(r"establish", re.I)),
    ("commencing", re.compile(r"\bcommenc\w*|\bpushing\b", re.I), re.compile(r"commenc", re.I)),
    ("check in", re.compile(r"\bcheck(?:ing)?[\s-]+in\b", re.I), re.compile(r"check ?-?in", re.I)),
    ("platform", re.compile(r"\bplatform\b", re.I), re.compile(r"platform", re.I)),
    ("azimuth", re.compile(r"\bazimuth\b", re.I), re.compile(r"azimuth", re.I)),
    ("ball", re.compile(r"\bball\b", re.I), re.compile(r"\bball\b", re.I)),
    ("clara", re.compile(r"\bclara\b|\bno ball\b", re.I), re.compile(r"clara", re.I)),
    ("see you at 10", re.compile(r"\bsee you at (?:ten|10)\b", re.I), re.compile(r"see you", re.I)),
    ("needles", re.compile(r"\bneedles\b", re.I), re.compile(r"needles", re.I)),
]
QUIET_MAX_S = 45 * 60
HELD_MAX_AGE_S = 60.0
_FREQ = re.compile(r"\(([\d.]+)\s*MHz\s*(AM|FM)?\)", re.I)


def call_of(text: str) -> Optional[str]:
    t = text or ""
    return next((key for key, said, _ in CALLS if said.search(t)), None)


def _item_rx(key: str) -> Optional[re.Pattern]:
    return next((rx for k, _, rx in CALLS if k == key), None)


class QuietWindow:
    """Holds unprompted speech during a carrier recovery. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._since: Optional[float] = None
        self._held: List[tuple] = []             # (when, text, speak_fn)

    def start(self) -> None:
        with self._lock:
            if self._since is None:
                self._since = self.clock()
                print("[CARRIER] Quiet window: holding our unprompted calls until you're on deck.")

    def active(self) -> bool:
        with self._lock:
            return self._since is not None

    def hold(self, text: str, speak: Callable[[str], None]) -> bool:
        """True (and kept for later) while quiet; False = speak it now."""
        with self._lock:
            if self._since is None:
                return False
            self._held.append((self.clock(), text, speak))
        print(f"[CARRIER] Held: '{text}'")
        return True

    def end(self, why: str) -> None:
        with self._lock:
            if self._since is None:
                return
            self._since = None
            held, self._held = self._held, []
        now = self.clock()
        fresh = [(t, s) for when, t, s in held if now - when <= HELD_MAX_AGE_S]
        print(f"[CARRIER] Quiet window over ({why}); {len(fresh)} held call(s), {len(held) - len(fresh)} stale dropped.")
        for text, speak in fresh:
            speak(text)

    def tick(self, snap: dict) -> None:
        """Ends the window when you're stopped on deck, or after QUIET_MAX_S."""
        with self._lock:
            since = self._since
        if since is None:
            return
        if self.clock() - since > QUIET_MAX_S:
            self.end("timeout")
        elif snap.get("telemetry_age", 0.0) <= 5.0 and (snap.get("speed_kts") or 99.0) < 30.0 \
                and (snap.get("alt_ft") if snap.get("alt_ft") is not None else 9999.0) < 150.0:
            self.end("on deck")

    def gate(self, speak: Callable[[str], None]) -> Callable[[str], None]:
        """speak, but held while quiet (for unprompted speech only)."""
        def f(text: str) -> None:
            if not self.hold(text, speak):
                speak(text)
        return f


class CarrierCalls:
    def __init__(self, comms, quiet: QuietWindow, is_jet: Callable[[], bool], carriers: Callable[[], bool]):
        """comms: common.comms_menu.CommsMenu. is_jet(): flying an aircraft with fuel_calls / jet profile.
        carriers(): the mission has a ship (AIRFIELDS)."""
        self.comms, self.quiet, self.is_jet, self.carriers = comms, quiet, is_jet, carriers
        self._lock = threading.Lock()
        self.freq: Optional[str] = None           # "127.5 AM", from the ATC list

    def wanted(self, text: str) -> bool:
        if not self.is_jet() or not self.carriers():
            return False
        return bool(_ADDRESS.search(text or "")) or call_of(text) is not None

    # ------------------------------------------------------------ menu walking
    def _page(self) -> dict:
        menu, _ = self.comms.snapshot()
        if menu["visible"]:
            self.comms._send("CLOSE")
            menu = self.comms._wait_change(menu)
        self.comms._send("OPEN")
        return self.comms._wait_change(menu)

    def _select(self, page: dict, n: int) -> dict:
        self.comms._send(f"SELECT {n}")
        return self.comms._wait_change(page)

    def _close(self, page: dict) -> None:
        if page.get("visible"):
            self.comms._send("CLOSE")
            self.comms._wait_change(page)

    @staticmethod
    def _items(page: dict):
        return [(n, name) for n, name in sorted(page["items"].items()) if n < 11]

    def _is_carrier_page(self, page: dict) -> bool:
        if _CARRIER_PAGE.search(page.get("caption") or ""):
            return True
        names = " ".join(name for _, name in self._items(page)).lower()
        return any(rx.search(names) for k, _, rx in CALLS if k not in ("inbound", "azimuth"))

    def _find_carrier_page(self, page: dict) -> Optional[dict]:
        """From the main page: the carrier page itself, or ATC > <carrier>."""
        if self._is_carrier_page(page):
            return page
        atc = next((n for n, name in self._items(page) if re.fullmatch(r"\s*atc\s*", name, re.I)), None)
        if atc is None:
            return None
        page = self._select(page, atc)
        hit = next(((n, name) for n, name in self._items(page) if CARRIER_ITEM.search(name)), None)
        if hit is None:
            return None
        m = _FREQ.search(hit[1])
        if m:
            self.freq = f"{m.group(1)} {(m.group(2) or 'AM').upper()}"
        return self._select(page, hit[0])

    @staticmethod
    def _say_items(page: dict) -> str:
        names = [re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", name)).strip().lower()
                 for n, name in sorted(page["items"].items()) if n < 11]
        return ", ".join(names)

    # ------------------------------------------------------------ the call
    def handle(self, text: str) -> Optional[str]:
        """'' = the call went to DCS (its voices answer); else what the crew says. Blocking (~1-2 s)."""
        if not self.wanted(text):
            return None
        if not self.comms.live():
            return "Unable, the radio menu link isn't running."
        rest = _SIDE.sub("", _ADDRESS.sub("", text or ""))
        key = call_of(rest) or call_of(text)
        if key is None:
            return None                           # addressed but nothing we know: let others try
        with self._lock:
            page = self._page()
            if not page.get("visible"):
                return "Unable, the radio menu didn't open."
            carrier = self._find_carrier_page(page)
            if carrier is None or not carrier.get("visible"):
                self._close(carrier or page)
                where = f" Carrier is on {self.freq}." if self.freq else ""
                return f"No carrier in the radio menu. Check your radio.{where}"
            rx = _item_rx(key)
            n = next((n for n, name in self._items(carrier) if rx and rx.search(clean_item(name))), None)
            if n is None:
                have = self._say_items(carrier)
                self._close(carrier)
                return f"No {key} call now. Carrier menu has: {have}."
            print(f"[CARRIER] '{text}' -> {carrier['items'][n]}")
            self._select(carrier, n)
        self._phase(key)
        return ""

    def _phase(self, key: str) -> None:
        if key == "abort inbound":
            self.quiet.end("inbound aborted")
        elif key in ("inbound", "established", "commencing", "check in", "platform", "ball", "see you at 10"):
            self.quiet.start()
