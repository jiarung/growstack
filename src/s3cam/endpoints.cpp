#include "endpoints.h"

#include <Arduino.h>
#include <esp_http_server.h>
#include <time.h>

#include "camera.h"

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

// ---- the /observation-held frame (PSRAM copy; same exposure as its JSON) ----
static uint8_t* heldJpg = nullptr;
static size_t heldLen = 0;
static char heldId[40] = "";

// ---- handlers ---------------------------------------------------------------
static esp_err_t indexHandler(httpd_req_t* req) {
    char body[400];
    snprintf(body, sizeof(body),
             "s3cam bring-up (phase 1B)\n"
             "sensor: %s\npsram: %u bytes (free %u)\nheap free: %u\n\n"
             "GET /stream      MJPEG live view\n"
             "GET /capture     full-res still (X-Capture-Id header)\n"
             "GET /observation still + observation JSON (pairs with /last.jpg)\n"
             "GET /last.jpg    the frame the last /observation held\n",
             cameraSensorName(), ESP.getPsramSize(), ESP.getFreePsram(),
             ESP.getFreeHeap());
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t captureHandler(httpd_req_t* req) {
    camera_fb_t* fb = cameraCapture();
    if (!fb) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "capture failed");
        return ESP_FAIL;
    }
    char id[40];
    makeCaptureId(id, sizeof(id));
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "X-Capture-Id", id);
    esp_err_t r = httpd_resp_send(req, (const char*)fb->buf, fb->len);
    Serial.printf("[http] /capture %s: %u bytes %s\n", id, (unsigned)fb->len,
                  r == ESP_OK ? "ok" : "SEND FAILED");
    cameraRelease(fb);
    return r;
}

static esp_err_t observationHandler(httpd_req_t* req) {
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

    // handoff §7 schema; pose/thermal/environment stay null in phase 1B
    char body[512];
    snprintf(body, sizeof(body),
             "{\n"
             "  \"capture_id\": \"%s\",\n"
             "  \"timestamp\": %s,\n"
             "  \"time_source\": \"%s\",\n"
             "  \"uptime_ms\": %lu,\n"
             "  \"plant_id\": null,\n"
             "  \"pose\": null,\n"
             "  \"rgb\": {\"file\": \"%s.jpg\", \"width\": %u, \"height\": %u, \"bytes\": %u},\n"
             "  \"thermal\": null,\n"
             "  \"environment\": null\n"
             "}\n",
             heldId, tsField, synced ? "ntp" : "unsynced",
             (unsigned long)millis(),
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
    if (httpd_start(&server, &cfg) != ESP_OK) return false;
    static const httpd_uri_t routes[] = {
        {"/",            HTTP_GET, indexHandler,       nullptr, false, false, nullptr},
        {"/stream",      HTTP_GET, streamHandler,      nullptr, false, false, nullptr},
        {"/capture",     HTTP_GET, captureHandler,     nullptr, false, false, nullptr},
        {"/observation", HTTP_GET, observationHandler, nullptr, false, false, nullptr},
        {"/last.jpg",    HTTP_GET, lastJpgHandler,     nullptr, false, false, nullptr},
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
