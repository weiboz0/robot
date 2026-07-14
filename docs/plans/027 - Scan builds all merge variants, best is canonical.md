# 027 — Scan builds all merge variants; the best one is canonical

## Goal

The user's archived 3D view came out blurry: `build_panorama` silently fell
back to the feature-based `_stitcher_pano` when seam-cut failed on that scan,
and that inferior result became `panorama.jpg` AND the archive. Fix:

1. Every scan **saves the best merge automatically** — variants are built in
   the quality order the user confirmed (`seamcut` is the clear one):
   seamcut → projector (known-pose equirect) → stitcher. The first success is
   the canonical `panorama.jpg` (and hence the archive).
2. **Debug switching**: all successful variants are published as
   `pano_var_<name>.jpg`, so the existing viewer buttons (blue active
   highlight, seamcut-first default, missing → no button — plan 026) switch
   between the *current scan's* stitching versions. No page changes needed.

## Root cause (found during the live stage)

Reproducing with the same frameset on both machines: seamcut **always failed
on the Pi** and always worked on the Mac. Two stacked cv2-4.6.0 issues,
invisible behind `_seamcut_pano`'s broad except:

1. `ExposureCompensator_GAIN_BLOCKS.feed()` on 13 **full-res** warped frames
   tried to allocate **12.7 GB** → `cv2.error` → silent None. Fixed the way
   OpenCV's own pipeline works: feed at seam scale (0.25×), `apply()`
   upscales the gain maps; fallback chain GAIN_BLOCKS → scalar GAIN →
   uncompensated so memory can never silently kill seamcut again.
2. cv2 4.6's `GraphCutSeamFinder.find()` and `ExposureCompensator.apply()`
   return `None` (in-place semantics) where newer cv2 returns values — both
   call sites are now version-proof.

After the fix, all-three-variants build on the Pi in ~40 s (was: seamcut
never succeeded on the rover, ever — every scan silently shipped a fallback,
which is exactly the blurry result the user reported).

## Design

- `scene.build_pano_variants(frames, builders=None)` — new pure helper: runs
  the ordered builders (default: `seamcut→_seamcut_pano`,
  `projector→build_panorama(try_stitcher=False)`, `stitcher→_stitcher_pano`
  — the exact `$panotest` trio), swallowing per-builder exceptions; returns
  `(best, {name: jpeg})` where best = first success. `builders` injectable
  for tests.
- `scene.py` CLI: `build-pano <frames_dir> <out.jpg> [variants_dir]` — with
  the new optional arg it uses `build_pano_variants`, writes each success to
  `<variants_dir>/pano_var_<name>.jpg` and best to `out.jpg`; without it,
  behavior per today (kept for compat). Exit 0 if any variant succeeded.
- `App.pano_build_cmd` passes the temp dir as `variants_dir`;
  `_build_pano_subprocess`, on the success path only, `os.replace`s each
  `pano_var_*.jpg` from the temp dir into `photo_dir` (same fs, atomic; the
  temp dir already lives in photo_dir) and **deletes any stale
  `pano_var_<name>.jpg` for the known variant names that did NOT build this
  run** — so a debug button never shows a previous scan's result under this
  scan's name (dedicated test: a leftover `pano_var_stitcher.jpg` is removed
  when this run's stitcher fails but seamcut succeeds). Ordering, all inside
  the `TemporaryDirectory` context: canonical `os.replace` → variant
  replaces → stale deletes → archive → `return True`. Variant-publish/delete
  failures log and never fail the scan — canonical publish + archive
  behavior unchanged.
- `projector` deliberately calls `build_panorama(try_stitcher=False)`: at
  the sparse 60° scan spacing that runs the equirect known-pose path only
  (never the internal seamcut/stitcher fallback) — the variants call the
  method leaves directly.
- CLI dispatch: the `__main__` guard is currently `len(argv) == 4` — it
  becomes 4-or-5 with the usage string updated.
- Timeout contingency: if the live three-build run exceeds ~200 s on the Pi,
  raise `SCAN_BUILD_TIMEOUT_S`; the live stage records the actual timing.
- Runtime: three builds ≈ 60–90 s on the Pi (was ~30 s); the existing 300 s
  killpg timeout and thread caps hold. Cancel semantics unchanged (the child
  dies as one process group).

## Deliverables

