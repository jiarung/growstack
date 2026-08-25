#include "measure.h"
#include "sensors.h"       // hx711WindowStats()
#include "mqtt_client.h"   // mqttPublishMeasureEvent()
#include "weight_ref.h"    // cached {sat_g, dry_g} per tag -> the "used %" line
#include "log.h"
#include <Adafruit_PN532.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>
#include <WiFi.h>          // WiFi.RSSI() for the publish-timing diagnostic

namespace {

// PN532 over SOFTWARE SPI (I2C abandoned: clock stretching broke it on the ESP32-S3).
constexpr uint8_t PN532_SCK = 12, PN532_MISO = 13, PN532_MOSI = 11, PN532_SS = 10;
Adafruit_PN532 nfc(PN532_SCK, PN532_MISO, PN532_MOSI, PN532_SS);
bool pn532Ok = false;

// Stability gate (grams): settled = window range under threshold, sustained, over a fresh window.
constexpr float    STABLE_RANGE_G = 2.0f;
constexpr uint32_t STABLE_MIN_MS  = 1500;
constexpr uint32_t SETTLE_MIN_MS  = 2000;    // min after IDENTIFIED (pre-placement samples age out)
constexpr uint32_t SETTLE_TIMEOUT = 20000;
constexpr uint32_t TAG_ABSENT_MS  = 500;     // tag gone this long before the next measurement
constexpr uint16_t TAG_POLL_MS    = 50;      // per readPassiveTargetID poll (bounded loop latency)
constexpr float    OLED_EMPTY_G   = 30.0f;   // scale considered "empty" below this
constexpr uint32_t OLED_SLEEP_MS  = 30000;
constexpr uint32_t NUDGE_MS       = 8000;    // load present + unscanned this long -> "scan tag" nudge

// Reliable delivery (app-level, since PubSubClient publishes QoS0): event_id + ack + retry.
constexpr uint32_t ACK_TIMEOUT_MS = 3000;    // no ack within this -> resend the same event_id
constexpr int      MAX_RETRIES    = 4;
constexpr uint32_t DONE_MIN_MS    = 3000;    // hold the result screen at least this long

enum MState { M_IDLE, M_STABILIZING, M_AWAIT_ACK, M_DONE };
MState mstate = M_IDLE;
enum Result { R_SENT, R_UNSENT, R_UNSTABLE };
Result doneResult = R_SENT;

char     curUid[24]   = {0};
uint32_t identifiedAt = 0, stableSince = 0, absentSince = 0, doneSince = 0;
float    lastWeight   = 0;

// transport state
uint32_t eventSeq = 0, curEventId = 0, pubMs = 0;
int      retries  = 0;
volatile bool ackMatched = false;            // set by measureOnAck() when an ack for curEventId arrives

// OLED
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
bool oledOk = false, oledSleeping = false;
uint32_t emptySince = 0, loadSince = 0;   // OLED auto-off + "placed but unscanned" nudge timers

// True + hex UID if a tag is present (bounded ~TAG_POLL_MS block; the bench-verified read path).
bool readTag(char* out, size_t outsz) {
    uint8_t uid[7] = {0}, len = 0;
    if (!nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &len, TAG_POLL_MS)) return false;
    int n = 0;
    for (int i = 0; i < len && n < (int)outsz - 3; i++) n += snprintf(out + n, outsz - n, "%02X", uid[i]);
    return true;
}

void publishEvent() {   // (re)publish the current pending event (same event_id across retries)
    char json[96];
    snprintf(json, sizeof(json), "{\"event_id\":%lu,\"uid\":\"%s\",\"weight_g\":%.1f}",
             (unsigned long)curEventId, curUid, lastWeight);
    uint32_t t0 = millis();
    mqttPublishMeasureEvent(json);
    uint32_t dt = millis() - t0;
    pubMs = millis();
    // DIAG: measure-event publish duration + signal — the weight-station path is where the
    // freeze was seen; a long dt here + low rssi points at WiFi send blocking.
    logf("[measure] publish event_id=%lu took %lums rssi=%d\n",
         (unsigned long)curEventId, (unsigned long)dt, WiFi.RSSI());
}

void oledBegin() {
    Wire.beginTransmission(0x3C);   // probe first (like the AS7263 gate) so a missing panel fails clean
    if (Wire.endTransmission() != 0) { oledOk = false; logln("[measure] OLED not found @ 0x3C"); return; }
    oledOk = oled.begin(SSD1306_SWITCHCAPVCC, 0x3C);
    logf("[measure] OLED %s\n", oledOk ? "ready" : "begin FAILED");
}

