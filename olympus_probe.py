"""
olympus_probe.py - check that George can talk to DCS Olympus (run it INSTEAD of main.py).

    python olympus_probe.py            login, paths and data (read only)
    python olympus_probe.py --smoke    also pops one green smoke 50 m in front of you (to check
                                       that commands work; needs DCS flying, Export.lua running)

Run it in a mission with Olympus loaded (the DCS Olympus mod enabled; the web interface doesn't
need to be open). Settings: OLYMPUS_* in config.py. Send the output to Claude.
"""
import argparse
import base64
import hashlib
import json
import socket
import sys
import time
import urllib.error
import urllib.request

import config
from common import geo, olympus


def raw(method, url, auth=None, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, "", b""
    except Exception as e:
        return None, "", str(e).encode()


def telemetry(seconds=5.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", config.UDP_TELEMETRY_PORT))
    s.settimeout(seconds)
    try:
        data, _ = s.recvfrom(65535)
        return json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  no telemetry from Export.lua ({e}). Is DCS flying, and main.py closed?")
        return None
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    url, user, pw = config.OLYMPUS_URL.rstrip("/"), config.OLYMPUS_USER, config.OLYMPUS_PASSWORD
    print(f"Olympus at {url}, user '{user}', password {'set' if pw else 'EMPTY'}\n")

    print("1. Login and paths (HTTP status per try):")
    for name, secret in (("sha256", hashlib.sha256(pw.encode()).hexdigest()), ("plain", pw), ("none", None)):
        auth = None if secret is None else "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
        line = []
        for path in ("/olympus/mission", "/olympus/units", "/olympus/bullseyes", "/olympus/airbases",
                     "/olympus/logs", "/olympus/spots"):
            st, ctype, body = raw("GET", url + path, auth)
            line.append(f"{path} {st}" + (f" ({len(body)} B {ctype.split(';')[0]})" if st == 200 else ""))
        print(f"   {name:6s}: " + " | ".join(line))
        if st is None:
            print(f"   (not reachable: {body.decode(errors='replace')[:120]})")
            break

    oly = olympus.Olympus(url, user, pw)
    if not oly.connect(force=True):
        print(f"\nNot connected: {oly.last_error}")
        return 1
    print("\n2. Mission data:")
    try:
        m = oly.get("/olympus/mission")
        if isinstance(m, dict):
            for k, v in m.items():
                desc = f"{type(v).__name__} ({len(v)})" if isinstance(v, (dict, list)) else repr(v)[:80]
                print(f"   {k}: {desc}")
            if isinstance(m.get("spots"), dict) and m["spots"]:
                print("   spots sample:", json.dumps(list(m["spots"].items())[:2])[:300])
        else:
            print("   (not JSON)", str(m)[:200])
    except Exception as e:
        print("   failed:", e)

    print("\n3. Export.lua (what George will use for orders):")
    tel = telemetry()
    if tel:
        print(f"   you: {tel.get('unit')} in group {tel.get('group')}, object id {tel.get('self_id')}, "
              f"lat/lon {tel.get('ll')}")
        print(f"   wingmen seen: {tel.get('wing')}")
        cs = tel.get("contacts") or []
        print(f"   contacts: {len(cs)}" + (f", e.g. {cs[0]}" if cs else ""))
    if a.smoke:
        print("\n4. Test command: green smoke 50 m ahead of you")
        if not tel or not tel.get("ll"):
            print("   no position: skipped")
        else:
            n, e = geo.body_to_ne(tel.get("hdg") or 0.0, 50.0, 0.0)
            lat, lon = geo.offset_ll(tel["ll"][0], tel["ll"][1], n, e)
            ok, why = oly.command("smoke", {"color": "green", "location": {"lat": lat, "lng": lon}})
            print(f"   sent: {ok} {why} (command path {oly._cmd_path}). Look for green smoke in front of you.")
    print("\nSend this output to Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
