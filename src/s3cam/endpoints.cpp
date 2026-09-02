#include "endpoints.h"

#include <Arduino.h>
#include <esp_http_server.h>
#include <time.h>

#include "camera.h"
#include "rangefinder.h"
#include "thermal/thermal_uart.h"

static httpd_handle_t server = nullptr;

// ---- capture ids ------------------------------------------------------------
// A monotonic sequence rides on EVERY id: two captures in the same UTC second
// (trivially reachable when curling in a loop) must never collide — the id is
// the download filename and the JSON<->JPEG pairing key.
// Returns true when the wall clock was NTP-synced (the caller decides what the
// JSON `timestamp` may honestly claim).
static bool makeCaptureId(char* out, size_t n) {
    static uint32_t seq = 0;
    uint32_t s = ++seq;
    time_t now = time(nullptr);
    if (now > 1600000000) {   // sane wall clock = NTP happened
        struct tm tm;
        gmtime_r(&now, &tm);
        snprintf(out, n, "cap-%04d%02d%02dT%02d%02d%02dZ-%04lu",
                 tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                 tm.tm_hour, tm.tm_min, tm.tm_sec, (unsigned long)s);
        return true;
    }
    snprintf(out, n, "cap-boot%lu-%04lu", (unsigned long)millis(), (unsigned long)s);
    return false;
}

// ---- pairing a distance with an exposure -----------------------------------
// The rangefinder cannot measure DURING the exposure, so a single reading
// beside a capture is only as good as the assumption that nothing moved. This
// brackets the frame instead: one reading before, one after. Agreement means
// the scene held still across the whole capture and the distance genuinely
// describes that frame; disagreement is reported as such rather than silently
// attaching one of two different distances to the image. (The scan workflow is
// stop-settle-capture, so agreement is the normal case; a hand-held target
// being walked around is exactly when this fires — and should.)
constexpr uint16_t RANGE_AGREE_MM = 30;

struct PairedRange {
    bool valid = false;
    uint16_t mm = 0;
    char reason[32] = "";     // why not, when !valid
};

static PairedRange pairRange(const Range& before, const Range& after) {
    PairedRange p;
    if (before.api_err || after.api_err) {
        snprintf(p.reason, sizeof(p.reason), "driver_err:%d",
                 (int)(before.api_err ? before.api_err : after.api_err));
        return p;
    }
    if (!before.valid || !after.valid) {
        snprintf(p.reason, sizeof(p.reason), "no_distance:s%u",
                 (unsigned)(before.valid ? after.status : before.status));
        return p;
    }
    int diff = (int)after.mm - (int)before.mm;
    if (diff < 0) diff = -diff;
    if (diff > RANGE_AGREE_MM) {
        snprintf(p.reason, sizeof(p.reason), "moved:%u->%u",
                 (unsigned)before.mm, (unsigned)after.mm);
        return p;
    }
    p.valid = true;
    p.mm = (uint16_t)((before.mm + after.mm) / 2);
    return p;
}

// ---- the /observation-held frame (PSRAM copy; same exposure as its JSON) ----
static uint8_t* heldJpg = nullptr;
static size_t heldLen = 0;
static char heldId[40] = "";

