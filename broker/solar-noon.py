#!/usr/bin/env python3
"""Solar noon for this installation's coordinates. NOAA algorithm, no dependencies.

    ./solar-noon.py                    # today, human-readable
    ./solar-noon.py 2026-08-17         # a given date
    ./solar-noon.py --noon-epoch       # epoch seconds, for a shell caller
    ./solar-noon.py --noon-epoch 2026-08-17

Location and timezone come from broker/.env — WEATHER_LAT, WEATHER_LON and
LIGHT_TZ — so there is nothing to configure here and nothing to keep in sync.
LIGHT_TZ is the same var the light controller evaluates its window in, which is
what makes a solar-noon window comparable to a lamp-schedule window.

Precision note: longitude moves solar noon by 4 minutes per degree, i.e. 2.4
seconds per 0.01 deg. Banqiao spans ~0.07 deg end to end, so 17 seconds covers
any point in the district — the exact address does not matter at the precision
anyone here can act on.

The algorithm evaluates the equation of time and declination at clock noon
rather than iterating to the solar-noon instant it computes. Over the ~2 minute
gap the declination moves ~0.0006 deg; deliberately not corrected.
"""
import argparse
import datetime as dt
import math
import os
import re
import sys
from zoneinfo import ZoneInfo

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def envval(key, default):
    """Bare value from .env: tolerates surrounding quotes, whitespace, inline comments.

    Mirrors the envval() shell function in ppfd-cal-daily.sh — the two read the
    same file and must agree on what a value is.
    """
    try:
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^%s=[ \t]*"?([^"#\s]+)' % re.escape(key), line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return default


def _jday(d):
    """Julian day at 00:00 UT of a civil date."""
    y, m = d.year, d.month
    if m <= 2:
        y, m = y - 1, m + 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d.day + b - 1524.5


def _sun(d, tz_hours):
    """(equation_of_time_minutes, declination_degrees) near local noon on date d."""
    jd = _jday(d) + (12.0 - tz_hours) / 24.0
    t = (jd - 2451545.0) / 36525.0

    L0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    M = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * Mr) * 0.000289)
    omega = 125.04 - 1934.136 * t
    app_long = L0 + C - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    secs = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    eps = (23.0 + (26.0 + secs / 60.0) / 60.0) + 0.00256 * math.cos(math.radians(omega))

    dec = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(app_long))))

    y = math.tan(math.radians(eps / 2.0)) ** 2
    L0r = math.radians(L0)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * L0r)
        - 2 * e * math.sin(Mr)
        + 4 * e * y * math.sin(Mr) * math.cos(2 * L0r)
        - 0.5 * y * y * math.sin(4 * L0r)
        - 1.25 * e * e * math.sin(2 * Mr))
    return eot, dec


def solar_noon(date=None):
    """Return (aware datetime of solar noon, max altitude in degrees)."""
    lat = float(envval("WEATHER_LAT", "25.01"))
    lon = float(envval("WEATHER_LON", "121.46"))
    zone = ZoneInfo(envval("LIGHT_TZ", "Asia/Taipei"))

    d = date or dt.datetime.now(zone).date()
    midnight = dt.datetime(d.year, d.month, d.day, tzinfo=zone)
    # The NOAA expression wants the zone as a fixed hour offset. Take it from the
    # zone itself on this date rather than hardcoding +8, so the value tracks
    # whatever LIGHT_TZ says — including a zone that observes DST.
    tz_hours = midnight.utcoffset().total_seconds() / 3600.0

    eot, dec = _sun(d, tz_hours)
    minutes = 720.0 - 4.0 * lon - eot + tz_hours * 60.0
    return midnight + dt.timedelta(minutes=minutes), 90.0 - abs(lat - dec)


def main(argv=None):
    p = argparse.ArgumentParser(description="Solar noon for the coordinates in broker/.env")
    p.add_argument("--noon-epoch", action="store_true",
                   help="print epoch seconds only, for a shell caller")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today, in LIGHT_TZ)")
    a = p.parse_args(argv)

    d = dt.date.fromisoformat(a.date) if a.date else None
    when, alt = solar_noon(d)

    if a.noon_epoch:
        print(int(when.timestamp()))
    else:
        print("%s  solar noon %s  max altitude %.2f deg (%.2f from zenith)"
              % (when.date().isoformat(), when.strftime("%H:%M:%S"), alt, 90.0 - alt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
