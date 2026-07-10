# 024 — Python controller port (remove Go)

## Goal

The user wants a single-language codebase: rewrite the Go controller
(`rovercontrol/rovercontrol.go`, ~3,200 lines) as a **1:1 functional replica in
Python**, delete all Go, and merge — with everything behaving the same way
(endpoints, safety envelope, gamepad, camera streaming performance).

## Design

- **`rovercontrold.py`** — single-file controller on `:8080`, stdlib-only at
  runtime (termios serial, ThreadingHTTPServer, threading.Timer). Same URL map,
  same JSON shapes (`sort_keys=True` + compact separators match Go's map
  marshaling), same CLI flags, same systemd/launcher entry points.
- **`rovercontrold_page.py`** — the web UI extracted byte-identically from Go's
  `htmlPage` constant (verified programmatically, 36,425 bytes).
- **Safety envelope preserved exactly**: nudge server-side auto-stop
  (threading.Timer + generation tokens so a stale timer never stops a newer
  command), e-stop latch (nonzero refused until a zero command), 0.5 s drive
  watchdog, state decision + serial write under one lock (Movement → Rover
  nesting).
- **Streaming stays pass-through** (SOI-split MJPEG fanout, latest-frame-wins
  Hub, no re-encode) — this is why Python can match Go here.
- Swap `roverctl`, `rovercontrol.service`, `ci-local.sh` to Python;
  `git rm rovercontrol/`; drop `rovercontrol-arm64`.

## Deliverables

- `rovercontrold.py`, `rovercontrold_page.py`
- `tests/test_controller.py` (unit: serial encoding pins, estop latch, nudge
  gen-tokens, watchdog, Hub/splitter, gamepad mapping + computeJoystick,
  driveGate, top-speed precedence)
- `tests/test_controller_http.py` (real server on port 0 + fake serial: every
  endpoint, validation rejects, streaming)
- Launcher/service/CI swapped; `CLAUDE.md` blurb updated; Go removed.

## Testing

- 53 ported tests, no hardware needed (fake serial link, synthetic JPEGs).
- CI (`./ci-local.sh`) green.
- **Live rover validation** (the go/no-go): deploy branch to the rover, run the
  Python controller, sweep every endpoint, and measure the one thing Go was
  chosen for — stream throughput.

## Risks

- *Stream performance* — the original reason for Go. Mitigated by the
  pass-through design; **measured on hardware: 15.0 fps sustained at 1080p
  (the camera's own cap), snapshot latency 12–25 ms — no regression.*
- *Threading semantics differ from goroutines* — covered by ported concurrency
  tests (stale-nudge, watchdog, estop latch) and an external safety review.
- *Keep-alive on multipart streams* — found in testing (clients hung on
  `read()`); fixed with `close_connection = True` on `/video_feed`/`/tour_feed`.

## Stages

1. Extract web page byte-identically → `rovercontrold_page.py`. ✅
2. Port the controller → `rovercontrold.py`. ✅
3. Port the test suite; make it pass. ✅ (53 tests)
4. Swap launcher/service/CI; delete Go. ✅
5. Deploy to the rover; live validation sweep + stream benchmark. ✅
6. Review gate, PR, merge; rover back on `main`. ⏳

## Reviews

### Code review

- **codex (GPT-5.5)** — safety core (TTYLink/Rover/Movement): **SOUND**.
  Confirmed: timer recheck of `gen == self._gen` under `Movement._mu` prevents
  stale stops; e-stop latch enforced under the same lock; consistent
  Movement→Rover lock nesting (no deadlock); correctly noted the lock, not the
  GIL, is what provides the guarantees. One note (not a bug): daemon timers
  can't stop motors after process death — true of any userspace timer, and the
  firmware-side heartbeat/estop path is unchanged.
- **Opus** — **PASS**, no blocking findings. Read the full 1,808 lines and
  cross-checked the deleted Go original: all safety invariants faithfully
  ported; path traversal blocked on all photo endpoints; NaN/Inf rejected
  without poisoning state; Hub condition-variable handshake race-free; gamepad
  unplug still stops the wheels. Four non-blocking notes — two fixed in this
  branch: (1) oversized POST body now sets `close_connection` (keep-alive
  desync), (2) `confidence` in `/photo_meta` is now NaN-checked like `bbox`
  (+ test). Two accepted as-is: blob writes aren't temp+rename (matches Go
  behavior), and shutdown runs in the signal handler (tty is CLOCAL, writes
  don't block).
- **glm-5.1** — **PASS**, no blocking findings. Independently traced all six
  load-bearing invariants (compact JSON wire format, e-stop latch, generation
  tokens, server-side nudge stop, 0.5 s watchdog, estop-then-close shutdown
  ordering) and confirmed each is pinned by a test; verified the Hub has no
  missed-wakeup bug and lock ordering has no inversion. Six polish notes
  (float repr on the wire, `hasattr` lazy-init smell, refused-nudge still arms
  a harmless timer, shutdown path untested, swallowed serial write errors) —
  noted, not blockers; "the rover stops safely under every failure mode I
  could trace."

## Post-execution report

**Implemented as planned**, merged as PR #58; the rover is back on `main` and
running the Python controller (serial + camera + gamepad all up on restart).

- **Delivered**: `rovercontrold.py` (1,808 lines), `rovercontrold_page.py`
  (web page byte-identical to Go's), 53 ported tests, launcher/service/CI
  swapped to Python, all Go removed (−5,164 lines).
- **Performance (the go/no-go question)**: measured live on the Pi 5 —
  15.0 fps sustained over 5 s at 1920×1080 (the camera's configured cap),
  snapshot latency 12–25 ms, ~57 MB RSS, ~0% idle CPU. The pass-through MJPEG
  design (no re-encode) is what makes Python equivalent here.
- **Deviations from a literal 1:1**: none functional. One porting bug found
  and fixed during testing (HTTP keep-alive hung clients on multipart
  streams → `close_connection = True` on `/video_feed` / `/tour_feed` —
  Go's chunked writer didn't need this). Two review-driven hardening fixes
  went beyond the Go original: keep-alive close on oversized POST bodies,
  NaN `confidence` rejected in `/photo_meta`.
- **Reviews**: codex SOUND, Opus PASS, glm-5.1 PASS — zero blocking findings
  (details above). CI green (222 tests).
- **Deferred** (reviewer polish notes, not regressions vs Go): temp+rename
  for blob writes, shutdown-path unit test, surfacing serial write errors in
  movement state, float rounding on the wire.
- **Ops note**: `pkill -f rovercontrold.py` inside an ssh one-liner matches
  the remote shell's own command line and kills the session — use a
  `"[r]over..."` bracket pattern and separate kill/start invocations.
