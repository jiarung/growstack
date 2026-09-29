#!/usr/bin/env python3
"""Poll /health and log the fragmentation numbers over hours.

    tools/s3cam/health_soak.py http://<ip>              # every 60 s, stdout
    tools/s3cam/health_soak.py http://<ip> 30 h.csv     # every 30 s, also CSV

The symptom this exists for — /observation going slow and then dead after a
long uptime — takes hours to appear, so a single curl cannot see it. What
matters is not free memory but the LARGEST CONTIGUOUS BLOCK: /observation
ps_malloc()s one whole JPEG per call while still holding the previous one, so
PSRAM fragments even though nothing leaks. `psram_free` stays large the whole
way down; `psram_largest` is the number that falls. Leave this running, then
compare psram_largest at boot with its value when snapshots go slow. A fall
towards one JPEG (~200-400 KB) confirms the cause.

The argument is `http://<ip>`, the same shape power_soak.sh takes, because two
soak tools in one directory with opposite argument conventions is a trap: a
bare host would have been accepted there and rejected here, or worse, silently
produce a run of unreachable rows. A bare host is accepted too and normalised.

A failed sample is DATA, not an error: it prints `--` in the offending columns
and the run continues — power_soak.sh's vocabulary, so the two logs read the
same way and can be put side by side. A soak that dies at hour four tells you
nothing.
"""
import csv
import json
import os
import subprocess
import sys
import time

# ONE declaration of the schema. Header, row, CSV header and CSV row all derive
# from it, so adding a field is a one-line edit. The shell version of this
# script spelled the field list out six times, and its own comment warned that
# one missing entry would shift every later column and produce a log that was
# confidently wrong rather than visibly broken.
FIELDS = [
    # (json key, column header, width)
    ("uptime_s",      "UPTIME",     8),
    ("heap_free",     "HEAP_FREE",  10),
    ("heap_largest",  "HEAP_LRG",   10),
    ("psram_free",    "PSRAM_FREE", 11),
    ("psram_largest", "PSRAM_LRG",  11),
    ("die_c",         "DIE",        6),
    ("rssi",          "RSSI",       5),
]
MISSING = "--"


def normalise(base):
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base.rstrip("/")


def sample(url):
    """-> {key: value} with MISSING for anything the board did not supply.

    Never raises. Unreachable and unparseable are different answers and the
    caller prints both, but neither ends the run.

    VIA CURL, NOT urllib — and this is not a style choice. macOS gates local
    network access per binary: on the dev Mac (Darwin 25.x) a pyenv python3
    connecting to a board on the same subnet gets EHOSTUNREACH while curl in
    the same shell, at the same second, succeeds. The gateway is exempt, so a
    quick "can python reach the LAN" check passes and the real failure looks
    like a dead board. power_soak.sh has always used curl and has never hit
    this; that is the reason.
    """
    try:
        out = subprocess.run(["curl", "-s", "--max-time", "10", url],
                             capture_output=True, timeout=15).stdout
        d = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return {k: d.get(k, MISSING) for k, _, _ in FIELDS}


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    url = normalise(argv[1]) + "/health"
    every = float(argv[2]) if len(argv) > 2 else 60.0
    csv_path = argv[3] if len(argv) > 3 else None

    writer = None
    if csv_path:
        new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        fh = open(csv_path, "a", newline="")
        writer = csv.writer(fh)
        if new:
            writer.writerow(["t"] + [k for k, _, _ in FIELDS])

    print(f"{'TIME':19} " + " ".join(f"{h:>{w}}" for _, h, w in FIELDS))
    # Absolute deadlines, not sleep(every): a 10 s timeout on a hung board would
    # otherwise stretch the interval and the time axis would drift over a
    # multi-hour run — in a log whose whole value is a trend against uptime.
    due = time.monotonic()
    while True:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        row = sample(url)
        # An unreachable board is a SAMPLE, not a gap — it gets a row on both
        # outputs. Printing the outage to the terminal while leaving a hole in
        # the CSV would make "the board was down" and "the logger was not
        # running" the same shape in the file somebody analyses later, which is
        # the one distinction a soak log exists to preserve.
        if row is None:
            row = {k: MISSING for k, _, _ in FIELDS}
            note = "  <- unreachable"
        else:
            note = ""
        print(f"{now:19} " +
              " ".join(f"{row[k]:>{w}}" for k, _, w in FIELDS) + note)
        if writer:
            writer.writerow([now] + [row[k] for k, _, _ in FIELDS])
            fh.flush()          # hours of buffered rows must not die with the process
        sys.stdout.flush()
        due += every
        time.sleep(max(0.0, due - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
