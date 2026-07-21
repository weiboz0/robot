# 026 — Pose tracking overlay and scan history

## Goal

1. Track the rover's **position in self-set coordinates** (origin 0,0 wherever
   it was booted/reset) and its **heading**, plus the camera's pan/tilt — and
   show them in a badge at the **top-right of the web page**, with a reset
   button.
2. Keep **previous 3D scans**: every successful scan is archived, and the
   gallery gets **two tabs — Photos | Scans** — with the scans tab opening any
   old panorama in the 3D viewer.

## Discovery (verified on hardware + firmware source)

- The ESP32 runs the **ugv_base_ros** firmware. Enabling continuous feedback
  (`{"T":131,"cmd":1}`) streams `{"T":1001, L,R, ax..az, gx..gz, mx..mz,
  odl, odr, v, pan, tilt}` at ~15–20 Hz (probed live).
- `odl`/`odr` are cumulative wheel odometry in **centimeters**
  (`ugv_advance.h: odl_cm = en_odom_l * 100`, from encoder pulses × π×0.08 m).
- `gx/gy/gz` are **raw ICM-20948 DMP gyro LSB** (FSR ±2000 dps → 16.4
  LSB per °/s). The fused-yaw query (`{"T":126}` → `T:1002`) exists but
  returns all zeros — fusion is disabled in this firmware build (probed live).
- **Post-implementation measurement**: the streamed `gz` is unusable — at
  rest it reads mean 10,558, stdev 9,194, range 4→20,495 over 6 s (~5 Hz
  actual feedback rate). That is not sensor noise; the firmware's DMP-FIFO
  parse is broken (its own history says "imu mag bug, ignored for now").
  **Heading therefore comes from differential wheel odometry**
  (`Δodr − Δodl` over the effective track width, firmware constant 0.172 m ×
  a calibratable skid factor) — encoder-based like position, monotonic,
  sign-stable, no bias machinery at all. The gyro/bias design below is
  RETIRED; kept for the record.
- `pan`/`tilt` are the **servo-reported** gimbal angles; `v` = battery × 100.

## Design

### Telemetry (serial read loop)

The controller currently only writes to serial. Add a **reader thread**:
`init_link` also sends `{"T":131,"cmd":1}` (feedback ON — sent AFTER the
existing echo-off `{"T":143,"cmd":0}`, so the reader never sees echoed
commands; order pinned by test); the thread reads lines (termios port is
already O_RDWR), parses JSON, ignores anything that isn't `T:1001`, and feeds
`Pose.update(...)` + a small Telemetry snapshot (battery, servo pan/tilt,
last-seen time). Safety rules:
- **Blocking discipline (reviewer must-fix)**: the current termios setup never
  sets `VMIN`/`VTIME` (c_cc left as whatever tcgetattr returned — undefined).
  TTYLink now sets `VMIN=0, VTIME=1` explicitly, and the reader loops on
  `select([fd], timeout)` + `stop_event` — a **daemon** thread that can never
  block indefinitely across `close_link()` at shutdown; it exits promptly on
  stop/EOF/OSError and marks telemetry stale.
- The reader NEVER touches Movement/CameraAim and takes no locks shared with
  the drive path (its own mutex around the pose snapshot only).
- Partial lines are buffered with a hard cap (flood/garbage → buffer reset,
  not growth); junk/non-JSON lines are discarded. Read errors are logged with
  backoff; a dead or blocked reader cannot block writes (full-duplex tty,
  separate syscalls).
- Serial writes are unchanged (same lock, same encoding pins).

### Pose (pure, unit-testable dead reckoning)

- State: `x, y` (m), `heading` (deg, CCW+, 0 = facing +X at reset).
- Per sample: `fwd = ((Δodl + Δodr)/2) / 100` m along current heading →
  `x += fwd·cos(h)`, `y += fwd·sin(h)`; heading `h += (gz − bias)/16.4 · Δt`
  (Δt from arrival timestamps, clamped to [0, 0.25] s against gaps).
