"""
apache/crew.py
George's extra jobs in the AH-64D beyond the CPG wheel: the pilot's checklists (checked on the
DCS-BIOS stream) and reading the Up-Front Display's warnings and cautions.
handle(text) runs on the voice thread and returns what George says, or None if it wasn't for him.
"""
import threading
import time
from typing import Callable, Optional

from apache.bios_map import APACHE_SWITCHES, eufd, power_lever, switch_state
from apache.checklists import APACHE_CHECKLISTS, SETTABLE, say_state
from apache.eufd import EufdMonitor, messages, speakable
from huey.checklists import ChecklistRunner
from huey.commands import match_checklist

import re

BIOS_FRESH_SEC = 3.0
_CAUTIONS = re.compile(r"\b(?:any )?(?:cautions?|warnings?|caution lights?|warning lights?|advisories)\b(?: check| status)?$|"
                       r"\bwhat(?:'s| s| is) (?:on )?(?:the )?(?:eufd|up front)\b")


class ApacheCrew:
    def __init__(self, speak: Callable[[str], None], state, bios_sender, readback_sec: float = 2.0):
        self.speak, self.state, self.sender = speak, state, bios_sender
        self.readback_sec = readback_sec
        self.checklist = ChecklistRunner(self._switch, lambda k: None, self._cautions, self.set_switch,
                                         checklists=APACHE_CHECKLISTS, can_set=lambda k: k in SETTABLE,
                                         say_state=say_state)
        self.monitor = EufdMonitor(speak)
        self._running = False

    # ------------------------------------------------------------ reading
    def _live(self) -> bool:
        return self.state.snapshot()["bios_age"] <= BIOS_FRESH_SEC

    def _switch(self, key: str) -> Optional[str]:
        if not self._live():
            return None
        w = self.state.bios_words_snapshot()
        if key in ("pl1", "pl2"):
            return power_lever(w, key)
        if key == "tail_wheel":                   # from the EUFD advisory column
            msgs = messages(eufd(w))
            adv = " ".join(msgs["advisory"]) if msgs else ""
            return ("UNLOCKED" if "TAIL WHL UNLK" in adv else "LOCKED" if "TAIL WHL LOCK" in adv
                    else None)
        if key == "levers":
            a, b = power_lever(w, "pl1"), power_lever(w, "pl2")
            if a is None or b is None:
                return None
            return a if a == b else f"{a} and {b}"
        return switch_state(w, key)

    def _cautions(self):
        if not self._live():
            return None
        msgs = messages(eufd(self.state.bios_words_snapshot()))
        if msgs is None:
            return None
        return [speakable(m) for m in msgs["warning"] + msgs["caution"]]

    def set_switch(self, key: str, state: str):
        ident = SETTABLE.get(key)
        names = APACHE_SWITCHES[key][3]
        if not ident or state not in names:
            return False, "I can't set that one."
        if not self._live():
            return False, "No cockpit data. Check DCS-BIOS."
        if not self.sender.send_command(ident, str(names.index(state))):
            return False, "Unable."
        end = time.monotonic() + self.readback_sec
        while time.monotonic() < end:
            if self._switch(key) == state:
                return True, f"{key.replace('_', ' ').capitalize()} {say_state(state)}."
            time.sleep(0.05)
        return False, f"{key.replace('_', ' ').capitalize()} didn't move."

    # ------------------------------------------------------------ voice
    def handle(self, text: str) -> Optional[str]:
        cmd = match_checklist(text, self.checklist.active)
        if cmd:
            if cmd["action"] == "CHECKLIST_START":
                name = cmd["value"]
                if name == "runup":
                    name = "before_takeoff"
                if name not in APACHE_CHECKLISTS:
                    return "No such checklist in the Apache."
                return self.checklist.start(name)
            return self.checklist.reply(cmd["value"])
        t = re.sub(r"[^a-z' ]+", " ", text.lower()).strip()
        if _CAUTIONS.search(t):
            return self.cautions_report()
        return None

    def cautions_report(self) -> str:
        if not self._live():
            return "No cockpit data. Check DCS-BIOS."
        msgs = messages(eufd(self.state.bios_words_snapshot()))
        if msgs is None:
            return "No up-front display data."
        parts = []
        for kind, label in (("warning", "Warnings"), ("caution", "Cautions"), ("advisory", "Advisories")):
            if msgs[kind]:
                parts.append(f"{label}: " + ", ".join(speakable(m) for m in msgs[kind]) + ".")
        return " ".join(parts) or "No warnings or cautions."

    # ------------------------------------------------------------ callouts
    def _loop(self, active: Callable[[], bool], interval: float):
        while self._running:
            time.sleep(interval)
            try:
                if active() and self._live():
                    self.monitor.check(eufd(self.state.bios_words_snapshot()), time.monotonic())
            except Exception as e:
                print(f"[EUFD ERROR] {type(e).__name__}: {e}")

    def start_callouts(self, active: Callable[[], bool], interval: float = 0.5) -> "ApacheCrew":
        self._running = True
        threading.Thread(target=self._loop, args=(active, interval), daemon=True, name="apache-eufd").start()
        print("[EUFD] Apache warning / caution callouts ready.")
        return self
