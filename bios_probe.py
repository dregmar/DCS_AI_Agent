"""
Standalone DCS-BIOS diagnostic. Run it with George STOPPED (so only one thing is listening).

DIFF MODE (default, best for finding a switch):
  python bios_probe.py --json "C:\\Users\\<you>\\Saved Games\\DCS\\Scripts\\DCS-BIOS\\doc\\json\\AH-64D.json"
      1. Learns which BITS are noisy (gauges, clocks, lamps) while you touch nothing.
      2. Asks you to set a switch to state A, then state B.
      3. Prints only the quiet bits that changed, with the matching control names from the JSON.
  (--json is optional; without it you still get address/mask/shift candidates.)

LIVE MODE (filtered stream):
  python bios_probe.py --live [--json ...]

LOOK UP ADDRESSES IN THE JSON:
  python bios_probe.py --json <AH-64D.json> --lookup 0x803C 0x872A 0x86F4

SEARCH THE JSON BY NAME:
  python bios_probe.py --json <AH-64D.json> --find MASTER WAS SIGHT

SEND ONE COMMAND TO DCS-BIOS (UDP 7778):
  python bios_probe.py --send PLT_MASTER_ARM_BTN 1

Other flags:
  --showloud     in diff mode, also list bits that were classed as noisy
  --learn SEC    noise-learning time in seconds (default 5)
"""
import argparse
import json
import socket
import sys
import threading
import time

MCAST_GROUP = "239.255.50.10"
PORT_IN = 5010
PORT_OUT = 7778


# ---------------------------------------------------------------- JSON helpers

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_controls(path, keywords):
    """Print address/mask/shift/max for every control whose identifier contains a keyword."""
    data = load_json(path)
    kws = [k.upper() for k in keywords]
    for category, controls in data.items():
        for ident, ctl in controls.items():
            if any(k in ident.upper() for k in kws):
                print(f"\n{ident}   [{category}]  {ctl.get('description', '')}")
                for o in ctl.get("outputs", []):
                    addr = o.get("address")
                    if addr is None:
                        continue
                    print(f"   OUT address={addr} (0x{addr:04X}) mask=0x{o.get('mask', 0):04X} "
                          f"shift={o.get('shift_by')} max={o.get('max_value')}")
                for i in ctl.get("inputs", []):
                    print(f"   IN  {i}")


def build_index(path):
    """address -> list of (identifier, mask, shift, max_value, description)."""
    idx = {}
    for category, controls in load_json(path).items():
        for ident, ctl in controls.items():
            for o in ctl.get("outputs", []):
                a = o.get("address")
                if a is None:
                    continue
                idx.setdefault(a, []).append(
                    (ident, o.get("mask", 0), o.get("shift_by"),
                     o.get("max_value"), ctl.get("description", "")))
    return idx


def lookup(path, addrs):
    """Print every control whose OUTPUT address is in addrs."""
    idx = build_index(path)
    for a in addrs:
        entries = idx.get(a)
        if not entries:
            print(f"0x{a:04X}  (no control found at this address)")
            continue
        for ident, mask, shift, mx, desc in entries:
            print(f"0x{a:04X}  {ident}  {desc}\n"
                  f"        mask=0x{mask:04X} shift={shift} max={mx}")


def annotate(index, addr, bits):
    """Lines naming JSON controls at addr whose mask overlaps the changed bits."""
    if not index:
        return []
    lines = []
    for ident, mask, shift, mx, desc in index.get(addr, []):
        if mask == 0 or (mask & bits):
            lines.append(f"        -> {ident}  mask=0x{mask:04X} shift={shift} max={mx}  {desc}")
    return lines


# ---------------------------------------------------------------- sending

def send(ident, arg, port=PORT_OUT):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(f"{ident} {arg}\n".encode(), ("127.0.0.1", port))
    print(f"Sent '{ident} {arg}' to 127.0.0.1:{port}. Did the switch move in the cockpit?")


# ---------------------------------------------------------------- receiving

