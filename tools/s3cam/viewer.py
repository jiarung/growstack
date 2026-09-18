#!/usr/bin/env python3
"""Look at RGB, thermal and distance together, one capture at a time.

    ./viewer.py http://<ip>              # then open http://localhost:8723
    ./viewer.py http://<ip> --port 9000

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
"""
import json
import sys
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import observe

PAGE = r"""<!doctype html><meta charset=utf-8><title>s3cam observation</title>
<style>
:root{--bg:#14161a;--fg:#e8e6e1;--dim:#8d9099;--line:#2b2f36;--hot:#ff9d5c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;gap:16px;align-items:baseline;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:13px;font-weight:600;margin:0;letter-spacing:.08em;text-transform:uppercase}
button{font:inherit;background:#222730;color:var(--fg);border:1px solid var(--line);padding:5px 12px;cursor:pointer}
button:hover{border-color:var(--hot)}
main{display:flex;gap:14px;padding:14px;align-items:flex-start;flex-wrap:wrap}
#wrap{position:relative;line-height:0;cursor:crosshair}
canvas{max-width:100%;border:1px solid var(--line)}
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
</style>
<header>
  <h1>s3cam observation</h1>
  <button id=cap>Capture</button>
  <label><input type=checkbox id=auto> auto <input type=number id=every value=5 min=1 max=60 style="width:3.5em;background:#222730;color:inherit;border:1px solid var(--line)"> s</label>
  <span id=status class=dim></span>
</header>
<main>
  <div id=wrap><canvas id=cv width=640 height=480></canvas><div id=sel></div></div>
  <aside>
    <table id=meta></table>
    <h2>overlay</h2>
    <div class=row><label>opacity</label><input type=range id=op min=0 max=100 value=45><span id=opv>45%</span></div>
    <div class=row><label>scale</label><input type=range id=sc min=20 max=300 value=100><span id=scv>1.00</span></div>
    <div class=row><label>dx</label><input type=range id=dx min=-160 max=160 value=0><span id=dxv>0.0</span></div>
    <div class=row><label>dy</label><input type=range id=dy min=-120 max=120 value=0><span id=dyv>0.0</span></div>
    <h2>box</h2>
    <table id=box><tr><td colspan=2>drag on the image</td></tr></table>
    <h2>record</h2>
    <div id=note>capture first</div>
  </aside>
</main>
<script>
const $=s=>document.querySelector(s), cv=$('#cv'), g=cv.getContext('2d');
let bundle=null, img=null, sel=null, timer=null;
const R=24,C=32;

function reg(){return{scale:$('#sc').value/100,dx:$('#dx').value/10,dy:$('#dy').value/10}}

let rect=null;
function draw(){
  if(!bundle)return;
  g.clearRect(0,0,cv.width,cv.height);
  if(img)g.drawImage(img,0,0,cv.width,cv.height);
  const px=bundle.thermal; if(!px||!rect)return;
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
  g.save(); g.globalAlpha=$('#op').value/100; g.imageSmoothingEnabled=false;
  // rect comes from observe.Registration.overlay_rect — the algebraic inverse
  // of the mapping that reads the box back out. Deriving it here again is how
  // you get an overlay that lines up while the numbers come from elsewhere.
  g.drawImage(off,rect.x,rect.y,rect.w,rect.h);
  g.restore();
  const r=reg();
  $('#note').textContent='range '+(bundle.range_mm??'null')+' mm  scale '+r.scale.toFixed(2)
    +'  dx '+r.dx.toFixed(1)+'  dy '+r.dy.toFixed(1)
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
    ['pairing',(b.co_timed?'co-timed':bad('separate requests'))+', skew &le; '
       +(b.stale?bad(Math.round(b.skew_bound_ms)+' ms — stale'):Math.round(b.skew_bound_ms)+' ms')
       +(b.carried?' '+bad('(thermal carried)'):'')],
  ];
  $('#meta').innerHTML=rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('');
}
async function capture(){
  $('#status').textContent='...';
  try{
    const r=await fetch('/api/observe'); const b=await r.json();
    if(b.error){$('#status').innerHTML='<span class=warn>'+b.error+'</span>';return}
    bundle=b; meta(b);
    img=new Image(); img.onload=()=>{cv.width=b.rgb_w;cv.height=b.rgb_h;stats()};
    img.onerror=()=>{img=null;draw()};
    img.src='/api/last.jpg?id='+encodeURIComponent(b.capture_id);
    $('#status').textContent='';
  }catch(e){$('#status').innerHTML='<span class=warn>'+e+'</span>'}
}
async function stats(){
  if(!bundle)return;
  const r=reg();
  const q=new URLSearchParams({scale:r.scale,dx:r.dx,dy:r.dy});
  if(sel)q.set('box',sel.join(','));
  const d=await(await fetch('/api/view?'+q)).json();
  rect=d.rect; draw();
  if(!sel)return;
  const s=d.stats;
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
for(const[id,el,f]of[['op','opv',v=>v+'%'],['sc','scv',v=>(v/100).toFixed(2)],
                     ['dx','dxv',v=>(v/10).toFixed(1)],['dy','dyv',v=>(v/10).toFixed(1)]])
  $('#'+id).addEventListener('input',e=>{$('#'+el).textContent=f(e.target.value);stats()});
$('#cap').onclick=capture;
$('#auto').onchange=e=>{clearInterval(timer);
  if(e.target.checked)timer=setInterval(capture,Math.max(1,$('#every').value)*1000)};
capture();
</script>
"""


