import json
import socket
import threading
import time

from config import UDP_TELEMETRY_PORT
from core.state import state_store


def listen_telemetry():
    """Background listener for Export.lua UDP packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(("127.0.0.1", UDP_TELEMETRY_PORT))
    print(f"[TELEMETRY] Listening on UDP port {UDP_TELEMETRY_PORT}...")

    last_err = 0.0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            if data:
                payload = json.loads(data.decode("utf-8"))
                if isinstance(payload, dict):
                    state_store.update_telemetry(payload)
        except ValueError as e:
            # Malformed/garbled packet (JSONDecodeError and UnicodeDecodeError are both
            # ValueErrors). Drop it and keep listening with no delay.
            now = time.monotonic()
            if now - last_err > 5.0:
                print(f"[TELEMETRY] Bad packet dropped: {type(e).__name__}: {e}")
                last_err = now
        except Exception as e:
            now = time.monotonic()
            if now - last_err > 5.0:
                print(f"[TELEMETRY ERROR] {type(e).__name__}: {e}")
                last_err = now
            time.sleep(0.1)   # back off so a persistent socket fault can't busy-spin


def start_telemetry_thread() -> threading.Thread:
    thread = threading.Thread(target=listen_telemetry, daemon=True)
    thread.start()
    return thread