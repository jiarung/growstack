# k-model Phase B fixture: photone rows (case coverage in file order)
# -- normal daylight diffuse session: 3 rows chained at 25-min gaps
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=100,lux=6000,lux_at=5500,lux_ref_at=5000,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788573600
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=101,lux=6100,lux_at=5600,lux_ref_at=5100,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788575100
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=99,lux=5900,lux_at=5400,lux_ref_at=4900,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788576600
# -- daylight direct session (2 rows, even-count medians)
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=600,lux=30000,lux_at=27000,lux_ref_at=25000,f415=8000.0,f445=8000.0,f480=8000.0,f515=8000.0,f555=8000.0,f590=8000.0,f630=8000.0,f680=8000.0,gain_x=4.0,tint_ms=280.78 1788667200
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=610,lux=31000,lux_at=27500,lux_ref_at=26000,f415=8000.0,f445=8000.0,f480=8000.0,f515=8000.0,f555=8000.0,f590=8000.0,f630=8000.0,f680=8000.0,gain_x=4.0,tint_ms=280.78 1788667800
# -- guard band (lux_ref_at between 10k and 20k): NO contributions at all
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=300,lux=16000,lux_at=15000,lux_ref_at=15000,f415=4000.0,f445=4000.0,f480=4000.0,f515=4000.0,f555=4000.0,f590=4000.0,f630=4000.0,f680=4000.0,gain_x=4.0,tint_ms=280.78 1788746400
# -- sentinel lux_ref_at=-1 on a daylight row: regime unknowable -> nothing
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=90,lux=5000,lux_at=4800,lux_ref_at=-1.0,f415=900.0,f445=900.0,f480=900.0,f515=900.0,f555=900.0,f590=900.0,f630=900.0,f680=900.0,gain_x=4.0,tint_ms=280.78 1788750000
# -- row EXACTLY at the e2 boundary (2026-09-08T00:00:00Z): belongs to e2
#    (half-open [start, next)); ALSO the S-invariance case: gain_x=64/tint=27.8
#    matches as7341_ppfd-e2 config, counts/ppfd identical to the first session
#    row -> its k must equal that row's k (S never reads gain/tint)
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=100,lux=6000,lux_at=5500,lux_ref_at=5000,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=64.0,tint_ms=27.8 1788825600
# -- 31-min gap: two SEPARATE sessions (in ref-e2; their gain_x=4.0 mismatches
#    the as7341-e2 config -> excluded from as7341, a per-epoch identity case)
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=98,lux=6000,lux_at=5450,lux_ref_at=5000,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788832800
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=99,lux=6050,lux_at=5500,lux_ref_at=5050,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788834660
# -- two more e2-period daylight sessions: ref-e2 diffuse reaches n=5 (valid)
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=98.5,lux=6020,lux_at=5470,lux_ref_at=5010,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788840000
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=98.2,lux=5980,lux_at=5430,lux_ref_at=4995,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788843600
# -- five e2-period lamp sessions (gain_x=64 matches as7341-e2 config):
#    as7341_ppfd lamp e2 reaches n=5 (valid); main lamp e0 rides along
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40.1,lux=2005,lux_at=1905,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=64.0,tint_ms=27.8 1788876000
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40.2,lux=2008,lux_at=1908,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=64.0,tint_ms=27.8 1788879600
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40.3,lux=2003,lux_at=1903,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=64.0,tint_ms=27.8 1788883200
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40.15,lux=2006,lux_at=1906,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=64.0,tint_ms=27.8 1788886800
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40.25,lux=2004,lux_at=1904,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=64.0,tint_ms=27.8 1788890400
# -- pre-registry row (e0-legacy), historical: no gain_x/tint_ms telemetry
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=95,lux=5800,lux_at=5300,lux_ref_at=4800,f415=950.0,f445=950.0,f480=950.0,f515=950.0,f555=950.0,f590=950.0,f630=950.0,f680=950.0 1788055200
# -- staging device: excluded everywhere (declared epochs bind livingroom;
#    e0-legacy admits only the station device)
photone,device=staging,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=100,lux=6000,lux_at=5500,lux_ref_at=5000,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788584400
# -- config mismatch (gain_x=64): as7341 excluded, lux targets keep the row
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=102,lux=6200,lux_at=5700,lux_ref_at=5200,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=64.0,tint_ms=27.8 1788588000
# -- config_override=1: as7341 excluded even though the config matches
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=1.0,source_override=0.0,ppfd=101,lux=6100,lux_at=5650,lux_ref_at=5150,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788591600
# -- ref-only row (daylight recorded under a lit lamp): only the ref target
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=97,lux=5800,lux_at=-1.0,lux_ref_at=4800,f415=-1.0,f445=-1.0,f480=-1.0,f515=-1.0,f555=-1.0,f590=-1.0,f630=-1.0,f680=-1.0,gain_x=4.0,tint_ms=280.78,lamp_state=1.0 1788670800
# -- saturated channel: as7341 rejected, lux targets keep the row
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=100,lux=6050,lux_at=5560,lux_ref_at=5060,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=65535.0,gain_x=4.0,tint_ms=280.78 1788595200
# -- lamp session at night (lux_ref dark sentinel)
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40,lux=2000,lux_at=1900,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=4.0,tint_ms=280.78 1788789600
photone,device=livingroom,source=lamp,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=40.5,lux=2010,lux_at=1910,lux_ref_at=-1.0,f415=400.0,f445=400.0,f480=400.0,f515=400.0,f555=400.0,f590=400.0,f630=400.0,f680=400.0,gain_x=4.0,tint_ms=280.78 1788790200
# -- mixed canopy row: main+as7341 in regime none, never the ref target
photone,device=livingroom,source=mixed,gain=4x,light_location=livingroom paired=1.0,config_override=0.0,source_override=0.0,ppfd=150,lux=8000,lux_at=7500,lux_ref_at=5000,f415=1500.0,f445=1500.0,f480=1500.0,f515=1500.0,f555=1500.0,f590=1500.0,f630=1500.0,f680=1500.0,gain_x=4.0,tint_ms=280.78 1788674400
# -- unpaired row: contributes nothing
photone,device=livingroom,source=daylight,gain=4x,light_location=livingroom paired=0.0,config_override=0.0,source_override=0.0,ppfd=100,lux=6000,lux_at=5500,lux_ref_at=5000,f415=1000.0,f445=1000.0,f480=1000.0,f515=1000.0,f555=1000.0,f590=1000.0,f630=1000.0,f680=1000.0,gain_x=4.0,tint_ms=280.78 1788678000