- **Gyro bias auto-cal — conservative (reviewer must-fix)**: bias updates ONLY
  when (a) odometry is unchanged this sample (both wheel deltas zero — a
  turn-in-place has opposite nonzero deltas and does NOT calibrate) AND
  (b) `|gz − bias|` is inside a small window (≈ 3 °/s) — so rotating the robot
  by hand while the wheels are idle is integrated as heading, not calibrated
  away. Initial bias seeds from the median of the first ~10 stationary
  samples before any integration starts.
- **Counter-reset / reconnect re-baseline (reviewer must-fix)**: odl/odr are
  cumulative since ESP32 boot — a mid-session ESP32 reboot or serial
  reconnect snaps them toward 0. Any sample where **|Δ| of either wheel
  exceeds a symmetric plausibility bound (50 cm in one ~50 ms sample)**
  re-baselines instead of integrating (pose holds, no phantom jump). The
  bound is symmetric on purpose: odl/odr are signed, and **reverse driving
  produces legitimate small negative deltas that must integrate as negative
  forward motion** (round-2 reviewer catch — a "negative beyond jitter"
  trigger would freeze the pose while backing up). A real counter reset
  jumps by the full accumulated distance, far beyond the bound. The pose
  also re-baselines whenever the link is (re)published.
- First sample only sets baselines.
- `reset()` re-zeroes x, y, heading (the "coordinates are self-set" ask).
- Honest accuracy: dead reckoning on a skid-steer — cm-to-decimeter over a
  room, drifts over time; that's what reset is for. Two sign/scale constants
  (`ODOM_SIGN`, `GYRO_SIGN`) default to +1 and are verified in the live test
  (one short commanded drive + turn), since encoder/gyro orientation can't be
  known until the wheels actually move.

### Endpoints

- `GET /pose` → `{"x","y","heading","pan","tilt","battery_v","fresh"}`.
  **Ungated** (no `_require_serial`) — when serial is down it returns 200 with
  the last-known pose and `fresh:false` (the badge greys out); pan/tilt from
  CameraAim (commanded aim; the servo-reported angles feed telemetry but the
  badge shows the commanded value — it's the intent, and always available).
- `POST /pose_reset` → zeroes the origin, `{"ok":true}` (underscore naming =
  existing house style, e.g. `/delete_photo/`).
- `GET /scans` → `{"scans":[…]}` newest-first; `GET /scans/<name>` serves the
  file (image/jpeg, strict basename — served only from `photos/scans/`, no
  path joins that can escape); `POST /delete_scan/<name>`. Read endpoints
  ungated. Name regex allows the collision suffix:
  `scan_\d{8}_\d{6}(_\d+)?\.jpg`.

### Scan history

After the atomic publish succeeds (the `return True` path only, after the
cancel re-check), the panorama is archived as
`photos/scans/scan_YYYYmmdd_HHMMSS[_N].jpg` (hard-link from the published
file, collision-safe `_N` suffix, `os.makedirs(scans_dir, exist_ok=True)`).
An archive failure logs and does NOT fail the scan — `/panorama` "latest"
stays authoritative. Nothing existing changes.

### Web page (rovercontrold_page.py)

- **Top-right pose badge** (fixed position): `(x, y) m · heading° · cam
  pan/tilt · battery V`, greyed with "no telemetry" when `fresh` is false;
  small ⌂ reset control (POST /pose_reset). Polls /pose at 2 Hz.
- **Gallery tabs / switcher** (user ask): a "Photos ⇄ 3D views" toggle sits
  next to the existing "Clear all photos" button. Photos tab = existing
  gallery, unchanged. 3D-views tab = grid of archived scans; click opens the
  3D viewer pointed at `/scans/<name>`; per-scan delete; a **"Clear all 3D
  views"** button mirroring `clearAll()` (client loop over `/delete_scan/`,
  same confirm style). The tab-appropriate clear button is shown per tab.
- **Viewer defaults to the clearest merge** (user ask): preference order
  **seamcut variant → live `/panorama` → projector → stitcher** — the first
  that exists (HEAD probe). Seam-cut is empirically the sharp one; the old
  averaging/projector variants stay reachable but never load first, and a
  missing variant is skipped instead of toasting. The **active variant's
  button is highlighted blue** and updates on every switch.
