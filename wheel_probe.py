"""
Wheel probe: press ONE George wheel key so you can see what it does in DCS.
Use it to check the left/right/up/down mapping against your own game.

  1. In DCS, sit in the pilot seat, press Ctrl+V so the George wheel is showing.
  2. Run one of these, then click back into DCS during the countdown:

     python wheel_probe.py left            short press of LEFT
     python wheel_probe.py right long      long press of RIGHT
     python wheel_probe.py --all           walks all 8 presses, one every --gap seconds

  Watch the wheel / the WPN page. Note what each press does, then compare with the table in
  core/george_wheel.py. If LEFT and RIGHT do each other's job, set in config.py:
     WHEEL_KEYS = {"LEFT": "d", "RIGHT": "a"}
  (or whatever keys your DCS bindings actually use).
"""
import argparse
import time

from pynput.keyboard import Controller

from core import george_wheel as gw

KEYS = {"up": gw.UP, "down": gw.DOWN, "left": gw.LEFT, "right": gw.RIGHT}


def press(kb, direction: str, long: bool) -> None:
    hold = gw.WHEEL_LONG_HOLD_SEC if long else gw.WHEEL_SHORT_HOLD_SEC
    key = KEYS[direction]
    print(f"   -> {direction.upper()} {'LONG' if long else 'short'}  (key '{key}', held {hold:.2f}s)")
    kb.press(key)
    time.sleep(hold)
    kb.release(key)


def countdown(n: int) -> None:
    for i in range(n, 0, -1):
        print(f"   click into DCS... {i}", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Press one George wheel key and watch DCS")
    ap.add_argument("direction", nargs="?", choices=list(KEYS))
    ap.add_argument("length", nargs="?", choices=["short", "long"], default="short")
    ap.add_argument("--all", action="store_true", help="walk all 8 presses")
    ap.add_argument("--delay", type=int, default=5, help="countdown seconds before the first press")
    ap.add_argument("--gap", type=float, default=6.0, help="seconds between presses with --all")
    a = ap.parse_args()
    if not a.all and not a.direction:
        ap.error("give a direction (up/down/left/right) or --all")

    kb = Controller()
    print(f"Keys in use: {KEYS}")
    countdown(a.delay)
    if a.all:
        for d in ("left", "right", "up", "down"):
            for long in (False, True):
                press(kb, d, long)
                time.sleep(a.gap)
    else:
        press(kb, a.direction, a.length == "long")
    print("Done.")