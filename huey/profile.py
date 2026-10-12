"""
huey/profile.py
The UH-1H profile: what happens to a transcript while you fly the Huey.

handle(text, captured) runs on the voice worker thread: it matches the command (fast path, else
the LLM) and hands it to the Huey worker thread, which does the slow parts (closed-loop crew
commands, DCS-BIOS readback) and speaks the result. "Hold fire" jumps the queue.
"""
import json
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Optional

import config
from core.state import clock_position
from huey import commands
from huey.checklists import ChecklistRunner
from huey.cockpit import Cockpit, RADIOS, SWITCHES, say_freq, switch_state
from huey.crew import HueyCrew, describe_crew
from huey.link import CommandLink

TELEMETRY_FRESH_SEC = 3.0
LINK_FRESH_SEC = 3.0
FUEL_BURN_LBS_HR = getattr(config, "HUEY_FUEL_BURN_LBS_HR", 555.0)   # 20-minute light ~ 185 lbs
AUTO_ARM_FLARES = getattr(config, "HUEY_AUTO_ARM_FLARES", True)
# Switch positions George refuses while you're flying (a misheard word must not stop the engine)
NOT_IN_FLIGHT = {("main_fuel", "OFF"), ("hyd_cont", "OFF"), ("battery", "OFF"), ("emer_gov", "EMER"),
                 ("starter_gen", "START"), ("main_gen", "OFF"), ("main_gen", "RESET")}


