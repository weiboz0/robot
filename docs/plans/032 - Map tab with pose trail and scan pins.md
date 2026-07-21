# Plan 032 — Map tab: pose trail + pose-stamped scans

## Goal

The rover already dead-reckons x/y/heading (plan 026) and archives 3D scans with
object meta (plans 029/031), but nothing connects them: the pose is a number in
a badge, and scans have no *place*. This plan adds:

1. **Pose-stamped scans** — every archived scan's meta sidecar records the pose
   where it was taken.
2. **Map tab** — a 2D canvas in the web UI showing the driven trail, the rover
   (with heading arrow), and a clickable pin for every pose-stamped scan; a pin
   click jumps to that scan in the 3D views tab.
3. **Server-side trail buffer** — the trail survives page reloads (not
   controller restarts; that's fine, pose itself doesn't either).

This is the foundation plan: 033 (world-coordinate object memory) builds on the
pose stamp added here.

## Design

### Trail buffer (rovercontrold.py, `Pose`)

- `Pose` grows a `collections.deque(maxlen=TRAIL_MAX)` of `(x, y)` points,
  guarded by the existing `Pose._mu` (no new lock-order edges; `Pose._mu` is a
  leaf lock touched only inside `Pose` methods).
- The trail **always contains the origin**: `(0, 0)` is seeded in `__init__`
  and again by `reset()` — a stationary or freshly reset rover shows a dot at
  the origin, never an empty map (codex suggestion; tests pin this).
- `update()` appends a point when the position has moved ≥ `TRAIL_MIN_STEP_M`
  (0.05 m) from the **last recorded** point — idle jitter records nothing.
- `reset()` clears the trail (a pose reset re-origins the world; the old trail
  is in the old frame and would be wrong) and re-seeds `(0, 0)`.
- `trail_snapshot()` returns `list(self._trail)` under the lock.
- Constants: `TRAIL_MAX = 2000` (≈ hard bound 32 KB JSON; at 5 cm spacing that
  is 100 m of driving), `TRAIL_MIN_STEP_M = 0.05`.

### `/pose_trail` endpoint (GET)

Returns `{"trail": [[x, y], ...], "pose": <same dict as /pose>}` in one fetch
so the map tick costs a single request. Registered next to `/pose`.

### Pose stamp in scan meta

Principle (plan-review revision): **the pose travels with the sidecar file,
keyed by scan name — never through a singleton attribute** — so a back-to-back
scan can't mis-attach one scan's pose to another's meta.

- `self._scan_pose = None` is initialized in `App.__init__` (defensive: no
  read-before-first-scan surprises). `start_scan()` captures
  `self.pose.snapshot()` into `self._scan_pose` under
  `_pano_mu`. This is safe to read anywhere *while the scan slot is held*
  (`_scan_active` blocks a second scan from overwriting it); it is **never**
  read after the slot is released. The rover's wheels don't move during a scan
  (gimbal-only sweep), so the start pose is *the* pose of the whole scan.
- The stamp is `{"x": .., "y": .., "heading": ..}` (drop `battery_v`/`fresh`).
- **Sidecar written at archive time** (inside `_build_pano_subprocess`, right
  after a successful `archive_scan`, i.e. still inside the scan slot): write
  `scans/<archived>.meta.json` = `{"made": <iso-now>, "objects": [],
  "pose": <self._scan_pose>}` via temp-file + `os.replace`. If identify later
  fails, this minimal sidecar remains — the map still gets its pin.