// "-165g  used 87%" at y=44 — current weight against the retained watering ref for
// this tag: grams short of the last full watering, and how much of the observed
// water span (sat-dry, span>5g guaranteed by the cache) is gone. Drawn only with a
// calibrated scale — raw counts must never meet a gram baseline — and a cached ref;
// otherwise the line is simply absent, and the screens look exactly as before.
void oledRefLine(float w, bool calibrated) {
    float sat, dry;
    if (!calibrated || !weightRefLookup(curUid, &sat, &dry)) return;
    char line[24];
    if (w > sat) snprintf(line, sizeof(line), "+%.0fg  wet", (double)(w - sat));
    else snprintf(line, sizeof(line), "-%.0fg  used %.0f%%",
                  (double)(sat - w), (double)((sat - w) / (sat - dry) * 100.0f));
    oled.setTextSize(1);
    oled.setCursor(0, 44);
    oled.print(line);
}

// Render the workflow; render on a state change, else throttle to ~3 Hz. Auto-off when empty + idle.
void oledRender() {
    if (!oledOk) return;
    uint32_t now = millis();

    WeightStats sw = hx711WindowStats();
    bool empty = sw.valid && sw.mean_g < OLED_EMPTY_G;
    if (empty) { if (!emptySince) emptySince = now; loadSince = 0; }
    else       { emptySince = 0; if (!loadSince) loadSince = now; }   // loaded (or invalid) → treat as load
    if (mstate == M_IDLE && emptySince && (now - emptySince) > OLED_SLEEP_MS) {
        if (!oledSleeping) { oled.ssd1306_command(SSD1306_DISPLAYOFF); oledSleeping = true; }
        return;
    }
    bool wokeUp = oledSleeping;   // placing a load or scanning a tag wakes it
    if (oledSleeping) { oled.ssd1306_command(SSD1306_DISPLAYON); oledSleeping = false; }

    static uint32_t lastMs = 0;
    static int lastState = -1;
    if (!wokeUp && (int)mstate == lastState && (now - lastMs) < 300) return;
    lastMs = now; lastState = (int)mstate;

    oled.clearDisplay();
    oled.setTextColor(SSD1306_WHITE);
    oled.setTextSize(1);
    oled.setCursor(0, 0);

    if (mstate == M_IDLE) {
        if (!loadSince) {                     // empty scale → the prompt
            oled.println("Measure station");
            oled.setCursor(0, 24); oled.setTextSize(2); oled.println("Place pot");
            oled.setCursor(0, 46); oled.println("+ scan tag");
        } else {                              // load present, not yet scanned → live weight + nudge
            oled.println("Ready to weigh");
            oled.setCursor(0, 22); oled.setTextSize(2);
            if (sw.valid) { oled.print(sw.mean_g, 1); oled.println(" g"); } else oled.println("-- g");
            oled.setTextSize(1); oled.setCursor(0, 54);
            oled.print((now - loadSince) > NUDGE_MS ? ">> SCAN TAG to save <<" : "scan tag to save");
        }
    } else if (mstate == M_STABILIZING) {
        oled.print("Tag "); oled.println(curUid);
        oled.setCursor(0, 22); oled.setTextSize(2);
        if (sw.valid) { oled.print(sw.mean_g, 1); oled.println(" g"); } else oled.println("-- g");
        if (sw.valid) oledRefLine(sw.mean_g, sw.calibrated);
        oled.setTextSize(1); oled.setCursor(0, 54); oled.print("stabilizing r=");
        if (sw.valid) oled.print(sw.range_g, 1);
    } else if (mstate == M_AWAIT_ACK) {
        oled.print("Tag "); oled.println(curUid);
        oled.setCursor(0, 22); oled.setTextSize(2); oled.print(lastWeight, 1); oled.println(" g");
        oledRefLine(lastWeight, sw.calibrated);
        oled.setTextSize(1); oled.setCursor(0, 54); oled.print("sending");
        for (int i = 0; i < retries; i++) oled.print('.');
    } else {  // M_DONE
        oled.print("Tag "); oled.println(curUid);
        oled.setCursor(0, 22); oled.setTextSize(2);
        if (doneResult == R_UNSTABLE) oled.println("UNSTABLE");
        else { oled.print(lastWeight, 1); oled.println(" g"); oledRefLine(lastWeight, sw.calibrated); }
        oled.setTextSize(1); oled.setCursor(0, 54);
        oled.println(doneResult == R_SENT   ? "sent OK - remove tag" :
                     doneResult == R_UNSENT ? "UNSENT - remove tag"  : "unstable - remove tag");
    }
    oled.display();
}

}  // namespace

