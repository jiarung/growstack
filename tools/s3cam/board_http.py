"""One way to GET from the board, so every tool keeps the board's own reason.

urllib turns a 500 into "HTTP Error 500: Internal Server Error" and throws the
body away — but the body is the entire diagnosis. This board answers
/observation and /capture with either "capture failed" (the sensor handed back
no fresh frame) or "psram exhausted — previous observation preserved"
(memory), and those are different faults with different next steps. Rendered
as the same generic sentence they cost an afternoon on 2026-09-29.

It lived only in observe._get at first, which fixed one path and left the
viewer's /thermal proxy, the servo commands and the focus sweeps still saying
"HTTP Error 500". The fix belongs at the one place every tool goes through.

HTTPError subclasses both URLError and OSError, so re-raising it with the body
folded into the message keeps every existing `except` clause working.
"""

import urllib.error
import urllib.request


def get_with_headers(url, timeout):
    """(body, headers) — for callers that read X-Capture-Id, X-Range-Mm, ..."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        try:
            said = e.read().decode("utf-8", "replace").strip()
        except Exception:                                        # noqa: BLE001
            said = ""
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason} — {said}" if said else str(e.reason),
            e.headers, None) from None


def get(url, timeout):
    return get_with_headers(url, timeout)[0]
