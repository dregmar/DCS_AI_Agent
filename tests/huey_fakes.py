"""
tests/huey_fakes.py
A simulated UH-1H for the Huey tests: it answers crew commands like the game (ROE cycles
HOLD -> RET.FIRE -> FREE FIRE, burst toggles, panel shows/hides) and DCS-BIOS commands like
DCS-BIOS (switch words change), and publishes what george_huey_export.lua / the BIOS stream
would, through the real parsers into the real state store.
"""
import threading
import time

from tests import fakes  # noqa: F401  (fast test timings, must be first)
from core.state import state_store
from huey.bios_map import HUEY_BIOS_INTS, HUEY_BIOS_STRINGS
from huey.cockpit import SWITCHES
from huey.link import CMD_PANEL, CMD_SEAT, parse_packet

SEP = "-" * 41
ROE_TEXT = {"HOLD": "HOLD", "RETURN": "RET.FIRE", "FREE": "FREE FIRE"}
NEXT_ROE = {"HOLD": "RETURN", "RETURN": "FREE", "FREE": "HOLD"}
NAMES = ("PILOT", "CO-PILOT", "LH GUNNER", "RH GUNNER")


def panel_text(rows):
    """rows: list of 4 dicts {status, ammo, burst} -> list_indication-style text."""
    out = f"{SEP}\nbase\n\nchildren are {{\n{SEP}\ntxt_Status\n\t\tCREW STATUS:\n{SEP}\ntxt_Hints\nHEALTH\tROE\t  AMMO BURST\n"
    for i, r in enumerate(rows):
        out += f"{SEP}\nmem_base{i}\n\nchildren are {{\n"
        out += f"{SEP}\ntxt_mem{i}\n{NAMES[i]}\n{SEP}\ntxt_status{i}\n{r['status']}\n"
        out += f"{SEP}\ntxt_ammo{i}\n{r['ammo']}\n{SEP}\ntxt_burst{i}\n{r['burst']}\n}}\n"
    return out + "}\n"