- **Viewer parameterization is more than a default swap** (reviewer
  must-fix): `pano3d()` hardcodes `/panorama` in three places (HEAD probe,
  the merge-variant buttons array, and the img src) — for an archived scan
  the variant buttons are **hidden** (variants only exist for the live pano;
  showing them would 404 into a misleading toast).

## Deliverables

- `rovercontrold.py`: TTYLink.read support + reader thread; `Pose` class;
  Telemetry snapshot; feedback-ON init; `/pose`, `/pose_reset`, `/scans*`,
  `/delete_scan/`; scan archiving.
- `rovercontrold_page.py`: pose badge + gallery tabs + parameterized viewer.
- Docs: controller-commands.md gains the new endpoints + badge note.
- Tests (no hardware): Pose math (straight line cm→m, **reverse drive
  integrates negative forward motion — no re-baseline freeze**, turn-in-place
  integrates heading and does NOT bias-calibrate, bias-cal at rest only
  inside the |gz−bias| window (hand-rotation while idle integrates), initial
  bias seeding, **counter reset / reconnect re-baselines without a pose
  jump**, implausible delta re-baselines, Δt clamp, reset, sign constants);
  feedback parser (good, junk, partial line split across reads, buffer cap
  under flood, non-1001); **reader thread exits promptly on stop_event/EOF**
  and marks stale; a telemetry flood does not delay serial writes (write path
  never blocks on reader state); HTTP: /pose 200 + `fresh:false` with serial
  down (NOT 503), /pose_reset, scans list/serve (content-type)/delete +
  traversal & suffix edge rejects, archive-failure does not fail the scan +
  /panorama stays latest, archive name matches the validation regex; page
  markers (badge ids, tab ids + switcher, clear-all-3D button, viewer param,
  **variant buttons hidden for archived scans**, variant preference order +
  active-button highlight); init_link sequence pin updated (echo-off BEFORE
  feedback-ON); TTYLink VMIN/VTIME pin.

## Testing

