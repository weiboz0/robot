# 031 — Boxes for old 3D views, chatbot auto-flashlight, and its kill switch

## Goal (user asks, verbatim intent)

1. **Add boxes to PREVIOUS 3D views** — e.g. "add a box for the stack of
   books on the 2nd-last 3D view". The robot must reach ALL saved scans.
2. **Auto-flashlight for the chatbot**: when a capture is too dark, turn the
   light on automatically, take the shot, restore the lights.
3. **Two buttons**: 🔍 *identify* on every saved 3D view, and a 🔦
   **auto-flash kill switch** — when off, the chatbot must NEVER turn the
   light on by itself (user clarification: "force disable flash light, so
   the chatbot won't enable that light when automated").

## Design

### Identify on archived scans (frames are gone — the equirect isn't)

The scan frames are deleted after stitching, but the archived panorama is
what the viewer renders — so boxes must be placed in **the viewer's own
coordinate mapping**, which is exact by construction for EVERY pano
(round-2 reviewer catch: seamcut/projector panos are full-sphere 2:1, but
the `_stitcher_pano` fallback yields arbitrary crops — an assumed-2:1
mapping would misplace boxes there; the viewer itself derives
`vspan = min(π, 2π·h/w)` from the aspect):
`lon = x·360 − 180`, `lat = (0.5 − y)·vspan_deg`,
`vspan_deg = min(180, 360·h/w)` — matching the shader means a box always
lands on the pixels it was drawn from. New
`scene.identify_equirect(vis, pano_jpeg, focus=None)`:
- Slices the pano into 6 vertical strips (90° wide, 60° step → 30° overlap,
  full height, shrunk for upload) — per-strip LLM calls exactly like the
  live per-frame path (single-image `/no_think`, budget, partial results),
  with strip-left lon offsets converting bboxes linearly in viewer coords.
  **The strip straddling the ±180 seam wraps its pixel columns** (the pano
  is cyclic in x; `np.hstack` of the two edges) — round-2 reviewer note.
- Optional `focus` hint appended to the prompt ("be sure to include, if
  visible: …") — how "the stack of books" gets prioritized.
- `_dedup_objects` handles the strip overlaps with a WIDER window for the
  pano path (25°/18° via parameters — warped-pano duplicates land farther
  apart than flat-frame ones; flagged as a tuning risk).
- CLI: `scene.py identify-pano <pano.jpg> <out.json> [focus…]`.

### Controller: `POST /scan_identify/<name>[?focus=…]`

- Validates `SCAN_NAME_RE` + file exists; **202 immediately** (no thread
  waits on an LLM — plan 030 rule); a worker thread runs the CLI subprocess
  (own group, `IDENTIFY_TIMEOUT_S` killpg — the identical `_run_identify`
  harness) and on success replaces `scans/<name>.meta.json`.
- **Single-flight, exact semantics (round-1 reviewer demand)** — one
  `_ident_busy` flag under `_pano_mu` covering the ENTIRE worker body
  (frame/strip prep, subprocess, meta writes — not merely the subprocess;
  round-2 note: `_identify_proc` alone has None-windows), NON-BLOCKING
  acquire on every path, nobody ever waits for it. The web worker also
  stashes/clears `_identify_proc` exactly like the scan-time path, so
  `start_scan`'s straggler-kill covers it too — a NEW scan killing a
  running web identify is accepted best-effort behavior (stated):
  - archived identify: try-acquire at submit → 409 `busy` if any identify
    (either kind) holds it; released in the worker's finally.
  - scan-time identify (runs inline on the scan thread AFTER the scan is
    done and published): try-acquire → on failure it SKIPS with a log
    ("identify busy — this scan gets no boxes; press 🔍 later"), never
    blocks or fails the scan.
  - `/scan` itself is NOT gated by this flag (scans and identifies may
    overlap; only identifies exclude each other).
- **Live-meta refresh at COMMIT time (round-1 reviewer demand)**: the worker
  re-checks `name == list_scans()[0]` under `_pano_mu` immediately before
  touching `panorama.meta.json` — if a newer scan archived while the LLM
  ran, only the sidecar is written and the live meta is left to the newer
  scan's own identify. (The scan-time path needs no such check: it IS the
  newest by construction, and the flag excludes a concurrent archived
  worker.)
- Completion is observable via the sidecar's `made` timestamp changing
  (`GET /scan_meta/<name>`), which is how both the page and the chat tool
  wait.

### Auto-flashlight (chatbot) + the kill switch

- **Kill switch lives on the CONTROLLER** (authoritative + survives chat
  restarts): `GET /auto_flash` → `{"on": bool}`, `POST /auto_flash?on=0|1`.
  Persisted as a marker file (`photo_dir/.auto_flash_off`; presence = off)
  so a controller restart keeps the user's choice. Default ON. **photo_dir
  is not created at controller startup** (round-2 catch): the POST does
  `os.makedirs(photo_dir, exist_ok=True)` before writing, and the read
  treats a missing dir as marker-absent = ON. (Leading-dot name is already
  excluded from photo listings by `safe_photo_name`.)
- **healthz gains `lights: {head, base}`** so the chatbot can restore the
  EXACT prior light state (not blindly off) after a flash-assisted photo.
- **agent_chat**: `rover_photo` (the chatbot's capture tool) gains the
  auto-flash wrap: grab a stream frame → mean luma (cv2, lazy; no cv2 → skip)
  `< DARK_MEAN (55/255)` AND the kill switch is ON → save prior light state,
  lights full on, ~0.8 s for auto-exposure, take the photo, restore the
  prior state; the tool result says "(dark — used the flashlight)" so the
  model can tell the user. Serial-backend REPL (no controller flag readable)
  defaults to allowed. Scans/find keep their own pipelines (untouched —
  they have exposure compensation).
- **RoverCtl/rovercontrol_client**: `auto_flash_allowed()` (reads the flag on
  the rovercontrol backend; True elsewhere) and `light_state()` (from
  healthz).

### The two buttons + chat tool

- **🔍 identify** on every scan card (3D-views tab): POST
  `/scan_identify/<name>`; the card shows "identifying…" and clears when the
  sidecar's `made` changes (piggybacks the existing 2 s tick while the tab
  is open). 409 → toast via the card label.
- **🔦 auto-flash** toggle in the lights/speed bar: shows current state
  (`auto-flash ON/OFF`), POST toggles; state polled with health.
- **Chat tool `rover_identify_scan`**: `{which, focus}` — which =
  1 (newest), 2 (second-newest)… or "latest"; resolves via `/scans`, POSTs
  `/scan_identify`, waits for the sidecar `made` change (bounded ~6 min,
  poll 5 s), returns the object-name list so the bot can answer "added
  boxes: books, shelf, …". Tool description teaches the indexing ("the
  2nd-last 3D view" → which=2). **Explicitly accepted**: the chat session
  stays `busy` for the duration (like a `$scan` turn today) — the async
  serve model keeps the controller unblocked and the page shows thinking;
  a second chat message correctly gets "busy".
- **Auto-flash restore is `finally`-guaranteed** (round-1 reviewer note):
  if the capture itself raises, the prior light state is still restored;
  if even the restore fails, the tool result SAYS the lights may be on.
  The luma measurement is its own stream grab — if THAT grab fails, skip
  cleanly (treat as not-dark, take the normal photo, touch no lights).

## Deliverables

- scene.py: `identify_equirect` + strip math + `identify-pano` CLI (+ focus).
- rovercontrold.py: `/scan_identify/`, `/auto_flash` GET/POST + marker file,
  healthz lights, shared identify single-flight.
- rovercontrol_client.py + rover_backend.py: flag + light-state + identify
  helpers.
- agent_chat.py: auto-flash wrap on `rover_photo`, `rover_identify_scan`
  tool.
- rovercontrold_page.py: the two buttons + card states.
- Tests (all fakes): **single-flight ownership** (archived-vs-archived 409;
  scan-time skips-not-blocks while an archived worker holds the flag; flag
  released on worker failure; no deadlock with both paths racing);
  **commit-time live-meta race** (worker for an older-by-commit-time name
  writes ONLY the sidecar — live meta always describes the newest pano);
  **flash restore is finally-guaranteed** (capture raises → prior state
  restored; luma-grab failure → no light command at all); a cross-±180-seam
  strip-wrap case; **a NON-2:1 pano case** (strip math uses the
  aspect-derived vspan, matching the viewer); **marker on a photo_dir that
  does not exist yet** (POST creates it; read defaults ON);
  strip→lon/lat math (centers/edges/wrap, viewer-exactness);
  identify_equirect with a fake vision (strips counted, focus in prompt,
  dedup across overlaps); CLI focus arg; endpoint validation (bad name 400,
  missing 404, busy 409, 202 shape) + worker updates sidecar + newest-also-
  updates-live + single-flight vs a scan-identify; auto_flash flag GET/POST
  + marker persistence across App instances + healthz lights shape; chatbot:
  dark frame → lights on → photo → EXACT prior state restored (fake rover
  records the sequence), kill-switch-off → no light command, no cv2 → clean
  skip, bright frame → no flash; identify tool happy path + "2nd last"
  resolution + focus passthrough (fake client); page markers (button ids,
  fn names).

## Testing

CI. Live: 🔍 on the **2nd-last** scan with focus "stack of books" (the
user's example) → boxes appear including books; kill switch off → a forced-
dark photo does NOT light up; kill switch on → it does (report both);
`rover_identify_scan` from the web chat end-to-end.

## Risks

- *Equirect distortion near poles*: objects live in the mid-band; strips are
  full-height so nothing is missed, and dedup absorbs stretched duplicates.
- *LLM boxes on warped panos are looser than on flat frames*: accepted —
  it's the only source for OLD scans (frames deleted); new scans keep the
  sharper frame-based path.
- *Flash restore races a human toggling lights mid-photo*: last-writer-wins
  for a sub-second window; harmless.
- *Two identifies fighting*: shared single-flight; 409 the loser.

## Stages

1. scene strip math + identify_equirect + CLI (+ tests).
2. Controller endpoints + flag + healthz lights (+ tests); CI.
3. Client/backend helpers + chatbot auto-flash + tool (+ tests); CI.
4. Page buttons (+ marker tests); CI.
5. Deploy; live: user's books example + kill-switch demo; PR.

## Reviews

### Plan review

- **codex — BLOCKED → PASS.** Blockers: single-flight semantics between the
  two identify paths underspecified (wanted non-blocking ownership, no
  waiting anywhere) and the newest-archive live-meta refresh racing a
  concurrent scan (wanted a commit-time check). Both specified (the
  `_ident_busy` try-acquire design + commit-time re-check under `_pano_mu`)
  and PASSed round 2.
- **Opus — BLOCKED → PASS.** Round 1 (on a pre-revision snapshot) found the
  same two concurrency issues plus three more that stuck: the "every
  archived pano is 2:1" premise is FALSE for `_stitcher_pano` crops → the
  mapping is now the VIEWER'S OWN aspect-derived vspan (viewer-exact for
  every pano); the ±180-straddling strip must wrap pixel columns; and
  `photo_dir` isn't created at startup → the marker POST does makedirs and
  a missing dir reads as ON. Round 2: PASS, with one implementation note —
  do the worker's newest-check AND live-meta write in ONE `_pano_mu` hold,
  also consulting `_scan_active` (the scan's own publish steps run outside
  the lock).

### Code review

- **codex — BLOCKED → PASS.** Two real catches: (1) archived identifies were
  killed by the STALE `_scan_cancel` event (drive/e-stop set it; only the
  next scan clears it — after any joystick input the new feature would die
  instantly; my live test passed only because nothing had driven since the
  last scan) → `_run_ident_proc` takes `cancel_event`, the archived path
  passes None, test pins a stale-event run completing; (2) two `_ident_busy`
  wedge paths (mkdtemp outside the try; thread-spawn failure) → all
  post-acquire work inside try/finally + release-on-spawn-failure, tests.
  Plus the page baseline-read-before-POST UX fix.
- **Opus — BLOCKED → PASS.** THE catch: `RoverCtl` had no
  `get_stream_frame` — the auto-flash luma probe raised AttributeError into
  a broad except, so the headline feature would have silently never fired
  on hardware; the test fake masked it. Fixed with the passthrough + a
  surface-pinning test (every method the flash/identify path calls must
  exist on the real classes). N2 (ValueError in auto_flash_allowed) fixed;
  N1 (409-routing substring) accepted as a nit. Verified all six
  plan-review demands implemented; 125 tests green.
- **glm-5.1 — derailed → PASS.** First run hallucinated file reads and hit a
  permission auto-reject (no verdict); re-run against the diff text passed.

## Post-execution report

Implemented on `feature/identify-old-scans-autoflash`; live-validated.

- **The user's exact example works**: `/scan_identify` on the 2nd-last
  archived scan with focus "stack of books" found the **stack of books**
  (lon 68.6°) + a bookshelf among 14 objects — on a scan that predates the
  boxes feature (frames long gone; identification runs on the archived
  equirect in the viewer's own coordinates).
- **Buttons delivered**: 🔍 per scan card; 🔦 auto-flash toggle — flipped
  off live, survived a controller restart (marker file), back on.
- **Auto-flash**: unit-pinned end to end (dark→flash→exact restore;
  kill-switch-off → zero light commands; luma/state unknowable → skip;
  restore finally-guaranteed). True dark-room behavior awaits a dark room.
- **Chat tool**: `rover_identify_scan` (which=2 = "2nd last", focus hint,
  bounded poll) — session-busy for the duration, accepted.
- **Reviews**: plan gate 2×BLOCKED→PASS (viewer-exact vspan replacing a
  false 2:1 premise; single-flight + commit-race semantics); code gate
  codex BLOCKED→PASS + Opus BLOCKED→PASS (both caught
  broken-in-production-only bugs that fakes/my-live-luck had masked), glm
  PASS. CI green (~380 tests).