- **`_identify_frames` publish path changes** (codex catch — the existing
  `os.link(live_meta, sidecar)` would now hit `FileExistsError` and strand the
  empty meta forever, freezing the `made` stamp the UI/chatbot poll on):
  1. `_run_scan` reads `archived = self._last_archived` (under `_pano_mu`)
     **after the builder returns but before `_finish_scan` releases the scan
     slot**, and passes it as a parameter: `_identify_frames(frames, archived)`
     — no post-release singleton read (codex catch #2).
  2. After identify produces `src_meta`, read the pose **from the sidecar file
     for `archived`** and inject it into the identify JSON (rewriting `made`
     as identify already does), then commit **under `_pano_mu` with the same
     "still newest and no scan mid-flight" guard `_identify_archived` uses**
     (code-review round-1 catch — a straggling identify from scan A must
     never describe scan B's newer live panorama): if newest,
     `os.replace(src_meta, live_meta)` + unlink-then-link the sidecar;
     otherwise sidecar-only. With `archived is None` (archive failed) the
     live publish happens only while `_last_archived` is still None.
- `_identify_archived()` (re-identify of an old scan, plan 031) currently
  replaces the sidecar wholesale: it must now **carry over** the old sidecar's
  `pose` into the new meta (read old pose from the sidecar before the commit,
  inject into the new dict). The carry-over read tolerates a missing,
  unreadable, or corrupt-JSON sidecar (any failure → no pose key, never an
  aborted identify). Old scans predating this plan have no pose —
  carry-over of a missing key is a no-op, and the map shows no pin for them.
- Chatbot/UI compat: `made` advances on every identify (minimal sidecar's
  stamp is archive time; identify rewrites it); `objects: []` is already a
  legal zero-boxes state.

### Map tab (rovercontrold_page.py)

- New tab button + panel: `Map` added to `TAB_BTNS` / `TAB_PANELS` (`gtab`
  machinery as-is), panel holds a `<canvas id="mapcanvas">` sized to its card.
- JS `mapTick()` joins the existing 2-s interval **only while the Map tab is
  visible** (skip work otherwise, same pattern as other ticks):
  - fetch `/pose_trail`; fetch `/scans` + each scan's `/scan_meta/<name>`
    (metas cached client-side by name, **including negative entries** — a
    legacy scan whose meta 404s is cached as "no pin" and not refetched every
    tick; entries for deleted scans are dropped, and null entries are retried
    when the user switches to the Map tab, so a later 🔍 identify gets picked
    up on the next tab visit).
  - `drawMap()`: compute bounds over trail ∪ pins ∪ rover (min span 2 m),
    uniform scale + center, then draw grid (1 m), trail polyline, scan pins
    (numbered circles, newest first = 1), rover triangle rotated to heading.
  - store each pin's screen position; canvas `click` handler hit-tests pins
    (≤ 12 px) → `showTab('scans')` (the 3D-views tab key) +
    `pano3d('/scans/' + name)` — the same call the 3D-views cards make.
- Y axis: world +y is CCW-left of +x; canvas y grows downward → draw with
  `cy = H - (y - miny) * s` so the map matches a top-down view.

## Deliverables

- `rovercontrold.py`: trail buffer in `Pose`, `/pose_trail`, `_scan_pose`
  capture, sidecar stamp + merge, `_identify_archived` pose carry-over.
- `rovercontrold_page.py`: Map tab, `mapTick`/`drawMap`, pin click-through.
- `docs/reference/controller-commands.md`: `/pose_trail` + meta `pose` key.
- Tests (below). No new files besides this plan.

## Testing (no hardware, fakes only — no wheel/gimbal motion)

- `test_pose.py`: trail accumulates on movement, min-step suppresses jitter,
  maxlen bounds it, `reset()` clears, `trail_snapshot` shape.
- `test_controller_http.py`: `/pose_trail` returns trail+pose JSON; page
  markers for the Map tab (button, canvas id, `mapTick`, `drawMap`, pin-click
  wiring).
- `test_controller_scan.py`: after a (fake-builder) scan completes, the
  archived sidecar contains the pose captured at scan start; identify-failure
  path still yields a minimal sidecar with pose; `_identify_archived` on a
  pose-bearing sidecar preserves the pose; on a pose-less legacy sidecar it
  stays absent. Plus the two race/compat tests codex demanded: (a) scan-time
  identify over a pre-existing minimal sidecar must replace it (link succeeds,
  `made` advances, pose survives); (b) back-to-back scans — scan B starting
  while scan A's identify is still publishing must not cause A's meta to
  attach to B's archived name or pose (archived name passed as a local).

## Risks

- **Lock order**: trail lives under `Pose._mu`, which stays a true leaf (its
  methods never call back into `App`). `start_scan` introduces one new
  **one-way** edge `_pano_mu → Pose._mu` (snapshot under the scan-slot claim);
  no reverse path exists anywhere, so no cycle (all three code reviewers
  verified this).
- **Meta shape drift**: chatbot polls `made`; minimal sidecar keeps that
  contract. Identify rewriting `made` is unchanged behavior.
- **Map perf**: trail ≤ 2000 points, redraw ≤ every 2 s, only when visible —
  negligible.
- **Pose accuracy**: signs/skid factor still await the user's calibration
  drive; the map is only as good as odometry. Not a correctness risk here.

## Stages

1. Trail buffer + `/pose_trail` + pose tests.
2. Scan-meta pose stamp (+ minimal sidecar, identify merge, archived
   carry-over) + scan tests.
3. Map tab UI + page-marker tests.
4. Docs, CI, review gate, PR.

## Reviews

### Plan review

- **codex (GPT-5.5)** — round 1: BLOCKED. (B1) minimal sidecar at archive time
  collides with `_identify_frames`' bare `os.link` → `FileExistsError`, empty
  meta stranded and `made` frozen; (B2) `_scan_pose` singleton can be
  overwritten by a back-to-back scan before the prior identify reads it.
  Non-blocking: seed the trail with the origin; negative-cache 404 metas in
  the map tick; add the two race/compat tests. **Resolution**: publish path
  now unlink-then-links (mirroring `_identify_archived`); archived name is
  read before the scan slot releases and passed as a parameter; pose is read
  back from the sidecar file keyed by name (no singleton read after release);
  all non-blocking items adopted verbatim.
- **Opus** — round 1: BLOCKED on the same `os.link` collision; independently
  recommended the pose-from-sidecar merge source; non-blocking: test the
  identify-*success* overwrite path, tab key is `'scans'` not `'3d'`,
  initialize `_scan_pose` in `__init__`, tolerate corrupt sidecar JSON in
  carry-over. Affirmed: no drive-path contact, meta-shape compat, and the
  stationary-during-scan pose assumption. **Resolution**: all folded into the
  design/testing sections.
- **Re-verification** — codex: PASS (B1/B2 + all three suggestions confirmed
  resolved, no new findings). Opus: PASS (every finding line-checked against
  the code; one NEW NON-BLOCKING note: the unlink-then-link publish shares
  `_identify_archived`'s pre-existing best-effort window where a racing scan
  B's live-meta delete can ENOENT the link — accepted as pre-existing
  behavior, "every failure just logs").

### Code review

- **glm-5.1** — PASS. Verified transform math, race fixes, meta compat, pin
  hit-test. Non-blocking: negative-cache never retried (fixed: nulls dropped
  on Map-tab revisit), pin numbering assumes newest-first (it is),
  `_write_min_sidecar` failure untested (test added), canvas realloc every
  tick (accepted, cosmetic), lock-edge wording (plan updated).
- **Opus** — PASS. Line-verified every plan-gate demand and its test; found
  the `Pose._mu` leaf claim, the non-reentrant `_write_min_sidecar` call
  path, and the drive-path non-contact all safe. Non-blocking notes: CSS/
  buffer pixel hit-test coincidence (1:1 here), grid letterbox (cosmetic),
  optimistic cache comment (fixed with the revisit invalidation).
- **codex** — round 1 BLOCKED: scan-time identify still published the LIVE
  `panorama.meta.json` unconditionally — scan A's straggler could describe
  scan B's panorama (pre-existing race, but the fix pattern existed in
  `_identify_archived`). **Resolution**: live commit now happens under
  `_pano_mu` with the newest+not-active guard (sidecar-only otherwise;
  `_last_archived is None` rule for archive-failed scans); three new tests
  pin the guard. Also flagged the negative-cache gap (fixed as above) and
  the lock-edge wording (plan updated). **Re-verification: PASS** — blocker
  confirmed resolved, no deadlock via `list_scans` under `_pano_mu`, no new
  findings.

## Post-execution report

Implemented as revised: trail deque in `Pose` (origin-seeded, 5 cm min step,
2000-point cap, cleared on reset), `GET /pose_trail`, pose captured in
`start_scan` under the scan slot, minimal sidecar written inside
`archive_scan` (temp+replace, failure costs only the pin), identify publish
rewritten — pose injected from the sidecar file keyed by the archived name
(passed as a parameter, bound before the slot releases), live commit under
`_pano_mu` with the newest+not-active guard, unlink-then-link for the
sidecar; `_identify_archived` carries the pose over tolerantly. Map tab:
canvas with 1 m grid, origin cross, trail polyline, numbered amber pins
(newest = 1, click → opens that scan in the 3D viewer), heading triangle
(grey when telemetry stale), tick only while visible, null metas retried on
tab revisit.

Deviations from the plan: the minimal sidecar write moved from
`_build_pano_subprocess` into `archive_scan` (same scan-slot timing, but
fake-builder tests can exercise it); the live-commit guard (codex code-review
catch) was added beyond the planned scope — it also fixes a pre-existing
straggler race. Deferred: canvas realloc per tick and grid letterbox
(cosmetic, noted by reviewers as accepted).

Outcomes: plan gate codex BLOCKED→PASS + Opus BLOCKED→PASS; code gate glm
PASS + Opus PASS + codex BLOCKED→PASS. CI green (389 tests). No hardware
motion during dev/test.
