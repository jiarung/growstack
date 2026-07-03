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
MIN_HOLD = 5 * 60        # after a switch, hold ≥ this (matches MANUAL_HOLD; lamp may raise own lux)
STALE = 5 * 60           # lux older than this → fail safe OFF (dead sensor)
TICK = 60                # decision cadence, seconds
MANUAL_HOLD = 5 * 60     # an external cmd suppresses auto for this long

# ---- env ----
LOC = os.getenv("LIGHT_LOCATION", "livingroom")
TZ = ZoneInfo(os.getenv("LIGHT_TZ", "Asia/Taipei"))
SENSOR = os.getenv("LIGHT_SENSOR_DEVICE", "sensor-01")
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
TAPO_IP = os.getenv("TAPO_IP", "")
TAPO_EMAIL = os.getenv("TAPO_EMAIL", "")
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "")

TELEM_TOPIC = f"monitor-air/{SENSOR}/telemetry"
CMD_TOPIC = f"monitor-air/{LOC}/light/cmd"
STATE_TOPIC = f"monitor-air/{LOC}/light/state"
AVAIL_TOPIC = f"monitor-air/{LOC}/light/availability"


def decide(lux, lux_at, now, current):
    """Desired 'ON'/'OFF'. Pure: window + hysteresis + staleness.

    MIN_HOLD and MANUAL_HOLD are enforced by the caller, not here.
    """
    cur = "ON" if current == "ON" else "OFF"
    t = now.time()                         # naive local wall time
    in_window = ON_START <= t < HARD_OFF
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
            while True:
                now = time.time()
                if now >= st["manual_until"] and now - st["last_switch"] >= MIN_HOLD:
                    want = decide(st["lux"], st["lux_at"], datetime.now(TZ),
                                  st["current"])
                    await drive(want, "auto", client)
                await asyncio.sleep(TICK)

        await asyncio.gather(consume(), tick())


def selftest():
    def at(h, m, lux, cur="OFF", age=10):
        now = datetime(2026, 6, 25, h, m, tzinfo=TZ)
        return decide(lux, now.timestamp() - age, now, cur)

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
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(run())