CI (all fakes) + live: deploy branch, verify feedback flows (pose fresh,
battery plausible), then a short commanded drive + 90° turn to verify the
sign constants and rough scale; scan → appears in Scans tab; reset zeroes the
badge. Wheel motion for the calibration drive happens in the live test with
the user aware (this plan's live step, small distances, cleared area).

## Risks

- *Sign/scale unknowns until wheels move*: isolated in two ±1 constants and
  one gyro-scale constant; live-verified, easily corrected.
- *Reader thread vs single-owner serial*: same process, full-duplex tty;
  reads never take the write lock. A flood of feedback lines is bounded by
  line-splitting with a max-buffer guard.
- *Enabling feedback changes boot init*: the historical init pin is updated
  deliberately (`cmd:1` instead of `cmd:0`); if a stale/echoing base spams,
  the parser ignores unknown lines — worst case is CPU noise at 20 Hz, and
  the OFF fallback is one constant.
- *Page size growth*: badge + tabs are small additive JS/CSS; page-marker
  tests keep the contract.

## Stages

1. Pose + parser + reader (unit-tested).
2. Endpoints + scan archiving (HTTP-tested).
3. Page: badge + tabs + viewer param (marker-tested).
4. Deploy to rover; live telemetry check; sign-calibration drive; scan-tab
   check.
5. Review gate, PR.

## Reviews

### Plan review

Round 1: **both reviewers BLOCKED.**
- codex: ESP32 odometry counter reset mid-session → phantom pose jump; bias
  cal too eager (hand-rotation while idle would calibrate away real heading);
  scan-name regex vs collision suffix conflict; /scans content-type/basename
  discipline; test gaps (flood, partial lines, reader exit, traversal edges).
- Opus: reader blocking mode undefined (TTYLink never sets VMIN/VTIME; a
  blocked read across close_link() at shutdown is a race) → select()+timeout
  daemon reader + explicit VMIN=0/VTIME=1; /pose must stay 200 (not 503) on
  serial-down; viewer parameterization understated — /panorama hardcoded in
  3 places and the merge-variant buttons must hide for archived scans;
  archive only on the return-True path and never fails the scan; echo-off
  before feedback-ON order.

All folded into Design/Deliverables above.

Round 2: **Opus BLOCKED once more** on a bug introduced by the round-1 fix —
"re-baseline on negative-beyond-jitter deltas" would freeze the pose during
legitimate reverse driving (odl/odr are signed). Fixed: the re-baseline
trigger is a symmetric |Δ| > 50 cm/sample bound only, with a reverse-drive
test. Round 3: **Opus PASS**. codex's re-verify was requested twice but its
CLI stalled indefinitely both times (same usage-limit behavior as earlier in
this project); Opus independently confirmed codex's two blockers were
resolved, and glm-5.1 joins the code-review gate as the third reviewer.

Mid-implementation amendment (user request, UI-only): viewer defaults to the
clearest merge variant (seamcut → live → projector → stitcher), active
variant button highlighted blue, Photos ⇄ 3D-views tab switcher next to the
clear-all button, and a "Clear all 3D views" button. Plus one
measurement-driven pivot recorded in Discovery: streamed gyro unusable →
heading from differential odometry (simpler than the reviewed design; the
bias machinery was deleted, not weakened).

### Code review

- **Opus** — **PASS**, no blocking findings; ran the full suite (274 tests
  green). Verified every plan-review must-fix is actually in the code
  (VMIN/VTIME on the cc array, select+stop_event daemon reader with no
  blocking across close_link, ungated /pose, hidden variant buttons for
  archived scans, symmetric re-baseline bound with signed reverse verified at
  49 cm/51 cm, archive-on-return-True-only, echo-off-before-feedback pin).
  Notes: two promised tests were missing (flood-vs-writes, VMIN/VTIME pin) —
  **both added post-review** (pty-based pin; interleaved flood/send with a
  100 ms worst-case bound); `rover.link()` technically touches Rover._mu (a
  brief pointer read, no I/O under it) — accepted as a wording deviation.
- **glm-5.1** — **PASS**. Independently verified the seven requirements and
  the JS quoting inside the raw string. Notes: VMIN/VTIME test gap (added, as
  above); servo-reported pan/tilt stored but unexposed (by design — the badge
  shows commanded aim); serial retry-on-drop is connect-time-only
  (pre-existing behavior, out of scope).
- **codex** — unavailable: its CLI stalled indefinitely on two invocations
  (as it did earlier in this project when hitting usage limits). The gate
  proceeded with the two available reviewers, both PASS.

## Post-execution report

Implemented on `feature/pose-and-scan-history` and validated live on the
rover from the branch.

- **Delivered**: serial telemetry reader (the controller's first serial
  read path), `Pose` dead reckoning, `/pose` + `/pose_reset` +
  `/scans*` endpoints, scan auto-archiving, the top-right pose badge, the
  Photos ⇄ 3D-views gallery tabs with per-tab clear-all, and the viewer's
  clearest-first default with blue active-variant highlight.
- **Biggest deviation (measurement-driven)**: the reviewed design integrated
  the gyro for heading; on hardware the streamed gz is firmware-broken (rest
  values 4→20,495, stdev ~9,200, and the feedback rate is ~5 Hz not 20).
  Heading pivoted to differential wheel odometry — simpler, encoder-solid,
  drift-free at rest (verified live: heading pinned at 0.0° at rest, where
  the gyro build wandered ±50°). Trade-off: hand-rotating the idle rover is
  invisible to it (reset covers this), and heading-in-turns needs the
  SKID_FACTOR calibrated against a real spin.
- **Live validation**: telemetry fresh at ~5 Hz, battery 12.16 V; scan →
  archived → listed in /scans → opens in the viewer, end to end.
- **Not validated**: ODOM_SIGN/HEADING_SIGN — the permission layer
  (correctly, per the standing motion-safety rule) refused the wheel-nudge
  calibration, so sign verification passes to the user's first joystick
  drive; each is a one-constant flip if reversed. SKID_FACTOR likewise
  awaits a measured 90°/360° spin.
- **Reviews**: plan gate 3 rounds (2× BLOCKED → PASS, incl. one bug caught in
  a round-1 fix: reverse-drive freeze); code gate Opus PASS + glm PASS with
  both flagged test gaps closed post-review; codex CLI stalled twice and was
  substituted per the earlier precedent. CI green (276 tests).