class HueyProfile:
    def __init__(self, speak: Callable[[str], None], state, bios_sender,
                 llm: Optional[Callable] = None, link: Optional[CommandLink] = None,
                 crew_step_sec: float = 1.5, readback_sec: float = 2.0):
        """speak(text): non-blocking radio speech. state: the state store.
        bios_sender: core.dcs_bios.cmd_sender. llm(system_prompt, history, text) -> dict."""
        self.speak, self.state, self.llm = speak, state, llm
        self.crew = HueyCrew(link or CommandLink(), state.huey_snapshot, step_sec=crew_step_sec)
        self.cockpit = Cockpit(bios_sender, state.bios_words_snapshot,
                               lambda: state.snapshot()["bios_age"], readback_sec=readback_sec)
        self.checklist = ChecklistRunner(self._switch, self._gauge, self._cautions,
                                         self.cockpit.set_switch)
        self.history = deque(maxlen=6)
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True, name="huey-worker")
        self._worker.start()

    # ------------------------------------------------------------ readers
    def _huey(self) -> dict:
        data, rx = self.state.huey_snapshot()
        return data if rx and time.monotonic() - rx <= LINK_FRESH_SEC else {}

    def _gauge(self, key: str):
        return self._huey().get(key)

    def _cautions(self):
        h = self._huey()
        return h.get("cautions") if h else None

    def _switch(self, key: str):
        if not self.cockpit.live():
            return None
        return switch_state(self.state.bios_words_snapshot(), key)

    def _flares(self):
        h = self._huey()
        if h.get("flares") is not None:
            return h["flares"]
        from huey.bios_map import decode_int
        return decode_int(self.state.bios_words_snapshot(), "flare_count")

    def _flying(self, snap: dict) -> bool:
        if snap["telemetry_age"] > TELEMETRY_FRESH_SEC:
            return False
        return snap.get("agl_ft", -1) > 10 or snap.get("speed_kts", 0) > 30

    # ------------------------------------------------------------ entry (voice thread)
    def handle(self, text: str, captured: float = None) -> None:
        t0 = time.perf_counter()
        cmd = commands.match(text, self.checklist.active)
        source = "FAST"
        if cmd is None:
            source = "LLM"
            if self.llm is None:
                self.speak("Say again.")
                return
            try:
                res = self.llm(commands.llm_prompt(self.state_line(), self._checklist_name()),
                               list(self.history), text)
            except Exception as e:
                print(f"[ERROR] LLM request failed: {type(e).__name__}: {e}")
                self.speak("Say again, system fault.")
                return
            cmd = commands.sanitize(res)
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": json.dumps(
            {k: v for k, v in cmd.items() if k != "source"})})
        print(f"[HUEY]: {cmd}")
        print(f"[TIMING] {source} {time.perf_counter() - t0:.2f}s")
        if cmd["action"] == "CREW_ROE" and cmd.get("value") == "HOLD":
            self._drain()                               # cease fire goes first
        self._q.put(cmd)

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _checklist_name(self):
        return self.checklist.name

    # ------------------------------------------------------------ worker
    def _work(self):
        while True:
            cmd = self._q.get()
            try:
                sentence = self.execute(cmd)
                if sentence:
                    self.speak(sentence)
            except Exception as e:
                print(f"[HUEY ERROR] {type(e).__name__}: {e}")
                self.speak("Say again, system fault.")

    def execute(self, cmd: Dict[str, Any]) -> str:
        """Runs one command and returns what George says. Blocking; worker thread (or tests)."""
        a = cmd["action"]
        if a == "CREW_ROE":
            return self.crew.set_roe(cmd["seats"], cmd["value"])[1]
        if a == "CREW_BURST":
            return self.crew.set_burst(cmd["seats"], cmd["value"])[1]
        if a == "CREW_REPORT":
            return self.crew.report()[1]
        if a == "PANEL":
            return self.crew.show_panel(cmd["value"] == "SHOW")[1]
        if a == "AUTOPILOT":
            return self.crew.autopilot(cmd["value"])[1]
        if a == "SWITCH":
            if (cmd["key"], cmd["value"]) in NOT_IN_FLIGHT and self._flying(self.state.snapshot()):
                return f"Negative, not in flight: {SWITCHES[cmd['key']][2]} {cmd['value'].lower()}."
            return self.cockpit.set_switch(cmd["key"], cmd["value"])[1]
        if a == "FLARE":
            return self.cockpit.dispense_flare(self._flares, auto_arm=AUTO_ARM_FLARES)[1]
        if a == "RADIO":
            return self.cockpit.set_radio(cmd["key"], cmd["mhz"])[1]
        if a == "RADIO_REPORT":
            return self.radio_report()
        if a == "CHECKLIST_START":
            return self.checklist.start(cmd["value"])
        if a == "CHECKLIST_REPLY":
            return self.checklist.reply(cmd["value"])
        if a == "STATUS":
            return self.status_report()
        if a == "FUEL":
            return self.fuel_report()
        if a == "ENGINE":
            return self.engine_report()
        if a == "CONTACTS":
            return self.contacts_report()
        if a == "CAUTIONS":
            return self.cautions_report()
        return cmd.get("speech") or "Copy."

    # ------------------------------------------------------------ reports
    def engine_report(self) -> str:
        h = self._huey()
        if not h:
            return "No engine data. Check george_huey_export.lua."
        parts = []
        if h.get("rotor_rpm") is not None:
            parts.append(f"Rotor {h['rotor_rpm']:.0f}")
        if h.get("engine_rpm") is not None:
            parts.append(f"engine {h['engine_rpm']:.0f}")
        if h.get("n1_pct") is not None:
            parts.append(f"N1 {h['n1_pct']:.0f} percent")
        if h.get("torque_psi") is not None:
            parts.append(f"torque {h['torque_psi']:.0f}")
        if h.get("egt_c") is not None:
            parts.append(f"EGT {h['egt_c']:.0f}")
        return (", ".join(parts) + ".") if parts else "No engine data."

    def fuel_report(self) -> str:
        lbs = self._gauge("fuel_lbs")
        if lbs is None:
            return "No fuel reading."
        mins = int(lbs / FUEL_BURN_LBS_HR * 60)
        t = f"{mins // 60} hour {mins % 60} minutes" if mins >= 60 else f"{mins} minutes"
        return f"Fuel {lbs:.0f} pounds, about {t}."

    def cautions_report(self) -> str:
        lit = self._cautions()
        if lit is None:
            return "No caution panel data."
        return "No cautions." if not lit else "Cautions: " + ", ".join(lit) + "."

    def contacts_report(self, limit: int = 3) -> str:
        snap = self.state.snapshot()
        if snap["telemetry_age"] > TELEMETRY_FRESH_SEC:
            return "No flight data."
        cs = [c for c in snap.get("contacts") or [] if c.get("dist_m") is not None][:limit]
        if not cs:
            return "No contacts."
        out = []
        for c in cs:
            clock = clock_position(c.get("brg"), snap.get("hdg"))
            out.append(f"{c.get('type', 'unknown')}" + (f" at {clock} o'clock" if clock else "")
                       + f", {c['dist_m'] / 1000:.1f} kilometers")
        return "Contacts: " + "; ".join(out) + "."

    def radio_report(self) -> str:
        if not self.cockpit.live():
            return "No cockpit data. Check DCS-BIOS."
        parts = []
        for key, (_, _, name) in RADIOS.items():
            f = self.cockpit.radio(key)
            try:
                parts.append(f"{name} {say_freq(float(f))}")
            except (TypeError, ValueError):
                continue
        return (", ".join(parts) + ".") if parts else "No radio data."

    def status_report(self) -> str:
        snap = self.state.snapshot()
        parts = []
        if snap["telemetry_age"] > TELEMETRY_FRESH_SEC:
            parts.append("No flight data.")
        else:
            agl = snap.get("agl_ft", -1)
            alt = f"{agl:.0f} feet above ground" if agl >= 0 else f"altitude {snap['alt_ft']:.0f} feet"
            parts.append(f"{alt[0].upper()}{alt[1:]}, {snap['speed_kts']:.0f} knots.")
        h = self._huey()
        if h:
            parts.append(self.engine_report())
            if h.get("fuel_lbs") is not None:
                parts.append(f"Fuel {h['fuel_lbs']:.0f} pounds.")
            parts.append(self.cautions_report())
        arm = self._switch("master_arm")
        if arm:
            parts.append(f"Master arm {arm.lower()}.")
        if h.get("panel"):
            parts.append(describe_crew(h["panel"]))
        if self.checklist.active:
            parts.append(f"{self.checklist.name.replace('_', ' ')} checklist running.")
        return " ".join(parts)

    def state_line(self) -> str:
        """Short state summary for the LLM prompt."""
        snap = self.state.snapshot()
        h = self._huey()
        bits = [f"{snap.get('agl_ft', -1):.0f} ft AGL, {snap['speed_kts']:.0f} kts"]
        if h:
            bits.append(f"rotor {h.get('rotor_rpm')}, torque {h.get('torque_psi')}, fuel {h.get('fuel_lbs')} lbs")
            if h.get("panel"):
                bits.append("crew: " + describe_crew(h["panel"]))
        arm = self._switch("master_arm")
        if arm:
            bits.append(f"master arm {arm}")
        return "; ".join(bits)
