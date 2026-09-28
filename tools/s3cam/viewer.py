#!/usr/bin/env python3
"""Look at RGB, thermal and distance together, one capture at a time.

    ./viewer.py http://<ip>              # then open http://localhost:8723
    ./viewer.py http://<ip> --port 9000
    ./viewer.py http://<ip> --lan          # reachable from a phone on the same wifi

Snapshots, not a stream. The roadmap's own line is that streaming is a tool and
not the data; the thermal sensor runs at 4 Hz; and the two httpd panics this
firmware has had were both KB-sized locals in a handler. A page that asks for
one complete observation and draws it has none of those problems and answers
the actual question — is the thermal frame pointing where the camera is — which
a smooth video would answer no better.

Drag a box on the image and it reports the temperature inside it. That is the
manual version of the thing this is all for: the roadmap defers ML segmentation
behind "先人工 ROI", and a human ROI that already returns a per-plant number is
the stage that comes first. When a detector replaces the dragging, it calls the
same observe.thermal_stats() this page calls — the mapping is not reimplemented
in JavaScript, it is fetched, so there is exactly one of it.

The alignment sliders exist to be WRITTEN DOWN. Parallax moves the overlay with
distance, so a setting is only meaningful next to the range it was found at;
the page shows both together for copying into a note.

AIM MODE is the other half: the same page, with the thermal frame going live at
4 Hz on top of the still RGB, a sub-pixel centroid drawn where the estimator
actually thinks the target is, and the running sigma of that centroid measured
against Phase 3's own thresholds. It exists because scan_repeat.py is an
ACCEPTANCE run — it answers pass or fail and needs the box, the flip flags and
the axis mapping already settled — and settling those is eye work. Asking for
them as numbers on a command line means guessing, running a hundred frames, and
reading "low_contrast" for an answer.

Every number in that panel comes from the same Python the acceptance run uses:
centroid() from thermal_view, block_stats() and the thresholds from scan_stats.
The JavaScript draws and it drags; it does not measure. An aiming tool that
disagreed with the acceptance run about where the target is would be worse than
no aiming tool, because you would only find out after the afternoon was spent.

While aim mode is live the RGB is STILL A SNAPSHOT and the thermal is not
paired with it. The page says so rather than letting a live overlay on an old
photograph look like an observation.
"""
import json
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import observe
import scan_stats as S
from head_datum import Datum
from thermal_view import (centroid, flip_box, orient,
                          orientation_conflict, orientation_mismatch)

CH = {"pan": 5, "tilt": 6}          # must match servo.h CH_PAN / CH_TILT
US_MIN, US_MAX = 600, 2400          # servo.h electrical span
AIM_WINDOW = 200                    # rolling centroid samples; --n 100 is the run

