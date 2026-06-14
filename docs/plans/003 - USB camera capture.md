# 003 — USB camera capture for rovercontrol

## Problem

`rovercontrol` (plan 002) captures video by spawning `rpicam-vid` (libcamera /
CSI). On the actual rover, **the camera is a USB UVC camera** at `/dev/video0`
(Realtek `0bda:5842`, driver `uvcvideo`) that outputs **MJPG natively** — there
is no CSI camera, so `rpicam-hello --list-cameras` reports "no cameras
available" and the controller's camera loop fails (`exit status 255`) forever.
Confirmed on hardware after deploy: serial, gimbal, lights, and the gamepad all
work; only video + snapshots are dead.

The stock `app.py` handled this with a `cv2.VideoCapture(0)` (V4L2) fallback
alongside Picamera2 (`cv_ctrl.py:151`) — i.e. it read the USB camera over V4L2.

## Fix

Add a **V4L2/USB MJPEG capture path** and keep the existing rpicam-vid path, with
auto-detection. The USB cam emits complete JPEGs back-to-back, so the existing
`splitFrames` (SOI-to-next-SOI), `Hub`, snapshot, and `/video_feed` code are
**unchanged** — only the spawned producer command changes.

Verified on the rover (read-only test): `v4l2-ctl` is already installed and
streams MJPG to stdout cleanly (frames start with `ff d8`); `ffmpeg` is also
present as a fallback.

### Capture command (V4L2 path)

```
v4l2-ctl -d <device> \
  --set-fmt-video=width=<W>,height=<H>,pixelformat=MJPG \
  --stream-mmap --stream-count=0 --stream-to=-
```

`--stream-count=0` = stream continuously; `--stream-to=-` writes the raw MJPG
frames to stdout. (Fallback, documented, if v4l2-ctl misbehaves:
`ffmpeg -f v4l2 -input_format mjpeg -video_size WxH -i <device> -c:v copy -f mjpeg -`.)

### Source selection

New flags on the controller:
- `-camera-mode auto|v4l2|rpicam` (default **auto**)
- `-camera-device /dev/video0` (V4L2 device for the v4l2 path)

**auto**: if `-camera-device` exists → **v4l2**; else → **rpicam** (keeps CSI
rovers working). The systemd unit / `@reboot` line needs no change (auto picks
v4l2 on this rover).

### Code shape (single file, minimal change)

- Add `buildCameraCmd(mode, device string, w, h, fps int) *exec.Cmd` that returns
  the right command for the resolved mode (this is the unit-testable seam).
- `Camera.runOnce` calls `buildCameraCmd` instead of hard-coding rpicam-vid; the
  backoff / busy-surfacing / `/healthz` status logic is unchanged.
- `resolveCameraMode(mode, device)` does the auto-detect (stat the device).
- `Camera` gains `mode`/`device` fields set from flags in `main`.

Everything else (frame splitter, hub, snapshot os.Link, video_feed multipart,
placeholder-when-down) stays as-is.

## Deliverables

- `rovercontrol/rovercontrol.go` — camera-command builder + mode/device flags.
- `rovercontrol/rovercontrol_test.go` — tests for `buildCameraCmd` (v4l2 + rpicam
  arg correctness) and `resolveCameraMode` (auto picks v4l2 when device exists,
  rpicam when not).
- `docs/plans/003 - USB camera capture.md` — this plan.

## Testing

- Unit (Mac, no hardware): `buildCameraCmd("v4l2", "/dev/video0", 1280,720,30)`
  produces the exact v4l2-ctl args incl. `pixelformat=MJPG` and `--stream-to=-`;
  `buildCameraCmd("rpicam", …)` still produces the rpicam-vid args;
  `resolveCameraMode("auto", <existing path>)` → "v4l2",
  `resolveCameraMode("auto", <missing path>)` → "rpicam". Reuse the existing
  splitter/hub/snapshot tests (capture format unchanged).
- `./ci-local.sh` green.
- Deploy smoke (on the rover): redeploy binary; `/healthz` shows `camera.up=true`;
  `GET /video_feed` yields multipart JPEG; `POST /snapshot` writes a real JPEG;
  the gallery shows it.

## Risks

- `v4l2-ctl --stream-count=0` continuous behavior: verified format works for a
  finite count; confirm `=0` streams indefinitely during implementation (else
  use a large count or the ffmpeg fallback).
- USB cam exclusivity: only one process may open `/dev/video0` — same single-
  owner model as before; the controller is the sole owner once deployed.
- Resolution support: the cam advertised MJPG; if 1280x720 isn't supported,
  fall back to the camera's default (don't force a size) — handle a start
  failure via the existing busy/retry + `/healthz` surfacing.

## Stages (autopilot-plan skill)