// ---- handlers ---------------------------------------------------------------
static esp_err_t indexHandler(httpd_req_t* req) {
    char body[640];   // grows with the endpoint list — the compiler checks it
    snprintf(body, sizeof(body),
             "s3cam bring-up (phase 1B)\n"
             "sensor: %s\nrangefinder: %s\n"
             "psram: %u bytes (free %u)\nheap free: %u\n\n"
             "GET /stream      MJPEG live view\n"
             "GET /capture     full-res still (X-Capture-Id + X-Range-Mm headers)\n"
             "GET /observation still + observation JSON (pairs with /last.jpg)\n"
             "GET /last.jpg    the frame the last /observation held\n"
             "GET /range?n=20  raw rangefinder burst (no camera) — is it ranging?\n"
             "GET /thermal     newest 32x24 frame + stream stats\n"
             "GET /thermal/raw what the module ACTUALLY sends (layout ground truth)\n",
             cameraSensorName(), rangefinderPresent() ? "VL53L0X" : "absent",
             ESP.getPsramSize(), ESP.getFreePsram(), ESP.getFreeHeap());
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

// GET /range?n=20 — raw rangefinder burst, no camera involved. The instrument
// for "is this sensor actually ranging, or reporting the same number forever":
// a constant value while the scene changes is not a measurement.
static esp_err_t rangeHandler(httpd_req_t* req) {
    int n = 20;
    char q[32];
    if (httpd_req_get_url_query_str(req, q, sizeof(q)) == ESP_OK) {
        char v[8];
        if (httpd_query_key_value(q, "n", v, sizeof(v)) == ESP_OK) {
            n = atoi(v);
            if (n < 1) n = 1;
            if (n > 40) n = 40;   // each read paces at the timing budget
        }
    }
    // CHUNKED, with a small line buffer: the httpd task's stack is 4 KB, so a
    // multi-KB local here overflows it (a 2048-byte body did exactly that —
    // "Stack canary watchpoint triggered (httpd)"). Streaming also means `n`
    // has no buffer-imposed ceiling.
    char line[128];
    httpd_resp_set_type(req, "text/plain");
    int m = snprintf(line, sizeof(line),
                     "VL53L0X %s - %d readings, ~%lus\n"
                     "  status 0=valid 4=out of range, others=signal/sigma fail\n\n",
                     rangefinderPresent() ? "present" : "ABSENT", n,
                     (unsigned long)(n * rangefinderIntervalMs() / 1000));
    httpd_resp_send_chunk(req, line, m);

    uint16_t mn = 0xFFFF, mx = 0;
    int valid = 0;
    for (int i = 0; i < n; i++) {
        Range rg = rangefinderRead();
        if (rg.valid) {
            valid++;
            if (rg.mm < mn) mn = rg.mm;
            if (rg.mm > mx) mx = rg.mm;
            m = snprintf(line, sizeof(line), "  %2d  %5u mm  status %u\n",
                         i + 1, (unsigned)rg.mm, (unsigned)rg.status);
        } else if (rg.api_err != 0) {
            m = snprintf(line, sizeof(line), "  %2d      --      driver error %d\n",
                         i + 1, (int)rg.api_err);
        } else {
            m = snprintf(line, sizeof(line), "  %2d      --      status %u (no distance)\n",
                         i + 1, (unsigned)rg.status);
        }
        if (httpd_resp_send_chunk(req, line, m) != ESP_OK) return ESP_FAIL;
        delay(rangefinderIntervalMs());   // derived from the timing budget
    }
    if (valid) {
        m = snprintf(line, sizeof(line),
                     "\n%d/%d valid, spread %u..%u mm (%u mm)\n",
                     valid, n, (unsigned)mn, (unsigned)mx, (unsigned)(mx - mn));
        httpd_resp_send_chunk(req, line, m);
        const char* hint =
            "A spread of 0 while the scene changes means it is NOT ranging:\n"
            "check the factory film over the window, and that nothing sits in\n"
            "front of it (wire, glue, mounting lip) within a few cm.\n";
        httpd_resp_send_chunk(req, hint, strlen(hint));
    } else {
        m = snprintf(line, sizeof(line),
                     "\n0/%d valid - nothing in range, or the sensor is not answering.\n", n);
        httpd_resp_send_chunk(req, line, m);
    }
    return httpd_resp_send_chunk(req, nullptr, 0);   // terminate the chunked body
}

// GET /thermal — the newest 32x24 frame as JSON, plus the parser's own view of
// the stream. The stats are half the point during bring-up: bytes with no
// frames means the wire is talking but the FRAME LAYOUT constants are wrong
// (they are marked VERIFY-ON-HARDWARE), while zero bytes means TX/RX are
// swapped or the module is unpowered — two very different next moves.
static esp_err_t thermalHandler(httpd_req_t* req) {
    // A ThermalFrame is ~3 KB (24x32 floats). As a LOCAL it overflows the
    // httpd task's 4 KB stack — which it did, on the first request after this
    // endpoint shipped. The httpd task runs one handler at a time, so a single
    // file-scope buffer is both sufficient and the only place 3 KB belongs.
    // (Same lesson as the /range 2 KB body; the earlier comment guarded the
    // JSON buffer and missed the frame struct sitting right beside it.)
    static gymcu::ThermalFrame f;
    bool have = thermal::take(f);
    const gymcu::Parser::Stats s = thermal::statsSnapshot();

    char line[224];
    httpd_resp_set_type(req, "application/json");
    int m = snprintf(line, sizeof(line),
        "{\n  \"stream\": {\"bytes_seen\": %lu, \"frames_ok\": %lu, "
        "\"bad_checksum\": %lu, \"bad_header\": %lu, \"resyncs\": %lu, "
        "\"bytes_dropped\": %lu, \"timeouts\": %lu, \"ms_since_frame\": %ld},\n",
        (unsigned long)thermal::bytesSeen(), (unsigned long)s.frames_ok,
        (unsigned long)s.bad_checksum, (unsigned long)s.bad_header,
        (unsigned long)s.resyncs, (unsigned long)s.bytes_dropped,
        (unsigned long)s.timeouts,
        thermal::everSawFrame() ? (long)thermal::sinceLastFrameMs() : -1L);
    httpd_resp_send_chunk(req, line, m);

    if (!have) {
        const char* none = "  \"frame\": null\n}\n";
        httpd_resp_send_chunk(req, none, strlen(none));
        return httpd_resp_send_chunk(req, nullptr, 0);
    }
    // row-major, one JSON row per chunk: 768 floats do not belong on a 4 KB
    // stack (the lesson from the /range panic, applied before it bites)
    m = snprintf(line, sizeof(line),
                 "  \"frame\": {\"seq\": %lu, \"ta_c\": %.2f, "
                 "\"checksum_ok\": %s, \"rows\": %u, \"cols\": %u, \"px\": [",
                 (unsigned long)f.seq, f.ambient_c,
                 f.checksum_ok ? "true" : "false",
                 (unsigned)gymcu::ROWS, (unsigned)gymcu::COLS);
    httpd_resp_send_chunk(req, line, m);
    const float* px = &f.pixels[0][0];
    for (size_t p = 0; p < gymcu::PIXELS; p++) {
        m = snprintf(line, sizeof(line), "%s%.2f", p ? "," : "", px[p]);
        if (httpd_resp_send_chunk(req, line, m) != ESP_OK) return ESP_FAIL;
    }
    const char* tail = "]}\n}\n";
    httpd_resp_send_chunk(req, tail, strlen(tail));
    return httpd_resp_send_chunk(req, nullptr, 0);
}

// GET /thermal/raw — arm the tee, wait for it to fill, then report what the
// module ACTUALLY sends. This exists because a parser can only say "checksum
// failed"; it cannot say what the real layout is.
static esp_err_t thermalRawHandler(httpd_req_t* req) {
    // the snapshot lives at file scope: 3.6 KB has no business on the httpd
    // task's stack (see endpoints.h)
    static uint8_t d[3600];

    thermal::rawArm();
    uint32_t t0 = millis();
    while (thermal::rawBusy() && millis() - t0 < 6000) delay(20);
    const size_t n = thermal::rawCopy(d, sizeof(d));

    char line[176];
    httpd_resp_set_type(req, "text/plain");
    int m = snprintf(line, sizeof(line),
                     "captured %u bytes%s\n\nframe marks (5A 5A 02 06):\n",
                     (unsigned)n, thermal::rawBusy() ? " (TIMED OUT, partial)" : "");
    httpd_resp_send_chunk(req, line, m);

    // Mark = the FOUR-byte header, not the two sync bytes: 0x5A5A is a
    // reachable pixel value (231.30 degC) and can also straddle the byte
    // boundary between two ordinary values, so a 2-byte match is not evidence
    // of a frame start. Marks cannot overlap, which also keeps the gaps honest.
    size_t marks[8];
    int nm = 0;
    for (size_t i = 0; i + 3 < n && nm < 8; ) {
        if (d[i] == 0x5A && d[i + 1] == 0x5A && d[i + 2] == 0x02 && d[i + 3] == 0x06) {
            marks[nm++] = i;
            i += 4;
        } else {
            i++;
        }
    }
    for (int k = 0; k < nm; k++) {
        if (k == 0) m = snprintf(line, sizeof(line), "  @%5u\n", (unsigned)marks[0]);
        else        m = snprintf(line, sizeof(line), "  @%5u   gap %u\n",
                                 (unsigned)marks[k], (unsigned)(marks[k] - marks[k - 1]));
        httpd_resp_send_chunk(req, line, m);
    }
    if (nm < 2) {
        const char* few = "\n(need two marks to measure a frame; capture again)\n";
        httpd_resp_send_chunk(req, few, strlen(few));
        return httpd_resp_send_chunk(req, nullptr, 0);
    }

    // Only a gap that repeats is a frame length; a single gap could be one
    // dropped frame or a false mark.
    size_t flen = marks[1] - marks[0];
    bool consistent = true;
    for (int k = 2; k < nm; k++) if (marks[k] - marks[k - 1] != flen) consistent = false;
    m = snprintf(line, sizeof(line), "\n%d marks, gap %s at %u bytes\n",
                 nm, consistent ? "CONSISTENT" : "VARIES (first pair)", (unsigned)flen);
    httpd_resp_send_chunk(req, line, m);

    const size_t f0 = marks[0];
    const size_t fend = f0 + flen;          // guaranteed <= n: marks[1] < n
    // ---- checksum candidates over the real frame -----------------------------
    {
        uint32_t s16 = 0, s8 = 0;
        for (size_t k = f0; k + 2 < fend; k++) s16 += d[k];
        for (size_t k = f0; k + 1 < fend; k++) s8 += d[k];
        uint16_t le = (uint16_t)(d[fend - 2] | (d[fend - 1] << 8));
        uint16_t be = (uint16_t)((d[fend - 2] << 8) | d[fend - 1]);
        m = snprintf(line, sizeof(line),
                     "  sum16=%04X tailLE=%04X tailBE=%04X%s\n"
                     "  sum8=%02X   tail8=%02X%s\n",
                     (unsigned)(s16 & 0xFFFF), le, be,
                     (s16 & 0xFFFF) == le ? "  <-- LE16" :
                     (s16 & 0xFFFF) == be ? "  <-- BE16" : "",
                     (unsigned)(s8 & 0xFF), d[fend - 1],
                     (s8 & 0xFF) == d[fend - 1] ? "  <-- BYTE" : "");
        httpd_resp_send_chunk(req, line, m);
    }

    // ---- where does the pixel data start? -----------------------------------
    // EVERY offset, odd included: the int16 alignment is exactly what is in
    // question, so stepping by two would answer a question nobody asked.
    // Count is a hint, not a pixel count — Ta, metadata and trailers can sit
    // in the same numeric range as a temperature.
    const char* hdr = "\ndata-start probe (LE int16/100 in -40..300 C):\n";
    httpd_resp_send_chunk(req, hdr, strlen(hdr));
    for (size_t off = 0; off <= 16; off++) {
        int plaus = 0, total = 0;
        float mn = 1e9f, mx = -1e9f;
        for (size_t k = f0 + off; k + 1 < fend; k += 2) {
            float c = (float)(int16_t)(d[k] | (d[k + 1] << 8)) / 100.0f;
            total++;
            if (c > -40.0f && c < 300.0f) {
                plaus++;
                if (c < mn) mn = c;
                if (c > mx) mx = c;
            }
        }
        m = snprintf(line, sizeof(line), "  +%2u: %4d/%4d  %.2f..%.2f C\n",
                     (unsigned)off, plaus, total,
                     plaus ? (double)mn : 0.0, plaus ? (double)mx : 0.0);
        httpd_resp_send_chunk(req, line, m);
    }

    m = snprintf(line, sizeof(line), "\nframe head:\n ");
    httpd_resp_send_chunk(req, line, m);
    for (size_t k = f0; k < f0 + 16 && k < fend; k++) {
        m = snprintf(line, sizeof(line), " %02X", d[k]);
        httpd_resp_send_chunk(req, line, m);
    }
    m = snprintf(line, sizeof(line), "\nframe tail:\n ");
    httpd_resp_send_chunk(req, line, m);
    size_t tailFrom = flen > 16 ? fend - 16 : f0;   // no unsigned underflow
    for (size_t k = tailFrom; k < fend; k++) {
        m = snprintf(line, sizeof(line), " %02X", d[k]);
        httpd_resp_send_chunk(req, line, m);
    }
    httpd_resp_send_chunk(req, "\n", 1);
    return httpd_resp_send_chunk(req, nullptr, 0);
}

static esp_err_t captureHandler(httpd_req_t* req) {
    Range before = rangefinderRead();     // bracket the exposure — see pairRange
    camera_fb_t* fb = cameraCapture();
    if (!fb) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "capture failed");
        return ESP_FAIL;
    }
    PairedRange rg = pairRange(before, rangefinderRead());
    char id[40];
    makeCaptureId(id, sizeof(id));
    // range rides on a HEADER so a plain `curl -O` still gets a usable file
    // while `curl -D -` yields the distance
    char rangeHdr[40];
    if (rg.valid) snprintf(rangeHdr, sizeof(rangeHdr), "%u", (unsigned)rg.mm);
    else          snprintf(rangeHdr, sizeof(rangeHdr), "invalid:%s", rg.reason);
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "X-Capture-Id", id);
    httpd_resp_set_hdr(req, "X-Range-Mm", rangeHdr);
    esp_err_t r = httpd_resp_send(req, (const char*)fb->buf, fb->len);
    Serial.printf("[http] /capture %s: %u bytes range=%s %s\n", id, (unsigned)fb->len,
                  rangeHdr, r == ESP_OK ? "ok" : "SEND FAILED");
    cameraRelease(fb);
    return r;
}

