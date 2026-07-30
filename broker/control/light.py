#!/usr/bin/env python3
"""monitor-air plant-light controller.

Reads lux from the sensor's telemetry, decides ON/OFF by a local-time window +
lux hysteresis, and drives a Tapo P110M plug. The `.../light/cmd` topic is the
external seam (manual / AI / Grafana) — auto decisions drive the plug directly,
they do NOT round-trip through cmd (no echo, no retained re-fire).

ponytail: one service, one file. lux thresholds + MIN_HOLD are the calibration
knobs (the lamp can raise the sensor's own reading). Swap plug type → edit only
the kasa calls in set_plug()/plug_is_on().
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import aiohttp
import aiomqtt
from kasa import Credentials, Discover

# ---- calibration knobs (tune in place) ----
LUX_ON_BELOW = 5000      # after ON_START, under this → ON
LUX_OFF_ABOVE = 15000    # over this → OFF (e.g. direct sun); else hold last state
# Operating window comes from .env (minutes since midnight) — the SAME vars gate
# the Grafana lux deadman alert, so window changes stay in one place.
_start_min = int(os.getenv("LIGHT_WINDOW_START_MIN", "480"))    # 08:00
_end_min = int(os.getenv("LIGHT_WINDOW_END_MIN", "1110"))       # 18:30
ON_START = dtime(_start_min // 60, _start_min % 60)  # only turn ON within [ON_START, HARD_OFF)
HARD_OFF = dtime(_end_min // 60, _end_min % 60)      # at/after this → force OFF regardless of lux
# Evening top-up: after HARD_OFF, keep supplementing until the day's accumulated
# DLI reaches DLI_TARGET, capped at EXTEND_END. Extends only the light window, not
# the deadman gate. DLI comes from InfluxDB (survives restarts, reuses the panel's
# computation).
#
# Why a target and not a "was today dark?" threshold: this room gets almost no
# usable daylight — measured 2026-07-30, natural light is 1-2% of the daily total
# (0.02-0.07 of ~3.5 mol), and indoor lux does not track outdoor solar at all. So
# "is today dark" has the same answer every day and a threshold on it only ever
# encodes a fixed extend-or-not, while silently breaking whenever the lamp or the
# sensor changes scale (which is exactly what happened: a lens clean on ~2026-07-23
# moved DLI ~2.2x and killed the old `< 1.5` rule without a single error).
# A target is in real units, so a recalibration means re-deriving it once.
#
# Useful range, at the measured ~0.33 mol/h the lamp delivers:
#   <= 3.47  never tops up (the 08:00-18:30 window already reaches it)
#    ~4.0    tops up most evenings, stops early when the sun did contribute
#   >= 4.12  always runs to EXTEND_END (then just move LIGHT_WINDOW_END_MIN instead)
# Derive it from the measured baseline, NOT from horticultural tables: lux/54 is a
# daylight conversion applied to an LED, so the absolute scale is not trustworthy.
_extend_min = int(os.getenv("LIGHT_EXTEND_END_MIN", "1230"))   # 20:30
EXTEND_END = dtime(_extend_min // 60, _extend_min % 60)
DLI_TARGET = float(os.getenv("DLI_TARGET", "4.0"))  # mol·m⁻²·day⁻¹
MIN_HOLD = 5 * 60        # after a switch, hold ≥ this (matches MANUAL_HOLD; lamp may raise own lux)
STALE = 5 * 60           # lux older than this → fail safe OFF (dead sensor)
TICK = 60                # decision cadence, seconds
MANUAL_HOLD = 5 * 60     # an external cmd suppresses auto for this long

# ---- env ----
LOC = os.getenv("LIGHT_LOCATION", "livingroom")
TZ_NAME = os.getenv("LIGHT_TZ", "Asia/Taipei")
TZ = ZoneInfo(TZ_NAME)
SENSOR = os.getenv("LIGHT_SENSOR_DEVICE", "sensor-01")
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
TAPO_IP = os.getenv("TAPO_IP", "")
TAPO_EMAIL = os.getenv("TAPO_EMAIL", "")
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "")
# InfluxDB read (for the dark-day DLI check) — token/org/bucket from .env
INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_ORG = os.getenv("DOCKER_INFLUXDB_INIT_ORG", "monitor-air")
INFLUX_BUCKET = os.getenv("DOCKER_INFLUXDB_INIT_BUCKET", "sensors")
INFLUX_TOKEN = os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN", "")

TELEM_TOPIC = f"monitor-air/{SENSOR}/telemetry"
CMD_TOPIC = f"monitor-air/{LOC}/light/cmd"
STATE_TOPIC = f"monitor-air/{LOC}/light/state"
AVAIL_TOPIC = f"monitor-air/{LOC}/light/availability"


def decide(lux, lux_at, now, current, hard_off=HARD_OFF):
    """Desired 'ON'/'OFF'. Pure: window + hysteresis + staleness.

    `hard_off` is the window's end for this call — normally HARD_OFF, but the
    caller passes EXTEND_END on dark days. MIN_HOLD/MANUAL_HOLD enforced by caller.
    """
    cur = "ON" if current == "ON" else "OFF"
    t = now.time()                         # naive local wall time
    in_window = ON_START <= t < hard_off
    if lux is None or (now.timestamp() - lux_at) > STALE:
        # sensor dropout: this environment is light-deficient, so inside the
        # window we HOLD an already-on light rather than fail it off; only fail
        # safe OFF if it was already off, or we're outside the window.
        return "ON" if (in_window and cur == "ON") else "OFF"
    if not in_window:
        return "OFF"                       # before window / past hard-off
    if lux < LUX_ON_BELOW:
        return "ON"                         # in window + dark
    if lux > LUX_OFF_ABOVE:
        return "OFF"
    return cur                             # hysteresis band → hold last state


def extend_decision(dli, target, last):
    """Keep supplementing this evening? Pure.

    A failed DLI query (None) HOLDS the previous answer rather than defaulting to
    off: the query runs every tick, so defaulting would drop the lamp on a single
    blip and flap it back a minute later.
    """
    return last if dli is None else dli < target


async def todays_dli():
    """Today's accumulated DLI (mol·m⁻²·day⁻¹) for the sensor device, from
    InfluxDB — same lux/54 integral the Grafana DLI panel uses. Returns None on
    any error (caller then does not extend). Queried from the DB rather than
    accumulated in-process so it survives service restarts."""
    if not INFLUX_TOKEN:
        return None
    flux = (
        'import "timezone"\n'
        f'option location = timezone.location(name: "{TZ_NAME}")\n'
        f'from(bucket: "{INFLUX_BUCKET}")\n'
        '  |> range(start: -25h)\n'
        '  |> filter(fn: (r) => r._measurement == "air" and r._field == "lux"'
        f' and r.device == "{SENSOR}")\n'
        '  |> map(fn: (r) => ({ r with _value: r._value / 54.0 }))\n'
        '  |> aggregateWindow(every: 1d, fn: (tables=<-, column) =>'
        ' tables |> integral(unit: 1s), createEmpty: false)\n'
        '  |> map(fn: (r) => ({ r with _value: r._value / 1000000.0 }))\n'
        '  |> last()'
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{INFLUX_URL}/api/v2/query", params={"org": INFLUX_ORG}, data=flux,
                headers={"Authorization": f"Token {INFLUX_TOKEN}",
                         "Content-Type": "application/vnd.flux",
                         "Accept": "application/csv"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
        data = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        if len(data) < 2:                  # header + at least one data row
            return None
        vi = data[0].split(",").index("_value")
        return float(data[-1].split(",")[vi])
    except Exception as e:
        print(f"DLI query failed: {e}", flush=True)
        return None


# ---- Tapo plug (the only code that knows about the hardware) ----
async def _device(holder):
    dev = holder.get("dev")
    if dev is None:
        dev = await Discover.discover_single(
            TAPO_IP, credentials=Credentials(TAPO_EMAIL, TAPO_PASSWORD))
        holder["dev"] = dev
    return dev


async def plug_is_on(holder):
    dev = await _device(holder)
    await dev.update()
    return dev.is_on


async def set_plug(holder, target):
    try:
        dev = await _device(holder)
        await (dev.turn_on() if target == "ON" else dev.turn_off())
        await dev.update()
    except Exception:
        holder.pop("dev", None)            # force rediscover next attempt
        raise


async def run():
    st = {"lux": None, "lux_at": 0.0, "current": "OFF",
          "manual_until": 0.0, "last_switch": 0.0}
    dev = {}

    async def drive(target, source, client):
        if target == st["current"]:
            return                         # idempotent (QoS1 dups, repeats)
        try:
            # hard timeout: a plug mid-firmware/protocol-flip can accept TCP and
            # then never finish the handshake, which would wedge the tick loop
            # forever (observed 2026-07-03 during the KLAP→TPAP→KLAP toggle)
            await asyncio.wait_for(set_plug(dev, target), timeout=30)
        except Exception as e:
            print(f"plug drive failed ({target}): {e!r}", flush=True)
            await client.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
            return
        st["current"] = target
        if source == "auto":            # MIN_HOLD anti-flap is for auto only;
            st["last_switch"] = time.time()  # a manual switch shouldn't delay auto resuming

        await client.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
        await client.publish(STATE_TOPIC, json.dumps(
            {"state": target, "on": 1 if target == "ON" else 0, "source": source}),
            qos=1, retain=True)
        print(f"{source}: → {target}", flush=True)

    will = aiomqtt.Will(AVAIL_TOPIC, "offline", qos=1, retain=True)
    async with aiomqtt.Client(MQTT_HOST, will=will) as client:
        try:                               # seed from the plug's real state
            st["current"] = "ON" if await asyncio.wait_for(plug_is_on(dev), timeout=30) else "OFF"
            await client.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
            await client.publish(STATE_TOPIC, json.dumps(  # refresh retained state
                {"state": st["current"], "on": 1 if st["current"] == "ON" else 0,
                 "source": "seed"}), qos=1, retain=True)
            print(f"seeded current={st['current']}", flush=True)
        except Exception as e:
            print(f"startup plug read failed: {e}", flush=True)
        await client.subscribe(TELEM_TOPIC, qos=1)
        await client.subscribe(CMD_TOPIC, qos=1)

        async def consume():
            async for m in client.messages:
                topic = str(m.topic)
                try:
                    payload = json.loads(m.payload)
                except Exception:
                    payload = {}
                if topic == TELEM_TOPIC and "lux" in payload:
                    try:                   # one bad reading must not kill the loop
                        st["lux"] = float(payload["lux"])
                        st["lux_at"] = time.time()
                    except (TypeError, ValueError):
                        pass
                elif topic == CMD_TOPIC and not m.retain:
                    # ignore a stray retained cmd replaying on reconnect — it would
                    # masquerade as a fresh manual override and suppress auto.
                    tgt = str(payload.get("state", "")).upper()
                    if tgt in ("ON", "OFF"):
                        st["manual_until"] = time.time() + MANUAL_HOLD
                        await drive(tgt, "manual", client)

        async def tick():
            ext = {"on": False, "day": None}   # evening top-up, re-evaluated each tick
            while True:
                now = time.time()
                now_local = datetime.now(TZ)
                t = now_local.time()

                # Evening top-up: inside [HARD_OFF, EXTEND_END), keep the window open
                # while today's DLI is still short of DLI_TARGET. Re-checked EVERY tick
                # rather than decided once at HARD_OFF, so the lamp stops the moment the
                # target is reached instead of running blind to EXTEND_END.
                today = now_local.date()
                if ext["day"] != today:
                    ext.update(on=False, day=today)
                hard_off = HARD_OFF
                if HARD_OFF <= t < EXTEND_END:
                    dli = await todays_dli()
                    was = ext["on"]
                    ext["on"] = extend_decision(dli, DLI_TARGET, was)
                    if ext["on"] != was and dli is not None:   # log transitions only
                        print(f"[top-up] DLI {dli:.2f}/{DLI_TARGET:.2f} → "
                              f"{'on until '+EXTEND_END.strftime('%H:%M')+' or target' if ext['on'] else 'target reached, stop'}",
                              flush=True)
                if ext["on"]:
                    hard_off = EXTEND_END
                    # Once the lamp goes off at target, DLI stops climbing, so this can
                    # re-arm within a tick or two. MIN_HOLD bounds that to one switch per
                    # 5 min, and each burst adds ~0.03 mol, so it settles just past target.

                if now >= st["manual_until"] and now - st["last_switch"] >= MIN_HOLD:
                    want = decide(st["lux"], st["lux_at"], now_local, st["current"], hard_off)
                    await drive(want, "auto", client)
                await asyncio.sleep(TICK)

        await asyncio.gather(consume(), tick())


def selftest():
    def at(h, m, lux, cur="OFF", age=10, ho=HARD_OFF):
        now = datetime(2026, 6, 25, h, m, tzinfo=TZ)
        return decide(lux, now.timestamp() - age, now, cur, ho)

    assert at(12, 0, 50) == "ON", "dark@noon (<5000) → ON"
    assert at(12, 0, 4300) == "ON", "lux 4300 (<5000) in window → ON (post-sun re-on)"
    assert at(12, 0, 20000) == "OFF", "very bright@noon (>15000) → OFF"
    assert at(7, 0, 50) == "OFF", "before 08:00 → OFF"
    assert at(18, 10, 50) == "ON", "18:10 now inside window (<18:30) → ON"
    assert at(18, 45, 50) == "OFF", "after 18:30 → OFF"
    assert at(18, 45, 50, "ON") == "OFF", "18:30 hard-off forces OFF even if was ON"
    assert at(12, 0, 8000, "ON") == "ON", "hold band (5000-15000) keeps ON"
    assert at(12, 0, 8000, "OFF") == "OFF", "hold band keeps OFF"
    assert at(12, 0, 5000, "ON") == "ON", "boundary: exactly 5000 holds ON (not <)"
    assert at(12, 0, 5000, "OFF") == "OFF", "boundary: exactly 5000 holds OFF"
    noon = datetime(2026, 6, 25, 12, 0, tzinfo=TZ)
    # sensor dropout inside the window holds an already-ON light (light-deficient env)
    assert decide(50, noon.timestamp() - STALE - 1, noon, "ON") == "ON", "stale+ON in window → hold ON"
    assert decide(None, noon.timestamp(), noon, "ON") == "ON", "no lux+ON in window → hold ON"
    assert decide(None, noon.timestamp(), noon, "OFF") == "OFF", "dropout+OFF in window → stays OFF"
    night = datetime(2026, 6, 25, 20, 0, tzinfo=TZ)
    assert decide(None, night.timestamp(), night, "ON") == "OFF", "dropout+ON outside window → OFF"
    open_t = datetime(2026, 6, 25, 8, 0, tzinfo=TZ)
    assert decide(None, open_t.timestamp(), open_t, "ON") == "ON", "dropout+ON at 08:00 (incl.) → hold ON"
    close_t = datetime(2026, 6, 25, 18, 30, tzinfo=TZ)
    assert decide(None, close_t.timestamp(), close_t, "ON") == "OFF", "dropout+ON at 18:30 (excl.) → OFF"
    # dark-day extension: caller passes EXTEND_END (20:30) as hard_off
    ext = dtime(20, 30)
    assert at(19, 0, 50) == "OFF", "19:00 past base 18:30 (normal day) → OFF"
    assert at(19, 0, 50, ho=ext) == "ON", "19:00 within extended 20:30 window + dark → ON"
    assert at(20, 30, 50, ho=ext) == "OFF", "20:30 extended hard-off → OFF"
    assert at(19, 0, 20000, ho=ext) == "OFF", "19:00 extended but bright (>15000) → OFF"

    # evening top-up: target comparison + hold-last on a failed DLI query
    assert extend_decision(3.40, 4.0, False) is True, "below target → top up"
    assert extend_decision(4.00, 4.0, True) is False, "at target (not <) → stop"
    assert extend_decision(4.20, 4.0, True) is False, "past target → stop"
    assert extend_decision(None, 4.0, True) is True, "query failed → hold ON, don't flap off"
    assert extend_decision(None, 4.0, False) is False, "query failed → hold OFF too"
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(run())
