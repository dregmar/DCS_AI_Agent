import socket
import threading
import time

from config import (DCS_BIOS_IN_PORT, DCS_BIOS_OUT_PORT, MASTER_ARM_IDENT,
                    MASTER_ARM_MODE, BIOS_PRESS_HOLD_SEC, WAS_HAT)
from core.state import state_store, WATCHED_ADDRESSES

DCS_BIOS_MCAST_GROUP = "239.255.50.10"


class DCSBIOSCommandSender:
    """Handles outgoing UDP control commands to DCS-BIOS on port 7778."""

    def __init__(self, host: str = "127.0.0.1", port: int = DCS_BIOS_OUT_PORT):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send_command(self, identifier: str, argument: str) -> bool:
        """
        Sends a text-based control command to DCS-BIOS.
        Example: send_command("PLT_MASTER_ARM", "1")
        Returns True if the datagram was handed to the OS (NOT proof DCS acted on it).
        """
        payload = f"{identifier} {argument}\n".encode("utf-8")
        try:
            self.sock.sendto(payload, (self.host, self.port))
            print(f"[DCS-BIOS OUT] Sent: {identifier} {argument}")
            return True
        except Exception as e:
            print(f"[DCS-BIOS ERROR] Failed to send command: {e}")
            return False

    # Identifiers verified against Scripts/DCS-BIOS/doc/json/AH-64D.json. A wrong identifier is
    # silently ignored by DCS-BIOS.
    def press_button(self, identifier: str) -> bool:
        """Momentary press: 1, short hold, 0."""
        ok = self.send_command(identifier, "1")
        time.sleep(BIOS_PRESS_HOLD_SEC)
        return self.send_command(identifier, "0") and ok

    def set_master_arm(self, state: str) -> bool:
        """state: 'ARM' or 'DISARM'.
        MASTER_ARM_MODE == "set_state": sends 1 (ARM) / 0 (SAFE) directly.
        MASTER_ARM_MODE == "pulse":     toggles with a press. The caller MUST only call this
                                        when the current state differs from the wanted one."""
        want_arm = state.upper() in ["ARM", "ARMED", "1"]
        if MASTER_ARM_MODE == "pulse":
            return self.press_button(MASTER_ARM_IDENT)
        return self.send_command(MASTER_ARM_IDENT, "1" if want_arm else "0")

    def set_was_mode(self, mode: str, current: str = None) -> bool:
        """Push the pilot's Weapons Action Switch (cyclic hat) for 'GUN' / 'MSL' / 'RKT'.
        'OFF' de-actions by pushing the CURRENTLY actioned direction again (needs `current`).
        The hat is momentary: push to the direction, hold briefly, release to centre (1)."""
        mode = mode.upper()
        if mode in ("OFF", "NONE"):
            if current not in WAS_HAT:
                print("[DCS-BIOS] WAS off: current weapon unknown, not pressing")
                return False
            mode = current
        if mode not in WAS_HAT:
            return False
        ident, value = WAS_HAT[mode]
        ok = self.send_command(ident, value)
        time.sleep(BIOS_PRESS_HOLD_SEC)
        return self.send_command(ident, "1") and ok


# Instantiate single outgoing command sender
cmd_sender = DCSBIOSCommandSender()

# Parser states (mirrors the official DCS-BIOS stream parser)
_WAIT_SYNC, _ADDR_LO, _ADDR_HI, _COUNT_LO, _COUNT_HI, _DATA_LO, _DATA_HI = range(7)


class DCSBIOSProtocolParser:
    """Parses incoming binary memory streams from DCS-BIOS (UDP 5010).

    Fix vs. the previous version: every byte is run through the state machine FIRST,
    and only then is the 4x0x55 sync check applied. Previously any data byte equal to
    0x55 was swallowed, corrupting addresses/values.
    """

    def __init__(self):
        self.state = _WAIT_SYNC
        self.address = 0
        self.count = 0
        self.sync_count = 0
        self.data_low = 0

    def process_byte(self, b: int) -> None:
        s = self.state
        if s == _ADDR_LO:
            self.address = b
            self.state = _ADDR_HI
        elif s == _ADDR_HI:
            self.address |= (b << 8)
            self.state = _COUNT_LO if self.address != 0x5555 else _WAIT_SYNC
        elif s == _COUNT_LO:
            self.count = b
            self.state = _COUNT_HI
        elif s == _COUNT_HI:
            self.count |= (b << 8)
            self.state = _DATA_LO if self.count > 0 else _ADDR_LO
        elif s == _DATA_LO:
            self.data_low = b
            self.count -= 1
            self.state = _DATA_HI
        elif s == _DATA_HI:
            if self.address in WATCHED_ADDRESSES:
                state_store.update_cockpit_switches(self.address, self.data_low | (b << 8))
            self.address += 2
            self.count -= 1
            self.state = _DATA_LO if self.count > 0 else _ADDR_LO

        # Sync detection happens after normal processing
        if b == 0x55:
            self.sync_count += 1
        else:
            self.sync_count = 0
        if self.sync_count == 4:
            self.state = _ADDR_LO
            self.sync_count = 0


def listen_dcs_bios() -> None:
    """Background thread listening for incoming DCS-BIOS multicast telemetry."""
    parser = DCSBIOSProtocolParser()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(("", DCS_BIOS_IN_PORT))

    mreq = socket.inet_aton(DCS_BIOS_MCAST_GROUP) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print(f"[DCS-BIOS] Listening for incoming telemetry on multicast port {DCS_BIOS_IN_PORT}...")

    last_err = 0.0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            state_store.mark_bios_frame()
            for byte in data:
                parser.process_byte(byte)
        except Exception as e:
            now = time.monotonic()
            if now - last_err > 5.0:
                print(f"[DCS-BIOS ERROR] {type(e).__name__}: {e}")
                last_err = now
            time.sleep(0.1)   # back off so a persistent fault can't busy-spin


def start_dcs_bios_thread() -> threading.Thread:
    """Starts the incoming listener thread as a daemon."""
    thread = threading.Thread(target=listen_dcs_bios, daemon=True)
    thread.start()
    return thread