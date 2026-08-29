#!/usr/bin/env python3
"""k-model adoption engine, pure core (Phase C of tasks/photone-cal-pipeline.md).

The sticky per-bucket "currently adopted value" with its brake: consumers read
ONLY k_adopted. Everything here is deterministic and I/O-free; the orchestrator
(compute-k-models.py) feeds the latest adopted row per bucket plus this round's
k_model bucket and writes back whatever changed. ack-k-hold.sh drives ack().

State machine (blueprint Step 5): adoption_state in {seed, adopted, held, stale}.
    seed     initial per-target value, not a real adoption
    adopted  a real value: in-band valid estimates update silently, out-of-band
             ones freeze the value and go to held awaiting a human
    held     value frozen at prev; pending_* carries the challenger
    stale    a carried value that outlived its 30d grace without promotion —
             sticky (never silently back to seed) but continuously alarmed

    ./kadopt.py --selftest
"""
import math
import sys
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# Seeds are the SYSTEM'S CURRENT BEHAVIOR per target, not a universal 1.0:
# multiplying by 1.0 is a no-op for the lux targets, but for as7341_ppfd the
# adopted value IS the CAL constant — 1.0 would be a few-hundred-fold error.
SEEDS = {"bh1750_lux_main": 1.0, "bh1750_lux_ref": 1.0, "as7341_ppfd": 0.0017469}
UNITS = {"bh1750_lux_main": "multiplier", "bh1750_lux_ref": "multiplier",
         "as7341_ppfd": "umol_m2_s_per_count"}
BAND = 0.10          # |delta| beyond this fraction of the adopted value -> hold
CARRY_GRACE_D = 30.0  # a carried value must be re-earned within this many days


def _row(key, computed_at, **kw):
    target, source, regime, epoch = key
    row = {"target": target, "source": source, "regime": regime, "epoch": epoch,
           "value": SEEDS[target], "unit": UNITS[target],
           "adoption_state": "seed", "model_id": "", "prev_value": None,
           "reason": "seed", "pending_value": None, "pending_model_id": None,
           "pending_evidence_rev": None, "hold_ack_state": "none",
           "adopted_at": computed_at, "state_since": computed_at}
    row.update(kw)
    return row


def _finish(row, old_state, computed_at):
    """state_since moves ONLY when adoption_state changes — a held bucket that
    keeps receiving new evidence revs stays held-since-the-beginning, so the
    stuck alert's 24h clock cannot be reset by churn."""
    if row["adoption_state"] != old_state:
        row["state_since"] = computed_at
    return row


def _event(row, model, event_type, message):
    return {"target": row["target"], "source": row["source"],
            "regime": row["regime"], "epoch": row["epoch"],
            "event_type": event_type,
            "model_id": model["model_id"] if model else "",
            "estimate": model.get("estimate") if model else None,
            "adopted_value": row["value"],
            "evidence_rev": model["evidence_rev"] if model else "",
            "message": message}


def within_band(estimate, adopted):
    return abs(estimate - adopted) / adopted <= BAND


def materialize(key, prev_epoch_row, epoch_start, computed_at):
    """A universe bucket with no row yet: carry the previous epoch's earned
    value if there is one, else seed. Blueprint: a held predecessor's pending
    is dropped and the carry rule takes over (its frozen value IS the adopted
    value); seed/stale predecessors have nothing earned to carry."""
    if prev_epoch_row and prev_epoch_row["adoption_state"] in ("adopted", "held"):
        return _row(key, computed_at, value=prev_epoch_row["value"],
                    adoption_state="adopted", reason="carry",
                    prev_value=prev_epoch_row["value"],
                    model_id=prev_epoch_row.get("model_id", ""))
    return _row(key, computed_at)


