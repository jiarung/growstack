#!/usr/bin/env python3
"""Find where the pan/tilt mechanically stops — one axis, one direction, safely.

    ./servo_probe.py http://<ip> --axis pan  --dir +
    ./servo_probe.py http://<ip> --axis tilt --dir - --step 25

Steps outward from the centre and pauses after every step. Enter continues;
's' or Ctrl-C stops. On stop — and on any error, and on an operator who walked
away — it RETREATS to the last width that was fine and leaves the axis holding
there. The measured stop is printed for the travel table in
docs/mlx90640/servo-wiring.md.

WHY IT RETREATS INSTEAD OF RELEASING
An earlier draft of servo-wiring.md said to send ?us=0 when the servo starts
buzzing. That is wrong once anything is mounted, and dangerous: servo.h's
second invariant explains that releasing an axis which is holding against
gravity makes it DROP, and what drops here is the camera and the thermal
module. Cutting the drive is the intuitive panic action and the wrong one —
backing off one step removes the stall while the axis keeps holding.

ONE AXIS, ONE DIRECTION, ONE INVOCATION
Deliberately not a sweep-everything tool. A tool that can drive two axes in a
loop is a tool that can wind a cable around the pan axis while you are looking
at the tilt one. The firmware enforces "never two axes at once" for current
reasons; this enforces "never two axes unattended" for cable reasons.

Uses argparse, unlike the other tools here, because its siblings' hand-rolled
`opt()` silently ignores flags it does not recognise. A mistyped --step on a
tool that drives a metal-geared servo into a hard stop should be an error, not
a default.
"""
import argparse
import json
import signal
import sys
import urllib.error
import urllib.request

CH = {"pan": 5, "tilt": 6}      # must match servo.h CH_PAN / CH_TILT
US_MIN, US_MAX = 600, 2400      # servo.h electrical span; firmware rejects outside
US_CENTER = 1500


def servo(base, ch, us, timeout=10):
    """One /servo command. Raises on anything the firmware refused."""
    url = f"{base.rstrip('/')}/servo?ch={ch}&us={us}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        doc = json.loads(r.read())
    if not doc.get("present"):
        raise RuntimeError("PCA9685 not present — check /i2c/scan for 0x40")
    if doc.get("set") != "ok":
        raise RuntimeError(f"firmware rejected ch={ch} us={us}: {doc.get('set')!r}")
    return doc


class Prober:
    def __init__(self, base, ch, name):
        self.base, self.ch, self.name = base, ch, name
        self.last_good = None       # the width we retreat TO

    def step_to(self, us):
        servo(self.base, self.ch, us)

    def retreat(self, why):
        """The one recovery path. Never releases — see the module docstring."""
        if self.last_good is None:
            print(f"\n[{why}] nothing commanded yet; leaving the axis alone.")
            return
        print(f"\n[{why}] retreating {self.name} to last good {self.last_good}us "
              f"(NOT releasing — a released axis holding weight drops)")
        try:
            servo(self.base, self.ch, self.last_good)
        except Exception as e:                                   # noqa: BLE001
            # Retreat is the safety path; if it fails the operator must know
            # immediately and in plain words, not via a traceback.
            print(f"  !! RETREAT FAILED: {e}\n"
                  f"  !! cut the servo rail by hand if the axis is straining.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base_url", help="e.g. http://192.168.50.126")
    ap.add_argument("--axis", choices=sorted(CH), required=True)
    ap.add_argument("--dir", choices=["+", "-"], required=True,
                    help="which way to walk from --start")
    ap.add_argument("--start", type=int, default=US_CENTER,
                    help=f"first width (default {US_CENTER}, the centre)")
    ap.add_argument("--step", type=int, default=50,
                    help="microseconds per step (default 50)")
    ap.add_argument("--margin", type=int, default=100,
                    help="how far inside the measured stop the WORKING limit "
                         "sits (default 100)")
    a = ap.parse_args()

    if not US_MIN <= a.start <= US_MAX:
        ap.error(f"--start must be within the electrical span {US_MIN}..{US_MAX}")
    if a.step < 1:
        ap.error("--step must be positive; --dir chooses the direction")

    ch = CH[a.axis]
    sign = 1 if a.dir == "+" else -1
    p = Prober(a.base_url, ch, a.axis)

    # Ctrl-C must retreat, not just exit — the axis is under load right now.
    signal.signal(signal.SIGINT, lambda *_: (p.retreat("Ctrl-C"), sys.exit(130)))

    print(f"probing {a.axis} (ch{ch}) from {a.start}us, {sign * a.step:+}us per step")
    print("Enter = next step   s = stop here   (the axis keeps holding throughout)\n")

    us = a.start
    try:
        while True:
            if not US_MIN <= us <= US_MAX:
                print(f"reached the ELECTRICAL limit ({US_MIN}..{US_MAX}) before a "
                      f"mechanical one — the bracket is not the constraint here.")
                break
            p.step_to(us)
            p.last_good = us
            reply = input(f"  {a.axis} @ {us}us  [Enter/s] ").strip().lower()
            if reply.startswith("s"):
                break
            us += sign * a.step
    except EOFError:
        p.retreat("stdin closed")
        return 1
    except Exception as e:                                       # noqa: BLE001
        print(f"\nERROR: {e}")
        p.retreat("error")
        return 1

    stop = p.last_good
    working = stop - sign * a.margin
    print(f"\n{a.axis} {a.dir} mechanical stop:  {stop} us")
    print(f"{a.axis} {a.dir} WORKING limit:     {working} us  "
          f"(stop {'-' if sign > 0 else '+'} {a.margin} margin)")
    print("\nPut BOTH numbers in the travel table in docs/mlx90640/servo-wiring.md.\n"
          "Scans use the working limit only: never approach a measured hard stop\n"
          "during a run, and keep poses off the ends so cable tension — a\n"
          "position-dependent load torque — stays roughly constant across them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
