"""
intelligence/engagement_monitor.py
Engagement callouts, from what DCS reports rather than from what we asked for:

  * "Missile away, 7 remaining."   Hellfire count dropped (Export.lua, LoGetPayloadInfo)
  * "Missiles winchester."         last Hellfire gone
  * "Firing gun."                  30 mm count dropped (at most once per burst)
  * "Target destroyed."            the unit George was engaging has vanished from the
                                   mission (LoGetWorldObjects) within 90 s of a shot
  * "Next target, BMP-2."          ... and George has more on his shoot list
  * "Shoot list complete."         ... and that was the last one

Runs on its own daemon thread, polls the ApacheStateStore 4x per second and only speaks
through the non-blocking radio queue. Never sends anything to DCS.
"""
import threading
import time
from typing import Callable, Dict, Optional

from config import TELEMETRY_STALE_SEC
from core.state import state_store

SHOT_WINDOW_SEC = 90.0      # a vanished target only counts as destroyed this soon after a shot
GUN_CALL_GAP_SEC = 4.0      # one "Firing gun" per burst
MISSING_POLLS = 3           # the target must be missing this many polls in a row (debounce)


class EngagementMonitor:
    def __init__(self, speak: Callable[[str], None], interval: float = 0.25,
                 clock: Callable[[], float] = time.monotonic):
        self._speak = speak
        self._interval = interval
        self._clock = clock
        self._running = False
        self._prev_ammo: Optional[Dict[str, Optional[int]]] = None
        self._last_shot = -1e9
        self._last_gun_call = -1e9
        self._last_seen: Dict[object, float] = {}     # contact id -> last distance seen (m)
        self._missing = 0

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="engagement-monitor").start()
        print("[ENGAGEMENT] Callout monitor running...")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            time.sleep(self._interval)
            try:
                self.check()
            except Exception as e:
                print(f"[ENGAGEMENT ERROR] {type(e).__name__}: {e}")

    def check(self) -> None:
        snap = state_store.snapshot()
        now = self._clock()
        if snap["telemetry_age"] > TELEMETRY_STALE_SEC or state_store.profile() != "apache":
            self._prev_ammo = None                    # mission paused/ended: start fresh
            return
        self._check_ammo(snap["ammo"], now)
        self._check_destroyed(snap, now)

    # ---------------------------------------------------------------- stores
    def _check_ammo(self, ammo: Dict[str, Optional[int]], now: float) -> None:
        prev, self._prev_ammo = self._prev_ammo, dict(ammo)
        if prev is None:
            return
        msl, pmsl = ammo.get("msl"), prev.get("msl")
        if msl is not None and pmsl is not None and msl < pmsl:
            self._last_shot = now
            from common.debrief import DEBRIEF
            DEBRIEF.shot("missile", pmsl - msl)
            if msl == 0:
                self._speak("Missile away. Missiles winchester.")
            else:
                self._speak(f"Missile away, {msl} remaining.")
        gun, pgun = ammo.get("gun"), prev.get("gun")
        if gun is not None and pgun is not None and gun < pgun:
            self._last_shot = now
            if now - self._last_gun_call >= GUN_CALL_GAP_SEC:
                self._last_gun_call = now
                from common.debrief import DEBRIEF
                DEBRIEF.shot("gun")
                self._speak("Firing gun." if gun > 0 else "Gun winchester.")
        rkt, prkt = ammo.get("rkt"), prev.get("rkt")
        if rkt is not None and prkt is not None and rkt < prkt:
            self._last_shot = now                     # you fire rockets; no callout, but a shot

    # ---------------------------------------------------- target destroyed
    def _check_destroyed(self, snap: dict, now: float) -> None:
        contacts = snap["contacts"]
        present = {c.get("id"): c.get("dist_m") for c in contacts}
        for cid, dist in present.items():
            if dist is not None:
                self._last_seen[cid] = dist

        eng = snap["engagement"]
        if not eng:
            self._missing = 0
            return
        current = eng[0]
        cid = current.get("contact_id")
        from tactical import link
        from tactical.models import dcs_key
        st = link.status(dcs_key(cid)) if cid is not None else None
        if st is not None:                            # a current tactical picture decides (step T7)
            self._missing = 0
            if st == "dead" and now - self._last_shot <= SHOT_WINDOW_SEC:
                self._destroyed(current, snap)        # gone from 3 fresh scans, soon after a shot
            return
        if cid is None or cid in present or cid not in self._last_seen:
            self._missing = 0
            return
        # Contacts are capped at the 20 nearest within 15 km. If the list is full and our target
        # was farther than the farthest listed one, it may just have dropped off the list.
        if len(contacts) >= 20:
            farthest = max((c.get("dist_m") or 0) for c in contacts)
            if self._last_seen[cid] >= farthest:
                self._missing = 0
                return
        if self._last_seen[cid] > 14000:              # was at the edge of the 15 km window
            self._missing = 0
            return
        if now - self._last_shot > SHOT_WINDOW_SEC:   # nobody fired: don't guess
            self._missing = 0
            return
        self._missing += 1
        if self._missing < MISSING_POLLS:
            return
        self._missing = 0
        self._destroyed(current, snap)

    def _destroyed(self, current: dict, snap: dict) -> None:
        from common.debrief import DEBRIEF
        DEBRIEF.kill("George", str(current.get("type") or "target"))
        nxt = state_store.pop_engagement()
        if nxt:
            self._speak(f"Target destroyed. Next target, {nxt['type']}.")
        elif snap.get("engagement_total", 0) > 1:
            self._speak("Target destroyed. Shoot list complete.")
        else:
            self._speak("Target destroyed.")