void measureOnAck(const uint8_t* payload, unsigned int len) {   // MQTT dispatcher -> here
    char buf[64];
    unsigned n = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
    memcpy(buf, payload, n); buf[n] = '\0';
    JsonDocument doc;
    if (deserializeJson(doc, buf)) return;   // ignore malformed acks
    // Match on ARRIVAL (no single-slot mailbox that could drop a 2nd ack in the same client.loop batch).
    if ((uint32_t)(doc["event_id"] | 0) == curEventId) ackMatched = true;
}

bool measurePn532Ok() { return pn532Ok; }  // live presence for the health topic (PN532 is on SPI)

bool measureBusy() { return mstate == M_STABILIZING || mstate == M_AWAIT_ACK; }

void measureBegin() {
    oledBegin();
    nfc.begin();
    pn532Ok = (nfc.getFirmwareVersion() != 0);
    if (pn532Ok) {
        nfc.SAMConfig();
        nfc.setPassiveActivationRetries(0x02);
    }
    logf("[measure] PN532 %s\n", pn532Ok ? "ready (SPI)" : "NOT found");
}

void measureLoop() {
    uint32_t now = millis();

    // A matching ack (parsed on arrival in measureOnAck) completes the in-flight event.
    if (ackMatched) {
        ackMatched = false;
        if (mstate == M_AWAIT_ACK) {
            doneResult = R_SENT; doneSince = now; mstate = M_DONE;
            logf("[measure] SENT (ack) event_id=%lu\n", (unsigned long)curEventId);
        }
    }

    if (pn532Ok) {
        switch (mstate) {
        case M_IDLE: {
            char uid[24];
            if (readTag(uid, sizeof(uid))) {
                strncpy(curUid, uid, sizeof(curUid) - 1); curUid[sizeof(curUid) - 1] = '\0';
                identifiedAt = now; stableSince = 0;
                mstate = M_STABILIZING;
                logf("[measure] tag %s — stabilizing…\n", curUid);
            }
            break;
        }
        case M_STABILIZING: {
            WeightStats s = hx711WindowStats();
            bool settled = s.valid && s.range_g < STABLE_RANGE_G && s.window_ms >= SETTLE_MIN_MS;
            if (settled) { if (!stableSince) stableSince = now; } else stableSince = 0;

            if (stableSince && (now - stableSince) >= STABLE_MIN_MS && (now - identifiedAt) >= SETTLE_MIN_MS) {
                lastWeight = s.mean_g;
                curEventId = ++eventSeq; retries = 0;
                publishEvent();
                mstate = M_AWAIT_ACK;
                logf("[measure] MEASURED uid=%s weight=%.1fg event_id=%lu — sending…\n",
                     curUid, lastWeight, (unsigned long)curEventId);
            } else if ((now - identifiedAt) > SETTLE_TIMEOUT) {
                doneResult = R_UNSTABLE; doneSince = now; mstate = M_DONE;
                logf("[measure] UNSTABLE uid=%s\n", curUid);
            }
            break;
        }
        case M_AWAIT_ACK:
            // Only advance retries/UNSENT while CONNECTED — a temporary broker drop must NOT lose the
            // event; it stays pending ("sending…") until reconnect, then the timer resends. (Codex #1)
            if (mqttConnected() && now - pubMs > ACK_TIMEOUT_MS) {
                if (retries < MAX_RETRIES) {
                    retries++; publishEvent();
                    logf("[measure] retry %d/%d event_id=%lu\n", retries, MAX_RETRIES, (unsigned long)curEventId);
                } else {
                    doneResult = R_UNSENT; doneSince = now; mstate = M_DONE;
                    logf("[measure] UNSENT (no ack) event_id=%lu\n", (unsigned long)curEventId);
                }
            }
            break;
        case M_DONE: {   // hold the result ≥ DONE_MIN_MS AND until the tag is removed, then re-arm
            char uid[24];
            bool same = readTag(uid, sizeof(uid)) && strcmp(uid, curUid) == 0;
            if (same) absentSince = 0;
            else if (!absentSince) absentSince = now;
            bool held    = (now - doneSince) >= DONE_MIN_MS;
            bool removed = absentSince && (now - absentSince) >= TAG_ABSENT_MS;
            if (held && removed) mstate = M_IDLE;
            break;
        }
        }
    }

    oledRender();   // reflect the current state on the local OLED (throttled)
}
