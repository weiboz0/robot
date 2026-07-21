"""The rover controller's embedded web UI — extracted byte-identically
from the Go controller (rovercontrol.go htmlPage) during the Python port.
Behavior and markup are 1:1 with the Go build."""

PAGE = r'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rover controller</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
 header{padding:10px 14px;background:#1c1c1c;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 h1{font-size:17px;margin:0}
 .live{display:block;max-width:640px;width:100%;margin:10px auto;border-radius:8px;background:#000}
 .pads{display:flex;gap:24px;justify-content:center;flex-wrap:wrap;padding:8px}
 .pad{display:grid;grid-template-columns:repeat(3,64px);grid-auto-rows:48px;gap:6px}
 button{background:#2d6cdf;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:14px;padding:8px}
 button:active{background:#1b50ad}
 button.warn{background:#a33}
 .pad .sp{visibility:hidden}
 .bar{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap;padding:8px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;padding:12px}
 figure{margin:0;background:#1c1c1c;border-radius:8px;overflow:hidden}
 figure a{position:relative;display:block}
 figure img{width:100%;display:block;aspect-ratio:4/3;object-fit:cover}
 .obox{position:absolute;border:1px solid rgba(90,255,90,.95);pointer-events:none}
 .hbox{position:absolute;border:1.5px solid rgba(120,200,255,.95);border-radius:4px;pointer-events:none;z-index:11}
 .hbox span{position:absolute;left:-1px;top:100%;background:rgba(0,0,0,.75);color:#9cf;
   font-size:11px;line-height:1.5;padding:0 4px;white-space:nowrap;border-radius:0 0 3px 3px}
 .obox span{position:absolute;right:-1px;top:100%;background:rgba(0,0,0,.6);color:#9f9;
   font-size:10px;line-height:1.4;padding:0 3px;white-space:nowrap;border-radius:0 0 3px 3px}
 .lb{position:fixed;inset:0;background:rgba(0,0,0,.86);display:flex;align-items:center;justify-content:center;z-index:10}
 .lbwrap{position:relative;display:inline-block}
 .lbwrap img{max-width:92vw;max-height:88vh;display:block}
 .lbbar{position:fixed;top:10px;right:14px;display:flex;gap:8px;z-index:11}
 figcaption{font-size:10px;padding:5px;display:flex;justify-content:space-between;gap:5px;word-break:break-all}
 small{color:#999}
 .help{max-width:640px;margin:6px auto;background:#1c1c1c;border-radius:8px;padding:10px 12px;font-size:13px}
 .help td{padding:2px 10px 2px 0;vertical-align:top}
 .help td:first-child{font-family:monospace;color:#9cf;white-space:nowrap;cursor:pointer;text-decoration:underline}
 .help td:first-child:hover{color:#fff}
 .prog{max-width:640px;margin:6px auto;padding:0 0 0 30px;color:#eee}
 .prog li{background:#1c1c1c;margin:3px 0;padding:5px 8px;border-radius:6px;display:flex;gap:6px;
   align-items:center;font-family:monospace;font-size:13px}
 .prog li.run{outline:2px solid #2d6cdf}
 .prog li span{flex:1;word-break:break-all}
 .prog li button{padding:2px 7px;font-size:12px}
 /* ── dashboard layout (plan 030): two columns wide, one column narrow ── */
 .dash{display:grid;grid-template-columns:minmax(420px,7fr) minmax(340px,5fr);
   gap:12px;padding:12px;max-width:1500px;margin:0 auto;align-items:start}
 .col{display:flex;flex-direction:column;gap:12px;min-width:0}
 .card{background:#17181c;border:1px solid #26272c;border-radius:12px;padding:10px}
 .card .live{max-width:100%;margin:0}
 @media(max-width:979px){.dash{grid-template-columns:1fr}}
 /* chat */
 #chatlog{display:flex;flex-direction:column;gap:8px;min-height:300px;
   max-height:62vh;overflow-y:auto;padding:6px}
 .cmsg{padding:8px 10px;border-radius:10px;max-width:88%;white-space:pre-wrap;
   word-break:break-word;font-size:14px;line-height:1.45}
 .cmsg.you{align-self:flex-end;background:#2d6cdf}
 .cmsg.bot{align-self:flex-start;background:#26272c}
 .cmsg.sys{align-self:center;background:none;color:#889;font-size:12px}
</style></head><body>
<header><h1>🤖 Rover controller</h1>
 <button class="warn" onclick="cmd('estop')">⛔ E-STOP</button>
 <button onclick="snap()">📸 Snapshot</button>
 <button onclick="pano3d()">🌐 3D view</button>
 <button onclick="tour()">▶ Room tour</button>
 <button onclick="detcmp()">🧪 Detectors</button>
 <span id="panostat"><small></small></span>
 <span id="gp" style="font-size:12px;color:#999">🎮 none (press a button)</span>
 <span id="health"><small>…</small></span>
 <div id="posebadge" style="margin-left:auto;background:rgba(0,0,0,.35);padding:4px 10px;border-radius:8px;font:12px ui-monospace,monospace;text-align:right">
  <span id="posetext" style="color:#667">pose: …</span>
  <button onclick="poseReset()" title="set current spot as origin (0,0), heading 0°" style="margin-left:6px;padding:2px 8px;border:0;border-radius:6px;cursor:pointer">⌂ 0,0</button>
 </div>
</header>
<div class="dash">
<section class="col">
<div class="card"><img class="live" src="/video_feed" alt="live view"></div>
<div class="card">
<div class="pads">
 <div class="pad" aria-label="drive">
  <span class="sp"></span>
  <button onmousedown="hold(1,1)" onmouseup="release()" ontouchstart="hold(1,1)" ontouchend="release()">▲</button>
  <span class="sp"></span>
  <button onmousedown="hold(-1,1)" onmouseup="release()" ontouchstart="hold(-1,1)" ontouchend="release()">◀</button>
  <button onclick="cmd('stop')">■</button>
  <button onmousedown="hold(1,-1)" onmouseup="release()" ontouchstart="hold(1,-1)" ontouchend="release()">▶</button>
  <span class="sp"></span>
  <button onmousedown="hold(-1,-1)" onmouseup="release()" ontouchstart="hold(-1,-1)" ontouchend="release()">▼</button>
  <span class="sp"></span>
 </div>
 <div class="pad" aria-label="camera">
  <span class="sp"></span>
  <button onclick="cmd('camera_up')">cam ▲</button>
  <span class="sp"></span>
  <button onclick="cmd('camera_left')">cam ◀</button>
  <button onclick="cmd('camera_center')">⊙</button>
  <button onclick="cmd('camera_right')">cam ▶</button>
  <span class="sp"></span>
  <button onclick="cmd('camera_down')">cam ▼</button>
  <span class="sp"></span>
 </div>
</div>
<div class="bar">
 <button onclick="cmd('light_head')">head light</button>
 <button onclick="cmd('light_base')">base light</button>
 <button onclick="cmd('gimbal_relax')">relax gimbal</button>
 <button onclick="cmd('gimbal_lock')">lock gimbal</button>
 <button id="autoflashbtn" onclick="toggleAutoFlash()" title="when OFF, the chatbot may never turn lights on automatically">🔦 auto-flash …</button>
 <label>speed cap <input id="cap" type="range" min="0" max="0.5" step="0.01" value="0.25"
   oninput="document.getElementById('capNum').value=this.value" onchange="setCap(this.value)">
  <input id="capNum" type="number" min="0" max="0.5" step="0.01" value="0.25"
   style="width:5em;padding:6px;border-radius:6px;border:0" onchange="setCap(this.value)"></label>
 <span id="capShow"><small>cap 0.25</small></span> <small>(0..0.5, not m/s)</small>
</div>
<form class="bar" style="margin:0" onsubmit="runCmd();return false">
 <input id="cmdin" type="text" autocomplete="off" spellcheck="false"
  placeholder="command — e.g. drive 0.2 0.2 · camera_aim 30 0 · light_head on · speed 0.15 · relax · stop"
  style="flex:1;min-width:200px;padding:8px;border-radius:6px;border:0">
 <button type="submit">Send</button>
 <button type="button" onclick="addStep()">＋ Add to program</button>
 <button type="button" onclick="toggleHelp()">❔ Commands</button>
 <small id="cmdout">Enter sends · ＋ adds it to the program below</small>
</form>
<div id="cmdhelp" class="help" style="display:none">
 <small>click a command to load it into the box, then edit the numbers:</small>
 <table>
  <tr><td onclick="pick('drive 0.2 0.2')">drive L R</td><td>drive, −1..1 (scaled by speed cap; ~0.5s pulse, then auto-stops)</td></tr>
  <tr><td onclick="pick('move_forward 400')">move_forward|back|left|right [MS]</td><td>nudge for MS ms (default 400)</td></tr>
  <tr><td onclick="pick('stop')">stop</td><td>stop the wheels</td></tr>
  <tr><td onclick="pick('estop')">estop</td><td>emergency stop (wheels + gimbal)</td></tr>
  <tr><td onclick="pick('camera_aim 0 0')">camera_aim PAN TILT</td><td>aim camera (pan −180..180, tilt −45..90, + is up)</td></tr>
  <tr><td onclick="pick('camera_up 15')">camera_up|down|left|right [DEG]</td><td>nudge camera (default 15°)</td></tr>
  <tr><td onclick="pick('camera_center')">camera_center</td><td>re-center the camera</td></tr>
  <tr><td onclick="pick('light_head on')">light_head|light_base [on|off]</td><td>no arg = toggle; or set on / off</td></tr>
  <tr><td onclick="pick('relax')">relax / lock</td><td>relax / lock the gimbal servos (hand-position the camera)</td></tr>
  <tr><td onclick="pick('speed 0.15')">speed CAP</td><td>set the speed cap, 0..0.5 (max wheel magnitude)</td></tr>
  <tr><td onclick="pick('snapshot')">snapshot</td><td>take a photo</td></tr>
  <tr><td onclick="pick('boxes on')">boxes on|off|all|NAMES</td><td>3D-view object boxes: toggle, show all, or filter (e.g. "boxes printer,suitcase")</td></tr>
 </table>
 <small>aliases: relax=gimbal_relax · lock=gimbal_lock · snap=snapshot · fwd=move_forward · back=move_back</small><br>
 <small>chatbot names also work: up/down/left/right = CAMERA nudge (wheels = spinl/spinr [S] or move_*) ·
 cam P T · center · photo · light F B (&gt;0 = on) · move L R = ONE ~0.5s pulse, not continuous ·
 note: spinl/spinr and move_* run at the speed cap here (the chatbot's spins are gentler)</small>
</div>
</div>
</section>
<section class="col">
<div class="card">
<div class="bar" style="justify-content:flex-start">
 <button id="tabchat" onclick="showTab('chat')">💬 Chat</button>
 <button id="tabphotos" onclick="showTab('photos')">📷 Photos</button>
 <button id="tabscans" onclick="showTab('scans')">🌍 3D views</button>
 <button id="tabprog" onclick="showTab('prog')">⚙ Program</button>
 <button class="warn" id="clearphotos" onclick="clearAll()" style="display:none">🗑 Clear all photos</button>
 <button class="warn" id="clearscans" onclick="clearAllScans()" style="display:none">🗑 Clear all 3D views</button>
</div>
<div id="chatpanel">
 <div id="chatlog"><div class="cmsg sys">talk to the rover in plain English — or $ commands ($help)</div></div>
 <div id="chatstat"><small>checking chatbot…</small></div>
 <button id="chatstartbtn" onclick="chatStart()" style="display:none;width:100%">▶ start chatbot</button>
 <form class="bar" style="margin:0;padding:8px 0 0" onsubmit="chatSend();return false">
  <input id="chatin" type="text" autocomplete="off" spellcheck="false"
   placeholder="e.g. what do you see? · take a photo · $status"
   style="flex:1;min-width:160px;padding:8px;border-radius:6px;border:0">
  <button type="submit" id="chatsendbtn">Send</button>
 </form>
</div>
<div class="grid" id="gallery" style="display:none"></div>
<div class="grid" id="scangrid" style="display:none"></div>
<div id="progpanel" style="display:none">
<div class="bar">
 <b>Program</b>
 <button onclick="runProgram()">▶ Run</button>
 <button class="warn" onclick="stopProgram()">■ Stop</button>
 <label>repeat <input id="reps" type="number" min="1" max="1000" value="1" style="width:4em;padding:6px;border-radius:6px;border:0"></label>
 <label>gap <input id="gap" type="number" min="0" max="10" step="0.1" value="0.6" style="width:4em;padding:6px;border-radius:6px;border:0">s</label>
 <button onclick="clearProg()">clear</button>
 <button onclick="saveProg()">💾 Save</button>
 <select id="saved" onchange="loadProg(this.value)" style="padding:6px;border-radius:6px;border:0"><option value="">load…</option></select>
 <span id="progstat"><small>empty — build a sequence with ＋ Add</small></span>
</div>
<ol id="program" class="prog"></ol>
<div style="padding:0 12px"><small>press ■ Stop to end a running program — E-STOP halts motion but the loop keeps going · build steps with ＋ Add in the command box</small></div>
</div>
</div>
</section>
</div>
<script>
const cmd = (c,q='')=>fetch('/'+c+(q?'?'+q:''),{method:'POST'});
let driving=false;
function hold(l,r){driving=true;send(l,r);}
function send(l,r){if(driving)fetch('/drive?l='+l+'&r='+r,{method:'POST'}).then(()=>{if(driving)setTimeout(()=>send(l,r),200);});}
function release(){driving=false;cmd('stop');}
async function snap(){await cmd('snapshot');load();}
let seen='';
async function load(){
 const j=await(await fetch('/latest')).json();
 const key=j.count+':'+(j.latest||'')+':'+(j.outlined||0);   // re-render when an outline arrives
 if(key===seen)return; seen=key;
 const p=await(await fetch('/photos')).json();
 const outlined=new Set(p.outlined||[]);            // ◻ only where there IS an outline
 document.getElementById('gallery').innerHTML=(p.photos||[]).map(n=>
  '<figure><a href="/photos/'+n+'" onclick="lightbox(\''+n+'\');return false"><img loading="lazy" src="/photos/'+n+'"></a>'+
  '<figcaption><span>'+n+'</span>'+
  (outlined.has(n)?'<button onclick="outline(this,\''+n+'\')" title="toggle found-object outline">◻</button>':'')+
  '<button class="warn" onclick="del(\''+n+'\')">del</button></figcaption></figure>').join('');
}
// ── 🌐 3D view: drag-to-look-around viewer over the stitched 360° panorama ──
// WebGL fragment shader maps view direction -> equirect UV. The pano's vertical
// span is inferred from its aspect (equirect: width = 360°). Esc/background
// closes. Falls back to a scrollable flat image if WebGL is unavailable.
async function pano3d(src){
 // src: archived scan URL ('/scans/…') — no merge variants exist for those.
 // Live view probes the variants and DEFAULTS TO THE CLEAREST that exists:
 // seamcut (graph-cut seams — the sharp one) → live pano → projector → stitcher.
 const VARIANTS=[['seamcut','/pano_variant/seamcut'],['live','/panorama'],
  ['projector','/pano_variant/projector'],['stitcher','/pano_variant/stitcher']];
 let start=src||null;const avail={};
 if(!src){
  for(const mv of VARIANTS){
   const h=await fetch(mv[1],{method:'HEAD'}).catch(()=>null);
   avail[mv[0]]=!!(h&&h.ok);
   if(!start&&avail[mv[0]])start=mv[1];
  }
  if(!start){cout('no 3D space yet — run a scene scan ($scan in the chatbot)');return;}
 }else{
  const h=await fetch(src,{method:'HEAD'}).catch(()=>null);
  if(!h||!h.ok){cout('that 3D view is gone');return;}
 }
 const lb=document.createElement('div');lb.className='lb';
 let downBg=false;                              // drag-safe: press AND release on the
 lb.onmousedown=e=>{downBg=(e.target===lb);};   // background — releasing a look-around
 lb.onclick=e=>{if(e.target===lb&&downBg)lb.remove();};  // drag outside must NOT close
 const bar=document.createElement('div');bar.className='lbbar';
 // merge-method buttons: only for the live pano, only for variants that exist,
 // with the ACTIVE one highlighted blue
 let setSrc=null,cur=start;const vbtns=[];
 function markActive(){vbtns.forEach(([u,b])=>{
  b.style.background=(u===cur)?'#08f':'';b.style.color=(u===cur)?'#fff':'';});}
 if(!src){
  VARIANTS.forEach(mv=>{
   if(!avail[mv[0]])return;                     // missing variant → no button
   const b=document.createElement('button');b.textContent=mv[0];b.id='pv_'+mv[0];
   b.onclick=()=>{if(setSrc){cur=mv[1];markActive();setSrc(mv[1]+'?ts='+Date.now());}};
   vbtns.push([mv[1],b]);bar.appendChild(b);
  });
  markActive();
 }
 const x=document.createElement('button');x.textContent='✕ close';x.onclick=()=>lb.remove();
 bar.appendChild(x);
 const img=new Image();
 img.onload=function(){
  const cv=document.createElement('canvas');
  cv.width=Math.min(innerWidth*0.92,1280);cv.height=Math.min(innerHeight*0.8,720);
  cv.style.borderRadius='8px';cv.style.cursor='grab';
  const gl=cv.getContext('webgl');
  if(!gl){ // fallback: flat scrollable pano
   const wrap=document.createElement('div');
   wrap.style.cssText='max-width:92vw;max-height:80vh;overflow:auto';
   img.style.maxHeight='78vh';wrap.appendChild(img);lb.appendChild(wrap);return;
  }
  const cwrap=document.createElement('div');cwrap.style.position='relative';
  cwrap.appendChild(cv);lb.appendChild(cwrap);
  const vs='attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
  const fs='precision mediump float;uniform sampler2D t;uniform vec2 res;'+
   'uniform float yaw,pitch,fov,vspan;'+
   'void main(){vec2 uv=(gl_FragCoord.xy/res-0.5);'+
   'float ar=res.x/res.y;'+
   'vec3 d=normalize(vec3(uv.x*ar*tan(fov*0.5)*2.0,uv.y*tan(fov*0.5)*2.0,1.0));'+
   'float cy=cos(pitch),sy=sin(pitch);'+
   'vec3 d2=vec3(d.x,d.y*cy - d.z*sy,d.y*sy + d.z*cy);'+
   'float cx=cos(yaw),sx=sin(yaw);'+
   'vec3 d3=vec3(d2.x*cx + d2.z*sx,d2.y,-d2.x*sx + d2.z*cx);'+
   'float lon=atan(d3.x,d3.z);float lat=asin(clamp(d3.y,-1.,1.));'+
   'float u=lon/6.2831853+0.5;float v=0.5 - lat/vspan;'+
   'if(v<0.||v>1.){gl_FragColor=vec4(0.06,0.06,0.06,1);}'+
   'else{gl_FragColor=texture2D(t,vec2(u,v));}}';
  function sh(ty,src){const h=gl.createShader(ty);gl.shaderSource(h,src);gl.compileShader(h);return h;}
  const pr=gl.createProgram();
  gl.attachShader(pr,sh(gl.VERTEX_SHADER,vs));gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,fs));
  gl.linkProgram(pr);
  if(!gl.getProgramParameter(pr,gl.LINK_STATUS)){    // shader failed → flat fallback
   cv.remove();const wrap=document.createElement('div');
   wrap.style.cssText='max-width:92vw;max-height:80vh;overflow:auto';
   img.style.maxHeight='78vh';wrap.appendChild(img);lb.appendChild(wrap);return;
  }
  gl.useProgram(pr);
  const buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
  const loc=gl.getAttribLocation(pr,'p');gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
  const tex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,tex);
  gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,img);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
  // vertical span from aspect: equirect width = 2π rad
  let vspan=Math.min(3.14159,6.2831853*img.height/img.width);
  let yaw=0,pitch=0,fov=1.2,drag=null;
  setSrc=function(url){const im2=new Image();
   im2.onload=function(){gl.bindTexture(gl.TEXTURE_2D,tex);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,im2);
    vspan=Math.min(3.14159,6.2831853*im2.height/im2.width);draw();};
   im2.onerror=function(){const t=document.createElement('div');
    t.textContent='no result for this method on this scene (it failed or gated out) — run $panotest to rebuild';
    t.style.cssText='position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.85);color:#fc6;padding:6px 12px;border-radius:6px;z-index:12';
    document.body.appendChild(t);setTimeout(()=>t.remove(),3500);};
   im2.src=url;};
  // ── object boxes (plan 029): labeled overlays that track the view. Meta
  // holds sphere directions; every draw() re-projects them with the EXACT
  // inverse of the shader: undo yaw (Ry^T) FIRST, then pitch (Rx^T), cull
  // behind-camera (z<=0) and outside the pano's vspan band.
  let boxMeta=null;const boxEls=[];
  const metaUrl=src?src.replace('/scans/','/scan_meta/'):'/pano_meta';
  function boxesOn(){return localStorage.getItem('roverboxes:on')!=='0';}
  function boxShown(o){if(!boxesOn())return false;
   const f=(localStorage.getItem('roverboxes:filter')||'').trim().toLowerCase();
   if(!f)return true;
   return f.split(',').some(t=>o.name.toLowerCase().indexOf(t.trim())>=0);}
  function drawBoxes(){
   if(!boxMeta)return;
   const k=2*Math.tan(fov/2),ar=cv.width/cv.height,ppr=cv.height/k;
   const cyw=Math.cos(yaw),syw=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
   boxEls.forEach(bo=>{const o=bo[0],d=bo[1];
    const lon=o.lon*Math.PI/180,lat=o.lat*Math.PI/180;
    if(!boxShown(o)||Math.abs(lat)>vspan/2){d.style.display='none';return;}
    const X=Math.sin(lon)*Math.cos(lat),Y=Math.sin(lat),Z=Math.cos(lon)*Math.cos(lat);
    const x1=X*cyw - Z*syw, z1=X*syw + Z*cyw;      // Ry(yaw)^T
    const y2=Y*cp + z1*sp,  z2=-Y*sp + z1*cp;      // Rx(pitch)^T
    if(z2<=0.05){d.style.display='none';return;}
    const u=x1/(z2*k*ar), v=y2/(z2*k);
    if(Math.abs(u)>0.55||Math.abs(v)>0.55){d.style.display='none';return;}
    const wpx=o.w*Math.PI/180*ppr, hpx=o.h*Math.PI/180*ppr;
    d.style.display='';
    d.style.left=((u+0.5)*cv.width-wpx/2)+'px';
    d.style.top=((0.5-v)*cv.height-hpx/2)+'px';    // GL y-up → CSS y-down
    d.style.width=wpx+'px';d.style.height=hpx+'px';
   });
  }
  window.redrawBoxes=drawBoxes;
  fetch(metaUrl).then(r=>r.ok?r.json():null).then(m=>{
   if(!m||!m.objects||!m.objects.length)return;
   boxMeta=m;
   m.objects.forEach(o=>{const d=document.createElement('div');d.className='hbox';
    const s=document.createElement('span');
    s.textContent=o.name+(o.color?' · '+o.color:'');       // textContent: no XSS
    d.appendChild(s);cwrap.appendChild(d);boxEls.push([o,d]);});
   const tb=document.createElement('button');tb.textContent='◻ boxes';tb.id='boxtoggle';
   tb.onclick=()=>{localStorage.setItem('roverboxes:on',boxesOn()?'0':'1');drawBoxes();};
   bar.insertBefore(tb,x);
   drawBoxes();
  }).catch(()=>{});
  function draw(){
   gl.viewport(0,0,cv.width,cv.height);
   gl.uniform2f(gl.getUniformLocation(pr,'res'),cv.width,cv.height);
   gl.uniform1f(gl.getUniformLocation(pr,'yaw'),yaw);
   gl.uniform1f(gl.getUniformLocation(pr,'pitch'),pitch);
   gl.uniform1f(gl.getUniformLocation(pr,'fov'),fov);
   gl.uniform1f(gl.getUniformLocation(pr,'vspan'),vspan);
   gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
   drawBoxes();
  }
  cv.onmousedown=e=>{drag=[e.clientX,e.clientY];cv.style.cursor='grabbing';};
  addEventListener('mouseup',()=>{drag=null;cv.style.cursor='grab';});
  addEventListener('mousemove',e=>{if(!drag)return;
   yaw-=(e.clientX-drag[0])*0.005;pitch-=(e.clientY-drag[1])*0.005;
   pitch=Math.max(-vspan/2,Math.min(vspan/2,pitch));drag=[e.clientX,e.clientY];draw();});
  cv.ontouchstart=e=>{drag=[e.touches[0].clientX,e.touches[0].clientY];};
  cv.ontouchmove=e=>{if(!drag)return;const t0=e.touches[0];
   yaw-=(t0.clientX-drag[0])*0.005;pitch-=(t0.clientY-drag[1])*0.005;
   pitch=Math.max(-vspan/2,Math.min(vspan/2,pitch));drag=[t0.clientX,t0.clientY];draw();e.preventDefault();};
  cv.onwheel=e=>{fov=Math.max(0.4,Math.min(2.0,fov+e.deltaY*0.001));draw();e.preventDefault();};
  draw();
 };
 img.src=start+'?ts='+Date.now();
 lb.appendChild(bar);
 document.body.appendChild(lb);
 document.addEventListener('keydown',function esc(e){
  if(e.key==='Escape'){lb.remove();document.removeEventListener('keydown',esc);}});
}
// ▶ Room tour: replay of the recorded gimbal sweep (looping MJPEG — no seams,
// it's real video). Esc/background closes; the stream stops on close.
function tour(){
 fetch('/tour_feed?loops=1',{method:'HEAD'}).catch(()=>null);
 const lb=document.createElement('div');lb.className='lb';
 let downBg=false;
 lb.onmousedown=e=>{downBg=(e.target===lb);};
 lb.onclick=e=>{if(e.target===lb&&downBg){img.src='';lb.remove();}};
 const bar=document.createElement('div');bar.className='lbbar';
 const x=document.createElement('button');x.textContent='✕ close';
 x.onclick=()=>{img.src='';lb.remove();};
 bar.appendChild(x);
 const img=new Image();
 img.style.cssText='max-width:92vw;max-height:85vh;border-radius:8px';
 img.onerror=()=>{cout('no room tour yet — say "record the room" in the chatbot');lb.remove();};
 img.src='/tour_feed?ts='+Date.now();
 lb.appendChild(img);lb.appendChild(bar);
 document.body.appendChild(lb);
 document.addEventListener('keydown',function esc(e){
  if(e.key==='Escape'){img.src='';lb.remove();document.removeEventListener('keydown',esc);}});
}
// 🧪 detector comparison: one button per model flips the displayed result
// (annotated frames uploaded by the chatbot's $detect).
function detcmp(){
 const models=['hsv','yolo11n','yolov8n','yolo11s'];
 const lb=document.createElement('div');lb.className='lb';
 let downBg=false;
 lb.onmousedown=e=>{downBg=(e.target===lb);};
 lb.onclick=e=>{if(e.target===lb&&downBg)lb.remove();};
 const bar=document.createElement('div');bar.className='lbbar';
 const img=new Image();
 img.style.cssText='max-width:92vw;max-height:82vh;border-radius:8px';
 img.onerror=()=>{cout('no results yet — run $detect in the chatbot first');lb.remove();};
 const btns=[];
 function show(m){
  img.src='/det_image/'+m+'?ts='+Date.now();
  btns.forEach(b=>b.style.background=(b.textContent===m)?'#1b50ad':'#2d6cdf');
 }
 models.forEach(m=>{
  const b=document.createElement('button');b.textContent=m;b.onclick=()=>show(m);
  btns.push(b);bar.appendChild(b);
 });
 const x=document.createElement('button');x.textContent='✕';x.onclick=()=>lb.remove();
 bar.appendChild(x);
 lb.appendChild(img);lb.appendChild(bar);document.body.appendChild(lb);
 show('yolo11s');
 document.addEventListener('keydown',function esc(e){
  if(e.key==='Escape'){lb.remove();document.removeEventListener('keydown',esc);}});
}
async function fetchMeta(n){
 try{const r=await fetch('/photo_meta/'+encodeURIComponent(n));if(r.ok)return await r.json();}catch(e){}
 return null;
}
// small label at the outline's bottom-right (e.g. "green pen"); textContent: no XSS
function boxLabel(d,m){
 const txt=(m&&(m.label||m.target))||'';
 if(!txt)return;
 const s=document.createElement('span');s.textContent=txt;d.appendChild(s);
}
// click a photo → zoomed lightbox with the outline + its toggle (plan 021 UX).
// The wrapper hugs the displayed image exactly, so bbox fractions map straight
// to % — no cover-crop math needed here. Esc / background click closes.
async function lightbox(n){
 const m=await fetchMeta(n);
 const lb=document.createElement('div');lb.className='lb';
 let downBg=false;
 lb.onmousedown=e=>{downBg=(e.target===lb);};
 lb.onclick=e=>{if(e.target===lb&&downBg)lb.remove();};
 const wrap=document.createElement('div');wrap.className='lbwrap';
 const img=document.createElement('img');img.src='/photos/'+encodeURIComponent(n);
 wrap.appendChild(img);
 const bar=document.createElement('div');bar.className='lbbar';
 const b=m&&m.bbox;
 if(b&&b.length===4){
  const d=document.createElement('div');d.className='obox';
  d.style.left=(b[0]*100)+'%';d.style.top=(b[1]*100)+'%';
  d.style.width=((b[2]-b[0])*100)+'%';d.style.height=((b[3]-b[1])*100)+'%';
  if(m.target||m.color)d.title=(m.target||'')+(m.color?' ('+m.color+')':'');
  boxLabel(d,m);
  wrap.appendChild(d);
  const t=document.createElement('button');t.textContent='◼ outline';
  t.onclick=()=>{const on=d.style.display!=='none';d.style.display=on?'none':'block';
   t.textContent=(on?'◻':'◼')+' outline';};
  bar.appendChild(t);
 }
 const x=document.createElement('button');x.textContent='✕ close';x.onclick=()=>lb.remove();
 bar.appendChild(x);
 lb.appendChild(wrap);lb.appendChild(bar);
 document.body.appendChild(lb);
 document.addEventListener('keydown',function esc(e){
  if(e.key==='Escape'){lb.remove();document.removeEventListener('keydown',esc);}});
}
// toggleable found-object outline (plan 020): lazy-fetch /photo_meta/{name} once,
// overlay a CSS box from the bbox fractions. The JPEG itself is untouched.
// coverPct maps image-fraction bbox -> container fractions accounting for the
// object-fit:cover crop, so the overlay aligns for ANY capture aspect (not just
// the default 4:3). Resize-safe: the container keeps its aspect, so % holds.
function coverPct(img,b){
 const cw=img.clientWidth,ch=img.clientHeight,iw=img.naturalWidth,ih=img.naturalHeight;
 if(!cw||!ch||!iw||!ih)return null;
 const s=Math.max(cw/iw,ch/ih),dw=iw*s,dh=ih*s,ox=(cw-dw)/2,oy=(ch-dh)/2;
 return [(ox+b[0]*dw)/cw,(oy+b[1]*dh)/ch,(b[2]-b[0])*dw/cw,(b[3]-b[1])*dh/ch];
}
async function outline(btn,n){
 const a=btn.closest('figure').querySelector('a');
 const old=a.querySelector('.obox');
 if(old){const on=(old.style.display==='none');old.style.display=on?'block':'none';
  btn.textContent=on?'◼':'◻';return;}
 let m=null;
 try{const r=await fetch('/photo_meta/'+encodeURIComponent(n));if(r.ok)m=await r.json();}catch(e){}
 const b=m&&m.bbox;
 if(!b||b.length!==4){btn.textContent='∅';btn.title='no outline data for this photo';return;}
 const img=a.querySelector('img');
 if(img&&!img.complete){try{await img.decode();}catch(e){}}
 const p=(img&&coverPct(img,b))||[b[0],b[1],b[2]-b[0],b[3]-b[1]];  // fallback: naive %
 const d=document.createElement('div');d.className='obox';
 d.style.left=(p[0]*100)+'%';d.style.top=(p[1]*100)+'%';
 d.style.width=(p[2]*100)+'%';d.style.height=(p[3]*100)+'%';
 if(m.target||m.color)d.title=(m.target||'')+(m.color?' ('+m.color+')':'');  // title property: no HTML
 boxLabel(d,m);
 a.appendChild(d);btn.textContent='◼';
}
async function del(n){await fetch('/delete_photo/'+encodeURIComponent(n),{method:'POST'});seen='';load();}
async function clearAll(){const p=await(await fetch('/photos')).json();const ns=p.photos||[];
 if(!ns.length||!confirm('Delete all '+ns.length+' photos?'))return;
 for(const n of ns){await fetch('/delete_photo/'+encodeURIComponent(n),{method:'POST'});}
 seen='';load();}

// ── side-panel tabs: Chat | Photos | 3D views | Program (plan 030) ───────────
let gtab='chat';
const TAB_PANELS={chat:'chatpanel',photos:'gallery',scans:'scangrid',prog:'progpanel'};
const TAB_BTNS={chat:'tabchat',photos:'tabphotos',scans:'tabscans',prog:'tabprog'};
function showTab(t){gtab=t;
 for(const k in TAB_PANELS){const el=document.getElementById(TAB_PANELS[k]);
  if(el)el.style.display=(t===k)?'':'none';}
 document.getElementById('clearphotos').style.display=t==='photos'?'':'none';
 document.getElementById('clearscans').style.display=t==='scans'?'':'none';
 for(const k in TAB_BTNS){const b=document.getElementById(TAB_BTNS[k]);
  if(b){b.style.background=(t===k)?'#08f':'';b.style.color=(t===k)?'#fff':'';}}
 if(t==='scans')loadScans();}

// ── chat panel: submit + poll against the controller's chat bridge ──────────
let chatBusy=false;
function chatAdd(who,text){const d=document.createElement('div');
 d.className='cmsg '+who;d.textContent=text;
 const log=document.getElementById('chatlog');log.appendChild(d);
 log.scrollTop=log.scrollHeight;return d;}
async function chatStatusTick(){try{
 const j=await(await fetch('/chat_status')).json();
 const up=j.ok===true;
 const sb=document.getElementById('chatstartbtn');if(sb)sb.style.display=up?'none':'';
 const st=document.getElementById('chatstat');if(!st)return;
 st.innerHTML=up
  ?'<small>🟢 chatbot up · '+(j.model?j.model:'no LLM — $ commands only')+(j.busy?' · thinking…':'')+'</small>'
  :'<small>⚪ chatbot not running — press start</small>';
}catch(e){}}
async function chatSend(){
 if(chatBusy)return;
 const inp=document.getElementById('chatin');const t=inp.value.trim();if(!t)return;
 inp.value='';chatAdd('you',t);chatBusy=true;
 const think=chatAdd('sys','thinking…');
 try{
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({text:t})});
  const j=await r.json().catch(()=>({}));
  if(r.status===409&&j.busy){think.textContent='a turn is already running — try again in a moment';}
  else if(!r.ok){think.textContent=j.error||'chat service not running — press start';}
  else{
   for(;;){await new Promise(res=>setTimeout(res,1000));
    const pr=await fetch('/chat_poll?turn='+j.turn);
    if(!pr.ok){think.textContent='chat error: lost the turn ('+pr.status+')';break;}
    const pj=await pr.json();
    if(pj.done){think.remove();chatAdd('bot',pj.reply||'(no reply)');break;}
   }
  }
 }catch(e){think.textContent='chat error: '+e;}
 chatBusy=false;}
async function chatStart(){
 const b=document.getElementById('chatstartbtn');
 b.disabled=true;b.textContent='starting…';
 try{
  const r=await fetch('/chat_start',{method:'POST'});
  const j=await r.json().catch(()=>({}));
  if(!r.ok&&j.error)chatAdd('sys',j.error);
 }catch(e){chatAdd('sys','start failed: '+e);}
 for(let i=0;i<25;i++){await new Promise(res=>setTimeout(res,1000));
  try{const j=await(await fetch('/chat_status')).json();
   if(j.ok){chatAdd('sys','chatbot started — say hi');break;}}catch(e){}}
 b.disabled=false;b.textContent='▶ start chatbot';chatStatusTick();}
async function loadScans(){
 const j=await(await fetch('/scans')).json();
 document.getElementById('scangrid').innerHTML=(j.scans||[]).map(n=>
  '<figure><a href="/scans/'+n+'" onclick="pano3d(\'/scans/'+n+'\');return false"><img loading="lazy" src="/scans/'+n+'"></a>'+
  '<figcaption><span>'+n+'</span>'+
  '<button onclick="identifyScan(this,\''+n+'\')" title="find objects in this saved view and add boxes">🔍</button>'+
  '<button class="warn" onclick="delScan(\''+n+'\')">del</button></figcaption></figure>').join('')
  ||'<small style="grid-column:1/-1;text-align:center">no 3D views saved yet — press Start on the gamepad or run a scan</small>';}
async function identifyScan(btn,n){
 btn.disabled=true;btn.textContent='…';
 try{
  // baseline BEFORE submitting — a fast worker must not outrun the read
  let before=null;
  try{const m=await fetch('/scan_meta/'+n);if(m.ok)before=(await m.json()).made;}catch(e){}
  const r=await fetch('/scan_identify/'+n,{method:'POST'});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){btn.textContent='🔍';btn.disabled=false;cout('✗ '+(j.error||'identify failed'));return;}
  // poll the sidecar until its made-stamp changes (bounded ~6 min)
  for(let i=0;i<72;i++){await new Promise(res=>setTimeout(res,5000));
   try{const m=await fetch('/scan_meta/'+n);
    if(m.ok){const meta=await m.json();
     if(meta.made!==before){cout('✓ boxes added to '+n+' ('+meta.objects.length+' objects)');break;}}}catch(e){}}
 }catch(e){cout('✗ identify: '+e);}
 btn.textContent='🔍';btn.disabled=false;}
async function delScan(n){await fetch('/delete_scan/'+n,{method:'POST'});loadScans();}
let scansSeen='';
async function scansTick(){                 // 3D-views tab auto-refresh
 if(gtab!=='scans')return;
 try{const j=await(await fetch('/scans')).json();
  const sig=(j.scans||[]).join(',');
  if(sig!==scansSeen){scansSeen=sig;loadScans();}
 }catch(e){}}
async function clearAllScans(){const j=await(await fetch('/scans')).json();const ns=j.scans||[];
 if(!ns.length||!confirm('Delete all '+ns.length+' 3D views?'))return;
 for(const n of ns){await fetch('/delete_scan/'+n,{method:'POST'});}
 loadScans();}

// ── pose badge: dead-reckoned position/heading + camera aim (top right) ──────
async function poseTick(){try{
 const p=await(await fetch('/pose')).json();
 const el=document.getElementById('posetext');if(!el)return;
 if(!p.fresh){el.style.color='#667';el.textContent='pose: no telemetry';return;}
 el.style.color='#cde';
 el.textContent='('+p.x.toFixed(2)+', '+p.y.toFixed(2)+')m ⟲'+p.heading.toFixed(0)+
  '° cam '+p.pan.toFixed(0)+'°/'+p.tilt.toFixed(0)+'°'+
  (p.battery_v?' 🔋'+p.battery_v.toFixed(1)+'V':'');
}catch(e){}}
async function poseReset(){await fetch('/pose_reset',{method:'POST'});poseTick();}
async function scanCancel(){await fetch('/scan_cancel',{method:'POST'});panoStat();}
// ── auto-flash kill switch: OFF forbids the chatbot's automatic flashlight ──
function paintAutoFlash(on){const b=document.getElementById('autoflashbtn');
 if(!b)return;b.textContent='🔦 auto-flash '+(on?'ON':'OFF');
 b.style.background=on?'':'#555';}
async function autoFlashTick(){try{
 const j=await(await fetch('/auto_flash')).json();paintAutoFlash(j.on);}catch(e){}}
async function toggleAutoFlash(){try{
 const cur=await(await fetch('/auto_flash')).json();
 const j=await(await fetch('/auto_flash?on='+(cur.on?0:1),{method:'POST'})).json();
 paintAutoFlash(j.on);cout('auto-flash '+(j.on?'enabled':'disabled — the chatbot cannot turn lights on by itself'));
}catch(e){}}
setInterval(poseTick,500);poseTick();

// ── speed cap: slider + number input + live value, synced from the server ────
function syncCap(v){v=Number(v);const c=document.getElementById('cap'),n=document.getElementById('capNum'),
 s=document.getElementById('capShow');if(c)c.value=v;if(n)n.value=v;if(s)s.innerHTML='<small>cap '+v.toFixed(2)+'</small>';}
function setCap(v){v=Number(v);if(!Number.isFinite(v))v=0;v=Math.max(0,Math.min(0.5,v));
 fetch('/speed?'+new URLSearchParams({cap:v}),{method:'POST'}).then(r=>r.json()).then(j=>syncCap(j.cap!==undefined?j.cap:v)).catch(()=>syncCap(v));}
async function initCap(){try{const j=await(await fetch('/speed')).json();if(j.cap!==undefined)syncCap(j.cap);}catch(e){}}

// ── direct command box: controller commands → existing HTTP endpoints ────────
// Pure client mapping; the server keeps all clamps/watchdog/estop. No raw serial.
const CMD_ALIAS={relax:'gimbal_relax',lock:'gimbal_lock',snap:'snapshot',fwd:'move_forward',back:'move_back',
 // chatbot-vocabulary parity (plan 019): bare up/down/left/right = CAMERA (as in the chatbot);
 // wheel turns are spinl/spinr or move_left/move_right. move = ONE ~0.5s pulse (not continuous).
 up:'camera_up',down:'camera_down',left:'camera_left',right:'camera_right',
 cam:'camera_aim',center:'camera_center',photo:'snapshot',move:'drive'};
const CMD_REQ={drive:['l','r'],camera_aim:['pan','tilt'],speed:['cap']};       // required numeric args
const CMD_OPT={move_forward:'ms',move_back:'ms',move_left:'ms',move_right:'ms', // one optional numeric arg
 camera_up:'deg',camera_down:'deg',camera_left:'deg',camera_right:'deg'};
const CMD_LIGHT=['light_head','light_base'];                                    // optional on|off
const CMD_NOARG=['stop','estop','camera_center','gimbal_relax','gimbal_lock','snapshot'];
function cout(m){document.getElementById('cmdout').textContent=m;}             // textContent: no XSS
function cnum(s){const v=Number(s);return Number.isFinite(v)?v:null;}          // rejects '10abc'/NaN/Inf (empty tokens are gated by the arity checks)
function toggleHelp(){const h=document.getElementById('cmdhelp');h.style.display=(h.style.display==='none')?'block':'none';}
// parseCmd: raw text → {cmd,path} or {error}. Shared by the box and the program.
function parseCmd(raw){
 const t=raw.trim().split(/\s+/);let c=(t[0]||'').toLowerCase();const a=t.slice(1);
 // chatbot-vocabulary special cases (units converted; plan 019)
 if(c==='spinl'||c==='spinr'){                       // chatbot: seconds → controller: ms
  if(a.length>1)return {error:c+' takes at most one number (seconds)'};
  let s=0.6;if(a.length===1){const v=cnum(a[0]);if(v===null)return {error:'not a number: '+a[0]};s=v;}
  const ms=Math.max(0,Math.min(5000,Math.round(s*1000)));
  return {cmd:c==='spinl'?'move_left':'move_right',
          path:'/'+(c==='spinl'?'move_left':'move_right')+'?'+new URLSearchParams({ms:ms})};
 }
 if(c==='light'){                                    // chatbot: light F B (PWM) → on/off pair (>0 = on)
  if(a.length!==2)return {error:'light needs 2 numbers: FRONT BASE (>0 = on)'};
  const f=cnum(a[0]),b=cnum(a[1]);if(f===null||b===null)return {error:'light args must be numbers'};
  return {cmd:'light',multi:['/light_head?on='+(f>0?1:0),'/light_base?on='+(b>0?1:0)]};
 }
 c=CMD_ALIAS[c]||c;
 let qs=null;
 if(CMD_REQ[c]){const k=CMD_REQ[c];
  if(a.length!==k.length)return {error:c+' needs '+k.length+' number(s): '+k.join(' ')};
  qs=new URLSearchParams();for(let i=0;i<k.length;i++){const v=cnum(a[i]);if(v===null)return {error:'not a number: '+a[i]};qs.set(k[i],v);}
 }else if(CMD_OPT[c]){
  if(a.length>1)return {error:c+' takes at most one number'};
  if(a.length===1){const v=cnum(a[0]);if(v===null)return {error:'not a number: '+a[0]};qs=new URLSearchParams();qs.set(CMD_OPT[c],v);}
 }else if(CMD_LIGHT.includes(c)){
  if(a.length>1)return {error:c+' takes on|off or nothing'};
  if(a.length===1){const s=a[0].toLowerCase();
   if(s==='on'||s==='1'||s==='true'){qs=new URLSearchParams();qs.set('on',1);}
   else if(s==='off'||s==='0'||s==='false'){qs=new URLSearchParams();qs.set('on',0);}
   else return {error:c+' arg must be on|off'};}
 }else if(CMD_NOARG.includes(c)){
  if(a.length)return {error:c+' takes no args'};
 }else return {error:'unknown command: '+t[0]};
 return {cmd:c,path:'/'+c+(qs?'?'+qs.toString():'')};
}
// 'boxes' is CLIENT-ONLY (controls the 3D viewer's object overlays) — it maps
// to no endpoint, so it is intercepted BEFORE parseCmd.
function boxesCmd(raw){
 const parts=raw.trim().split(/\s+/);
 if(parts[0].toLowerCase()!=='boxes')return false;
 const arg=parts.slice(1).join(' ').toLowerCase()||'on';
 if(arg==='on'||arg==='off'){localStorage.setItem('roverboxes:on',arg==='on'?'1':'0');}
 else if(arg==='all'){localStorage.setItem('roverboxes:filter','');localStorage.setItem('roverboxes:on','1');}
 else{localStorage.setItem('roverboxes:filter',arg);localStorage.setItem('roverboxes:on','1');}
 if(window.redrawBoxes)window.redrawBoxes();
 cout('✓ boxes '+(arg==='on'||arg==='off'?arg:(arg==='all'?'all shown':'filter: '+arg)));
 return true;
}
// sendCommand: run one command; returns a Promise<bool ok> (awaited by the program).
function sendCommand(raw,signal){
 if(boxesCmd(raw))return Promise.resolve(true);
 const p=parseCmd(raw);
 if(p.error){cout('✗ '+p.error);return Promise.resolve(false);}
 cout('… '+raw);
 if(p.multi){                                        // e.g. 'light F B' = two channel sets
  return Promise.all(p.multi.map(u=>fetch(u,{method:'POST',signal}).then(r=>r.ok).catch(()=>false)))
   .then(oks=>{const ok=oks.every(Boolean);cout((ok?'✓ ':'✗ ')+raw);return ok;});
 }
 return fetch(p.path,{method:'POST',signal}).then(r=>r.json().then(j=>({ok:r.ok,j})).catch(()=>({ok:r.ok,j:{}}))).then(function(res){
  if(res.ok){let extra='';const j=res.j||{};
   if(j.cap!==undefined){extra=' (cap '+j.cap+')';syncCap(j.cap);}
   else if(j.pan!==undefined){extra=' (pan '+j.pan+' tilt '+j.tilt+')';}
   else if(j.on!==undefined){extra=' ('+(j.on?'on':'off')+')';}
   cout('✓ '+raw+extra);
   if(p.cmd==='snapshot'){seen='';load();}
   return true;
  }
  cout('✗ '+raw+' → '+((res.j&&res.j.error)||'HTTP error'));return false;
 }).catch(e=>{if(!e||e.name!=='AbortError')cout('✗ '+e);return false;});
}
function runCmd(){const el=document.getElementById('cmdin');const raw=el.value.trim();if(!raw)return;sendCommand(raw);el.value='';}
function pick(tpl){const el=document.getElementById('cmdin');el.value=tpl;el.focus();}

// ── program: a saved 'scratch' stack of commands (build, reorder, run, repeat) ─
let prog=[], running=false, runGen=0, runAbort=null;
const MIN_STEP_MS=60;   // floor so repeat×0-gap of instant steps can't hammer the server
function renderProg(){
 const ol=document.getElementById('program');
 ol.innerHTML=prog.map((s,i)=>'<li><span></span>'+
  '<button onclick="mv('+i+',-1)">↑</button><button onclick="mv('+i+',1)">↓</button>'+
  '<button class="warn" onclick="rm('+i+')">×</button></li>').join('');
 [...ol.children].forEach((li,i)=>{li.querySelector('span').textContent=(i+1)+'. '+prog[i];});   // textContent: no XSS
 if(!running)document.getElementById('progstat').innerHTML='<small>'+(prog.length?prog.length+' step(s)':'empty — build a sequence with ＋ Add')+'</small>';
}
function addStep(){const el=document.getElementById('cmdin');const raw=el.value.trim();if(!raw)return;
 const p=parseCmd(raw);if(p.error){cout('✗ '+p.error);return;}
 prog.push(raw);renderProg();el.value='';cout('added: '+raw);}
function rm(i){prog.splice(i,1);renderProg();}
function mv(i,d){const j=i+d;if(j<0||j>=prog.length)return;[prog[i],prog[j]]=[prog[j],prog[i]];renderProg();}
function clearProg(){if(prog.length&&confirm('Clear the program?')){prog=[];renderProg();}}
function sleepMs(ms){return new Promise(r=>setTimeout(r,ms));}
// motionMs: how long a step keeps the wheels moving, so we wait it out before the
// next step (non-overlap). Must mirror the server: /drive auto-stops after
// watchdogTTL (500ms in rovercontrol.go — keep in sync); /move_* self-stops after
// its ms arg (nudge default 400, clamped 0..5000). Camera/light/etc = 0.
function motionMs(raw){const p=parseCmd(raw);if(p.error||!p.cmd||!p.path)return 0;const c=p.cmd;
 if(c==='drive')return 500;
 if(c.indexOf('move_')===0){
  // read ms from the PARSED path (spinl converts seconds→ms there; re-reading the
  // raw token would treat 'spinl 2' as 2ms and under-wait the sequencer).
  // Missing ms must fall back to the server default 400 — note Number(null)===0,
  // so test for null explicitly or a bare move_forward would under-wait.
  const q=new URLSearchParams(p.path.split('?')[1]||'').get('ms');
  const m=(q===null)?NaN:Number(q);
  return Number.isFinite(m)?Math.max(0,Math.min(5000,m)):400;}
 return 0;}
async function runProgram(){
 if(running||!prog.length)return;                          // ignore while running: Stop, then Run to restart
 const my=++runGen; running=true;
 const ac=(typeof AbortController!=='undefined')?new AbortController():null; runAbort=ac;
 const steps=prog.slice();                                 // snapshot: mid-run edits can't corrupt the loop
 const reps=Math.max(1,Math.min(1000,parseInt(document.getElementById('reps').value)||1));
 const gap=Math.max(0,Math.min(10,parseFloat(document.getElementById('gap').value)||0))*1000;
 const ol=document.getElementById('program');
 const ps=document.getElementById('progstat');
 const clearHi=()=>[...ol.children].forEach(li=>li.classList.remove('run'));
 try{
  for(let r=0;r<reps;r++){
   for(let i=0;i<steps.length;i++){
    if(my!==runGen)return;                                 // superseded / stopped
    clearHi();if(ol.children[i])ol.children[i].classList.add('run');
    ps.innerHTML='<small>rep '+(r+1)+'/'+reps+' · step '+(i+1)+'/'+steps.length+'</small>';
    const ok=await sendCommand(steps[i],ac&&ac.signal);
    if(my!==runGen)return;
    if(!ok){ps.innerHTML='<small>stopped: step '+(i+1)+' failed</small>';return;}   // a failed step aborts
    await sleepMs(Math.max(gap,motionMs(steps[i]),MIN_STEP_MS));
   }
  }
  ps.innerHTML='<small>done</small>';
 }finally{
  // ownership-guarded: only THIS run (if still current) clears state + stops the
  // wheels — a superseded/stopped old run must not stomp a newer run or re-/stop.
  if(my===runGen){running=false;clearHi();fetch('/stop',{method:'POST'});}
 }
}
function stopProgram(){runGen++;running=false;
 if(runAbort)try{runAbort.abort();}catch(e){}                 // cancel any in-flight step request
 [...document.getElementById('program').children].forEach(li=>li.classList.remove('run'));
 fetch('/stop',{method:'POST'});cout('program stopped');      // + server drive-watchdog stops any leaked pulse (≤500ms)
 document.getElementById('progstat').innerHTML='<small>stopped</small>';}
// named programs saved in the browser (localStorage)
function refreshSaved(){const sel=document.getElementById('saved');
 const names=Object.keys(localStorage).filter(k=>k.indexOf('roverprog:')===0).map(k=>k.slice(10)).sort();
 sel.innerHTML='<option value="">load…</option>';
 names.forEach(n=>{const o=document.createElement('option');o.textContent=n;sel.appendChild(o);});}
function saveProg(){if(!prog.length)return;const n=(prompt('Save program as:')||'').trim();if(!n)return;
 localStorage.setItem('roverprog:'+n,JSON.stringify(prog));refreshSaved();cout('saved "'+n+'"');}
function loadProg(n){if(!n)return;try{const p=JSON.parse(localStorage.getItem('roverprog:'+n));
 if(Array.isArray(p)){prog=p.filter(x=>typeof x==='string');renderProg();cout('loaded "'+n+'"');}}catch(e){}}
const PANO_LABELS={scanning:'📷 scanning the room…',recording:'🎥 recording room tour…',describing:'🧠 building scene memory…',
 stitching:'🌀 creating 3D view…',uploading:'⬆ saving 3D view…'};
async function panoStat(){try{
 const j=await(await fetch('/pano_status')).json();
 const el=document.getElementById('panostat');if(!el)return;
 if(PANO_LABELS[j.state])el.innerHTML='<small>'+PANO_LABELS[j.state]+'</small>'+
  ((j.state==='scanning'||j.state==='stitching')?
   ' <button class="warn" id="scanstop" onclick="scanCancel()" title="stop the 3D scan and discard it" style="padding:2px 8px;font-size:11px">⏹ stop</button>':'');
 else if(j.state==='done'&&j.age_s>=0&&j.age_s<60)el.innerHTML='<small>✅ 3D view updated</small>';
 else if(j.state==='failed'&&j.age_s>=0&&j.age_s<60)el.innerHTML='<small>⚠ 3D view failed</small>';
 else el.textContent='';
}catch(e){}}
async function health(){try{const h=await(await fetch('/healthz')).json();
 document.getElementById('health').innerHTML='<small>serial '+(h.serial.up?'✓':'✗')+
 ' · cam '+(h.camera.up?'✓':'✗')+' · pad '+(h.gamepad.up?'✓':'–')+'</small>';}catch(e){}}
setInterval(()=>{load();health();panoStat();scansTick();chatStatusTick();autoFlashTick();},2000);
showTab('chat');load();health();panoStat();chatStatusTick();autoFlashTick();initCap();renderProg();refreshSaved();

// ── Mac-side gamepad: Gamepad API → existing HTTP endpoints (no server change).
// Drive is in-flight-guarded and refreshed continuously while deflected (feeds
// the 500ms server watchdog); centered → /stop once. Camera integrates the
// right stick into an absolute angle for /camera_aim. getGamepads() is re-read
// every tick (never cached).
let padIndex=null,padPrev=[],driveBusy=false,aimBusy=false,wasMoving=false;
let panAngle=0,tiltAngle=0,lastDrive=0,lastAim=0;
const DZ=0.15,PANR=90,TILTR=70,SENDMS=120;
const gpEl=document.getElementById('gp');
function gpStop(){fetch('/stop',{method:'POST'});}
addEventListener('gamepadconnected',e=>{padIndex=e.gamepad.index;padPrev=[];
 if(gpEl)gpEl.textContent='🎮 '+e.gamepad.id.slice(0,20);});
addEventListener('gamepaddisconnected',e=>{if(e.gamepad.index===padIndex){
 padIndex=null;gpStop();if(gpEl)gpEl.textContent='🎮 none';}});
document.addEventListener('visibilitychange',()=>{if(document.hidden){wasMoving=false;gpStop();}});
addEventListener('pagehide',()=>{try{fetch('/stop',{method:'POST',keepalive:true});}catch(e){}});
function dzf(v){return Math.abs(v)<DZ?0:v;}
function gpEdge(b,i){const n=!!(b&&b.pressed);const f=n&&!padPrev[i];padPrev[i]=n;return f;}
function gpPoll(){
 if(padIndex===null||document.hidden)return; // never drive while backgrounded
 const gp=navigator.getGamepads&&navigator.getGamepads()[padIndex];
 if(!gp)return;
 const ax=gp.axes,bt=gp.buttons,now=Date.now();
 if(gpEdge(bt[0],0))cmd('stop');
 if(gpEdge(bt[1],1))snap();
 if(gpEdge(bt[2],2))cmd('light_head');
 if(gpEdge(bt[3],3)){panAngle=0;tiltAngle=0;cmd('camera_center');}
 if(gpEdge(bt[4],4))cmd('light_base');
 if(gpEdge(bt[8],8))cmd('estop');
 const thr=-dzf(ax[1]||0),str=dzf(ax[0]||0);
 const l=Math.max(-1,Math.min(1,thr+str)),r=Math.max(-1,Math.min(1,thr-str));
 if(l!==0||r!==0){wasMoving=true;
  if(!driveBusy&&now-lastDrive>=SENDMS){driveBusy=true;lastDrive=now;
   fetch('/drive?l='+l.toFixed(3)+'&r='+r.toFixed(3),{method:'POST'}).finally(()=>{driveBusy=false;});}
 }else if(wasMoving){wasMoving=false;gpStop();}
 const dp=dzf(ax[2]||0)*PANR/20,dt=-dzf(ax[3]||0)*TILTR/20;
 if(dp!==0||dt!==0){
  panAngle=Math.max(-180,Math.min(180,panAngle+dp));
  tiltAngle=Math.max(-45,Math.min(90,tiltAngle+dt));
  if(!aimBusy&&now-lastAim>=SENDMS){aimBusy=true;lastAim=now;
   fetch('/camera_aim?pan='+panAngle.toFixed(1)+'&tilt='+tiltAngle.toFixed(1),{method:'POST'}).finally(()=>{aimBusy=false;});}
 }
}
setInterval(gpPoll,50);
</script>
</body></html>'''
