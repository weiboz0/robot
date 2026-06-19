# 008 - Camera stream lag and stall fix

## Problem

Two distinct symptoms reported from the live web UI (`http://192.168.1.131:8080/`):

1. **Stall / freeze that never recovers.** The live view sticks on one frame; only
   a page reload brings it back. `/healthz` still reports `camera.up: true` during the
   freeze. Observed after panning the camera (the USB cam rides the pan/tilt head, so
   movement can jostle the USB connection and the `v4l2-ctl` / `rpicam-vid` producer
   goes silent **without exiting**).

2. **Lag that a reload does not cure.** After reconnecting the stream is live but
   consistently delayed.

## Root cause

Both live in the MJPEG path in `rovercontrol.go`.

- **Stall:** `Camera.run` only re-spawns the capture process when `runOnce` *returns*,
  i.e. when the producer **exits**. `splitFrames` reads the producer's stdout with a
  plain blocking `Read` and no deadline, so a producer that stalls-but-stays-alive
  wedges `splitFrames` forever — the run loop never cycles, `cam.status()` stays `up`,
  and `videoFeed`'s 1 s keepalive keeps re-sending the last good frame. Result: a frozen
  real frame that only a fresh subscription (reload) clears. Separately, a wedged client
  can block `videoFeed`'s `w.Write` indefinitely (no write deadline), pinning a hub
  subscription until the OS TCP timeout.

- **Lag:** the camera defaults are **1280×720 @ 30fps** (`main` flag defaults; the
  systemd unit passes no overrides, so the deployed rover runs them). MJPEG sends a full
  JPEG per frame (no inter-frame compression), so that is ~15–25 Mbps. On a thinner
  Wi‑Fi link the kernel TCP **send buffer** fills with a FIFO backlog of whole frames.
  The `Hub` is latest-frame-wins, but it only drops *upstream* of `w.Write`; once frames
  are handed to the socket they play out in order. A reload flushes that backlog for an
  instant, then it refills — steady-state latency.

## Goals

- Live view recovers on its own from a silent-producer stall (no manual reload).
- Steady-state latency stays bounded even on a constrained link.
- Out-of-the-box defaults favour low-latency teleop; full res stays available via flags.
- No regression to snapshot, gallery, drive, lights, or joystick paths.

## Non-goals

- Switching the transport to H.264 / WebRTC (big change; tracked as a future option).
- Hot-plug camera detection or per-client adaptive bitrate.

## Design / changes (all in `rovercontrol/rovercontrol.go`)

1. **Lower default capture size/rate.** Introduce named consts
   `defaultCamWidth=640`, `defaultCamHeight=480`, `defaultCamFPS=15` and use them as the
   `-width/-height/-fps` flag defaults (was 1280/720/30). Operators who want more can still
   pass flags. (`0 = let the camera choose` semantics unchanged.)
   - **Make `-fps` actually apply on V4L2.** `buildCameraCmd`'s v4l2 branch currently sets
     only resolution, so the USB cam ignores `-fps` (would stay at the camera default,
     ~30fps). Add `--set-parm=<fps>` when `fps > 0` so the framerate reduction is real on
     the deployed USB path — without it the "~6× less data" does not hold. Extend
     `TestBuildCameraCmd` to cover it.

