"""
huey_probe.py - check the UH-1H wiring in one short flight (run it INSTEAD of main.py).

    python huey_probe.py

Sit in the Huey (engine off is fine) and:
  1. Flip each overhead / pedestal switch the Huey crew uses (battery, starter-gen, main fuel,
     force trim, hydraulics, inverter, pitot heat, landing light, flares ARM...). The probe prints
     the switch name, the raw DCS-BIOS value and what George thinks that means, e.g.
         battery = 1  -> ON
     If George's meaning is wrong for any switch, tell Claude (or fix huey/cockpit.py SWITCHES).
  2. Press LWin+H (or your binding) to show the gunners' Crew Status panel: the probe prints
     each crew row as George reads it.
  3. Watch the gauges line: rotor / engine RPM, EGT, torque, fuel as George converts them.
Ctrl+C to stop. Nothing is ever sent to DCS.
"""
import socket
import threading
import time

from core.dcs_bios import DCSBIOSProtocolParser, DCS_BIOS_MCAST_GROUP
from config import DCS_BIOS_IN_PORT
from core.state import state_store
from huey.bios_map import HUEY_BIOS_INTS, decode_int, decode_string
from huey.cockpit import SWITCHES, switch_state
from huey.link import HUEY_DATA_PORT, parse_packet


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


def huey_thread():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", HUEY_DATA_PORT))
    while True:
        data, _ = s.recvfrom(65535)
        text = data.decode("utf-8", errors="replace")
        if text.startswith("HUEY"):
            state_store.update_huey(parse_packet(text))


def main():
    threading.Thread(target=bios_thread, daemon=True).start()
    threading.Thread(target=huey_thread, daemon=True).start()
    print(__doc__)
    last_sw, last_panel, last_gauge, last_radio = {}, None, 0.0, {}
    try:
        while True:
            time.sleep(0.2)
            words = state_store.bios_words_snapshot()
            for key in HUEY_BIOS_INTS:
                v = decode_int(words, key)
                if v is not None and last_sw.get(key) != v:
                    meaning = switch_state(words, key) if key in SWITCHES else "-"
                    flag = "  (assumed, please confirm)" if key in SWITCHES and SWITCHES[key][3] else ""
                    print(f"[SWITCH] {key:14s} = {v}  -> {meaning}{flag}")
                    last_sw[key] = v
            for key in ("uhf", "vhf_am", "vhf_fm", "uhf_dial", "vhf_dial"):
                f = decode_string(words, key)
                if f and last_radio.get(key) != f:
                    print(f"[RADIO]  {key} = {f}")
                    last_radio[key] = f
            h, rx = state_store.huey_snapshot()
            if not rx:
                continue
            panel = h.get("panel")
            if panel != last_panel:
                print("[PANEL] " + ("not readable (hidden?)" if not panel else
                                    " | ".join(f"{s}: {r}" for s, r in panel.items())))
                last_panel = panel
            if time.monotonic() - last_gauge > 3:
                last_gauge = time.monotonic()
                print(f"[GAUGES] rotor {h.get('rotor_rpm')} engine {h.get('engine_rpm')} N1 {h.get('n1_pct')} "
                      f"EGT {h.get('egt_c')} torque {h.get('torque_psi')} fuel {h.get('fuel_lbs')} lbs "
                      f"DC {h.get('dc_volts')} AC {h.get('ac_volts')} flares {h.get('flares')} "
                      f"lamps {h.get('lamps')}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
