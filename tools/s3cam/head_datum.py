#!/usr/bin/env python3
"""Where this particular head sits level — per axis, recorded, with provenance.

NOT the same thing as servo.h's US_CENTER.

US_CENTER is 1500 because that is what a neutral RC pulse MEANS. It is a fact
about the protocol and it belongs to every servo ever made. The datum is the
width at which THIS head is level, which depends on which spline tooth the
horn was pressed onto, differs per axis, and changes the next time anybody
takes the horn off. Folding the second into the first would leave "neutral"
meaning two things and a remounted horn silently redefining the protocol.

So the datum is measured, written down, and carried into every recording that
depended on it — like any other calibration in this repo. An axis nobody has
measured falls back to 1500 and is reported as a DEFAULT, never as a
measurement, for the same reason the viewer labels an un-commanded servo width
"assumed": a number whose provenance is unstated is a number somebody will
later treat as evidence.

    from head_datum import Datum
    d = Datum.load()                       # docs/mlx90640/head-datum.json
    us, measured = d.us("pan")             # (1513, True) or (1500, False)
"""
import datetime
import json
import os

DEFAULT_US = 1500          # servo.h US_CENTER: the protocol's neutral, not a datum
US_MIN, US_MAX = 600, 2400
AXES = ("pan", "tilt")
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", "docs", "mlx90640", "head-datum.json")


class Datum:
    VERSION = 1

    def __init__(self, axes=None):
        self.axes = dict(axes or {})

    # --- reading ------------------------------------------------------------

    def us(self, axis):
        """-> (width, measured). `measured` False means this is only the default."""
        e = self.axes.get(axis)
        if not e:
            return DEFAULT_US, False
        return int(e["us"]), True

    def describe(self, axis):
        us, measured = self.us(axis)
        if not measured:
            return f"{us} us (DEFAULT, not measured)"
        e = self.axes[axis]
        note = f" — {e['note']}" if e.get("note") else ""
        return f"{us} us (measured {e.get('t', '?')}{note})"

    def as_recorded(self):
        """What goes into a run manifest: the widths AND whether each is real."""
        return {a: {"us": self.us(a)[0], "measured": self.us(a)[1]} for a in AXES}

    # --- writing ------------------------------------------------------------

    def set(self, axis, us, note=""):
        if axis not in AXES:
            raise SystemExit(f"unknown axis {axis!r}; expected one of {AXES}")
        if not US_MIN <= int(us) <= US_MAX:
            raise SystemExit(f"{axis} datum {us} is outside the electrical span "
                             f"{US_MIN}..{US_MAX}")
        # A new object, not a mutation: the loaded record stays what was on disk
        # until something deliberately writes it back.
        out = dict(self.axes)
        out[axis] = {"us": int(us), "note": note,
                     "t": datetime.datetime.now().astimezone().isoformat(
                         timespec="seconds")}
        return Datum(out)

    def save(self, path=None):
        path = path or DEFAULT_PATH
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"version": self.VERSION, "axes": self.axes}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)      # a crash mid-write must not eat the record
        return path

    # --- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path=None):
        path = path or DEFAULT_PATH
        if not os.path.exists(path):
            return cls()
        with open(path) as fh:
            d = json.load(fh)
        if d.get("version") != cls.VERSION:
            raise SystemExit(f"{path}: version {d.get('version')}, this code "
                             f"writes {cls.VERSION} — migrate it deliberately")
        return cls(d.get("axes"))

    @classmethod
    def from_cli(cls, text, base=None):
        """'pan=1513,tilt=1498' -> a Datum layered over `base`.

        An override names the axes it knows about and leaves the rest alone,
        because measuring one axis is the normal case and silently resetting
        the other to 1500 would undo a measurement by omission.
        """
        out = cls(dict(base.axes) if base else {})
        if not text:
            return out
        for part in text.split(","):
            if not part.strip():
                continue
            try:
                axis, us = part.split("=", 1)
                us = int(us)
            except ValueError:
                raise SystemExit(f"--datum wants axis=us pairs (got {part!r})")
            out = out.set(axis.strip(), us, note="--datum")
        return out