def _receiver(mem, chg, lock, stop, port=PORT_IN, group=MCAST_GROUP):
    """Background thread: parses the DCS-BIOS stream.
    mem {addr: 16-bit value}; chg {addr: OR of every bit that has changed since chg was cleared}."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))
    sock.settimeout(1.0)

    state, addr, count, low, sync = 0, 0, 0, 0, 0   # 0 = wait for sync
    while not stop.is_set():
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            time.sleep(0.1)
            continue
        with lock:
            for b in data:
                if state == 1:
                    addr = b
                    state = 2
                elif state == 2:
                    addr |= b << 8
                    state = 3 if addr != 0x5555 else 0
                elif state == 3:
                    count = b
                    state = 4
                elif state == 4:
                    count |= b << 8
                    state = 5 if count > 0 else 1
                elif state == 5:
                    low = b
                    count -= 1
                    state = 6
                elif state == 6:
                    val = low | (b << 8)
                    old = mem.get(addr)
                    if old is not None and old != val:
                        chg[addr] = chg.get(addr, 0) | (old ^ val)
                    mem[addr] = val
                    addr += 2
                    count -= 1
                    state = 5 if count > 0 else 1
                # sync detection AFTER normal byte processing (matches official parser)
                sync = sync + 1 if b == 0x55 else 0
                if sync == 4:
                    state, sync = 1, 0


def _bits_str(mask):
    return ",".join(str(i) for i in range(16) if mask & (1 << i))


def _describe(addr, old, new, bits):
    return (f"0x{addr:04X}: 0x{old:04X} -> 0x{new:04X}   "
            f"changed bits [{_bits_str(bits)}]  mask=0x{bits:04X}")


def listen(index=None, live=False, learn_sec=5.0, showloud=False):
    mem, chg, lock, stop = {}, {}, threading.Lock(), threading.Event()
    threading.Thread(target=_receiver, args=(mem, chg, lock, stop), daemon=True).start()

    print("Waiting for DCS-BIOS data (initial dump)...")
    time.sleep(3.0)
    with lock:
        if not mem:
            print("NO DATA. DCS not running / DCS-BIOS not exporting / "
                  "multicast blocked by firewall or wrong NIC.")
            stop.set()
            return
        chg.clear()

    print(f"Got {len(mem)} addresses. Learning noise for {learn_sec:.0f}s - "
          f"DON'T TOUCH ANY SWITCHES.")
    time.sleep(learn_sec)
    with lock:
        noise = dict(chg)          # addr -> bits that flicker on their own
        chg.clear()
    print(f"Ignoring noisy bits on {len(noise)} addresses (gauges, clocks, lamps).")
    print("(Noise is tracked per BIT, so a quiet switch sharing a word with a noisy lamp "
          "is still visible.)\n")

    if live:
        print("LIVE mode: printing only changes on quiet bits. Ctrl+C to stop.\n")
        with lock:
            seen = dict(mem)
        while True:
            time.sleep(0.1)
            with lock:
                now = dict(mem)
            for a, v in now.items():
                old = seen.get(a)
                if old is None:
                    seen[a] = v
                    continue
                if old != v:
                    bits = (old ^ v) & ~noise.get(a, 0) & 0xFFFF
                    if bits:
                        print(_describe(a, old, v, bits))
                        for line in annotate(index, a, bits):
                            print(line)
                    seen[a] = v

    # DIFF mode (default): snapshot A, snapshot B, show only what differs.
    while True:
        input("Put the switch in state A, then press Enter... ")
        with lock:
            snap_a = dict(mem)
        input("Flip it to state B, then press Enter... ")
        with lock:
            snap_b = dict(mem)

        quiet, quiet_new, loud = [], [], []
        for a, new in snap_b.items():
            old = snap_a.get(a)
            first_seen = old is None
            if first_seen:
                old = 0
            diff = (old ^ new) & 0xFFFF
            if not diff:
                continue
            n = noise.get(a, 0)
            q = diff & ~n & 0xFFFF
            if q:
                (quiet_new if first_seen else quiet).append((a, old, new, q))
            elif not first_seen:
                loud.append((a, old, new, diff & n))

        print("\n=== CANDIDATES (quiet bits that changed) ===")
        if not quiet and not quiet_new:
            print("  none - the switch may not be exported, or all its bits were flickering "
                  "during the learning phase")
        for a, old, new, bits in sorted(quiet):
            print("  " + _describe(a, old, new, bits))
            for line in annotate(index, a, bits):
                print(line)
        if quiet_new:
            print("\n  First seen in state B (old value unknown, assumed 0) - "
                  "repeat the A/B step to confirm:")
            for a, old, new, bits in sorted(quiet_new):
                print("  " + _describe(a, old, new, bits))
                for line in annotate(index, a, bits):
                    print(line)

        print(f"\n({len(loud)} addresses changed only on noisy bits; ignored.)")
        if showloud:
            for a, old, new, bits in sorted(loud):
                print("  noisy " + _describe(a, old, new, bits))
        print("\nCtrl+C to quit, or repeat with another switch/position.\n")


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DCS-BIOS diagnostic probe")
    ap.add_argument("--json", help="path to AH-64D.json from the DCS-BIOS doc\\json folder")
    ap.add_argument("--find", nargs="+", metavar="KEYWORD",
                    help="with --json: list controls whose identifier contains a keyword")
    ap.add_argument("--lookup", nargs="+", type=lambda x: int(x, 0), metavar="ADDR",
                    help="with --json: look up addresses, e.g. 0x803C 0x872A")
    ap.add_argument("--send", nargs=2, metavar=("IDENT", "ARG"),
                    help="send one command to UDP 7778")
    ap.add_argument("--live", action="store_true",
                    help="stream filtered changes instead of A/B diff")
    ap.add_argument("--showloud", action="store_true",
                    help="in diff mode, also list addresses that changed only on noisy bits")
    ap.add_argument("--learn", type=float, default=5.0,
                    help="noise-learning time in seconds (default 5)")
    a = ap.parse_args()

    if a.send:
        send(*a.send)
    elif a.json and a.lookup:
        lookup(a.json, a.lookup)
    elif a.json and a.find:
        find_controls(a.json, a.find)
    else:
        if (a.lookup or a.find) and not a.json:
            print("--lookup and --find need --json <path to AH-64D.json>")
            sys.exit(1)
        idx = build_index(a.json) if a.json else None
        try:
            listen(index=idx, live=a.live, learn_sec=a.learn, showloud=a.showloud)
        except (KeyboardInterrupt, EOFError):
            pass