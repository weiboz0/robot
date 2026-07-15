# 029 — Object boxes in the 3D viewer, gamepad scan-stop, live scans tab

## Goal

1. **Gamepad stop**: the ⏹ scan-stop from plan 028, bindable on the pad.
   Note: on Xbox-layout pads "Select/Back" is button 6 = the existing
   e-stop, which ALREADY aborts scans — the new dedicated `scan_stop`
   action defaults to **button 8** (Guide on Xbox, Select on many generic
   pads) and is rebindable like everything else.
2. **3D views tab auto-updates**: new scans appear without a page refresh.
3. **Object boxes in saved scans** ("hitboxes that move as you drag"):
   after a scan is saved, the pipeline identifies notable objects **from the
   already-captured frames** (no camera movement), stores them as angular
   positions, and the 3D viewer overlays labeled boxes that track the view
   as the user drags/zooms. First live targets: the suitcase, the container,
   and the two printers. Boxes can be toggled, and the web command box
   filters which names are shown.

## Discovery

- No ultralytics on the Pi, and COCO/YOLO has no "printer" class anyway —
  but the rover resolves the `opencode` LLM provider (functional probe), and
  `vision.VisionModel.describe_many` (multi-image JSON) is the proven path.
- The scan subprocess already has the 13 source frames on disk with known
  (pan, tilt) poses and FOV 88° — detections on flat frames convert directly
  to sphere directions; detecting on the warped equirect would be far worse.

## Design

### Identification (in the existing hardened scan subprocess)

- `scene.identify_objects(vis, frames)` — one chunked `describe_many` call
  (all frames, labeled with their index + direction) asking for JSON:
  `objects: [{frame, name, color, bbox:[x1,y1,x2,y2] fractions}]`, each
  PHYSICAL object reported once (frames overlap 60°/88° — the prompt says to
  pick the frame where the object is most central, mirroring the existing
  OVERLAP-aware describe_scene). **Angular conversion is exact pinhole
  vector math** (round-1 reviewer catch: the linear approximation erred up
  to ~3.8° mid-frame at 88° FOV): the bbox center maps to a **y-UP**
  camera-space ray `[(2cx−1)·tan(HFOV/2), (1−2cy)·tan(HFOV/2)·h/w, 1]`,
  rotated by **`Ry(pan)·Rx(−tilt)`** — NOT the warper's y-down `R(pan,tilt)`
  verbatim (round-2 reviewer catch, verified numerically: mixing the y-up
  ray with the y-down matrix negates the tilt term and mirrors upper-ring
  objects ~70° below the horizon; the flipped-tilt form is the exact F·R·F
  conjugation of the warper's matrix into y-up space). Then
  `lon = atan2(x, z)`, `lat = asin(y/|v|)` in y-up. Box angular size from
  converting both bbox corners. The cv2-gated warper consistency test exists
  precisely to pin this convention.
  Output meta: `{"objects":[{name, color, lon, lat, w, h}], "made": <iso>}`.
- CLI: `build-pano … [variants_dir] [--identify]` — after the variants,
  tries identification; ANY failure (no provider, timeout, bad JSON) logs to
  stderr and is skipped. **Explicit budget/cancel semantics** (round-1
  reviewer ask): identification runs INSIDE the same process group, after
  the variants — the controller's cancel/timeout `killpg` kills a mid-flight
  LLM HTTP call with everything else (requests die with the process; no
  cleanup needed). The LLM call itself gets `timeout=90` via the vision
  client so a hung provider can't eat the 300 s budget (build ≈ 40 s + one
  chunked call ≈ 15–40 s → worst case well clear). Writes `meta.json` into
  variants_dir.
- **Key availability (round-1 blocker)**: the controller's env has no LLM
  key under systemd/nohup — the CLI itself calls `llm_config.load_dotenv()`
  before constructing `VisionModel` (functionally probed on the rover:
  `load_dotenv()+pick_provider()` resolves `opencode` there). Without it the
  feature would be silently inert in production.
