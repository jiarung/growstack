#pragma once

// HOW THE HEAD IS MOUNTED. One fact, declared once.
//
// Two sensors ride one bracket — the OV5640 and the MLX90640 — so one mounting
// decides the correction for both. Until 2026-09-28 that fact was written out
// three times in three vocabularies: camera.cpp set sensor registers,
// thermal_uart.cpp reversed a pixel array, and two separate functions turned
// each into a tag the host then had to reconcile. Nothing kept them in step
// except a comment asking whoever edited one to remember the other.
//
// That instruction failed twice. The camera moved to vflip-only on 09-24 when
// text photographed through the lens settled it; the thermal followed four
// days later. In between, every /observation carried one image mirrored
// against the other — and a mirror is precisely the transform the
// four-parameter registration cannot absorb, so nothing downstream could have
// corrected for it. orientation_mismatch() on the host reported it on every
// single observation for those four days, correctly, to nobody.
//
// MEASURED, not reasoned. Rotation preserves handedness and a mirror destroys
// it, and a room full of furniture shows neither — which is why three rounds
// of arguing from verbal descriptions of what looked wrong produced a
// confidently wrong answer. Text settles it in ten seconds: photograph a
// screen. "INSERT" coming back as "TЯƎƧИI" is upright and mirrored, which is
// one flip, not a rotation. The thermal half was then measured against that
// corrected camera, both halves of one observation: a hand filling the upper
// third sat at 34% of the RGB width and at 66-74% of the rotated thermal,
// 26-34% once mirrored back — with the forearm agreeing independently.
//
// TO REMOUNT THE HEAD: change these two lines and nothing else.
namespace head {

constexpr bool MIRRORED_V = true;    // rows arrive bottom-to-top
constexpr bool MIRRORED_H = false;   // columns arrive left-to-right

// One vocabulary for both sensors, so a host comparing the two tags is
// comparing like with like. It used to be two — the camera said "none" where
// the thermal said "wire" — and the host grew a translation table to make the
// comparison work, which is a fix at the wrong end: the device knows what it
// did and should say it the same way twice.
constexpr const char* tag() {
    return MIRRORED_V && MIRRORED_H ? "rot180"
         : MIRRORED_V               ? "vflip"
         : MIRRORED_H               ? "hmirror"
                                    : "none";
}

}  // namespace head