2. **Write deadline + status-gated send in `videoFeed`.** Wrap the writer in
   `http.NewResponseController(w)`; set a per-frame write deadline
   (`streamWriteTimeout = 5s`) before each multipart part and use `rc.Flush()` (so flush
   errors are observed, not deferred to the next write). On any send/flush error
   (including a deadline) `return` to close the connection — the `<img>` auto-reconnects
   with a fresh subscription, so a stuck/slow client can never wedge the handler (or the
   keepalive) forever, and worst-case staleness is bounded. `SetWriteDeadline` returns
   `ErrNotSupported` under `httptest` recorders; that error is ignored.
   - **Gate every send on camera status** (codex #3): `subscribe()` preloads `hub.latest`
     and the `frames` case did not check status, so a reloaded client could be served one
     stale real frame after a stall. Substitute `placeholderFrame` on *all* send paths
     when `!cam.status().up` (not just the keepalive tick).

3. **Frame-staleness watchdog in the capture loop.** Track the time of the last published
   frame in an **`atomic.Int64`** (UnixNano — race-free under `go test -race`, codex #4),
   updated by wrapping the `hub.publish` callback passed to `splitFrames`. A per-attempt
   watchdog goroutine cancels the producer's context if no frame arrives for
   `camStallTimeout = 5s`, so `Camera.run` re-spawns it and `cam.status()` flips to `down`.
   The staleness clock starts at process spawn, giving an implicit ~5s first-frame grace.
   - **Don't poison backoff** (codex #2): a watchdog cancel must not count as a hard
     failure. `runOnce` returns a sentinel `errStalled`; `Camera.run` treats it as a
     quick respawn (reset backoff to 1s) instead of doubling toward the 15s ceiling, while
     genuine producer exits keep the existing exponential backoff. Shutdown (`ctx.Done`)
     is still distinguished and exits cleanly.

4. **Bound the per-connection send buffer.** Set `http.Server.ConnContext` to stash the
   raw `net.Conn` in the request context; in `videoFeed`, shrink that connection's TCP
   send buffer (`streamSendBuffer = 256 KiB`) so `w.Write` back-pressures quickly and the
   hub's latest-frame-wins caps in-flight latency to a few frames rather than a large
   kernel backlog. The tuning is applied from inside `videoFeed`, so in practice only
   streaming connections are shrunk; note this is connection-level (an HTTP/1 keep-alive
   reuse would inherit it — acceptable, and the server is HTTP/1 only). The kernel may
   round/double the value, so this bounds latency only approximately — the firm bounds
   come from the lower bitrate + write deadline + reconnect-on-stuck. Non-TCP / missing
   conn is tolerated as a no-op.

## Tests (Go, `rovercontrol/*_test.go`)

- `videoFeed` returns promptly (does not hang) when the writer errors on `Write` —
  proves the close-on-error path and that a wedged client can't pin the handler.
- `videoFeed` substitutes the placeholder (does not emit the stale real frame) when the
  camera is down even though `hub.latest` is still set (covers codex #3).
- Send-buffer tuning helper is a safe no-op when the context has no conn / a non-TCP conn.
- Stall watchdog: a producer that starts then goes silent gets its attempt cancelled and
  re-spawned with backoff **reset** (not exponentially grown), and status flips to `down`
  (using `Camera.attemptFn` injection, as in `TestCameraRunUnsizedRetryAndExhaustion`).
- V4L2 `--set-parm=<fps>` is emitted when fps>0 and omitted when fps==0; the new
  `defaultCam*` consts wire into capture args (extends `TestBuildCameraCmd`).
- Existing `TestVideoFeedMultipart`, `TestHubLatestWins`, `TestSplitFrames` must stay green.

## Risks / tradeoffs

- **Lower default quality.** 640×480 is noticeably softer than 720p; acceptable for
  teleop and reversible per-launch via flags. Documented in the plan and `-width/-height`
  help text.
- **5 s timeouts.** Long enough not to trip healthy clients/producers, short enough that
  recovery is quick. Tunable consts.
- **Small send buffer** trades a few extra syscalls for lower latency; negligible for one
  stream. Disables kernel send autotuning on that socket by design.
- **Reconnect on stuck client** briefly shows the placeholder before the `<img>`
  reconnects — preferable to an unbounded frozen frame.

## Rollout

Code-only; deploy by `git pull` on the rover + restart `rovercontrol`. No new default
flags needed in `rovercontrol.service` (the binary defaults change). Verify with `--all`
integration tests against the live rover after deploy.

---

## Plan review

**Gate: PASSED** after revisions (2 reviewers; opencode/glm not part of the plan gate).

**Opus (Claude) — APPROVE with notes.** Architecture sound: the two symptoms are
genuinely separate (producer-side stall vs. client-side/bandwidth), and the fixes map
1:1. Notes raised and addressed: (a) the watchdog, not just the write deadline, is what
fixes the *reported* freeze (camera up but frozen = silent producer); (b) close on a
mid-frame deadline must `return`, never continue, or the multipart stream desyncs;
(c) confirm the coverage floor still holds after the untested `main()` ConnContext line.

**codex (GPT-5.5) — 4 blocking, all accepted and folded into the design above:**
1. *`-fps` ignored on V4L2* → added `--set-parm=<fps>` to the v4l2 command (design #1);
   without it the framerate cut never reaches the deployed USB cam.
2. *Watchdog cancel poisons backoff* → `errStalled` sentinel resets backoff on a
   stall-respawn instead of escalating to the 15s ceiling (design #3).
3. *Stale-frame leak via `subscribe()` preload* → gate **all** `videoFeed` send paths on
   `cam.status()`, not just the keepalive (design #2).
4. *Timestamp race* → `atomic.Int64` UnixNano for `lastFrame` (design #3).
   Nits (warmup grace, `rc.Flush()`, ConnContext reuse wording, ~approximate buffer bound,
   extra tests for stale-frame + backoff-reset) also incorporated.

## Code review

**Gate: PASSED** after revisions. Reviewers: Opus + codex. **opencode/glm-5.1 was
unavailable** (not installed on this machine), so the 3-way review ran 2-way; re-run glm
before merge if it becomes available.

**Opus (Claude) — APPROVE after the codex fixes.** Verified: the `runOnce` watchdog
goroutine can't deadlock (`cancel()` after `Wait()` unblocks it; `<-watchdogDone` then
reads `stalled` in its final state); `lastFrame` is the only cross-goroutine field and is
`atomic`; the `firstFrame` flag is touched only by the single splitter goroutine (no
race); `videoFeed` returns on any write/flush error so a wedged client can't pin it.
Passes `go vet` and `go test -race`.

**codex (GPT-5.5) — 2 blocking, both fixed:**
1. *Stale frame re-served on respawn* — status was set `up` at `cmd.Start()`, so during a
   respawn the old `hub.latest` was served until the watchdog re-fired. Fixed: status flips
   `up` only on the **first published frame**; until then `videoFeed`'s gate serves the
   placeholder. (`runOnce` no longer calls `setStatus(true)` at start.)
2. *`errStalled` ignored on the unsized-retry path* — a stall during the v4l2 unsized retry
   was wrapped as a hard failure (grew backoff, set `unsizedExhausted`). Fixed: the unsized
   retry is skipped for a stall, and an unsized attempt that stalls is routed through the
   fast-respawn path.
   Nit (respawn-before-first-frame placeholder) addressed by fix #1 and covered by
   `TestVideoFeedPlaceholderWhenCameraDown` + the new `TestRunOnceWatchdogStallAndFirstFrameStatus`
   (drives `runOnce` against a real emit-then-stall producer via the `newCmd`/`stallTimeout`
   test hooks).

## Post-execution report

**Branch:** `fix/camera-stream-lag-and-stall` (not pushed/merged — auto-merge not set;
work left on the branch per request). One logical change.

### What shipped (all in `rovercontrol/rovercontrol.go`)
- **Lower default capture** to 640×480 @ 15fps via `defaultCam*` consts, and made `-fps`
  real on V4L2 by emitting `--set-parm=<fps>` (it was silently ignored on the USB cam).
- **`videoFeed`:** per-frame write deadline (`streamWriteTimeout`, 5s) via
  `http.ResponseController`, `rc.Flush()` with error checking, **status-gated sends**
  (placeholder whenever the camera is down, on every path), and close-on-error so a
  slow/stuck client can't pin the handler.
- **Frame-staleness watchdog** in `runOnce`: an `atomic.Int64` last-frame clock + a
  per-attempt goroutine that cancels a silent-but-alive producer after `camStallTimeout`
  (5s); `run` respawns it via the `errStalled` sentinel without growing backoff. Status
  now flips `up` only on the first published frame.
- **Per-connection send-buffer cap** (`streamSendBuffer`, 256 KiB) via `Server.ConnContext`
  + `tuneStreamConn`, so a slow link can't build a multi-frame kernel backlog.

### Tests
- New `rovercontrol_lagfix_test.go`: close-on-write-error, placeholder-when-down,
  `tuneStreamConn` safety (no/non-TCP/real-TCP conn), stall fast-respawn (backoff reset),
  and a real emit-then-stall producer exercising the `runOnce` watchdog.
- Updated `TestBuildCameraCmd` for `--set-parm` + the new defaults.
- CI (`ci-local.sh`): `go vet` + `go test -race` **PASS**, coverage **74.6%** (floor 70%),
  linux/arm64 cross-compile OK, Python suite OK. Integration (`--all`) not run — rover
  unreachable at report time (Wi-Fi link degraded, ~2–3 s RTT).

### Deviations from the plan (driven by code-review)
- Added test-only hooks `Camera.newCmd` / `Camera.stallTimeout` to make the watchdog
  unit-testable (not in the original design).
- Status-up-only-on-first-frame and routing unsized-retry stalls through the fast-respawn
  path were added after codex's code review (blocking #1/#2).

### Tradeoffs / limitations
- Default view is softer (640×480) — reversible per-launch with `-width/-height/-fps`.
- Watchdog/write timeouts are 5s: recovery isn't instant, and a real freeze still shows
  the last frame for up to ~5s before flipping to the placeholder.
- Send-buffer cap is approximate (kernel may round/double); the firm latency bounds come
  from lower bitrate + write deadline + reconnect-on-stuck.
- **This does not fix the underlying ~2–3 s Wi-Fi RTT** observed during testing — that's a
  link/RF problem (see ops notes), not addressable in this code. MJPEG is still the
  transport; H.264/WebRTC remains a separate future change.

### Deploy / verify (when ready)
`git pull` on the rover + `sudo systemctl restart rovercontrol` (no service-file change
needed — binary defaults changed). Then `./ci-local.sh --all` against the live controller,
and confirm the live view recovers on its own after a forced camera stall.
