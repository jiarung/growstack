#include "gymcu_parser.h"

#include <string.h>

namespace gymcu {

namespace {
inline uint16_t rd16(const uint8_t* p) {   // the wire byte order, in ONE place
    return (uint16_t)(p[0] | (p[1] << 8));
}
}  // namespace

void Parser::reset() {
    pos_ = 0;
    have_ = false;
    seq_ = 0;
    memset(&stats_, 0, sizeof(stats_));
}

void Parser::feed(const uint8_t* data, size_t len) {
    size_t i = 0;
    while (i < len) {
        if (pos_ < HDR) {
            const uint8_t b = data[i++];
            if (pos_ == 0) {
                if (b != SYNC0) { stats_.bytes_dropped++; continue; }
            } else if (pos_ == 1 && b != SYNC1) {
                // SYNC0 == SYNC1, so a byte that fails the SYNC1 test cannot
                // itself restart a header: both the abandoned 0x5A and this
                // byte are noise.
                stats_.bytes_dropped += 2;
                pos_ = 0;
                continue;
            }
            buf_[pos_++] = b;
            if (pos_ == HDR && (buf_[2] != TYPE || buf_[3] != SUBTYPE)) {
                stats_.bad_header++;
                resync();
            }
            continue;
        }
        // Header accepted: the rest of the frame is an unconditional fill, so
        // consume the caller's chunk in bulk instead of a per-byte state
        // machine — that is the whole benefit of feeding DMA bursts.
        size_t n = FRAME_LEN - pos_;
        if (n > len - i) n = len - i;
        memcpy(buf_ + pos_, data + i, n);
        pos_ += n;
        i += n;
        if (pos_ == FRAME_LEN) accept();
    }
}

void Parser::accept() {
    uint32_t sum = 0;
    for (size_t i = 0; i < FRAME_LEN - 2; i++) sum += buf_[i];
    const bool csOk = (uint16_t)sum == rd16(buf_ + FRAME_LEN - 2);
    if (!csOk) {
        stats_.bad_checksum++;
        if (policy_ == ChecksumPolicy::STRICT) {
            resync();
            return;
        }
        // REPORT: fall through and decode, but the frame says so itself
    }
    // the 2-D pixel array is contiguous row-major — fill it flat, no per-pixel
    // division/modulo on the ingest path
    float* out = &slot_.pixels[0][0];
    const uint8_t* p = buf_ + HDR;
    for (size_t k = 0; k < PIXELS; k++, p += 2) out[k] = (int16_t)rd16(p) / 100.0f;
    slot_.ambient_c = (int16_t)rd16(buf_ + OFF_TA) / 100.0f;
    slot_.checksum_ok = csOk;
    slot_.seq = ++seq_;
    if (have_) stats_.overwritten++;
    have_ = true;
    stats_.frames_ok++;
    pos_ = 0;
}

void Parser::resync() {
    // The buffered bytes may CONTAIN the real frame start (one corrupt byte
    // shifted us). Hunt for the next PLAUSIBLE header inside the buffer and
    // keep the tail from there — one bad byte costs one frame, not the stream.
    // STRICTLY ITERATIVE with a single trailing memmove: a hostile buffer
    // packed with 0x5A pairs (a corrupt frame CAN contain thousands) must cost
    // a scan, not a recursion — ~1500 stack frames would overflow an ESP32
    // task stack — and not a quadratic cascade of memmoves either.
    stats_.resyncs++;
    size_t at = 1;
    for (;;) {
        while (at + 1 < pos_ && !(buf_[at] == SYNC0 && buf_[at + 1] == SYNC1)) at++;
        if (at + 1 >= pos_) {
            // no pair left; a trailing lone 0x5A may pair with the NEXT byte
            // fed (pos_ >= HDR always holds here — resync is only reachable
            // from the pos_==HDR header check or a full-frame accept())
            if (buf_[pos_ - 1] == SYNC0) {
                stats_.bytes_dropped += pos_ - 1;
                buf_[0] = SYNC0;
                pos_ = 1;
            } else {
                stats_.bytes_dropped += pos_;
                pos_ = 0;
            }
            return;
        }
        // candidate: apply the same type/subtype rule feed() enforces at pos 4
        // when enough bytes are buffered to judge; too-short candidates are
        // accepted here and judged by feed() as bytes arrive
        if (pos_ - at >= 4 && (buf_[at + 2] != TYPE || buf_[at + 3] != SUBTYPE)) {
            stats_.bad_header++;
            at++;
            continue;
        }
        stats_.bytes_dropped += at;
        memmove(buf_, buf_ + at, pos_ - at);
        pos_ -= at;
        return;
    }
}

void Parser::discardPartial() {
    // The parser never reads a clock; the UART layer calls this after an idle
    // gap longer than a frame time. A partial frame is abandoned and counted —
    // unlike reset(), the diagnostic stats and the frame sequence survive.
    if (pos_ == 0) return;
    stats_.timeouts++;
    stats_.bytes_dropped += pos_;
    pos_ = 0;
}

bool Parser::take(ThermalFrame& out) {
    if (!have_) return false;
    out = slot_;
    have_ = false;
    return true;
}

}  // namespace gymcu