class FakeHuey:
    """Simulated Huey crew + cockpit. Use .link as the CommandLink and .bios as the DCS-BIOS sender."""

    def __init__(self, panel_visible=False, gunners=True, respond=True):
        self.lock = threading.RLock()
        self.panel_visible = panel_visible
        self.respond = respond                         # False: gunners ignore commands
        self.roe = {"copilot": "HOLD", "left": "HOLD", "right": "HOLD"}
        self.burst = {"copilot": "SHORT", "left": "SHORT", "right": "SHORT"}
        self.ammo = {"copilot": 100, "left": 100, "right": 100}
        self.dead = set()
        self.gunners = gunners
        self.gauges = {}                               # arg -> needle
        self.sent = []                                 # (command, value) received
        self.words = {}                                # DCS-BIOS words
        self.bios_log = []
        self.bios_responds = True
        self.flares = 30
        self._stop = False
        self.link = self
        self.bios = self
        for key, (ident, states, _, _) in SWITCHES.items():
            if key in HUEY_BIOS_INTS:
                self._set_field(key, 0)
        self._set_field("master_arm", 1)               # SAFE
        self._set_field("search_light", 1)             # OFF
        self._set_field("nav_lts", 1)                  # OFF
        self._set_field("main_gen", 1)
        self._set_field("inverter", 1)
        self._set_field("chip_det", 1)
        self._set_field("flare_count", self.flares)
        # radio dials (the radios follow them)
        self.uhf = [23, 2, 0]                          # 10 MHz group, MHz, 50 kHz index -> 232.00
        self.vhf = [124, 30]                           # MHz, 25 kHz index -> 124.750
        self.fm = [1, 0, 0, 0]                         # 40.00
        self.uhf_follows = True                        # False: mode dial not on manual
        self.uhf_inc_down = True                       # this sim's UHF INC turns the number DOWN
        self._radios()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # ---------------------------------------------------------- crew link (CommandLink API)
    def send(self, command, value=1.0, device=9):
        with self.lock:
            self.sent.append((command, round(value, 2)))
            if command == CMD_PANEL:
                self.panel_visible = not self.panel_visible
            for seat, cmd in CMD_SEAT.items():
                if command == cmd and self.respond and seat not in self.dead:
                    if round(value, 2) == 0.0:
                        self.roe[seat] = NEXT_ROE[self.roe[seat]]
                    else:
                        self.burst[seat] = "LONG" if self.burst[seat] == "SHORT" else "SHORT"
        threading.Timer(0.03, self.publish).start()
        return True

    def text(self):
        rows = [{"status": "PLAYER", "ammo": " -", "burst": "  -"}]
        for seat in ("copilot", "left", "right"):
            if seat in self.dead:
                rows.append({"status": "DEAD", "ammo": " -", "burst": "  -"})
            else:
                rows.append({"status": ROE_TEXT[self.roe[seat]], "ammo": f"{self.ammo[seat]}%",
                             "burst": self.burst[seat]})
        return panel_text(rows)

    def packet(self):
        with self.lock:
            args = ",".join(f"{a}={v:.4f}" for a, v in self.gauges.items())
            crew = ("---CREW---\n" + self.text()) if self.panel_visible and self.gunners else ""
            return f"HUEY 1\nac=UH-1H\nargs={args}\nsnares=30,{self.flares}\n{crew}"

    def publish(self):
        state_store.update_huey(parse_packet(self.packet()))

    def _heartbeat(self):
        while not self._stop:
            self.publish()
            state_store.mark_bios_frame()
            time.sleep(0.1)

    def stop(self):
        self._stop = True

    # ---------------------------------------------------------- DCS-BIOS (cmd_sender API)
    def _set_field(self, key, v):
        addr, mask, shift = HUEY_BIOS_INTS[key]
        w = self.words.get(addr, 0)
        self.words[addr] = (w & ~mask) | ((v << shift) & mask)
        state_store.update_cockpit_switches(addr, self.words[addr])

    def set_string(self, key, s):
        addr, length = HUEY_BIOS_STRINGS[key]
        s = s.ljust(length, "\0")[:length]
        for i in range(0, length, 2):
            lo = ord(s[i])
            hi = ord(s[i + 1]) if i + 1 < length else 0
            self.words[addr + i] = lo | (hi << 8)
            state_store.update_cockpit_switches(addr + i, self.words[addr + i])

    def _radios(self):
        u = self.uhf[0] * 10 + self.uhf[1] + self.uhf[2] * 0.05
        self.set_string("uhf_dial", f"{u:.2f}")
        if self.uhf_follows:
            self.set_string("uhf", f"{u:.3f}")
        v = self.vhf[0] + self.vhf[1] * 0.025
        self.set_string("vhf_dial", f"{v:.3f}")
        self.set_string("vhf_am", f"{v:.3f}")
        for key, val in zip(("fm_tens", "fm_units", "fm_tenths", "fm_fifty"), self.fm):
            self._set_field(key, val)

    def _dial(self, ident, arg):
        lim = {"UHF_10MHZ": (self.uhf, 0, 22, 39), "UHF_1MHZ": (self.uhf, 1, 0, 9),
               "UHF_50KHZ": (self.uhf, 2, 0, 19), "VHFCOMM_MHZ": (self.vhf, 0, 116, 149),
               "VHFCOMM_KHZ": (self.vhf, 1, 0, 39)}
        if ident in lim:
            arr, i, lo, hi = lim[ident]
            d = 1 if arg == "INC" else -1
            if ident.startswith("UHF") and self.uhf_inc_down:
                d = -d
            arr[i] = lo + (arr[i] - lo + d) % (hi - lo + 1)       # dials wrap around
        fm = {"VHFFM_FREQ1": 0, "VHFFM_FREQ2": 1, "VHFFM_FREQ3": 2, "VHFFM_FREQ4": 3}
        if ident in fm:
            self.fm[fm[ident]] = int(arg)
        self._radios()

    def send_command(self, ident, arg):
        self.bios_log.append((ident, arg))
        if not self.bios_responds:
            return True

        def apply():
            with self.lock:
                for key, (sid, _, _, _) in SWITCHES.items():
                    if sid == ident and key in HUEY_BIOS_INTS:
                        self._set_field(key, int(arg))
                if ident == "CM_FLARE_BTN" and arg == "1" and self.flares > 0:
                    armed = (self.words[HUEY_BIOS_INTS["cm_arm"][0]] & HUEY_BIOS_INTS["cm_arm"][1]) != 0
                    if armed:
                        self.flares -= 1
                        self._set_field("flare_count", self.flares)
                self._dial(ident, arg)
        threading.Timer(0.02, apply).start()
        return True


def reset_state():
    with state_store._lock:
        state_store.huey, state_store.huey_rx = {}, 0.0
        state_store.bios_words = {}
        state_store.aircraft = ""
        state_store.agl_ft, state_store.vs_fpm = -1.0, 0.0