static esp_err_t observationHandler(httpd_req_t* req) {
    Range rgBefore = rangefinderRead();   // bracket the exposure — see pairRange
    camera_fb_t* fb = cameraCapture();
    if (!fb) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "capture failed");
        return ESP_FAIL;
    }
    // Prepare the ENTIRE new observation before touching the held state: a
    // failed allocation must leave the previous JSON/JPEG pair fully intact —
    // never an old image wearing a new id.
    uint8_t* copy = (uint8_t*)ps_malloc(fb->len);
    if (!copy) {
        cameraRelease(fb);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                            "psram exhausted — previous observation preserved");
        return ESP_FAIL;
    }
    char id[40];
    bool synced = makeCaptureId(id, sizeof(id));
    PairedRange rg = pairRange(rgBefore, rangefinderRead());
    memcpy(copy, fb->buf, fb->len);
    size_t w = fb->width, h = fb->height, len = fb->len;
    cameraRelease(fb);

    // swap (the httpd task is the only writer, so this is single-threaded)
    if (heldJpg) free(heldJpg);
    heldJpg = copy;
    heldLen = len;
    strlcpy(heldId, id, sizeof(heldId));

    // timestamp is only claimed when the wall clock is real; a boot-millis id
    // is a fine pairing key but NOT a timestamp
    char ts[28];
    const char* tsField = "null";
    if (synced) {
        time_t now = time(nullptr);
        struct tm tm;
        gmtime_r(&now, &tm);
        snprintf(ts, sizeof(ts), "\"%04d-%02d-%02dT%02d:%02d:%02dZ\"",
                 tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                 tm.tm_hour, tm.tm_min, tm.tm_sec);
        tsField = ts;
    }

    // an unmeasurable distance is null, never a number; the REASON is a string
    // rather than a bare status code, because "the sensor said out of range",
    // "the driver call failed" and "the scene moved mid-capture" are different
    // facts and a single numeric field cannot tell them apart honestly
    char rangeField[16], reasonField[40];
    if (rg.valid) {
        snprintf(rangeField, sizeof(rangeField), "%u", (unsigned)rg.mm);
        snprintf(reasonField, sizeof(reasonField), "null");
    } else {
        snprintf(rangeField, sizeof(rangeField), "null");
        snprintf(reasonField, sizeof(reasonField), "\"%s\"", rg.reason);
    }

    // handoff §7 schema; pose/thermal/environment stay null in phase 1B
    char body[576];
    snprintf(body, sizeof(body),
             "{\n"
             "  \"capture_id\": \"%s\",\n"
             "  \"timestamp\": %s,\n"
             "  \"time_source\": \"%s\",\n"
             "  \"uptime_ms\": %lu,\n"
             "  \"plant_id\": null,\n"
             "  \"pose\": null,\n"
             "  \"range_mm\": %s,\n"
             "  \"range_invalid_reason\": %s,\n"
             "  \"rgb\": {\"file\": \"%s.jpg\", \"width\": %u, \"height\": %u, \"bytes\": %u},\n"
             "  \"thermal\": null,\n"
             "  \"environment\": null\n"
             "}\n",
             heldId, tsField, synced ? "ntp" : "unsynced",
             (unsigned long)millis(),
             rangeField, reasonField,
             heldId, (unsigned)w, (unsigned)h, (unsigned)len);
    httpd_resp_set_type(req, "application/json");
    esp_err_t r = httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
    Serial.printf("[http] /observation %s: %u bytes (%s)\n", heldId,
                  (unsigned)len, synced ? "ntp" : "unsynced");
    return r;
}