PAGE = r"""<!doctype html><meta charset=utf-8><title>s3cam observation</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#14161a;--fg:#e8e6e1;--dim:#8d9099;--line:#2b2f36;--hot:#ff9d5c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;gap:16px;align-items:baseline;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:13px;font-weight:600;margin:0;letter-spacing:.08em;text-transform:uppercase}
button{font:inherit;background:#222730;color:var(--fg);border:1px solid var(--line);padding:5px 12px;cursor:pointer}
button:hover{border-color:var(--hot)}
main{display:flex;gap:14px;padding:14px;align-items:flex-start;flex-wrap:wrap}
/* touch-action:none — a drag on a touchscreen scrolls the page by default, so
   without this the box can never be drawn on a phone, which is the one thing
   the page is for. Pointer events already cover mouse and touch alike. */
#wrap{position:relative;line-height:0;cursor:crosshair;max-width:var(--imgw,64vw);
      touch-action:none}
canvas{width:100%;height:auto;border:1px solid var(--line)}
#sel{position:absolute;border:1px solid var(--hot);background:rgba(255,157,92,.12);pointer-events:none;display:none}
aside{min-width:290px;flex:1}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
td{padding:2px 0;vertical-align:top}td:first-child{color:var(--dim);padding-right:12px;white-space:nowrap}
.row{display:flex;gap:8px;align-items:center;margin:5px 0}
.row label{color:var(--dim);width:64px}
input[type=range]{flex:1}
b{color:var(--hot);font-weight:600}
.warn{color:#ffd166}
h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:16px 0 6px;border-top:1px solid var(--line);padding-top:10px}
#note{white-space:pre-wrap;color:var(--dim);font-size:12px}
/* On a phone the aside wraps underneath, so the image may as well have the
   width. The zoom slider still applies on top of this. */
@media (max-width:760px){
  #wrap{max-width:100%}
  aside{min-width:0}
  main{padding:8px;gap:8px}
}
.ok{color:#7bd88f}.bad{color:#ff6b6b}
.jog{display:flex;gap:6px;align-items:center;margin:4px 0}
.jog span{color:var(--dim);width:3.2em}
.jog button{padding:3px 7px}
.abs{width:5.2em;background:#1b1f26;color:var(--fg);border:1px solid var(--line);
     font:inherit;padding:2px 4px}
#jwarn{margin:4px 0;font-size:12px}
.jog.unverified .abs{border-color:var(--bad)}
#jhint{font-size:12px;margin-top:6px}
code{background:#1b1f26;border:1px solid var(--line);padding:6px 8px;display:block;
     white-space:pre-wrap;word-break:break-all;color:var(--fg);font:inherit}
#cross{position:absolute;pointer-events:none;display:none}
</style>
<header>
  <h1>s3cam observation</h1>
  <button id=cap>Capture</button>
  <label><input type=checkbox id=auto> auto <input type=number id=every value=5 min=1 max=60 style="width:3.5em;background:#222730;color:inherit;border:1px solid var(--line)"> s</label>
  <label><input type=checkbox id=live> live thermal (aim)</label>
  <label><input type=checkbox id=tflipv> t-flipv</label>
  <label><input type=checkbox id=tfliph> t-fliph</label>
  <label><input type=checkbox id=cold> cold target</label>
  <label>size <input type=range id=zoom min=30 max=100 value=80 style="width:90px;vertical-align:middle"><span id=zoomv>80%</span></label>
  <span id=status class=dim></span>
</header>
<main>
  <div id=wrap><canvas id=cv width=640 height=480></canvas><div id=sel></div>
    <svg id=cross></svg></div>
  <aside>
    <table id=meta></table>
    <h2>overlay</h2>
    <div class=row><label>opacity</label><input type=range id=op min=0 max=100 value=45><span id=opv>45%</span></div>
    <div class=row><label>scale x</label><input type=range id=sx min=20 max=300 value=100><span id=sxv>1.00</span></div>
    <div class=row><label>scale y</label><input type=range id=sy min=20 max=300 value=100><span id=syv>1.00</span></div>
    <div class=row><label>dx</label><input type=range id=dx min=-160 max=160 value=0><span id=dxv>0.0</span></div>
    <div class=row><label>dy</label><input type=range id=dy min=-120 max=120 value=0><span id=dyv>0.0</span></div>
    <h2>box</h2>
    <table id=box><tr><td colspan=2>drag on the image</td></tr></table>
    <div id=owarn></div>
    <h2>aim &mdash; estimator</h2>
    <table id=est><tr><td colspan=2 class=dim>turn on live thermal</td></tr></table>
    <h2>aim &mdash; noise floor</h2>
    <table id=nf></table>
    <div class=jog><span>pan</span>
      <button data-a=pan data-d=-100>&minus;100</button>
      <button data-a=pan data-d=-25>&minus;25</button>
      <button data-a=pan data-d=25>+25</button>
      <button data-a=pan data-d=100>+100</button>
      <b id=panus>--</b>
      <input type=number id=panabs class=abs min=600 max=2400 placeholder=us>
      <button data-set=pan>set</button></div>
    <div class=jog><span>tilt</span>
      <button data-a=tilt data-d=-100>&minus;100</button>
      <button data-a=tilt data-d=-25>&minus;25</button>
      <button data-a=tilt data-d=25>+25</button>
      <button data-a=tilt data-d=100>+100</button>
      <b id=tiltus>--</b>
      <input type=number id=tiltabs class=abs min=600 max=2400 placeholder=us>
      <button data-set=tilt>set</button></div>
    <div id=jwarn></div>
    <div id=jhint class=dim>Nudge one axis and watch which way the centroid
      moves: that is the axis mapping and the flip flags. No release control on
      purpose &mdash; a released axis holding weight drops.</div>
    <h2>run it</h2>
    <code id=cmd>drag a box</code>
    <h2>record</h2>
    <div id=note>capture first</div>
  </aside>
</main>
<script>
const $=s=>document.querySelector(s), cv=$('#cv'), g=cv.getContext('2d');
let bundle=null, img=null, sel=null, timer=null;
let aim=null, tbox=null, aimTimer=null;   // live thermal, and the box in THERMAL px
// A promise while an aim request is ACTUALLY IN FLIGHT. Refusing to start new
// polls is not enough: one already on the wire will consume /thermal before
// /api/observe reaches the board — take() is consume-once — and the capture
// comes back with no thermal frame, or a carried one. capture() waits for it.
let aimBusy=null;
// The held frame AS THE SERVER ORIENTED IT. Never bundle.thermal directly:
// that is wire order, and the box coordinates are not.
let held=null;
const R=24,C=32;
const n3=v=>(v===null||v===undefined)?'--':v.toFixed(3);

// Two scales, not one. The sensors' fields of view differ in each axis
// independently, so a single slider forces a residual no offset can absorb —
// it just gets split between the axes until the overlay looks least bad.
function reg(){return{sx:$('#sx').value/100,sy:$('#sy').value/100,
                      dx:$('#dx').value/10,dy:$('#dy').value/10}}

let rect=null;
function draw(){
  g.clearRect(0,0,cv.width,cv.height);
  if(img)g.drawImage(img,0,0,cv.width,cv.height);
  // Live thermal wins over the held frame when aiming. The RGB underneath is
  // still a SNAPSHOT — see the pairing row, which keeps saying so.
  const liveOn=$('#live').checked && aim;
  const px=liveOn?aim.thermal:held;
  // With no capture yet the overlay has no mapping to sit in; fall back to the
  // whole canvas so aim mode still works when the camera is dead, which this
  // one has been once already.
  const r0=rect||(liveOn?{x:0,y:0,w:cv.width,h:cv.height}:null);
  if(!px||!r0)return;
  const lo=Math.min(...px), hi=Math.max(...px), span=(hi-lo)||1;
  // nearest-neighbour, like thermal_view.py: interpolation invents detail a
  // 768-pixel sensor does not have
  const off=document.createElement('canvas'); off.width=C; off.height=R;
  const id=off.getContext('2d').createImageData(C,R);
  for(let i=0;i<px.length;i++){
    const t=(px[i]-lo)/span, c=ramp(t);
    id.data[i*4]=c[0]; id.data[i*4+1]=c[1]; id.data[i*4+2]=c[2]; id.data[i*4+3]=255;
  }
  off.getContext('2d').putImageData(id,0,0);
  g.save(); g.globalAlpha=liveOn&&!img?1:$('#op').value/100; g.imageSmoothingEnabled=false;
  // rect comes from observe.Registration.overlay_rect — the algebraic inverse
  // of the mapping that reads the box back out. Deriving it here again is how
  // you get an overlay that lines up while the numbers come from elsewhere.
  g.drawImage(off,r0.x,r0.y,r0.w,r0.h);
  g.restore();
  // The centroid sits in THERMAL pixels; it is placed through the same rect the
  // overlay was drawn into, so the crosshair cannot drift from the picture.
  // +0.5 is the centre of a pixel: r=12.0 means the middle of row 12.
  const x=$('#cross'); x.setAttribute('width',cv.width); x.setAttribute('height',cv.height);
  x.style.width='100%'; x.style.height='100%'; x.style.left=0; x.style.top=0;
  x.setAttribute('viewBox',`0 0 ${cv.width} ${cv.height}`);
  if(liveOn&&aim.centroid.ok){
    const cx=r0.x+(aim.centroid.c+0.5)/C*r0.w, cy=r0.y+(aim.centroid.r+0.5)/R*r0.h;
    const k=Math.max(10,r0.w/32);
    x.style.display='block';
    x.innerHTML=`<line x1="${cx-k}" y1="${cy}" x2="${cx+k}" y2="${cy}" stroke="#fff"/>`
      +`<line x1="${cx}" y1="${cy-k}" x2="${cx}" y2="${cy+k}" stroke="#fff"/>`
      +`<circle cx="${cx}" cy="${cy}" r="${k/2.5}" fill="none" stroke="#fff"/>`;
  } else x.style.display='none';
  if(!bundle)return;
  const r=reg();
  $('#note').textContent='range '+(bundle.range_mm??'null')+' mm'
    +'  sx '+r.sx.toFixed(2)+'  sy '+r.sy.toFixed(2)
    +'  dx '+r.dx.toFixed(1)+'  dy '+r.dy.toFixed(1)
    +'  (aniso '+(r.sx/r.sy).toFixed(3)+')'
    +'\nthermal '+lo.toFixed(1)+'..'+hi.toFixed(1)+' C   (colour maps this frame only)';
}
function ramp(t){ // dark -> warm; monotone in luminance so it reads on both eyes
  const p=[[8,12,40],[70,20,110],[190,45,90],[245,130,40],[255,240,170]];
  const x=Math.max(0,Math.min(.999,t))*(p.length-1), i=Math.floor(x), f=x-i;
  return p[i].map((v,k)=>Math.round(v+(p[i+1][k]-v)*f));
}
function meta(b){
  const bad=x=>'<span class=warn>'+x+'</span>';
  const rows=[
    ['capture',b.capture_id],
    ['time',b.timestamp||bad('unsynced ('+b.time_source+')')],
    ['range',b.range_mm!=null?'<b>'+b.range_mm+' mm</b>':bad('none — '+b.range_invalid_reason)],
    ['rgb',b.rgb_w+'x'+b.rgb_h+', '+b.rgb_bytes+' B'],
    ['thermal',b.thermal?('seq '+b.seq+', Ta '+b.ta_c.toFixed(1)+' C'+(b.checksum_ok?'':' '+bad('CHECKSUM BAD'))):bad('no frame')],
    ['pairing',(b.orientation_mismatch?bad(b.orientation_mismatch)+'<br>':'')
       +($('#live').checked?bad('LIVE thermal over a still RGB — not a pair')+'<br>':'')
       +(b.co_timed?'co-timed':bad('separate requests'))+', skew &le; '
       +(b.stale?bad(Math.round(b.skew_bound_ms)+' ms — stale'):Math.round(b.skew_bound_ms)+' ms')
       +(b.carried?' '+bad('(thermal carried)'):'')],
  ];
  $('#meta').innerHTML=rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('');
}
// Captures are SERIALISED, and each one is stamped.
//
// /api/observe makes the board take a picture and hold it; the page then asks
// for that picture BY ID. Let two captures overlap and the second replaces the
// held bundle while the first is still fetching its image, so the image
// request arrives naming a capture the server no longer holds — a 409, an
// onerror, and a blank canvas. The page load already fires one capture, so a
// user pressing Capture is enough to cause it.
//
// The 409 itself is right and stays: it is what stops one capture's photograph
// being shown beside another's matrix, which is the entire reason this pairing
// is tracked. The fix belongs here, where the overlap is created.
let capturing=false, capSeq=0;
async function capture(){
  if(capturing) return;              // one in flight is enough
  capturing=true;
  const mine=++capSeq;
  $('#status').textContent='...';
  try{
    // Let any aim request already on the wire finish first. It has taken, or
    // is about to take, the frame /observation needs; waiting means the board
    // has produced another by the time we ask.
    if(aimBusy) await aimBusy.catch(()=>{});
    const r=await fetch('/api/observe'); const b=await r.json();
    if(mine!==capSeq) return;        // superseded while we waited
    if(b.error){$('#status').innerHTML='<span class=warn>'+b.error+'</span>';return}
    bundle=b; meta(b);
    img=new Image();
    img.onload=()=>{ if(mine!==capSeq) return;
                     cv.width=b.rgb_w;cv.height=b.rgb_h;stats() };
    img.onerror=()=>{ if(mine!==capSeq) return;
                      img=null; draw();
                      $('#status').innerHTML='<span class=warn>the held capture '
                        +'moved on — press Capture again</span>'; };
    img.src='/api/last.jpg?id='+encodeURIComponent(b.capture_id);
    $('#status').textContent='';
  }catch(e){$('#status').innerHTML='<span class=warn>'+e+'</span>'}
  finally{ capturing=false; if($('#live').checked) poll(true); }
}
// Stamped, like capture(), and for the same reason one layer down. Dragging
// a slider fires one of these per input event; a slower earlier /api/view can
// land after a later one and overwrite rect, held and tbox with the answer to
// a registration the controls no longer show. The overlay would then be drawn
// from one transform while the numbers beside it — and the --box in the
// command you are about to copy — came from another, with nothing on screen
// admitting it.
let statSeq=0;
async function stats(){
  if(!bundle)return;
  const mine=++statSeq;
  const r=reg();
  const q=new URLSearchParams({sx:r.sx,sy:r.sy,dx:r.dx,dy:r.dy});
  // The capture this page is DISPLAYING, not whichever is newest. Another tab
  // capturing between our image load and this call would otherwise hand back
  // its matrix to sit under our photograph.
  if(bundle&&bundle.capture_id)q.set('id',bundle.capture_id);
  if(sel)q.set('box',sel.join(','));
  if($('#tflipv').checked)q.set('flipv','1');
  if($('#tfliph').checked)q.set('fliph','1');
  const d=await(await fetch('/api/view?'+q)).json();
  if(mine!==statSeq) return;          // a newer control value already answered
  rect=d.rect; held=d.thermal||null; draw();
  if(!sel)return;
  const s=d.stats;
  // scan_repeat takes THERMAL pixels, and this is where the RGB drag became
  // them — mapped by observe.Registration, server side. Re-deriving it in JS
  // would be a second mapping, and the aim panel would slowly stop agreeing
  // with the acceptance run about which pixels the box covers.
  tbox=s?s.box:null;
  // The full command is assembled server side (it knows the board URL) and
  // arrives with the next aim poll; until then show the part the drag decided.
  if(!aim) $('#cmd').textContent=tbox?`--box ${tbox.join(',')}`:'drag a box';
  if($('#live').checked) poll(true);   // the box changed: the window is stale
  $('#box').innerHTML = s
    ? [['thermal px',s.box.join(', ')+'  (n='+s.n+')'],
       ['mean','<b>'+s.mean.toFixed(2)+' C</b>'],
       ['min / max',s.min.toFixed(2)+' / '+s.max.toFixed(2)+' C'],
       ['coverage',s.coverage>=0.999?'full'
          :'<span class=warn>'+(s.coverage*100).toFixed(0)+'% of the box was in thermal view</span>']]
      .map(x=>'<tr><td>'+x[0]+'</td><td>'+x[1]+'</td></tr>').join('')
    : '<tr><td colspan=2 class=warn>'+(d.reason||'no reading')+'</td></tr>';
}
// drag a box in canvas pixels
let a=null;
const pt=e=>{const b=cv.getBoundingClientRect();
  return[(e.clientX-b.left)*cv.width/b.width,(e.clientY-b.top)*cv.height/b.height]};
$('#wrap').addEventListener('pointerdown',e=>{a=pt(e);$('#sel').style.display='block';e.target.setPointerCapture(e.pointerId)});
$('#wrap').addEventListener('pointermove',e=>{
  if(!a)return; const b=pt(e), r=cv.getBoundingClientRect(), k=r.width/cv.width;
  const s=$('#sel').style;
  s.left=Math.min(a[0],b[0])*k+'px'; s.top=Math.min(a[1],b[1])*k+'px';
  s.width=Math.abs(b[0]-a[0])*k+'px'; s.height=Math.abs(b[1]-a[1])*k+'px';
});
$('#wrap').addEventListener('pointerup',e=>{
  if(!a)return; const b=pt(e);
  sel=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])].map(Math.round);
  a=null; stats();
});
for(const[id,el,f]of[['op','opv',v=>v+'%'],['sx','sxv',v=>(v/100).toFixed(2)],
                     ['sy','syv',v=>(v/100).toFixed(2)],
                     ['dx','dxv',v=>(v/10).toFixed(1)],['dy','dyv',v=>(v/10).toFixed(1)]])
  $('#'+id).addEventListener('input',e=>{$('#'+el).textContent=f(e.target.value);stats()});
function aimPanel(d){
  const c=d.centroid, w=d.window, crit=d.crit;
  const cls=ok=>ok?'ok':'bad';
  $('#est').innerHTML=[
    ['state', c.ok?'<span class=ok>accepted</span>'
                  :`<span class=bad>rejected</span> <span class=dim>${c.reason}</span>`],
    ['contrast', `${n3(c.contrast)} C <span class=dim>(need &ge; 5.0, `
                  + ($('#cold').checked?'target colder than room':'target hotter than room')
                  + `)</span>`],
    ['support', `${c.n} px <span class=dim>over ${n3(c.tth)} C, bg ${n3(c.tbg)}</span>`],
    ['centroid', c.ok?`r ${n3(c.r)}  c ${n3(c.c)}`:'--'],
    ['frame', `seq ${d.seq}  Ta ${d.ta_c} C`+(d.checksum_ok?'':' <span class=warn>[checksum UNVERIFIED]</span>')],
  ].map(r=>`<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('');
  $('#nf').innerHTML=[
    ['samples', `${w.n} / ${d.window_max}`],
    ['sigma_r', w.sigma_r===null?'--':`<span class=${cls(w.sigma_r<=crit.sigma_static_good_px)}>${n3(w.sigma_r)}</span> px`],
    ['sigma_c', w.sigma_c===null?'--':`<span class=${cls(w.sigma_c<=crit.sigma_static_good_px)}>${n3(w.sigma_c)}</span> px`],
    ['drift', w.contrast_drift===null?'--':`<span class=${cls(w.contrast_drift<=crit.contrast_drift_max)}>${n3(w.contrast_drift)}</span>`],
    ['verdict', d.verdict],
  ].map(r=>`<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('');
  // "assumed" is not decoration: servo.h drives nothing at boot, so until
  // something has moved an axis its width is a guess and the first nudge is
  // measured from that guess rather than from where the head is.
  const a=d.servo.assumed?' <span class=warn>(assumed)</span>':'';
  $('#panus').innerHTML=d.servo.present?d.servo.pan+' us'+a:'no PCA9685';
  $('#tiltus').innerHTML=d.servo.present?d.servo.tilt+' us'+a:'';
  // A lost reply leaves the head at the old width or the new one, and the
  // backend refuses relative steps until an absolute one settles it. Saying so
  // HERE, next to the control that fixes it, is the difference between a
  // recovery and a puzzle.
  const unver=d.servo.present && d.servo.verified===false;
  document.querySelectorAll('.jog').forEach(el=>el.classList.toggle('unverified',unver));
  $('#jwarn').innerHTML=unver
    ? '<span class=bad>the last command\u2019s reply was lost \u2014 the head is '
      + 'at the old width or the new one. Type the width you can see and press '
      + 'set; the &plusmn; buttons stay refused until then.</span>'
    : '';
  $('#cmd').textContent=d.cmd;
  // Loud, because a double rotation is invisible in the data itself.
  $('#owarn').innerHTML=d.orientation_warning
    ? '<span class=bad>'+d.orientation_warning+'</span>' : '';
}

async function poll(reset){
  // Stand aside while a capture is running. The module produces 4 frames a
  // second and take() is CONSUME-ONCE, so a 250 ms aim poll eats very nearly
  // all of them — and /observation needs one too. Polling through a capture is
  // how the bundle comes back with "thermal": null, which then shows up as the
  // overlay simply vanishing the moment live thermal is switched off.
  if(capturing) return;
  // ONE AIM REQUEST AT A TIME. The server is threaded now, so the 250 ms
  // interval can start a poll while the previous one is still waiting on
  // /thermal — and a frame period IS 250 ms, so that is the normal case, not
  // a rare one. Two concurrent polls then race for frames take() hands out
  // once each, halving the rate each of them sees; worse, `aimBusy` used to
  // be overwritten by the second and cleared by whichever returned first, so
  // capture() could believe the coast was clear and take the frame an older
  // poll was still waiting for.
  //
  // Skipping a tick costs nothing: the next one is 250 ms away, which is
  // exactly one frame.
  if(aimBusy) return;
  const q=new URLSearchParams();
  if(tbox)q.set('box',tbox.join(','));
  if($('#tflipv').checked)q.set('flipv','1');
  if($('#tfliph').checked)q.set('fliph','1');
  if($('#cold').checked)q.set('cold','1');
  if(reset)q.set('reset','1');
  // Assigned BEFORE the first await, so the guard above and this assignment
  // cannot be separated by another tick.
  const mine = fetch('/api/aim?'+q);
  aimBusy = mine;
  try{
    const r=await mine;
    if(r.status===204)return;          // 4 Hz: nothing new, keep the last frame
    const d=await r.json();
    if(d.error){$('#status').innerHTML='<span class=warn>'+d.error+'</span>';return}
    aim=d; aimPanel(d); draw(); $('#status').textContent='';
  }catch(e){$('#status').innerHTML='<span class=warn>'+e+'</span>'}
  // Only clear what WE set. Belt and braces given the guard above, but the
  // bug it prevents — one poll clearing another's flag — is exactly the one
  // that let a capture steal a frame.
  finally{ if(aimBusy===mine) aimBusy=null; }
}

// Changing a flip re-indexes every pixel, so the rolling window is about a
// different image from the one it started on.
$('#cold').onchange=()=>{ if($('#live').checked) poll(true); };
$('#tflipv').onchange=$('#tfliph').onchange=()=>{
  stats();          // re-fetches the box AND the held frame in the new orientation
  if($('#live').checked) poll(true);    // and the window is about another image
};
$('#live').onchange=e=>{
  clearInterval(aimTimer); aimTimer=null;
  if(e.target.checked){ poll(true); aimTimer=setInterval(()=>poll(false),250); }
  else { aim=null; draw(); }
  // The pairing row asserts whether what is on screen is one moment or two.
  // Leaving it saying "not a pair" after live thermal is switched off is a
  // false statement about the data, and the whole point of that row is that
  // it is never false.
  if(bundle) meta(bundle);
};

async function sendJog(qs){
  const all=document.querySelectorAll('.jog button');
  all.forEach(x=>x.disabled=true);
  try{
    const r=await(await fetch('/api/jog?'+qs)).json();
    if(!r.ok)$('#status').innerHTML='<span class=warn>jog: '+r.reason+'</span>';
    else $('#status').textContent='';
  }finally{ all.forEach(x=>x.disabled=false); }
  poll(true);
}
document.querySelectorAll('.jog button[data-a]').forEach(b=>b.onclick=()=>
  sendJog(`axis=${b.dataset.a}&d=${b.dataset.d}`));
// Absolute: the only way out of an unverified width, so it sits in the same
// row as the buttons that got you there.
document.querySelectorAll('.jog button[data-set]').forEach(b=>b.onclick=()=>{
  const ax=b.dataset.set, v=$('#'+ax+'abs').value.trim();
  if(v==='') { $('#status').innerHTML='<span class=warn>type a width first</span>'; return; }
  sendJog(`axis=${ax}&us=${encodeURIComponent(v)}`);
});

// Image size. The drag maths already divides by the canvas's RENDERED width,
// so shrinking here moves no pixel: the box you drag still names the same
// thermal cells. Kept in localStorage because the right size depends on the
// screen in front of you, and re-choosing it every reload is friction on the
// one control whose only job is to remove friction.
function zoom(pct){
  document.documentElement.style.setProperty('--imgw', (pct*0.8)+'vw');
  $('#zoomv').textContent=pct+'%';
  try{ localStorage.setItem('s3cam.zoom', pct); }catch(e){}
  if(bundle)draw();          // the overlay rect is in canvas px and is unaffected
}
$('#zoom').addEventListener('input',e=>zoom(+e.target.value));
try{
  const saved=localStorage.getItem('s3cam.zoom');
  if(saved){ $('#zoom').value=saved; }
}catch(e){}
zoom(+$('#zoom').value);

$('#cap').onclick=capture;
$('#auto').onchange=e=>{clearInterval(timer);
  if(e.target.checked)timer=setInterval(capture,Math.max(1,$('#every').value)*1000)};
capture();
</script>
"""