- Controller: `pano_build_cmd` appends `--identify`; on the success path the
  meta is `os.replace`d to `photos/panorama.meta.json` and then hard-linked
  as `photos/scans/<name>.meta.json` (same file → sidecar can't diverge).
  Failures never fail the scan; stale live meta is deleted when a scan
  produces none. `/delete_scan/` removes the sidecar (mirroring photo meta).
  **`POST /panorama` (the chatbot publish path, which bypasses the
  subprocess) also deletes `panorama.meta.json`** — a chatbot-published pano
  must never show a previous scan's boxes (round-1 blocker; tested).

### Endpoints

- `GET /pano_meta` → live `panorama.meta.json` (404 when absent).
- `GET /scan_meta/<name>` → the archived sidecar (name validated by
  `SCAN_NAME_RE`). Both read-only, ungated.

### Viewer overlay (rovercontrold_page.py)

- `pano3d` fetches the meta for its source (live → `/pano_meta`, archived →
  `/scan_meta/<name>`). For each object, an absolutely-positioned label div
  over the canvas, re-projected on every `draw()`. **Exact inverse of the
  shader** (round-1 reviewer spec): the shader's forward pass is
  `W = Ry(yaw)·Rx(pitch)·d_cam`, so the overlay computes
  `d_cam = Rx(pitch)ᵀ · Ry(yaw)ᵀ · W(lon, lat)` — undo yaw FIRST, then
  pitch; visible iff `d_cam.z > 0` (frustum cull) AND the object's `lat`
  lies inside the pano's covered band (same `vspan` the shader uses);
  screen `u = d.x/(d.z·ar·k)+0.5`, `v = 0.5−d.y/(d.z·k)`,
  `k = 2·tan(fov/2)`. Box pixel size from its angular size
  (`px per rad = cv.height/(2·tan(fov/2))`). Dragging/zooming re-renders →
  boxes track.
- **◻ boxes** toggle button in the viewer bar (default on when meta exists);
  hidden entirely when there is no meta.
- **Command box**: `boxes on` / `boxes off` / `boxes all` /
  `boxes <name,name…>` (substring match filter, persisted in localStorage,
  applied live to an open viewer). Client-only command **intercepted before
  `parseCmd`** (it maps to no endpoint — otherwise it would error). The ❔
  commands panel documents them.

### Gamepad + tab refresh

- Mapping action `scan_stop` (default `_btn(8)`, edge-triggered) →
  `app.cancel_scan()`; wizard prompt; CONTROL_KEYS/validation/docs updated.
- The existing 2 s poll refreshes the scans grid when the tab is active and
  the `/scans` list signature changed.

## Deliverables

- `scene.py`: `identify_objects` + angular conversion + CLI `--identify`.
- `rovercontrold.py`: `scan_stop` mapping + hook; meta move/link/stale-delete
  + sidecar delete; `/pano_meta`, `/scan_meta/`; cmd pin gains `--identify`.
- `rovercontrold_page.py`: overlay renderer + toggle + `boxes` commands +
  scans auto-refresh.
- Tests: angular conversion math — exact pinhole cases (center → frame
  pose; frame edge → pose ± HFOV/2; the mid-frame points codex quantified,
  asserting the atan values NOT the linear ones; wrap at ±180; aspect), and
  a **shader-convention consistency test**: a synthetic frame with a marker
  at a known pixel run through the REAL warper (cv2-gated) must land within
  1° of where the conversion says (round-1 reviewer ask); an explicit
  pinhole-vs-linear regression pin (the atan value asserted at a point where
  linear errs ~3.8°); `POST /panorama` clears stale live meta (test);
  identify LLM-call budget documented as ≤ 2 chunked requests ≈ ≤ 60 s
  worst case inside the 300 s window;
  identify_objects with a fake vision (chunking, dedup instruction shape,
  malformed-JSON skip); CLI --identify failure never breaks the build
  (stub vision import failure); controller meta publish + archive sidecar +
  stale delete + delete_scan removes sidecar; endpoints (200/404/validation);
  cmd pin; mapping default/edge for scan_stop; page markers (overlay fn,
  toggle id, boxes command table entries, scans-refresh signature check).

## Testing

CI (fakes; LLM never called in tests). Live: fresh scan on the rover →
meta exists; verify the suitcase, container, and both printers are among the
identified objects with sane directions (compare against the known room);
boxes render and track in the viewer (user confirms visually); `boxes off`
hides, `boxes printer` shows only printers; gamepad button 8 aborts a scan;
a new scan pops into the 3D views tab without refresh.

## Risks

- *LLM bbox quality*: markerless LLM boxes are approximate — good enough for
  labeled hitboxes; the angular conversion is exact given the pose, so
  placement error is bounded by the model's bbox error. If a target is
  missed, the prompt lists exemplar categories (luggage, printers, bins).
- *Cost/latency*: one chunked call per scan (~2 requests at 13 frames);
  skipped silently if the provider is unavailable.
- *Equirect seam wrap*: lon normalized ±180; the viewer projection handles
  wrap by vector math (no seam special-casing).

## Stages

1. scene: angular math + identify_objects + CLI flag (tests).
2. Controller: mapping, meta plumbing, endpoints (tests); CI.
3. Page: overlay + toggle + commands + auto-refresh (marker tests); CI.
4. Deploy; live scan; verify the 4 targets; gamepad + tab checks.
5. Review gate, PR.

## Reviews

### Plan review

- **codex** — **BLOCKED → resolved** (its plan-re-verify CLI stalled and was
  killed; the resolutions were instead verified in its full code review
  below, which PASSed with the conversion/tests in place).
  Round-1 blockers, all resolved with stronger measures: linear angular
  conversion materially wrong (quantified ~3.8° mid-frame at 88° FOV; wanted
  atan pinhole) → exact pinhole VECTOR math adopted; explicit identify
  timeout/cancel semantics → 90 s vision timeout inside the killpg'd group,
  budget corrected to ≤ 2 chunks ≈ ≤ 60 s worst; projection tested against
  the shader convention → cv2-gated real-warper consistency test (< 1°).
- **Opus** — **BLOCKED → BLOCKED → PASS.** Round 1 (four blockers): same
  math issue; exact inverse-projection spec (undo yaw then pitch, vspan
  band cull, frustum test) — now spelled out; LLM key silently absent in the
  subprocess env under systemd/nohup (feature would be inert in production)
  → the CLI calls `load_dotenv()` itself, functionally probed on the rover;
  `POST /panorama` (chatbot path) would show stale boxes → meta deleted
  there, tested. Round 2: caught a sign bug in MY round-1 fix — the y-up ray
  mixed with the warper's y-down matrix mirrors tilt (~70° error for
  upper-ring objects; verified numerically) → `Ry(pan)·Rx(−tilt)`
  conjugation adopted verbatim. Round 3: PASS, formula re-verified
  numerically.