1. Plan — this doc.
2. Plan-review gate — Opus + codex; record into `## Reviews`.
3. Implement on a local branch.
4. Tests.
5. Code-review gate (3-way: Opus + codex + glm); record into `## Reviews`.
6. `./ci-local.sh` gate → PR → merge (AUTO_MERGE per the user) → deploy + verify.
7. Post-execution report below.

## Reviews

### Plan review (2-way) — both APPROVE, no blocking items

- **Opus — APPROVE.** Approach sound, splitter reuse correct, right-sized. Fold
  in (non-blocking): (1) make `/healthz` status + startup log **mode-aware**, not
  hard-coded "rpicam-vid"; (2) explicit unknown-`-camera-mode` handling; (3) a
  concrete resolution-failure strategy.
- **codex — APPROVE.** No blocking. (1) Verify `--stream-count=0` streams
  continuously (else omit it / use ffmpeg). (2) `stat`-based auto-detect is fine.
  (3) **Resolution:** if the sized v4l2 command fails, **retry once without
  width/height before normal backoff** (don't let an unsupported resolution be a
  permanent failure). (4) Capture the spawned command's **stderr** to the log so
  v4l2 errors are diagnosable from `/healthz`.

**Resolution folded into implementation:** mode-aware status/log strings; v4l2
builder omits width/height when ≤0; `run()` tries the configured size, and on a
v4l2 failure retries once unsized before backoff (codex); spawned-command stderr
is captured into the error/log; unknown mode falls back to rpicam with a logged
warning and `main` validates the flag.

### Code review (3-way) — Opus + glm APPROVE, codex REQUEST-CHANGES → resolved

- **Opus — APPROVE.** No blocking. Flagged a real risk: the `err==nil` path
  re-spawned with no delay → possible hot-loop if a producer exits 0 instantly.
  **Fixed:** `run()` now treats a sub-3s exit as a failure and always waits the
  (≥1s) backoff before re-spawning.
- **glm — APPROVE.** No blocking. Noted the unsized retry double-spawns every
  cycle and the guard didn't match `buildCameraCmd`. **Fixed:** an
  `unsizedExhausted` flag stops the retry after it fails once (reset on a healthy
  stream); guard tightened to `width>0 && height>0`.
- **codex — REQUEST-CHANGES (2 blocking) → both resolved:** (1) rpicam still got
  `--width 0 --height 0` — **fixed**, `buildCameraCmd` now omits dims/fps ≤0 for
  rpicam too (+ test). (2) unsized retry dropped `err2` — **fixed**, both errors
  surfaced. Re-verified with codex after the fixes.
- **Added** a control-flow test (`TestCameraRunUnsizedRetryAndExhaustion`) via an
  injectable `attemptFn`, covering sized→unsized order, one-shot exhaustion, and
  that the clearer unsized error is surfaced. All tests race-clean; `ci-local.sh`
  green.

## Post-execution report

**Shipped (PR #5, merged to main):** a V4L2 capture backend in `rovercontrol.go`
— `resolveCameraMode` (auto/v4l2/rpicam), `buildCameraCmd` (v4l2-ctl MJPG or
rpicam-vid; dims/fps omitted when ≤0), a hardened `Camera.run` (unsized retry
with one-shot exhaustion, floored backoff, both-error surfacing, mode-aware
status/log), bounded stderr capture, and `-camera-mode`/`-camera-device` flags.
Frame splitter / hub / snapshot / `/video_feed` unchanged. New unit tests for the
command builder, mode resolver, tailBuffer, and the run-loop retry/exhaustion
control flow (via an injectable `attemptFn`).

**Deviations from the plan:** none material. Beyond the plan we added, from code
review: a floored backoff (no zero-delay re-spawn / hot-loop), an
`unsizedExhausted` flag (stop double-spawning the unsized retry every cycle), and
rpicam zero-dimension omission (the plan only specified v4l2). codex's first code
review was REQUEST-CHANGES (rpicam zero-dims; dropped unsized error) — both fixed
and re-verified to APPROVE.

**Tradeoffs:** camera backend is chosen once at startup (no hot-plug
re-detection) — explicit `-camera-mode` is the escape hatch. Capture shells out
to `v4l2-ctl` (already on the rover) rather than reading V4L2 ioctls in-process —
simpler and dependency-light, at the cost of one child process (same model as the
rpicam path). Snapshot/video reuse the unchanged MJPEG path, so format fidelity is
identical to before.

**Deploy result (verified live on the rover, 2026-06-14):** rebuilt arm64,
rsynced (checksum match), restarted the controller. `/healthz` →
`camera.up=true` (v4l2, /dev/video0, 1280×720), `serial.up=true`, `gamepad=true`.
`/video_feed` serves multipart MJPEG (frame starts `ff d8`); `POST /snapshot`
wrote a valid 190 KB JPEG to `~/robot/photos`. `@reboot` autostart unchanged
(auto-detects v4l2). The deploy that was "everything but video" is now complete.
