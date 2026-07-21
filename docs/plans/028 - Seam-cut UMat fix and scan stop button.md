# 028 — Seam-cut actually cuts on the Pi (UMat masks) + a scan stop button

## Goal

1. **Fix the blurry seamcut** (regression the user hit after scans moved
   on-Pi): on cv2 4.6, `GraphCutSeamFinder.find()` returns `None` AND does
   not write back into numpy masks — plan 027's `seams = small_masks`
   fallback therefore blended full uncut overlaps → the double-image blur.
   (The "good" seamcuts of earlier sessions were all built on the Mac's newer
   cv2 by the chatbot; on-Pi seamcut never cut.)
2. **Stop button**: a ⏹ control on the web page, visible while a scan is
   running, that aborts the scan and discards the result.

## Discovery (probed on both machines)

| find() called with | cv2 4.6 (Pi) | cv2 5.0 (Mac) |
|---|---|---|
| numpy masks | returns None, masks NOT mutated | returns cut masks |
| UMat masks | returns None, **UMats mutated in place** | returns cut masks (UMats also mutated) |

## Design

- `_seamcut_pano`: wrap the seam masks in `cv2.UMat` before `find()`; use the
  return value when present, else the (mutated) UMat list; the existing
  `.get()` normalization already handles UMat elements. **Defensive guard**:
  if the found seams are pixel-identical to the input masks (no cut
  happened — whatever the cause), return None instead of shipping a blurry
  uncut blend; the canonical then falls to the next variant, and the always-
  logged builder line says so.
- **`POST /scan_cancel`** (controller): sets the existing `_scan_cancel`
  event — the exact machinery e-stop/drive-input use (sweep aborts before
  the next gimbal command, stitcher process group killed, result discarded,
  state → `failed`). Returns 200 `{"ok":true}`; 409 if no scan is running.
  Serial-gate not required (it touches no hardware) but harmless — ungated.
- **Page**: the pano-status indicator gains a small ⏹ stop button rendered
  only while `pano_status` is `scanning`/`stitching`; click → `/scan_cancel`.

## Deliverables

- `scene.py`: UMat seam finding + identical-masks guard.
- `rovercontrold.py`: `/scan_cancel` endpoint; `App.cancel_scan()` helper
  (event set only if a scan is active; returns whether one was).
- `rovercontrold_page.py`: ⏹ button in the pano status area.
- Tests: seam guard (identical masks → None) via a stub finder; UMat path
  covered by the cv2-gated e2e (synthetic frames must produce CUT masks —
  assert the builder log line reports seamcut success on both cv2s);
  `/scan_cancel` 200-aborts-a-running-scan (state failed, builder killed /
  never publishes) and 409 when idle; page markers (stop button id, shown
  only during active states).

## Testing

CI + live on the Pi: build the same frameset that blurred; verify the seam
masks now cut (sharpness: Laplacian variance of the Pi build within ~15% of
the Mac build of the same frames); run a scan and stop it mid-sweep from the
web page.

## Risks

- *UMat overhead*: seam finding at 0.25 scale — negligible.
- *Guard false-positive*: theoretically a perfect no-overlap layout needs no
  cuts; with the 60° ring overlap that cannot happen — and failing to the
  projector variant is strictly better than shipping blur.

## Stages

1. scene fix + guard + tests.
2. /scan_cancel + page button + tests; CI.
3. Deploy, live blur comparison + live stop test.
4. Review gate, PR.

## Reviews

### Plan review

- **codex** — **PASS.** Notes folded: the guard must compare against deep
  COPIES of the pre-find masks (UMat aliasing would make it vacuous);
  normalize returned-vs-mutated through one path; `cancel_scan` holds the
  scan-state lock for check+set; late-publish already prevented by the
  post-exit cancel checks.
- **Opus** — **PASS.** Notes folded: keep the originals as numpy while
  wrapping copies in UMat; per-mask comparison with "ALL identical" as the
  no-cut signal; `cancel_scan` is atomic under `_pano_mu` and does NOT set
  the event when idle (+ test); the ⏹ button renders INSIDE the polled
  `panostat` innerHTML with an inline onclick (the 2 s poll rewrites that
  element — a sibling button would flicker and lose listeners).

### Code review

- **codex** — **BLOCKED → PASS.** Real race: `/scan_cancel` could return 200
  after the panorama had already published (cancel landing between the
  builder's pre-publish check and `_finish_scan`). Fixed with a
  `_scan_published` point-of-no-return flag: `_mark_published()` and
  `cancel_scan()` serialize on `_pano_mu`, whichever wins is consistent —
  publish-first → cancel 409s; cancel-first → result discarded. Both
  orderings pinned by tests. Re-verified PASS.
- **Opus** — **PASS** (initial review) — empirically confirmed the UMat copy
  independence, the ALL-identical guard semantics, no new lock nesting
  (Movement hooks still bind the bare event-set, never `cancel_scan`), page
  innerHTML syntax, and that all plan-review refinements were implemented.
  Race-fix delta re-verify: **PASS** — published-flag handshake atomic and
  lock-clean, both orderings pinned (91/91 tests). Residual notes: a
  Movement-hook cancel (e-stop/drive) after the publish point still flips
  the display state to "failed" though the file published — display-only
  and intentional (those hooks bypass the published guard on purpose); the
  409 body already names both refusal reasons.
- **glm-5.1** — **PASS.** Verified UMat non-aliasing empirically, the
  fail-safe guard directions, lock cleanliness, and the inline-onclick
  pattern. One test gap (guard's trip-to-None path unit-tested only via the
  happy path) — closed post-review with a stub-finder test simulating the
  exact cv2-4.6 regression.

## Post-execution report

Implemented on `feature/seamcut-umat-and-stop` (stacked on 027's branch).

- **The blur is fixed and measured**: with UMat masks the Pi's seamcut is
  sharpness-identical to the Mac's (Laplacian variance 141.1 vs 141.2,
  ratio 1.00 on the same frameset; live scan canonical measured 140.6).
  The prior "successful" Pi seamcuts were uncut blends — cv2 4.6 discards
  numpy-mask mutations from `find()` (probed, table in Discovery). The
  no-cut guard means this failure class can never silently ship again.
- **Stop button delivered**: ⏹ appears during scanning/stitching, aborts the
  sweep/stitcher and discards the result (live: archive count unchanged
  after a mid-scan stop; idle press → 409). The cancel-vs-publish race codex
  caught is closed with an atomic point-of-no-return flag.
- **Reviews**: plan gate codex PASS + Opus PASS (refinements folded in
  before implementation); code gate codex BLOCKED→PASS (the publish race),
  Opus PASS ×2 (initial + race-delta), glm PASS (guard-trip test gap closed
  post-review). CI green (91 controller/scene-side tests in the touched
  suites; full run green).
- **Deferred**: none.