static esp_err_t lastJpgHandler(httpd_req_t* req) {
    if (!heldJpg) {
        httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "no /observation captured yet");
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "X-Capture-Id", heldId);
    return httpd_resp_send(req, (const char*)heldJpg, heldLen);
}

static esp_err_t streamHandler(httpd_req_t* req) {
    if (!cameraSetStreaming(true)) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "sensor reconfig failed");
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
    Serial.println("[http] /stream start (VGA)");
    esp_err_t r = ESP_OK;
    while (r == ESP_OK) {
        camera_fb_t* fb = esp_camera_fb_get();
        if (!fb) { r = ESP_FAIL; break; }
        char hdr[96];
        int n = snprintf(hdr, sizeof(hdr),
                         "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                         (unsigned)fb->len);
        r = httpd_resp_send_chunk(req, hdr, n);
        if (r == ESP_OK) r = httpd_resp_send_chunk(req, (const char*)fb->buf, fb->len);
        if (r == ESP_OK) r = httpd_resp_send_chunk(req, "\r\n", 2);
        esp_camera_fb_return(fb);
    }
    // client went away (the normal way a stream ends) — restore still resolution
    if (cameraSetStreaming(false)) {
        Serial.println("[http] /stream end (back to QSXGA)");
    } else {
        // stills would silently come back VGA-sized (the JSON stays honest, but
        // the dataset wouldn't be) — make the failure loud
        Serial.println("[http] /stream end but QSXGA restore FAILED — reboot before stills");
    }
    return r;
}