class Aim:
    """Rolling centroid window and the head's commanded widths.

    Server-side because it is a MEASUREMENT. A browser tab computing its own
    sigma would be a second implementation of the number the acceptance run
    reports, and the two would disagree the first time either changed.
    """
    # One lock for every field below. With a threaded server two concurrent
    # /api/aim requests would otherwise append to the deque while one of them
    # was clearing it, and a jog arriving mid-poll could reset samples the
    # poll then reads back.
    lock = threading.Lock()
    # Serialises a whole jog TRANSACTION — read the current width, command the
    # board, commit the result. `lock` alone is not enough: the board call
    # blocks, and two jogs that each read the width before either commits both
    # compute the same target, so one button press is silently lost and the
    # widths land in response-arrival order rather than command order.
    #
    # A second mutex rather than holding `lock` across the HTTP call, because
    # that call can take ten seconds against an unreachable board and the aim
    # panel must not freeze for it.
    jog_lock = threading.Lock()
    # TWO counters, because they answer different questions and conflating
    # them made a second tab's box change look like a move.
    #
    # `generation` — this window's identity. Bumped by anything that empties
    # it: a move, a new box, a flip. The page uses it to tell a growing window
    # from a fresh one.
    #
    # `moves` — the head physically went somewhere. ONLY this invalidates a
    # sample already in flight. A window reset by another tab selecting a
    # different target does not make this tab's frame wrong; it was still
    # taken at the pose it describes.
    generation = 0
    moves = 0
    # True from just before a servo command goes out until its width is
    # committed. The board applies the move BEFORE it answers, so bumping the
    # generation on the reply is too late: a poll fetching in that gap gets a
    # post-move frame while the generation still says nothing has happened.
    # Anything sampled while this is set describes a head in motion, which is
    # not a pose at all.
    moving = False
    # False after a command whose ANSWER was lost. The two failures are not the
    # same: servo.cpp returns false before touching the PCA9685 on every
    # refusal path, so an explicit `set != "ok"` means the axis was not driven
    # and the width is exactly as good as it was. A timeout says nothing — the
    # command may have landed — so the head is at either the old width or the
    # new one and nobody knows which. A relative jog from an unknown base is a
    # guess dressed as arithmetic, so it is refused until an absolute width
    # re-establishes the number.
    verified = True
    samples = deque(maxlen=AIM_WINDOW)
    key = None                      # the box the window belongs to
    pan = tilt = None
    assumed = False                 # True while a width is a guess, not a command
    # None means "read the recorded datum from disk". Injectable because a test
    # that reads the repo's real head-datum.json is a test whose result depends
    # on a file somebody edited for the bench — it would pass today, fail after
    # the next horn remount, and the failure would say nothing about the code.
    datum = None

    @classmethod
    def reset(cls, key):
        """Caller holds `lock`."""
        cls.samples.clear()
        cls.key = key
        cls.generation += 1

    @classmethod
    def servo(cls, board):
        # Under the lock, because the assignment below sets pan before tilt
        # and another thread reading between them sees a present servo whose
        # tilt is still None — which a tilt jog then adds a delta to.
        with cls.lock:
            if cls.pan is not None:
                return {"present": True, "pan": cls.pan, "tilt": cls.tilt,
                        "assumed": cls.assumed}
        # /i2c/scan answers in PLAIN TEXT — it is a bring-up tool meant to be
        # read by a person over curl, and it lists one device per line:
        #
        #     0x29  VL53L0X rangefinder
        #     0x40  PCA9685 servo driver
        #
        # An earlier version here called json.loads() on it, which raised, was
        # swallowed, and reported "no PCA9685" against a board whose scan was
        # showing 0x40 the whole time. The tests did not catch it because the
        # fake board answered JSON: the fake encoded MY assumption instead of
        # the endpoint's contract, which makes it a test of nothing.
        #
        # Matched against the DEVICE LINES only, never the whole body: the
        # scan's closing line reads "Expected for the pan/tilt head: 0x29 +
        # 0x40.", so a substring search over the response reports the driver
        # present on a bus where it is precisely what is missing. A listed
        # device is a line whose first token is its address.
        try:
            with urllib.request.urlopen(f"{board}/i2c/scan", timeout=5) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception:                                            # noqa: BLE001
            return {"present": False, "pan": None, "tilt": None}
        listed = {ln.strip().split()[0].lower()
                  for ln in body.splitlines() if ln.strip()}
        if "0x40" not in listed:
            return {"present": False, "pan": None, "tilt": None}
        # Unknown until something commands it: servo.h deliberately drives
        # nothing at boot, so there is no width to read back. Say the number is
        # assumed rather than printing a position the head may not be at.
        # Assumed to be at THIS head's level position, not the protocol's
        # neutral: if the horn sits 13 us off, the first jog is measured from
        # a guess that is wrong by that much.
        d = cls.datum or Datum.load()
        with cls.lock:
            # Re-check: another thread may have finished its own probe while
            # this one was on the network. Publishing both widths under the
            # lock is what makes "pan is not None" mean "both are set".
            if cls.pan is None:
                cls.pan, cls.tilt = d.us("pan")[0], d.us("tilt")[0]
                cls.assumed = True
            return {"present": True, "pan": cls.pan, "tilt": cls.tilt,
                    "assumed": cls.assumed}

    @classmethod
    def jog(cls, board, axis, delta=None, absolute=None):
        # One jog at a time, from reading the width to committing it. Two
        # concurrent +25s must become +50, not +25 with one press dropped.
        with cls.jog_lock:
            st = cls.servo(board)
            if not st["present"]:
                return {"ok": False, "reason": "no PCA9685"}
            with cls.lock:
                cur = cls.pan if axis == "pan" else cls.tilt
                verified = cls.verified
            if absolute is not None:
                us = max(US_MIN, min(US_MAX, int(absolute)))
            else:
                if not verified:
                    return {"ok": False, "reason":
                            "width unverified after a lost reply — the head is "
                            "at the old width or the new one and nothing here "
                            "knows which. Command an absolute width to "
                            "re-establish it."}
                us = max(US_MIN, min(US_MAX, cur + delta))
            # Invalidate FIRST. Over-invalidating costs one restarted window;
            # under-invalidating shows a spread computed across two poses.
            with cls.lock:
                cls.samples.clear()
                cls.generation += 1
                cls.moves += 1
                cls.moving = True
            try:
                with urllib.request.urlopen(
                        f"{board}/servo?ch={CH[axis]}&us={us}", timeout=10) as r:
                    doc = json.loads(r.read())
            except Exception as e:                                   # noqa: BLE001
                # The ANSWER was lost; the command may well have landed. The
                # width is now one of two values and the next relative step
                # must not silently pick one.
                with cls.lock:
                    cls.moving = False
                    cls.assumed = True
                    cls.verified = False
                return {"ok": False, "reason": str(e), "unverified": True}
            if doc.get("set") != "ok":
                # The firmware answered, and it refuses BEFORE driving —
                # servo.cpp returns false on every refusal path above its first
                # pca call. So the axis did not move and the width is exactly
                # as good as it was; marking it assumed would throw away a
                # verification that is still true.
                with cls.lock:
                    cls.moving = False
                return {"ok": False, "reason": str(doc.get("set"))}
            with cls.lock:
                if axis == "pan":
                    cls.pan = us
                else:
                    cls.tilt = us
                cls.assumed = False
                cls.moving = False
                cls.verified = True     # an acknowledged command IS the proof
            return {"ok": True, "us": us}

    @classmethod
    def snapshot(cls, board, cd, key, reset, moves0):
        """Everything the response says about shared state, from ONE instant.

        Caller must have warmed servo() first, so no HTTP happens under the
        lock. Taken piecemeal, a jog landing between computing the window and
        reading the widths produced a reply that paired a pre-move stability
        figure with post-move servo state — a spread the head was no longer
        anywhere near, shown at the exact moment somebody had just moved it
        and would most trust the number.

        `moves0` is the move count read BEFORE the frame was fetched. Fetching
        and centroiding take a frame period or more, and a jog can land inside
        that window: the sample then describes the old pose, and appending it
        would seed the new pose's window with a point from somewhere else —
        inflating the very spread the panel exists to measure. A sample that
        outlived its pose is dropped, not corrected.
        """
        with cls.lock:
            # Either the head moved since this frame was requested, or it is
            # moving right now. Both mean the sample is not of a pose — and
            # NEITHER is true merely because somebody reset the window.
            moved = cls.moves != moves0 or cls.moving
            if reset or key != cls.key:
                cls.samples.clear()
                cls.key = key
                cls.generation += 1
            if cd["ok"] and not moved:
                cls.samples.append(cd)
            return {
                "window": S.block_stats(list(cls.samples)),
                "servo": ({"present": True, "pan": cls.pan, "tilt": cls.tilt,
                           "assumed": cls.assumed, "verified": cls.verified}
                          if cls.pan is not None
                          else {"present": False, "pan": None, "tilt": None}),
                "generation": cls.generation,
                "sample_dropped": moved,
                "moving": cls.moving,
            }

    @classmethod
    def verdict(cls, win, crit):
        """The same three-way call the acceptance run makes, live."""
        worst = max([v for v in (win["sigma_r"], win["sigma_c"]) if v is not None],
                    default=None)
        if worst is None or win["n"] < 10:
            return "<span class=dim>collecting&hellip;</span>"
        if worst > crit["sigma_static_max_px"]:
            return ("<span class=bad>STOP &mdash; cannot resolve 1 px.</span> "
                    "<span class=dim>constant-power source, smaller box, bad "
                    "pixels, then vibration</span>")
        if worst > crit["sigma_static_good_px"]:
            return ("<span class=warn>usable, resolution-limited</span> "
                    "<span class=dim>clear pass/fail only, no marginal call</span>")
        return "<span class=ok>noise floor is good &mdash; run the acceptance</span>"


