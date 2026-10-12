"""
huey/link.py
The UH-1H crew link with Saved Games\\DCS\\Scripts\\george_huey_export.lua:
  * listens on UDP 5008 for gauges, lamps, flares and the Crew Status panel text and stores
    them in the state store (only while you fly the Huey; the script sends nothing otherwise);
  * sends crew commands to UDP 5009. The script only accepts the weapon-system commands the
    game's own key bindings use (autopilot, Crew Status panel, gunner ROE / burst).
"""
import socket
import threading
import time

import config
from core.state import state_store
from huey.gauges import convert, parse_args
from huey.panel import parse_panel

HUEY_DATA_PORT = getattr(config, "HUEY_DATA_PORT", 5008)
HUEY_CMD_PORT = getattr(config, "HUEY_CMD_PORT", 5009)

# Weapon system (devices.WEAPON_SYS = 9) commands, from Input/UH-1H/keyboard/default.lua.
WEAPON_SYS = 9
CMD_AUTOPILOT = 3026            # on / off
CMD_AP_ATTITUDE = 3028          # ATTITUDE HOLD
CMD_AP_LEVEL = 3029             # LEVEL FLIGHT
CMD_AP_ORBIT = 3030             # ORBIT
CMD_PANEL = 3037                # Crew Status panel show / hide
CMD_SEAT = {"left": 3038, "copilot": 3039, "right": 3040}
VALUE_ROE, VALUE_BURST = 0.0, 0.1


def parse_packet(text: str) -> dict:
    """'HUEY 1\\nac=..\\nargs=..\\nsnares=c,f\\n---CREW---\\n<panel text>' -> dict for the state store."""
    head, _, crew = text.partition("---CREW---\n")
    fields = {}
    for line in head.splitlines()[1:]:
        k, _, v = line.partition("=")
        fields[k.strip()] = v.strip()
    data = convert(parse_args(fields.get("args", "")))
    data["aircraft"] = fields.get("ac", "UH-1H")
    chaff = flare = None
    try:
        c, f = (int(x) for x in fields.get("snares", "").split(","))
        chaff, flare = (c if c >= 0 else None), (f if f >= 0 else None)
    except ValueError:
        pass
    data["chaff"], data["flares"] = chaff, flare
    data["panel"] = parse_panel(crew)
    data["panel_seen"] = bool(data["panel"])
    return data


class CommandLink:
    """Sends 'C <device> <command> <value>' datagrams to the export script."""

    def __init__(self, port: int = HUEY_CMD_PORT):
        self.addr = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, command: int, value: float = 1.0, device: int = WEAPON_SYS) -> bool:
        try:
            self.sock.sendto(f"C {device} {command} {value:.2f}\n".encode(), self.addr)
            print(f"[HUEY OUT] device {device} command {command} value {value:.2f}")
            return True
        except OSError as e:
            print(f"[HUEY ERROR] send failed: {e}")
            return False


def listen_huey() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(("127.0.0.1", HUEY_DATA_PORT))
    print(f"[HUEY] Listening on UDP port {HUEY_DATA_PORT}...")
    last_err, announced = 0.0, False
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            text = data.decode("utf-8", errors="replace")
            if not text.startswith("HUEY"):
                continue
            state_store.update_huey(parse_packet(text))
            if not announced:
                announced = True
                print("[HUEY] UH-1H crew link is live.")
        except Exception as e:
            now = time.monotonic()
            if now - last_err > 5.0:
                print(f"[HUEY ERROR] {type(e).__name__}: {e}")
                last_err = now
            time.sleep(0.1)


def start_huey_link_thread() -> threading.Thread:
    t = threading.Thread(target=listen_huey, daemon=True, name="huey-link")
    t.start()
    return t
