// Host-side fixture runner for the GY-MCU90640 parser (mlx90640 Phase 1).
// Compiled by test/thermal/run.sh with the SAME gymcu_parser.cpp the firmware
// builds — the byte-level contract is verified on the laptop, offline.
//
//   host_runner <stream.bin> <bytewise|chunk7|whole|timeout700>
//
// Prints canonical JSON: the frames observed (polling take() after every feed
// call) plus the final stats. Chunking must not change parser STATE (stats);
// what the consumer SEES can differ (latest-wins slot), which is why run.sh
// compares full output only for bytewise and stats across the other modes.
// timeout700 feeds bytewise and injects the UART layer's idle-gap
// discardPartial() call at offset 700 — the timeout-contract fixture.
// NOTE: `pio test` is not used in this repo; this dir is plain host tooling.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "gymcu_parser.h"

static const char* MODES = "bytewise|chunk7|whole|timeout700";

static void printFrame(const gymcu::ThermalFrame& f, bool first) {
    const float* px = &f.pixels[0][0];
    float mn = px[0], mx = px[0];
    for (size_t p = 1; p < gymcu::PIXELS; p++) {
        if (px[p] < mn) mn = px[p];
        if (px[p] > mx) mx = px[p];
    }
    printf("%s    {\"seq\": %u, \"ta_c\": %.2f, \"min\": %.2f, \"max\": %.2f, \"px\": [",
           first ? "" : ",\n", f.seq, f.ambient_c, mn, mx);
    for (size_t p = 0; p < gymcu::PIXELS; p++)
        printf("%s%.2f", p ? ", " : "", px[p]);
    printf("]}");
}

int main(int argc, char** argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s <stream.bin> <%s>\n", argv[0], MODES);
        return 2;
    }
    FILE* fp = fopen(argv[1], "rb");
    if (!fp) { fprintf(stderr, "cannot open %s\n", argv[1]); return 2; }
    fseek(fp, 0, SEEK_END);
    long n = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    uint8_t* data = (uint8_t*)malloc(n);
    if (fread(data, 1, n, fp) != (size_t)n) { fprintf(stderr, "short read\n"); return 2; }
    fclose(fp);

    long timeout_at = -1;
    size_t chunk;
    if (!strcmp(argv[2], "bytewise")) chunk = 1;
    else if (!strcmp(argv[2], "chunk7")) chunk = 7;
    else if (!strcmp(argv[2], "whole")) chunk = (size_t)n;
    else if (!strcmp(argv[2], "timeout700")) { chunk = 1; timeout_at = 700; }
    else { fprintf(stderr, "bad mode %s; want %s\n", argv[2], MODES); return 2; }

    gymcu::Parser parser;
    gymcu::ThermalFrame f;
    printf("{\n  \"frames\": [\n");
    bool first = true;
    for (long off = 0; off < n; off += chunk) {
        if (off == timeout_at) parser.discardPartial();
        size_t len = (size_t)(n - off) < chunk ? (size_t)(n - off) : chunk;
        parser.feed(data + off, len);
        while (parser.take(f)) { printFrame(f, first); first = false; }
    }
    const gymcu::Parser::Stats& s = parser.stats();
    printf("\n  ],\n  \"stats\": {\"frames_ok\": %u, \"bad_checksum\": %u, "
           "\"bad_header\": %u, \"resyncs\": %u, \"bytes_dropped\": %u, "
           "\"overwritten\": %u, \"timeouts\": %u}\n}\n",
           s.frames_ok, s.bad_checksum, s.bad_header, s.resyncs,
           s.bytes_dropped, s.overwritten, s.timeouts);
    free(data);
    return 0;
}
