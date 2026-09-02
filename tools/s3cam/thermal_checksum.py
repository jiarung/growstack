#!/usr/bin/env python3
"""Search for the GY-MCU90640's trailing-checksum algorithm.

    curl 'http://<ip>/thermal/raw?hex=1' > raw.txt
    ./thermal_checksum.py raw.txt

The module's last two bytes match none of sum16-LE, sum16-BE, or sum8, so the
firmware currently accepts frames under ChecksumPolicy::REPORT and marks them
checksum_ok=false. That is honest but temporary: this script exists to end it.

It brute-forces the plausible space — every combination of

  * algorithm: byte sum, word sum (LE and BE), XOR, CRC-16 over the full
    catalogue of (poly, init, refin, refout, xorout) in common use, CRC-8, and
    the "sum then negate/two's-complement" variants module vendors like
  * covered range: from each of several start offsets (0 = include the header,
    2 = skip sync, 4 = payload only) to each of several ends (before the
    checksum, before Ta+checksum)
  * stored form: LE or BE, and the low byte alone

against a real frame, and prints every hypothesis that fits.

ONE frame fitting is not proof — with 65536 possible 16-bit values and a few
thousand hypotheses, coincidences are expected. Pass several independently
captured frames (repeat the curl) and only a hypothesis that fits ALL of them
is worth putting in the firmware. The script enforces this: it intersects.
"""
import re
import sys

# (name, poly, init, refin, refout, xorout) — the catalogue that covers
# essentially every CRC-16 seen in hobby sensor modules.
CRC16 = [
    ("CCITT-FALSE", 0x1021, 0xFFFF, False, False, 0x0000),
    ("XMODEM",      0x1021, 0x0000, False, False, 0x0000),
    ("KERMIT",      0x1021, 0x0000, True,  True,  0x0000),
    ("CCITT-X25",   0x1021, 0xFFFF, True,  True,  0xFFFF),
    ("MCRF4XX",     0x1021, 0xFFFF, True,  True,  0x0000),
    ("GENIBUS",     0x1021, 0xFFFF, False, False, 0xFFFF),
    ("MODBUS",      0x8005, 0xFFFF, True,  True,  0x0000),
    ("ARC",         0x8005, 0x0000, True,  True,  0x0000),
    ("USB",         0x8005, 0xFFFF, True,  True,  0xFFFF),
    ("MAXIM",       0x8005, 0x0000, True,  True,  0xFFFF),
    ("DDS110",      0x8005, 0x800D, False, False, 0x0000),
    ("DECT-R",      0x0589, 0x0000, False, False, 0x0001),
    ("CMS",         0x8005, 0xFFFF, False, False, 0x0000),
]

CRC8 = [
    ("CRC8",        0x07, 0x00, False, False, 0x00),
    ("CRC8-MAXIM",  0x31, 0x00, True,  True,  0x00),
    ("CRC8-DVB-S2", 0xD5, 0x00, False, False, 0x00),
    ("CRC8-SAE-J1850", 0x1D, 0xFF, False, False, 0xFF),
    ("CRC8-ROHC",   0x07, 0xFF, True,  True,  0x00),
]


def rev(v, bits):
    r = 0
    for _ in range(bits):
        r = (r << 1) | (v & 1)
        v >>= 1
    return r


def crc(data, poly, init, refin, refout, xorout, bits):
    top = 1 << (bits - 1)
    mask = (1 << bits) - 1
    reg = init
    for b in data:
        reg ^= (rev(b, 8) if refin else b) << (bits - 8)
        for _ in range(8):
            reg = ((reg << 1) ^ poly) & mask if reg & top else (reg << 1) & mask
    if refout:
        reg = rev(reg, bits)
    return reg ^ xorout