bool endpointsStart() {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.lru_purge_enable = true;   // a stuck stream socket gets evicted, not fatal
    // 4 KB (the default) has panicked twice as endpoints grew; 6 KB is margin
    // for the JSON/format work handlers legitimately do. The no-big-locals
    // discipline in endpoints.h still stands — this only stops a near-miss
    // from taking the whole board down with it.
    cfg.stack_size = 6144;
    if (httpd_start(&server, &cfg) != ESP_OK) return false;
    static const httpd_uri_t routes[] = {
        {"/",            HTTP_GET, indexHandler,       nullptr, false, false, nullptr},
        {"/stream",      HTTP_GET, streamHandler,      nullptr, false, false, nullptr},
        {"/capture",     HTTP_GET, captureHandler,     nullptr, false, false, nullptr},
        {"/observation", HTTP_GET, observationHandler, nullptr, false, false, nullptr},
        {"/last.jpg",    HTTP_GET, lastJpgHandler,     nullptr, false, false, nullptr},
        {"/range",       HTTP_GET, rangeHandler,       nullptr, false, false, nullptr},
        {"/thermal",     HTTP_GET, thermalHandler,     nullptr, false, false, nullptr},
        {"/thermal/raw", HTTP_GET, thermalRawHandler,  nullptr, false, false, nullptr},
    };
    for (auto& u : routes) {
        if (httpd_register_uri_handler(server, &u) != ESP_OK) {
            Serial.printf("[http] failed to register %s — refusing a half-wired server\n",
                          u.uri);
            httpd_stop(server);
            server = nullptr;
            return false;
        }
    }
    return true;
}
