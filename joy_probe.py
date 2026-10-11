"""
joy_probe.py - find your PTT button.

    python joy_probe.py            list devices, then show every button you press for 30 s
    python joy_probe.py --winmm    same, using Windows' built-in API (what's used without pygame)
    python joy_probe.py --seconds 60

Uses the same code as the program (core/joystick_ptt.py), so the [index] and button numbers
it prints are exactly what config.py wants:
    JOY_NAME_CONTAINS = part of the name shown here
    JOY_DEVICE_INDEX  = the [index] shown here (only used if the name isn't found)
    JOY_BUTTON        = the button number shown when you press it (1-based)
"""
import argparse
import time

from core.joystick_ptt import make_backend


def main():
    ap = argparse.ArgumentParser(description="List joysticks and show button presses")
    ap.add_argument("--winmm", action="store_true", help="use the Windows winmm API instead of pygame")
    ap.add_argument("--seconds", type=float, default=30.0)
    a = ap.parse_args()

    be, note = make_backend("winmm" if a.winmm else "auto")
    if be is None:
        print(f"Can't read joysticks: {note}\nInstall pygame-ce:  pip install pygame-ce")
        return
    if note:
        print(f"({note})")
    print(f"Backend: {be.name}\n")
    devs = be.devices()
    if not devs:
        print("No joysticks found.")
        return
    for i, name, n in devs:
        print(f"  [{i}] {name}   ({n} buttons)")
    handles = {i: (name, be.open(i)) for i, name, _ in devs}

    print(f"\nPress buttons now (watching for {a.seconds:.0f} s, Ctrl+C to stop)...")
    held = set()
    end = time.monotonic() + a.seconds
    try:
        while time.monotonic() < end:
            for i, (name, h) in handles.items():
                n = next(nb for j, _, nb in devs if j == i) or 32
                for b in range(1, min(n, 128) + 1):
                    state = be.button(h, b)
                    key = (i, b)
                    if state and key not in held:
                        held.add(key)
                        print(f"  DOWN  button {b:3d}  on [{i}] {name}")
                    elif not state and key in held:
                        held.discard(key)
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    print("\nPut the name, index and button number into config.py (JOY_NAME_CONTAINS / "
          "JOY_DEVICE_INDEX / JOY_BUTTON).")


if __name__ == "__main__":
    main()