def adopt_step(prev_row, prev_epoch_row, model, epoch_start, computed_at):
    """One round for one universe bucket -> (row, written, events).

    `prev_row`: latest k_adopted row for this exact bucket (None = never seen);
    `prev_epoch_row`: latest row of the same (target, source, regime) in the
    epoch this one chains from (for carry); `model`: this round's k_model
    bucket (always present — empty buckets are materialized unvalidated);
    `epoch_start`: this epoch's start (None for e0-legacy: no carry clock).
    `written` says whether the row changed (the Influx no-change-no-write rule:
    value/state/pending/ack are the triggers; model_id alone is not).
    """
    events = []
    if prev_row is None:
        row = materialize((model["target"], model["source"], model["regime"],
                           model["epoch"]), prev_epoch_row, epoch_start, computed_at)
        written = True
        old_state = None            # fresh row: _row already stamped state_since
    else:
        row = dict(prev_row)
        written = False
        old_state = row["adoption_state"]

    status, est = model["status"], model.get("estimate")
    state = row["adoption_state"]

    def alarm_hold(msg):
        events.append(_event(row, model, "hold", msg))

    # --- held: frozen, waiting for a human or for the estimate to come home
    if state == "held":
        recovered = (status == "valid" and est is not None
                     and within_band(est, row["value"])) or status != "valid"
        if recovered:
            row.update(adoption_state="adopted", pending_value=None,
                       pending_model_id=None, pending_evidence_rev=None,
                       hold_ack_state="none", reason="hold-resolved",
                       adopted_at=computed_at)
            events.append(_event(row, model, "resolved",
                                 "hold 自動解除:估計回到帶內或降級,現值未變"))
            return _finish(row, old_state, computed_at), True, events
        if model["evidence_rev"] == row["pending_evidence_rev"]:
            return row, written, events        # same evidence: nothing new to say
        row.update(pending_value=est, pending_model_id=model["model_id"],
                   pending_evidence_rev=model["evidence_rev"],
                   hold_ack_state="awaiting", adopted_at=computed_at)
        alarm_hold("hold 期間出現新證據版本,仍在剎車帶外 — 待人工確認")
        return _finish(row, old_state, computed_at), True, events

    # --- carry expiry: a carried value must be re-earned within the grace
    if (state == "adopted" and row.get("reason") == "carry" and epoch_start
            and computed_at > epoch_start + timedelta(days=CARRY_GRACE_D)
            and status != "valid"):
        row.update(adoption_state="stale", reason="carry-expired",
                   adopted_at=computed_at)
        events.append(_event(row, model, "stale",
                             "carry 值逾 30d 未由本 epoch 證據接手 — 補量測"))
        return _finish(row, old_state, computed_at), True, events

    # --- seed: bootstrap on the first respectable estimate (state, not value:
    #     the as7341 seed is 0.0017469, not 1.0)
    if state == "seed":
        if status in ("provisional", "valid") and est is not None:
            row.update(value=est, prev_value=row["value"],
                       adoption_state="adopted", model_id=model["model_id"],
                       reason="bootstrap", adopted_at=computed_at)
            return _finish(row, old_state, computed_at), True, events
        return row, written, events

    # --- adopted (and sticky-stale): the automatic adoption table
    if status == "valid" and est is not None:
        if row["hold_ack_state"] == "acked_reject" \
                and model["evidence_rev"] == row["pending_evidence_rev"]:
            return row, written, events        # human already rejected THIS evidence
        if within_band(est, row["value"]):
            if est == row["value"]:
                return row, written, events    # nothing changed -> no write
            row.update(prev_value=row["value"], value=est,
                       adoption_state="adopted", model_id=model["model_id"],
                       reason="in-band", hold_ack_state="none",
                       pending_evidence_rev=None, adopted_at=computed_at)
            return _finish(row, old_state, computed_at), True, events
        row.update(adoption_state="held", pending_value=est,
                   pending_model_id=model["model_id"],
                   pending_evidence_rev=model["evidence_rev"],
                   hold_ack_state="awaiting", adopted_at=computed_at)
        alarm_hold("新 valid 估計偏離採納值超過 ±10% — 漏登 epoch?待人工確認")
        return _finish(row, old_state, computed_at), True, events

    if status == "stale" and state == "adopted":
        events.append(_event(row, model, "stale",
                             "模型逾 90d 無新參考量測 — 值黏住,請補量"))
        return row, written, events

    # provisional never displaces an earned value; unvalidated says nothing
    return row, written, events


