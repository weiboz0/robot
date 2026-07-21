# 025 — Gamepad 3D scan button

## Goal

One gamepad button press on the controller triggers a full 3D scan: the gimbal
sweeps the room (the proven serpentine two-ring + ceiling path from `scene.py`),
the seam-cut equirect panorama is built **on the Pi**, and the result lands at
`photos/panorama.jpg` — exactly where the web page's 🌍 3D viewer and the
existing `pano_status` indicator already look. No chatbot needed.

## Design

- **New mapping action `scan`** — added to `CONTROL_KEYS`, default **button 7
  (Start on an Xbox pad)** — the lowest free index in the default map (0–6, 9,
  10 are taken). Rebindable via the mapping JSON or the calibration wizard
  (added to the optional-bindings prompts). Edge-triggered like
  `snapshot`/`center`.
- **`App.start_scan()`** — one scan at a time, guarded by the existing
  `_pano_mu`: while `pano_state` is `scanning`/`stitching`, another press is
  refused (returns False; the button press logs and does nothing). Runs
  `_run_scan()` on a daemon thread.
- **Single-flight is a private flag** (`_scan_active` under `_pano_mu`), NOT
  `pano_state` — `POST /pano_status` is externally writable and must not be
  able to defeat the interlock. `pano_state` stays display-only.
- **`App._run_scan()`**:
  1. `pano_state = "scanning"` → reuses **`scene.scan_frames()`** (import is
     cheap — cv2 is lazily imported inside build functions only) with a
     **cancel-gating adapter**: every `set_camera` and `get_stream_frame`
     checks the scan-cancel event FIRST and raises `ScanCancelled` if set — so
     after a cancel **no further gimbal command can ever be issued by the
     scan**, including the recenter in `scan_frames`' `finally` (the adapter
     raises there too; `scan_frames` swallows it — verified in code). The
     injected `sleep` also polls the event (50 ms granularity) so the abort is
     prompt. No `scene.scan_frames` signature change needed: the adapter *is*
     the client, and every gimbal command flows through it.
     `get_stream_frame → hub.latest_frame` (≤ 66 ms old at 15 fps, well inside
     the 1.4 s settle); `None` frame → scan fails cleanly.
  2. `pano_state = "stitching"` → frames written to a temp dir using the
     established `pan±ddd_t±dd.jpg` naming (parent drops its frame list), then
     the panorama is built in a **hardened subprocess**: `[sys.executable,
     scene.py, "build-pano", dir, out]` with `start_new_session=True` (own
     process group), `nice` 10, env `OMP_NUM_THREADS=1`,
     `OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1`,
     `VECLIB_MAXIMUM_THREADS=1`, and `cv2.setNumThreads(2)` inside the CLI —
     the build gets at most half the Pi's 4 cores, keeping serial/stream/
     watchdog threads responsive. 300 s timeout → `killpg`. Out temp file
     lives **inside `photo_dir`** so the final `os.replace` to `panorama.jpg`
     is atomic (same filesystem). Stderr tail logged.
  3. Success → `pano_state = "done"`; any failure/cancel → `"failed"`. The web
     page's indicator strings (`scanning`/`stitching`/`done`/`failed`) already
     exist — zero page changes.