class Viewer(BaseHTTPRequestHandler):
    board = ""
    last = None
    lock = threading.Lock()
    # The last few captures, by id. `last` is still what /api/view draws —
    # one current capture — but an image must remain fetchable by the client
    # that was GIVEN that id.
    #
    # Single-threaded, /api/observe and the /api/last.jpg that followed it
    # could not interleave. Threaded, two tabs overlap: the second capture
    # replaces `last` before the first client asks for its JPEG, and that
    # client gets a 409 for a capture it was handed a moment earlier. The
    # pairing rule is unchanged — you get the image belonging to YOUR
    # capture_id or an error, never somebody else's — it is only that a
    # slightly older id is no longer automatically somebody else's.
    recent = OrderedDict()
    RECENT_MAX = 4
    # One capture at a time, because THE BOARD holds one. observe.fetch() is
    # three requests — /observation, then the matrix and the JPEG by id — and
    # the board's X-Capture-Id check rejects a fetch whose capture has been
    # replaced underneath it. That check is right and stays; two overlapping
    # fetches simply cannot both win, so they queue instead of one failing.
    #
    # This is the board's contract, not a limitation of the code above:
    # `recent` covers the part the viewer CAN fix — once a bundle's bytes are
    # in hand, a later capture must not make them unreachable.
    capture_lock = threading.Lock()

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # A browser that abandons a request — a reload, a closed tab, a fetch
        # superseded by a newer one — closes the socket while the server is
        # still writing. That is ordinary client behaviour, not a fault, and
        # the default handler answers it with a full traceback that reads like
        # a crash. The page polls /api/aim four times a second and each
        # response carries 768 floats, so the window is wide open on every
        # reload.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)

        if u.path == "/api/observe":
            with Viewer.capture_lock:
                try:
                    bun = observe.fetch(Viewer.board)
                except (urllib.error.URLError, OSError, ValueError) as e:
                    return self._json({"error": f"{Viewer.board}: {e}"}, 502)
            # hold the whole bundle: the page's image and its matrix must come
            # from ONE capture, or the box reads a temperature from a scene the
            # picture no longer shows
                with Viewer.lock:
                    Viewer.last = bun
                    Viewer.recent[bun.capture_id] = bun
                    while len(Viewer.recent) > Viewer.RECENT_MAX:
                        # Bounded on purpose: each bundle holds a 650 kB JPEG
                        # and 768 floats, and this is a bench tool, not a
                        # store.
                        Viewer.recent.popitem(last=False)
            return self._json({
                "capture_id": bun.capture_id, "timestamp": bun.timestamp,
                "time_source": bun.time_source, "range_mm": bun.range_mm,
                "range_invalid_reason": bun.range_invalid_reason,
                "rgb_w": bun.rgb_w, "rgb_h": bun.rgb_h, "rgb_bytes": bun.rgb_bytes,
                "thermal": bun.thermal, "ta_c": bun.ta_c, "seq": bun.thermal_seq,
                "orientation_mismatch": orientation_mismatch(
                    bun.rgb_orientation, bun.thermal_orientation) or "",
                "checksum_ok": bun.checksum_ok, "co_timed": bun.co_timed,
                "skew_bound_ms": bun.skew_bound_ms, "stale": bun.stale,
                "carried": bun.carried,
            })

        if u.path == "/api/last.jpg":
            want = q.get("id", [None])[0]
            with Viewer.lock:
                # Named id: serve exactly that capture if it is still held.
                # Unnamed: the current one, as before.
                bun = Viewer.recent.get(want) if want else Viewer.last
                current = Viewer.last.capture_id if Viewer.last else None
            if not bun:
                if not want:
                    return self.send_error(404, "no capture yet")
                # Still an error, never a swap: serving a different capture's
                # JPEG would put one scene's picture beside another's matrix,
                # which is the one thing this tool exists to prevent.
                return self.send_error(
                    409, f"capture {want} is no longer held (current: {current})")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(bun.rgb_jpeg)))
            self.end_headers()
            return self.wfile.write(bun.rgb_jpeg)

        if u.path == "/api/view":
            # BY ID, like the JPEG. Keeping recent captures fetchable fixed
            # half the pairing: a tab could load its own image while this
            # endpoint still answered from whatever capture was newest, so the
            # page drew capture A's photograph under capture B's matrix — the
            # exact swap the id check on /api/last.jpg exists to prevent,
            # arriving through the other door.
            want = q.get("id", [None])[0]
            with Viewer.lock:
                bun = Viewer.recent.get(want) if want else Viewer.last
                current = Viewer.last.capture_id if Viewer.last else None
            if not bun:
                # Same keys as the success path. A response whose shape depends
                # on which branch produced it makes every caller learn the
                # branches, and the one that forgets reads a missing key as a
                # value.
                return self._json({
                    "rect": None, "thermal": None, "stats": None,
                    "reason": (f"capture {want} is no longer held "
                               f"(current: {current})") if want
                              else "no capture yet"})
            try:
                reg = observe.Registration(float(q.get("sx", ["1"])[0]),
                                           float(q.get("sy", ["1"])[0]),
                                           float(q.get("dx", ["0"])[0]),
                                           float(q.get("dy", ["0"])[0]),
                                           ref_mm=bun.range_mm)
            except ValueError as e:
                return self._json({"rect": None, "thermal": None,
                                   "stats": None,
                                   "reason": f"bad overlay: {e}"}, 400)
            fv = q.get("flipv", [""])[0] == "1"
            fh = q.get("fliph", [""])[0] == "1"
            # The HELD frame in the same orientation the box is named in. The
            # page used to draw bundle.thermal straight, in wire order, while
            # /api/view answered in flipped coordinates and aim mode flipped
            # the live pixels — so with either flip ticked the still overlay
            # showed one orientation and the ROI meant another, and an
            # operator would box the wrong physical object with nothing on
            # screen disagreeing. Oriented HERE, by the same orient() every
            # other path calls, rather than a second copy in JavaScript.
            held = (orient(bun.thermal, S.ROWS, S.COLS, fv, fh)
                    if bun.thermal else None)
            out = {"rect": reg.overlay_rect(bun.rgb_w, bun.rgb_h),
                   "thermal": held, "stats": None, "reason": None}
            if "box" in q:
                try:
                    box = tuple(float(x) for x in q["box"][0].split(","))
                except ValueError as e:
                    out["reason"] = f"bad box: {e}"
                    return self._json(out, 400)
                st = bun.thermal_stats(box, reg=reg)
                if st:
                    # Registration works in WIRE order, which is the order the
                    # bundle carries. The page draws — and aims on — the
                    # ORIENTED frame, and scan_repeat's --box is oriented too.
                    # Hand back the box in the orientation the user is looking
                    # at, or the aim panel and the drag disagree about which
                    # pixels are selected the moment a flip is ticked.
                    st = dict(st)
                    st["box"] = list(flip_box(tuple(st["box"]), S.ROWS, S.COLS, fv, fh))
                out["stats"] = st
                if not st:
                    out["reason"] = ("no thermal frame" if not bun.thermal
                                     else "box is outside the thermal view")
            return self._json(out)

        if u.path == "/api/jog":
            axis = q.get("axis", [""])[0]
            if axis not in CH:
                return self._json({"ok": False, "reason": "axis"}, 400)
            # ?us= is absolute and ?d= is relative. The absolute form exists
            # because it is the ONLY way out of an unverified width, and a
            # recovery that lives somewhere other than where the problem
            # appears is a recovery nobody reaches for.
            if "us" in q:
                try:
                    return self._json(Aim.jog(Viewer.board, axis,
                                              absolute=int(q["us"][0])))
                except ValueError:
                    return self._json({"ok": False, "reason": "us"}, 400)
            try:
                d = int(q.get("d", ["0"])[0])
            except ValueError:
                return self._json({"ok": False, "reason": "d"}, 400)
            return self._json(Aim.jog(Viewer.board, axis, delta=d))

        if u.path == "/api/aim":
            # A LIVE thermal frame, not the held capture: aiming is a thing you
            # do while watching, and the held frame belongs to a photograph
            # taken before you moved anything.
            # BEFORE the fetch. Everything below takes a frame period or
            # more, which is plenty of room for a jog.
            moves0 = Aim.moves
            # The board's frames are consume-once and this read competes with a
            # capture for them. The page's own guard only covers one tab: a
            # second tab aiming while the first captures would eat the frame
            # /observation needs, and against firmware that has no bundled
            # thermal the observation then comes back with none.
            #
            # Non-blocking, and a busy lock answers 204 — the same "nothing new
            # yet" the 4 Hz module produces several times a second anyway, so
            # the page already handles it by keeping the frame it has. Waiting
            # instead would stall the panel for the length of somebody else's
            # 5 MP exposure.
            if not Viewer.capture_lock.acquire(blocking=False):
                self.send_response(204)
                self.end_headers()
                return
            try:
                with urllib.request.urlopen(f"{Viewer.board}/thermal", timeout=15) as r:
                    doc = json.loads(r.read())
            except (urllib.error.URLError, OSError, ValueError) as e:
                return self._json({"error": f"{Viewer.board}: {e}"}, 502)
            finally:
                Viewer.capture_lock.release()
            f = doc.get("frame")
            if not f:
                # take() is consuming and the module is 4 Hz: nothing new yet.
                self.send_response(204)
                self.end_headers()
                return
            rows, cols = f.get("rows", S.ROWS), f.get("cols", S.COLS)
            # The MLX's scan order is undocumented and the firmware stores wire
            # order on purpose, so the flips live here — and they are
            # STRUCTURAL: a box picked on an unflipped view and then handed to
            # scan_repeat --flipv indexes entirely different pixels. Same
            # orient() the acceptance run calls, same place in the chain.
            flipv = q.get("flipv", [""])[0] == "1"
            fliph = q.get("fliph", [""])[0] == "1"
            px = orient(f["px"], rows, cols, flipv, fliph)
            # The incoming box is in ORIENTED coordinates — the same ones the
            # page shows and scan_repeat --box takes — and px above is already
            # oriented, so it indexes directly. /api/view is what converts.
            box = None
            if q.get("box", [""])[0]:
                try:
                    r0, c0, r1, c1 = (int(float(x)) for x in q["box"][0].split(","))
                    box = (max(0, min(rows - 1, r0)), max(0, min(cols - 1, c0)),
                           max(0, min(rows - 1, r1)), max(0, min(cols - 1, c1)))
                except ValueError:
                    box = None
            cold = q.get("cold", [""])[0] == "1"
            cd = centroid(px, rows, cols, box,
                          polarity="cold" if cold else "hot")
            key = (q.get("box", [""])[0], flipv, fliph, cold)
            # Probe the bus BEFORE the lock — it may do HTTP on first call —
            # then take one consistent snapshot of everything shared.
            Aim.servo(Viewer.board)
            snap = Aim.snapshot(Viewer.board, cd, key,
                                q.get("reset", [""])[0] == "1", moves0)
            win = snap["window"]
            crit = dict(S.CRITERION_DEFAULTS)
            bs = ",".join(str(v) for v in box) if box else "0,0,23,31"
            fl = "".join(f" --{n}" for n, v in (("flipv", flipv), ("fliph", fliph),
                                                 ("cold", cold)) if v)
            return self._json({
                "thermal": px, "rows": rows, "cols": cols,
                "seq": f.get("seq"), "ta_c": f.get("ta_c"),
                "checksum_ok": f.get("checksum_ok", True),
                "centroid": cd, "window": win, "window_max": AIM_WINDOW,
                "crit": crit, "verdict": Aim.verdict(win, crit),
                # From the same instant as `window`, not read again here.
                "servo": snap["servo"],
                # Changes whenever the head moved or the window restarted, so
                # the page can tell a growing window from a fresh one.
                "generation": snap["generation"],
                # True when this frame outlived its pose and was discarded.
                "sample_dropped": snap["sample_dropped"],
                "moving": snap["moving"],
                "orientation": f.get("orientation", "wire"),
                "orientation_warning": orientation_conflict(
                    f.get("orientation", "wire"), flipv, fliph) or "",
                "cmd": (f"tools/s3cam/scan_repeat.py {Viewer.board} --mode static "
                        f"--box {bs}{fl} --n 100"),
            })

        self.send_error(404)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    Viewer.board = argv[1].rstrip("/")
    port = 8723
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    host = "127.0.0.1"
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--lan" in argv:
        host = "0.0.0.0"
    # THREADING, because a capture is slow and the aim poll is not optional.
    # /api/observe makes the board wake the sensor, raise to 5 MP, expose and
    # ship 650 kB — measured at 2.5 s, longer from standby. On a
    # single-threaded server every 250 ms aim poll queues behind that, so one
    # capture stacks up ten of them; the browser abandons the pile on the next
    # reload, and each abandoned one used to print a traceback.
    #
    # Aim's state is shared across those threads and is locked. Viewer.last
    # already was.
    # LOOPBACK BY DEFAULT. Binding wider is opt-in because this page can drive
    # the servos: anything that reaches it can move the head, and there is no
    # authentication. On a home network that is usually fine and sometimes not,
    # which is exactly the kind of decision that should be typed rather than
    # inherited.
    # The address family follows the host. ThreadingHTTPServer is AF_INET, so
    # `--host ::1` would fail at construction — while the banner below treats
    # ::1 as a local address, which is a promise the binding could not keep.
    server_cls = ThreadingHTTPServer
    if ":" in host:
        class _V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6

            def server_bind(self):
                # DUAL STACK. Without this, `--host ::` on a platform that
                # defaults IPV6_V6ONLY to 1 listens on IPv6 only — while the
                # banner below happily advertises the machine's IPv4 LAN
                # address, which nothing is serving. A phone would time out on
                # a URL the tool itself printed.
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6,
                                           socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass          # some platforms refuse; the banner still holds
                super().server_bind()
        server_cls = _V6
    srv = server_cls((host, port), Viewer)
    # The URL has to name the socket that was actually bound. "localhost" is
    # not a synonym for whatever --host said: bound to ::1 alone it may resolve
    # to 127.0.0.1 and refuse, and bound to one LAN interface it is simply a
    # different address. A banner that sends somebody somewhere the server is
    # not is how a working feature gets reported broken.
    wildcard = host in ("0.0.0.0", "::", "")
    local = host in ("127.0.0.1", "localhost", "::1")
    # ::1 gets its own literal, not "localhost": on a machine where that name
    # resolves to 127.0.0.1 first, the printed URL would reach nothing.
    shown = ("localhost" if wildcard or host in ("127.0.0.1", "localhost")
             else f"[{host}]" if ":" in host else host)
    print(f"board {Viewer.board}  ->  http://{shown}:{port}")
    if wildcard:
        # Bound everywhere, so name the address a phone can type. "0.0.0.0" is
        # what to bind, never what to open.
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))          # no packet is sent
            lan = probe.getsockname()[0]
            probe.close()
            print(f"  on this network:  http://{lan}:{port}")
        except OSError:
            print(f"  on this network:  http://<this machine's LAN IP>:{port}")
    if not local:
        print("  NO AUTHENTICATION — anything that can reach this page can "
              "move the head.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