def ack(latest_row, rev, decision, now):
    """ack-k-hold.sh core -> (row|None, error|None). CAS: the authority is the
    LATEST row's pending_evidence_rev — a drifted rev is refused so the human
    always confirms what is actually pending.

    KNOWN LIMIT of reject: the rejected rev lives on the latest row only, so a
    later in-band adoption clears the marker; if a data rollback then
    reproduces the rejected rev out-of-band, the bucket re-holds silently (the
    k_event dedupe remembers the old alert). The k-adoption-stuck state rule
    backstops that: any bucket held >24h alarms regardless of event history."""
    if latest_row is None:
        return None, "no k_adopted row for this bucket"
    if latest_row["hold_ack_state"] != "awaiting" or not latest_row["pending_evidence_rev"]:
        return None, "bucket has no pending hold awaiting ack"
    if latest_row["pending_evidence_rev"] != rev:
        return None, (f"evidence rev mismatch: pending is "
                      f"{latest_row['pending_evidence_rev']} "
                      f"(value {latest_row['pending_value']}) — re-review that")
    row = dict(latest_row)
    if decision == "accept":
        row.update(prev_value=row["value"], value=row["pending_value"],
                   model_id=row["pending_model_id"] or row["model_id"],
                   adoption_state="adopted", reason="ack-accept",
                   pending_value=None, pending_model_id=None,
                   pending_evidence_rev=None, hold_ack_state="acked_accept",
                   adopted_at=now, state_since=now)
    else:
        # value stays; the rejected rev is KEPT as a marker so the same
        # evidence never re-alarms (a new rev starts a fresh hold) — see the
        # docstring for the known limit of this marker
        row.update(adoption_state="adopted", reason="ack-reject",
                   pending_value=None, pending_model_id=None,
                   hold_ack_state="acked_reject", adopted_at=now,
                   state_since=now)
    return row, None


def to_line(row, ts_s):
    """One k_adopted row -> a line-protocol line, EVERY field always present
    (float None -> -1, string None -> ""): an omitted field would let a later
    Influx last()-per-field reconstruction resurrect a cleared pending_* from
    an older point. Shared by compute-k-models.py and ack-k-hold.sh so the two
    writers can never drift. Pure string building."""
    def tag(s):
        return s.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

    def qs(s):
        return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'

    tags = ",".join(f"{k}={tag(row[k])}" for k in ("target", "source", "regime", "epoch"))
    f = [f"value={row['value']}",
         f"unit={qs(row['unit'])}",
         f"adoption_state={qs(row['adoption_state'])}",
         f"model_id={qs(row['model_id'])}",
         f"prev_value={row['prev_value'] if row['prev_value'] is not None else -1.0}",
         f"reason={qs(row['reason'])}",
         f"pending_value={row['pending_value'] if row['pending_value'] is not None else -1.0}",
         f"pending_model_id={qs(row['pending_model_id'])}",
         f"pending_evidence_rev={qs(row['pending_evidence_rev'])}",
         f"hold_ack_state={qs(row['hold_ack_state'])}",
         f"state_since={qs(row['state_since'].strftime('%Y-%m-%dT%H:%M:%SZ'))}"]
    return f"k_adopted,{tags} {','.join(f)} {ts_s}"


# ---------------------------------------------------------------- selftest