- `scene.py`: `build_pano_variants` + CLI arg.
- `rovercontrold.py`: cmd + variant-move in `_build_pano_subprocess`.
- Tests: `build_pano_variants` preference order with injected builders
  (best = first success; later failure ignored; all-fail → (None, {}));
  CLI with variants dir (out == seamcut bytes when seamcut succeeds, behind
  the cv2 skip; pano_var files written; 3-arg form still works); controller:
  variants land in photo_dir on success, **stale variant for a failed
  builder is deleted**, variant-move failure doesn't fail the scan, cmd pin
  updated (argv gains the variants dir).

## Testing

CI (fakes + cv2-gated e2e). Live: run a scan; confirm `/pano_variant/seamcut`
etc. return this scan's builds, the viewer buttons switch between them, and
`panorama.jpg`/the archive equal the seamcut output.

## Risks

- *Longer stitch phase*: ~2–3× CPU time in the same hardened subprocess;
  bounded by the unchanged timeout; single-flight prevents overlap.
- *Old variants lingering when a builder fails*: a failed variant leaves the
  previous scan's file in place; the viewer would show a stale version under
  that button. Mitigation: on the success path, delete stale `pano_var_*`
  files for variants that did NOT build this time.

## Stages

1. scene helper + CLI + tests.
2. Controller pass-through + tests; CI.
3. Deploy branch, live scan, verify buttons + archive quality.
4. Review gate (Opus + glm; codex if its CLI responds), PR.

## Reviews

### Plan review

- **codex** — **PASS** (CLI responsive again); one note, folded: stale-variant
  deletion promoted from Risks to an explicit Design step with a test.
- **Opus** — **BLOCKED → PASS.** Blocker was the same stale-deletion item
  (already being folded when its round-1 landed); round-2 verified the
  explicit ordering (canonical replace → variant replaces → stale deletes →
  archive, inside the TemporaryDirectory context), the dedicated test, the
  timeout contingency (~200 s trigger, live timing recorded), the CLI
  `len(argv)` 4-or-5 guard, and the projector-equirect-path note.

### Code review

- **codex** — **BLOCKED → PASS.** Real finding: the compensator fallback
  could leave `imgs_w` part-compensated if `apply()` failed mid-loop (the
  scalar-GAIN retry would double-compensate a subset). Fixed: each attempt
  compensates copies and commits atomically only after every image applies;
  re-verified PASS.
- **Opus** — **PASS.** Empirically verified the seam-scale feed/full-res
  apply on cv2 4.12 (gain maps upscale correctly; both `is not None`
  version-proof branches behave right on 4.6 and 4.12 semantics), the
  find()-returns-None handling, the publish/stale-delete ordering and lambda
  binding, CLI dispatch, and byte-identity of canonical == best variant.
  Action item (done): commit the then-uncommitted compensator fix so the
  merged commit contains it.
- **glm-5.1** — **PASS.** Independently found the same fallback
  partial-state bug (its NB-1 = codex's blocker, already fixed) plus three
  hardenings, all applied: `MemoryError` added to the fallback catch, a test
  pinning `PANO_VARIANT_NAMES` == `scene.VARIANT_BUILDERS` order, and
  per-variant write guards in the CLI so a variant write can never cost the
  canonical result.

## Post-execution report

Implemented on `feature/scan-variant-quality` (stacked on the pose branch).

- **Delivered as planned** plus the root-cause fix the live stage surfaced:
  seamcut had NEVER succeeded on the Pi — `ExposureCompensator_GAIN_BLOCKS`
  fed full-res frames tried to allocate 12.7 GB (silent `cv2.error` → None),
  and behind that hid a second breaker (cv2 4.6's in-place `None` returns
  from `comp.apply`/`find`). Both fixed and version-proofed; the compensator
  now feeds at seam scale with a GAIN_BLOCKS → GAIN → uncompensated chain
  committing copies atomically.
- **Live validation**: all-variant build on the Pi ≈ 40 s (65 s scan total —
  well inside the 300 s timeout, no bump needed); `canonical == seamcut`
  byte-identical; projector published as a debug button; stitcher failed →
  no button; stale variants deleted; builder reasons now logged.
- **Reviews**: plan gate codex PASS + Opus BLOCKED→PASS; code gate all three
  reviewers (codex BLOCKED→PASS on a real fallback bug; Opus PASS; glm PASS
  with three hardenings applied). CI green (282 tests).
- **Deferred**: none. The user should re-run a scan and confirm the archived
  3D view is now the sharp seamcut result.