- **Motion interlocks** (both reviewers' core demand):
  - `start_scan` is **refused while the wheels are moving** (`Movement`
    exposes `is_moving()`).
  - **Any nonzero drive command cancels a running scan** (operator keeps
    priority): a single `on_nonzero_drive` hook in `Movement._apply_drive`
    covers HTTP `/drive`, `/move_*` nudges, and the joystick in one place.
  - **E-stop cancels everything**: an `on_estop` hook in `Movement.do_estop`
    (gamepad estop, HTTP `/estop`, and SIGINT shutdown all funnel through it)
    sets the scan-cancel event AND kills the stitcher's process group. After
    e-stop the scan issues zero further gimbal commands — not even recenter.
  - Scan moves the **gimbal only** — wheels are never commanded (inherited
    from `scan_frames`; pinned by a test on the fake serial log).
- **HTTP `/scan`** (same method convention as `/snapshot`, gated on
  `_require_serial`): starts the same scan, `200 {"ok":true}`; `409` if one is
  already running or the wheels are moving. Gives the web command box and
  tests a trigger without a gamepad.
- Out of scope: LLM scene description/inventory (that stays a chatbot feature —
  the controller has no LLM), website button for scan (the command box can
  reach `/scan` already), scene.py duplicate-definition cleanup (separate
  branch).

## Deliverables

- `rovercontrold.py`: `scan` mapping key + default + validation + wizard
  prompt; `compute_joystick` edge; `joystick_loop` hook; `Movement.is_moving`
  + `on_nonzero_drive` + `on_estop` hooks; `App.start_scan` / `_run_scan` /
  cancel event / builder-kill; `/scan` endpoint.
- `scene.py`: `build-pano` CLI — `__main__` + filename parser (inverse of
  `save_scene`'s naming) + `cv2.setNumThreads(2)`, placed at the **true end of
  the file** (it has last-wins duplicate defs; cleanup is out of scope but the
  CLI must land after them).
- Tests (no hardware, fake serial/hub, builder injectable): mapping
  default/parse for `scan`; compute_joystick scan edge; single-flight (second
  press refused during BOTH `scanning` and `stitching`); `POST /pano_status`
  cannot defeat single-flight; start refused while wheels moving; nonzero
  drive mid-scan cancels; **e-stop mid-scan → zero further gimbal commands,
  including no recenter** (fake serial log pinned); e-stop during stitching
  kills the builder; builder timeout kills the process group; scan never
  emits a wheel command; no-frame failure; subprocess argv/env construction
  pin; HTTP `/scan` 200/409 + serial gate; CLI filename roundtrip; CLI
  corrupt-frame failure; CLI end-to-end behind the existing cv2 skip guard.
- Docs: `docs/reference/controller-commands.md` gains `/scan`.

## Testing

Unit + integration as above via `./ci-local.sh`. Live: deploy the branch to the
rover, press Start, watch the web indicator walk scanning → stitching → done
and the 3D viewer refresh; verify a second press mid-scan is refused and estop
aborts the sweep.

## Risks

- *Pano build load on the Pi* (4 cores, ~1–2 min at seam-cut scale): the
  hardened subprocess (nice 10, thread caps to ≤ 2 cores, own process group,
  timeout-kill) keeps the motor/serial/stream threads responsive; single-
  flight prevents pile-ups. Live check: drive-watchdog and e-stop latency
  observed during a real stitch on the Pi.
- *Joystick pan/tilt fighting the sweep*: deliberate choice — camera-stick
  wiggles are NOT blocked (harmless, one blurred frame at worst); wheel
  motion, by contrast, cancels the scan outright.
- *Missing scene.py/cv2 on some install*: import/build failures are caught,
  state → `failed`, controller keeps running.
- *Accidental button press*: starts gimbal-only motion, refused while driving,
  cancelled by any drive input or e-stop — worst case is a 20 s camera sweep
  from a stationary rover.

## Stages

1. scene.py `build-pano` CLI + tests.
2. Controller: mapping + joystick + App scan + `/scan` + tests.
3. CI, deploy branch to rover, live button test.
4. Review gate, PR.

## Reviews

### Plan review

Round 1: **both reviewers BLOCKED** the initial plan on overlapping safety
gaps — codex: no wheel-motion interlock, e-stop didn't kill the stitcher,
nice-only isolation, single-flight defeatable via `POST /pano_status`;
Opus: cancel was racy (one more gimbal pose + the recenter could fire after
e-stop) and the claimed cancel hook didn't exist in `scan_frames`.

Resolution (all folded into Design above): cancel-gating adapter (every
gimbal/frame call checks the event first and raises; the `finally` recenter
is provably swallowed), motion interlocks via `is_moving` + `on_nonzero_drive`
+ `on_estop` hooks, hardened subprocess (own process group, nice, thread caps,
timeout-killpg, atomic replace), private `_scan_active` single-flight.

Round 2: **codex PASS** (watchpoint: `is_moving` must reflect active nudges),
**Opus PASS** (notes: same nudge point; hooks under `Movement._mu` must only
set the event — killpg happens on the scan thread; `on_nonzero_drive` only
for accepted commands; residual microsecond aim-vs-estop race accepted as
identical to the existing joystick-vs-estop race).

### Code review

- **Opus** — **PASS**, no blocking findings. Traced all six safety invariants
  to code+tests (zero gimbal after cancel incl. recenter, nudge-aware
  `is_moving`, private single-flight, event-only hooks, no wheel commands, no
  cv2 in-process); confirmed no `_pano_mu`↔`Movement._mu` nesting and killpg
  correctness (PGID==PID via start_new_session). Three trivial notes (local
  `import re` style, cancel-vs-failure both map to `failed` — per plan, dead
  test variable).
- **codex** — BLOCKED twice, then **PASS**. Round 1 blockers (both real, both
  fixed + tested): a scan started while the e-stop latch was held cleared the
  cancel event and swept the gimbal → now refused (`e-stopped`); a cancel
  landing after stitcher exit but before publish still published → result now
  discarded (post-builder and post-exit checks). Round 2 blocker: e-stop
  landing between the pre-check and `clear()` had its cancel erased → fixed
  with a post-clear re-check of both `is_estopped()` and `is_moving()`
  (deterministic False-then-True stub test). Round 3: PASS.
- **glm-5.1** — **PASS**. Independently verified hook lock-freedom, killpg
  group semantics, `_l/_r_val` zeroing on every path, same-fs atomic replace,
  and the three clear-vs-drive race windows. Non-blocking notes only (in-
  flight serial write at cancel instant — accepted in plan review; `nice`
  PATH dependency — OSError-guarded).

## Post-execution report

(appended after merge)