def hypotheses(frame, ta_off, cs_off):
    """Every (label -> computed value) for one frame. Labels are the identity
    of a hypothesis, so intersecting label sets across frames is the test."""
    out = {}
    starts = {"from0": 0, "from2": 2, "from4": 4}
    ends = {"toCS": cs_off, "toTa": ta_off}
    for sn, s in starts.items():
        for en, e in ends.items():
            if e <= s:
                continue
            body = frame[s:e]
            tag = f"{sn}..{en}"
            bsum = sum(body)
            out[f"sum8/{tag}"] = bsum & 0xFF
            out[f"sum16/{tag}"] = bsum & 0xFFFF
            out[f"sum16neg/{tag}"] = (-bsum) & 0xFFFF
            out[f"sum16inv/{tag}"] = (~bsum) & 0xFFFF
            out[f"sum8neg/{tag}"] = (-bsum) & 0xFF
            x8 = 0
            for b in body:
                x8 ^= b
            out[f"xor8/{tag}"] = x8
            if len(body) % 2 == 0:
                wle = sum(body[i] | (body[i + 1] << 8) for i in range(0, len(body), 2))
                wbe = sum((body[i] << 8) | body[i + 1] for i in range(0, len(body), 2))
                out[f"wsum16LE/{tag}"] = wle & 0xFFFF
                out[f"wsum16BE/{tag}"] = wbe & 0xFFFF
                # the internet-checksum fold, used by more than just IP
                f = wle
                while f >> 16:
                    f = (f & 0xFFFF) + (f >> 16)
                out[f"fold16/{tag}"] = f & 0xFFFF
                out[f"fold16inv/{tag}"] = (~f) & 0xFFFF
            for name, p, i, ri, ro, xo in CRC16:
                out[f"crc16-{name}/{tag}"] = crc(body, p, i, ri, ro, xo, 16)
            for name, p, i, ri, ro, xo in CRC8:
                out[f"crc8-{name}/{tag}"] = crc(body, p, i, ri, ro, xo, 8)
    return out


def matches(frame, ta_off, cs_off):
    """Labels whose computed value equals the stored checksum, in some form."""
    lo, hi = frame[cs_off], frame[cs_off + 1]
    stored = {"LE16": lo | (hi << 8), "BE16": (lo << 8) | hi,
              "byte0": lo, "byte1": hi}
    hyp = hypotheses(frame, ta_off, cs_off)
    hits = set()
    for label, value in hyp.items():
        width = 8 if ("sum8" in label or "xor8" in label or "crc8" in label) else 16
        for form, sv in stored.items():
            if width == 8 and form not in ("byte0", "byte1"):
                continue
            if width == 16 and form in ("byte0", "byte1"):
                continue
            if value == sv:
                hits.add(f"{label} == {form}")
    return hits


def parse(path):
    """Every hex dump in the file becomes one frame. `?hex=1` emits one frame
    per request, so concatenating several captures into one file just works."""
    text = open(path).read()
    frames, cur = [], []
    for line in text.splitlines():
        s = line.strip()
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", s) and len(s) >= 8:
            cur.extend(bytes.fromhex(s))
        elif cur:
            frames.append(bytes(cur))
            cur = []
    if cur:
        frames.append(bytes(cur))
    return frames


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    frames = [f for p in argv for f in parse(p)]
    if not frames:
        print("no hex frames found — did you use ?hex=1 ?", file=sys.stderr)
        return 1
    common = None
    for i, f in enumerate(frames):
        cs_off, ta_off = len(f) - 2, len(f) - 4
        hits = matches(f, ta_off, cs_off)
        print(f"frame {i}: {len(f)} bytes, stored "
              f"{f[cs_off]:02X} {f[cs_off + 1]:02X} — {len(hits)} hypotheses fit")
        common = hits if common is None else (common & hits)
    print()
    if not common:
        print("NOTHING fits all frames. The checksum covers a range or uses a")
        print("form this search does not model — keep ChecksumPolicy::REPORT,")
        print("and look at the module's own datasheet/vendor code next.")
        return 1
    print(f"fits ALL {len(frames)} frames:")
    for h in sorted(common):
        print("  ", h)
    if len(frames) < 3:
        print(f"\n{len(frames)} frame(s) is weak evidence — capture more and rerun")
        print("before changing the firmware back to STRICT.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