def selftest():
    K = ("bh1750_lux_ref", "daylight", "diffuse", "bh1750_lux_ref-e2")
    T = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    ES = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)

    def mk(status, est, rev="rev000000001", mid="m@1"):
        return {"target": K[0], "source": K[1], "regime": K[2], "epoch": K[3],
                "status": status, "estimate": est, "model_id": mid,
                "evidence_rev": rev}

    # seed materialization + bootstrap (state decides, not the value)
    r, w, ev = adopt_step(None, None, mk("unvalidated", None), ES, T)
    assert (r["adoption_state"], r["value"], w, ev) == ("seed", 1.0, True, [])
    r2, w, ev = adopt_step(r, None, mk("provisional", 1.19), ES, T)
    assert (r2["adoption_state"], r2["value"], r2["reason"]) == ("adopted", 1.19, "bootstrap")
    assert w and not ev
    ad = {**r2}

    # in-band valid: silent update; identical value: no write
    r3, w, ev = adopt_step(ad, None, mk("valid", 1.20), ES, T)
    assert (r3["value"], r3["reason"], w, ev) == (1.20, "in-band", True, [])
    r4, w, ev = adopt_step(r3, None, mk("valid", 1.20), ES, T)
    assert not w and not ev, "same value -> no write, model_id alone never writes"

    # provisional never displaces; unvalidated says nothing; both no-write
    for s in ("provisional", "unvalidated"):
        r5, w, ev = adopt_step(r3, None, mk(s, 2.0), ES, T)
        assert r5["value"] == 1.20 and not w and not ev

    # out-of-band valid -> held + hold event; value frozen
    r6, w, ev = adopt_step(r3, None, mk("valid", 1.50, rev="rev000000002"), ES, T)
    assert (r6["adoption_state"], r6["value"], r6["pending_value"]) == ("held", 1.20, 1.50)
    assert r6["hold_ack_state"] == "awaiting" and w
    assert [e["event_type"] for e in ev] == ["hold"]

    # held + same rev rerun: silent; held + NEW rev still out-of-band: re-alarm
    r7, w, ev = adopt_step(r6, None, mk("valid", 1.50, rev="rev000000002"), ES, T)
    assert not w and not ev, "same evidence rev must not re-alert"
    later = T + timedelta(hours=23)
    r8, w, ev = adopt_step(r6, None, mk("valid", 1.55, rev="rev000000003"), ES, later)
    assert r8["pending_value"] == 1.55 and [e["event_type"] for e in ev] == ["hold"]
    # state_since regression: churn inside held must NOT reset the stuck clock
    assert r8["state_since"] == r6["state_since"] == T, \
        "a new rev at hour 23 must not reset held-since (stuck alert at 24h+)"
    assert r8["adopted_at"] == later, "the row itself is rewritten (audit trail)"

    # held recovery: back in band OR downgraded -> adopted, value unchanged, resolved
    for m in (mk("valid", 1.21, rev="rev000000004"), mk("unvalidated", None, rev="rev5")):
        r9, w, ev = adopt_step(r6, None, m, ES, T)
        assert (r9["adoption_state"], r9["value"], r9["pending_value"]) == ("adopted", 1.20, None)
        assert [e["event_type"] for e in ev] == ["resolved"] and w

    # ack accept / reject / rev-mismatch / no-pending
    ra, err = ack(r6, "rev000000002", "accept", T)
    assert err is None and ra["value"] == 1.50 and ra["adoption_state"] == "adopted"
    assert ra["hold_ack_state"] == "acked_accept" and ra["pending_evidence_rev"] is None
    rr, err = ack(r6, "rev000000002", "reject", T)
    assert err is None and rr["value"] == 1.20 and rr["hold_ack_state"] == "acked_reject"
    assert rr["pending_evidence_rev"] == "rev000000002", "rejected rev stays as the marker"
    _, err = ack(r6, "revWRONG", "accept", T)
    assert err and "mismatch" in err
    _, err = ack(r3, "rev000000002", "accept", T)
    assert err, "no pending hold -> refuse"

    # reject silence: same rev valid out-of-band estimate never re-alarms;
    # a NEW rev starts a fresh hold
    r10, w, ev = adopt_step(rr, None, mk("valid", 1.50, rev="rev000000002"), ES, T)
    assert not w and not ev, "rejected evidence must stay silent"
    r11, w, ev = adopt_step(rr, None, mk("valid", 1.52, rev="rev000000009"), ES, T)
    assert r11["adoption_state"] == "held" and [e["event_type"] for e in ev] == ["hold"]

    # model-status stale with an adopted value: keep + alarm, no write
    r12, w, ev = adopt_step(r3, None, mk("stale", 1.19, rev="rev000000006"), ES, T)
    assert r12["value"] == 1.20 and not w and [e["event_type"] for e in ev] == ["stale"]

    # carry: predecessor adopted -> value crosses the epoch, reason=carry;
    # held predecessor carries its FROZEN value; seed predecessor does not carry
    prev_ad = _row(("bh1750_lux_ref", "daylight", "diffuse", "bh1750_lux_ref-e1"),
                   T - timedelta(days=2), value=1.18, adoption_state="adopted")
    rc, w, ev = adopt_step(None, prev_ad, mk("unvalidated", None), ES, T)
    assert (rc["value"], rc["reason"], rc["adoption_state"]) == (1.18, "carry", "adopted")
    prev_hd = dict(prev_ad, adoption_state="held", pending_value=9.9)
    rc2, _, _ = adopt_step(None, prev_hd, mk("unvalidated", None), ES, T)
    assert (rc2["value"], rc2["pending_value"]) == (1.18, None), \
        "held predecessor: pending dropped, frozen value carried"
    prev_seed = _row(("bh1750_lux_ref", "daylight", "diffuse", "bh1750_lux_ref-e1"), T)
    rc3, _, _ = adopt_step(None, prev_seed, mk("unvalidated", None), ES, T)
    assert rc3["adoption_state"] == "seed", "nothing earned -> seed, not carry"

    # carry expiry: past start+30d with no valid promotion -> sticky stale + alarm
    late = ES + timedelta(days=31)
    rce, w, ev = adopt_step(rc, None, mk("unvalidated", None), ES, late)
    assert (rce["adoption_state"], rce["value"]) == ("stale", 1.18), "sticky, never back to seed"
    assert [e["event_type"] for e in ev] == ["stale"]
    # ...but a valid estimate before expiry promotes normally (in-band adopt)
    rcv, w, ev = adopt_step(rc, None, mk("valid", 1.19, rev="rev000000007"), ES, T)
    assert (rcv["adoption_state"], rcv["value"], rcv["reason"]) == ("adopted", 1.19, "in-band")

    # sticky-stale bucket re-earning valid: normal table (in-band adopt / hold)
    rsv, w, ev = adopt_step(rce, None, mk("valid", 1.20, rev="rev000000008"), ES,
                            late + timedelta(days=1))
    assert (rsv["adoption_state"], rsv["value"]) == ("adopted", 1.20)
    rsh, w, ev = adopt_step(rce, None, mk("valid", 2.0, rev="rev000000010"), ES,
                            late + timedelta(days=1))
    assert rsh["adoption_state"] == "held" and [e["event_type"] for e in ev] == ["hold"]

    # as7341 seed is the CAL constant, not 1.0
    r13, _, _ = adopt_step(None, None,
                           {"target": "as7341_ppfd", "source": "lamp",
                            "regime": "none", "epoch": "as7341_ppfd-e1",
                            "status": "unvalidated", "estimate": None,
                            "model_id": "m", "evidence_rev": "r"}, ES, T)
    assert r13["value"] == 0.0017469 and r13["unit"] == "umol_m2_s_per_count"

    print("kadopt selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("pure library — run with --selftest", file=sys.stderr)
        sys.exit(1)
