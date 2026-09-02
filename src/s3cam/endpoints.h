#pragma once

// Phase 1B pull-model HTTP contract (phase-1b.md D1=A). One client at a time —
// this is a bring-up skeleton, not a server. In particular: /stream occupies
// the single httpd task, so a /capture issued DURING a stream waits (it does
// not run concurrently and does not fail fast) — close the stream first.
//
// HANDLER STACK: the httpd task gets 4 KB. A multi-KB local buffer in a
// handler overflows it and panics the core ("Stack canary watchpoint
// triggered (httpd)") — build long responses with httpd_resp_send_chunk() and
// a small line buffer instead of assembling them in one array.
//
//   GET /            tiny index: links + sensor/PSRAM status
//   GET /stream      MJPEG live view (sensor drops to VGA while streaming)
//   GET /capture     fresh full-res still -> image/jpeg, X-Capture-Id header
//   GET /observation fresh still HELD in PSRAM + observation JSON (handoff §7,
//                    pose/thermal/environment null) — id pairs it with /last.jpg
//   GET /last.jpg    the still held by the LAST /observation (same frame as its
//                    JSON — the pairing is same-exposure, not two captures)

bool endpointsStart();