class Viewer(BaseHTTPRequestHandler):
    board = ""
    last = None
    lock = threading.Lock()

    def log_message(self, *a):
        pass

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
            try:
                bun = observe.fetch(Viewer.board)
            except (urllib.error.URLError, OSError, ValueError) as e:
                return self._json({"error": f"{Viewer.board}: {e}"}, 502)
            # hold the whole bundle: the page's image and its matrix must come
            # from ONE capture, or the box reads a temperature from a scene the
            # picture no longer shows
            with Viewer.lock:
                Viewer.last = bun
            return self._json({
                "capture_id": bun.capture_id, "timestamp": bun.timestamp,
                "time_source": bun.time_source, "range_mm": bun.range_mm,
                "range_invalid_reason": bun.range_invalid_reason,
                "rgb_w": bun.rgb_w, "rgb_h": bun.rgb_h, "rgb_bytes": bun.rgb_bytes,
                "thermal": bun.thermal, "ta_c": bun.ta_c, "seq": bun.thermal_seq,
                "checksum_ok": bun.checksum_ok, "co_timed": bun.co_timed,
                "skew_bound_ms": bun.skew_bound_ms, "stale": bun.stale,
                "carried": bun.carried,
            })

        if u.path == "/api/last.jpg":
            with Viewer.lock:
                bun = Viewer.last
            if not bun:
                return self.send_error(404, "no capture yet")
            # The page names the capture it is drawing. Serving a newer JPEG
            # under that request would put one capture's image beside another's
            # matrix — which is precisely the pairing this whole tool exists to
            # keep — so a mismatch is an error the page can retry, not a swap.
            want = q.get("id", [None])[0]
            if want and want != bun.capture_id:
                return self.send_error(409, f"held capture is {bun.capture_id}")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(bun.rgb_jpeg)))
            self.end_headers()
            return self.wfile.write(bun.rgb_jpeg)

        if u.path == "/api/view":
            with Viewer.lock:
                bun = Viewer.last
            if not bun:
                return self._json({"rect": None, "stats": None,
                                   "reason": "no capture yet"})
            try:
                reg = observe.Registration(float(q.get("scale", ["1"])[0]),
                                           float(q.get("dx", ["0"])[0]),
                                           float(q.get("dy", ["0"])[0]),
                                           ref_mm=bun.range_mm)
            except ValueError as e:
                return self._json({"rect": None, "stats": None,
                                   "reason": f"bad overlay: {e}"}, 400)
            out = {"rect": reg.overlay_rect(bun.rgb_w, bun.rgb_h),
                   "stats": None, "reason": None}
            if "box" in q:
                try:
                    box = tuple(float(x) for x in q["box"][0].split(","))
                except ValueError as e:
                    out["reason"] = f"bad box: {e}"
                    return self._json(out, 400)
                out["stats"] = bun.thermal_stats(box, reg=reg)
                if not out["stats"]:
                    out["reason"] = ("no thermal frame" if not bun.thermal
                                     else "box is outside the thermal view")
            return self._json(out)

        self.send_error(404)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    Viewer.board = argv[1].rstrip("/")
    port = 8723
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    srv = HTTPServer(("127.0.0.1", port), Viewer)
    print(f"board {Viewer.board}  ->  http://localhost:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
