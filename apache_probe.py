"""
apache_probe.py - check the AH-64D wiring in one short session (run it INSTEAD of main.py).

    python apache_probe.py

Sit in the Apache pilot seat and:
  1. Watch the EUFD lines it prints: tell Claude which lines hold the warnings / cautions /
     advisories (the program assumes the top 6 lines, three 18-character columns).
  2. Move each power lever OFF -> IDLE -> FLY: it prints the raw position (0..65535) and what
     George calls it. If IDLE or FLY is misread, the boundaries are PL_IDLE_RANGE in
     apache/bios_map.py.
  3. Flip master ignition, rotor brake, parking brake, canopy, lights, CMWS switches: it prints
     each one's position as George reads it.
Ctrl+C to stop. Nothing is sent to DCS.
"""
import socket
import threading
import time

from config import DCS_BIOS_IN_PORT
from core.dcs_bios import DCSBIOSProtocolParser, DCS_BIOS_MCAST_GROUP
from core.state import state_store
from apache.bios_map import APACHE_SWITCHES, POWER_LEVERS, eufd, power_lever, switch_state
from apache.eufd import messages


def bios_thread():
    parser = DCSBIOSProtocolParser()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", DCS_BIOS_IN_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(DCS_BIOS_MCAST_GROUP) + socket.inet_aton("0.0.0.0"))
    while True:
        data, _ = s.recvfrom(65535)
        state_store.mark_bios_frame()
        for b in data:
            parser.process_byte(b)


def main():
    threading.Thread(target=bios_thread, daemon=True).start()
    print(__doc__)
    last_lines, last_sw, last_pl = None, {}, {}
    try:
        while True:
            time.sleep(0.3)
            w = state_store.bios_words_snapshot()
            for key in APACHE_SWITCHES:
                st = switch_state(w, key)
                if st is not None and last_sw.get(key) != st:
                    print(f"[SWITCH] {key:11s} -> {st}")
                    last_sw[key] = st
            for key, addr in POWER_LEVERS.items():
                raw = w.get(addr)
                if raw is not None and abs(last_pl.get(key, -99999) - raw) > 1500:
                    print(f"[LEVER]  {key} raw {raw:5d} ({raw / 655.35:3.0f}%) -> {power_lever(w, key)}")
                    last_pl[key] = raw
            lines = eufd(w)
            if all(l is not None for l in lines) and lines != last_lines:
                print("[EUFD] ---------------------------------------------------------")
                for i, l in enumerate(lines, 1):
                    print(f"[EUFD] {i:2d} |{l}|")
                print(f"[EUFD] George reads: {messages(lines)}")
                last_lines = lines
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
