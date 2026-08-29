# prior adoption state (one scenario per current-epoch universe bucket):
# ref-e2 diffuse: adopted 1.185, model valid ~1.198 (in-band) -> silent update
k_adopted,target=bh1750_lux_ref,source=daylight,regime=diffuse,epoch=bh1750_lux_ref-e2 value=1.185,unit="multiplier",adoption_state="adopted",model_id="m@prior",prev_value=-1.0,reason="bootstrap",pending_value=-1.0,pending_model_id="",pending_evidence_rev="",hold_ack_state="none" 1788868800
# ref-E1 direct: adopted 1.21 -> carried into the empty e2 direct bucket
k_adopted,target=bh1750_lux_ref,source=daylight,regime=direct,epoch=bh1750_lux_ref-e1 value=1.21,unit="multiplier",adoption_state="adopted",model_id="m@prior",prev_value=-1.0,reason="bootstrap",pending_value=-1.0,pending_model_id="",pending_evidence_rev="",hold_ack_state="none" 1788609600
# as7341-e2 lamp: adopted 0.0045, model valid ~0.0056 (>10% off) -> hold
k_adopted,target=as7341_ppfd,source=lamp,regime=none,epoch=as7341_ppfd-e2 value=0.0045,unit="umol_m2_s_per_count",adoption_state="adopted",model_id="m@prior",prev_value=-1.0,reason="bootstrap",pending_value=-1.0,pending_model_id="",pending_evidence_rev="",hold_ack_state="none" 1788868800
# as7341-e2 direct: HELD with a pending challenger; model is empty/unvalidated
# -> downgrade recovery: pending cleared, back to adopted, resolved event
k_adopted,target=as7341_ppfd,source=daylight,regime=direct,epoch=as7341_ppfd-e2 value=0.005,unit="umol_m2_s_per_count",adoption_state="held",model_id="m@prior",prev_value=-1.0,reason="bootstrap",pending_value=0.007,pending_model_id="m@challenger",pending_evidence_rev="deadbeef1234",hold_ack_state="awaiting" 1788868800
# main-e0 lamp: adopted 1.05; model can only be provisional (e0 cap)
# -> provisional never displaces an earned value: no write this round
k_adopted,target=bh1750_lux_main,source=lamp,regime=none,epoch=e0-legacy value=1.05,unit="multiplier",adoption_state="adopted",model_id="m@prior",prev_value=-1.0,reason="bootstrap",pending_value=-1.0,pending_model_id="",pending_evidence_rev="",hold_ack_state="none" 1788782400
# (main-e0 diffuse has NO prior row + provisional model -> seed then
#  bootstrap-adopt; main-e0 direct / as7341-e2 diffuse: seed stays seed)