### Code review

- **codex** — **PASS**, no blockers. Notes (accepted trade-offs): two-corner
  box sizing vs four-corner min/max; `_frame_aspect` doesn't special-case
  fill bytes (controlled camera JPEGs, safe fallback); colorless twins can
  still merge in dedup.
- **Opus** — **PASS.** Verified the conversion + warper test are load-bearing
  (ran them), the JS overlay is the exact shader inverse line-by-line, test
  fixtures cannot reach a real LLM, and `parse_frame_name` keeps pano
  artifacts out of the identify input. One reproduced non-blocking
  regression — e-stop/drive during the minutes-long identify window flipped
  the state to "failed" though the pano published — **fixed** (published
  flag is the state authority) with a test.
- **glm-5.1** — **PASS** with two flagged follow-ups, one **fixed in-branch**:
  N1 (identify held the scan slot ~5 min — back-to-back scans refused, and a
  fresh scan could inherit a straggler's stale meta) → the slot now releases
  at publish, identification runs after `_finish_scan`, and a new scan kills
  a straggling identify (tested). N2 (chatbot `POST /panorama` racing an
  in-flight identify can attach the rover scan's meta to the chatbot's pano
  within an ε-window) → **deferred**: the chatbot path is manual and rare;
  noted here for a future freshness-token if it ever bites. Also deferred:
  ⏹ killing a mid-flight identify (it currently only aborts the scan).

## Post-execution report

Implemented on `feature/scan-object-boxes`; validated live on the rover.

- **All four user targets found live**: blue suitcase (lon 22.7°), clear
  storage bin (149.4°), black printer (39.5°, upper ring), white printer
  (143.7°) — among 18 objects, each with sphere directions the viewer
  overlays as drag-tracking boxes.
- **Live-measurement pivots** (each recorded in Design): per-frame
  `/no_think` LLM calls (multi-image: 100 s→timeout; single-image: reliable)
  with eye-ring-first ordering and a 300 s budget keeping partial results;
  identification detached into its own killpg-bounded subprocess AFTER
  publish (scan shows done at ~66 s; boxes arrive minutes later); live meta
  deleted at publish so old boxes never describe a new pano (a live-test
  catch); openai SDK installed on the Pi system python (was missing —
  identification would have been inert).
- **Plan gate earned its keep**: 3 rounds — codex quantified a 3.8° math
  error in the angular conversion; Opus caught a sign bug in the round-1 fix
  (70° mirror for upper-ring objects), the missing LLM key in the subprocess
  env, and the chatbot stale-meta path.
- CI green (~330 tests). Sign-off checks the user should do visually: boxes
  land on the right objects in the viewer; `boxes printer` filters; the tab
  auto-updates; gamepad button 8 stops a scan.
