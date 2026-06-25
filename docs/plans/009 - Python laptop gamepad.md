# 009 - Python laptop gamepad (HTTP)

## Goal

Drive the rover with a USB gamepad **plugged into the laptop**, controlling it over
the network via the rover's running `app.py` (Flask, `:5000`) — so the **camera and web
UI keep working** (no serial contention). Ports the "solid basics" of the Go controller's
gamepad to Python. Replaces the on-rover, direct-serial, now-graveyarded
`rover_joystick.py` for the laptop-remote use case.

This is the chosen architecture (user decision): **laptop → HTTP**, **solid-basics** scope.

## Why HTTP, not direct serial

`app.py` owns `/dev/ttyAMA0` and serves the camera. A direct-serial joystick would have to
stop `app.py` (losing the camera). Sending control commands to `app.py`'s `/send_command`
endpoint (the same path `rover_client.py` already uses) lets the gamepad and the camera
coexist, and lets you drive from your desk.

## Scope (solid basics)

Mapping (hard-coded Xbox-style, mirrors the graveyard script):
- **Left stick** → drive (throttle = up/down, steer = left/right), mixed to L/R wheels.
- **Right stick** → camera pan/tilt (rate-integrated to absolute angles).
- **D-pad up/down** → raise/lower speed cap (`SPEED_STEPS`).
- **RB (hold)** → turbo cap. **A** → stop wheels. **Back** → e-stop (latched until sticks recenter).
- **X** → toggle head light. **LB** → toggle base light. **Y** → center camera.
- **L3 / R3** → relax / lock gimbal. **B** → snapshot. **Start / Ctrl-C** → quit.

Out of scope (deferred to a "full parity" follow-up): editable JSON mapping, `-calibrate`
wizard, optional precision/boost/panic/D-pad-camera bindings, trigger-axis bindings.

## Design

New file **`rover_gamepad.py`** (laptop-side), plus a small reuse of `rover_client.py`.

1. **Pure decision function** `compute_step(state, prev, ctrl) -> (Step, NextCtrl)` — the
   testable seam the Python side lacked (mirrors Go's `computeJoystick`). It is **pure: it
   does not mutate its inputs** (codex #5). Inputs: a `PadState` (axes + buttons, plain
   values, no pygame), the previous `PadState` (for rising edges), and an **immutable**
   `ControlState` (carried runtime: speed-cap index, current wheel L/R, pan/tilt, head/base
   light flags, estop-latched). Returns a `Step` (target wheel L/R, pan/tilt, light states,
   one-shot flags: stop/estop/snapshot/center/relax/lock) **and a new `ControlState`** the
   caller carries to the next tick. **All tuning, deadzone, mix, ramp, speed-cap, turbo,
   latch, and edge logic lives here** — while estop is latched, it emits no drive until the
   sticks recenter.
2. **pygame shell** reads the pad each tick into a `PadState` (drains the event queue —
   the graveyard's hard-won `pygame.event.get()` each frame), calls `compute_step`, and
   dispatches the result over HTTP. A `--debug` mode prints live axis/button indices.
3. **HTTP dispatch via `rover_client.py`**: `move(l,r)`, `set_camera(pan,tilt)`,
   `lights(front,base)`, `estop()`, `servo_torque(lock)`. **Host override (codex #4):**
   `rover_client.ENDPOINT` is computed at import, so add `set_host(host)` (and build the URL
   inside `_send_json`) — `--host`/`ROVER_HOST` calls it before use, or the CLI silently
   posts to the default rover.
4. **Snapshot (B)**: laptop-side, on its **own thread** (codex nit) — pull one JPEG from
   `http://<host>:5000/video_feed` (the MJPEG-grab trick from `rover_camera.py`) and save
   under `./photos/`. MJPEG grabbing can block, so it stays **off the control-command lane**.

### Send-rate + safety (the main risk)

The loop ticks at `RATE_HZ=25` for smooth sticks/ramp, but **25 HTTP POSTs/sec is too
many** and `{"T":1,...}` sets a *continuous* wheel speed that persists until the next
command — so if the laptop/Wi-Fi drops mid-drive, **the robot keeps going**.

**Scope decision on runaway (codex #1):** client-side measures cannot *guarantee* a stop
on a hard network drop or `kill -9` while a stick is held — only a **server/firmware-side
watchdog** can. This tool is therefore scoped for **supervised driving** (operator present,
rover in open space or on a stand, ready to e-stop), and that limit is stated loudly in
`--help` and the README. A proper **`app.py` drive-watchdog** (stop wheels if no drive
command for ~400 ms) is filed as a **separate follow-up** for unsupervised safety.

**Non-blocking, prioritized sender thread (Opus review + codex #2/#3).** The 25 Hz loop
must NEVER block on HTTP (`rover_client._send_json` blocks up to 4 s). One background sender
implements an explicit scheduler:
- **stop / estop = immediate, highest priority, non-coalesced.** They jump ahead of any
  pending drive/camera, and **clear the pending drive slot** so a queued `move()` can't fire
  after a stop. While the estop latch is set, drive sends are suppressed entirely.
- **drive = single coalesced latest-wins slot**, emitted only on change (>epsilon) or as a
  **≤8 Hz heartbeat** while moving; **stop on neutral** (one `stop()`, then quiet).
- **camera = separate coalesced slot, capped ≤8 Hz**, change-gated.
- **lights = edge-only** (on toggle).
- **snapshot = its own thread**, never on the control lane.
- Short HTTP **timeout (~0.5 s)** so a stuck link can't back commands up (adds a `timeout=`
  param to `rover_client._send_json`, default unchanged). Failures are swallowed + logged
  once.
- **Global cap:** the single sender serializes all lanes, so total request rate stays
  bounded (stop/estop immediate; drive ≤8 Hz; camera ≤8 Hz; lights/snapshot rare).

**Deadman (codex nit):** if pygame reports the controller disconnected or the window loses
focus, immediately enqueue `stop` (treat as neutral). On quit / KeyboardInterrupt / unhandled
exception, send a **short bounded burst of `stop()`** in a `finally` (more robust than one).
None of this solves a hard network loss — hence the supervised-use scope above.

## Tests (`tests/test_gamepad.py`, unittest)

Pure-`compute_step` tests, no pygame/HTTP:
- Deadzone: tiny stick → zero drive.
- Mix: full forward → L≈R>0; pure steer → opposite signs.
- Ramp: one tick can't jump from 0 to full (bounded by `RAMP*dt`).
- Speed cap: D-pad up/down moves the cap; turbo overrides; output never exceeds the cap.
- Camera: pan/tilt integrate by rate*dt and clamp to limits; center flag zeroes them.
- Edges: A=stop, Back=estop (latched until neutral), X/LB toggle lights once per press,
  Y=center, L3/R3 relax/lock — each fires on the rising edge only.
- Purity: `compute_step` returns a new `ControlState` and does not mutate its inputs.
- Send-gating helper: no change → no send; change/heartbeat → send.

Sender/dispatch tests (mock `rover_client`, no real HTTP):
- Priority: estop/stop sent immediately and **clear a pending drive** slot; queued `move`
  does not fire after a stop; drive suppressed while estop latched.
- Non-blocking: a `rover_client` whose send sleeps/raises does not stall the loop (the loop
  enqueues and returns; the sender absorbs the delay/error).
- Host override: `set_host("1.2.3.4")` makes `_send_json` POST to that host's URL.
- Deadman: simulated controller-disconnect/focus-loss enqueues `stop`.
- Dispatch mapping: `Step` → correct `rover_client` calls with clamping.

## Risks / tradeoffs

- **Latency** rides the Wi-Fi link (acceptable for teleop; the camera already does).
- **Runaway on network drop / hard kill** — mitigated, NOT eliminated. The Back e-stop and
  recentering only recover *once connectivity returns*; they don't help during an outage.
  Hence the supervised-use scope + the `app.py` watchdog follow-up. Documented loudly.
- **pygame dependency** on the laptop — add `pygame` to a laptop-only requirements note;
  the rover/app.py is untouched.
- Hard-coded Xbox mapping — `--debug` helps remap; full config deferred.

## Rollout

Pure laptop tool; no rover changes, no deploy. Run:
`python rover_gamepad.py --host 192.168.1.131` (gamepad plugged into the laptop, app.py
running on the rover). `--debug` to discover indices for a non-Xbox pad.

---

## Plan review

**Gate: PASSED** after revisions (Opus + codex; glm not part of plan gate).

**Opus (Claude):** flagged that `rover_client._send_json` blocks up to 4 s, so the 25 Hz
control loop must dispatch through a non-blocking background sender (short timeout, latest-
wins) — folded into the design before codex review.

**codex (GPT-5.5) — 5 blocking, all accepted:**
1. *Runaway under-designed* — client-side can't guarantee a stop on hard network drop/kill.
   Resolution: explicit **supervised-use scope** + a separate **`app.py` drive-watchdog**
   follow-up for unsupervised safety. (Stated in `--help`/README.)
2. *Sender priority* — stop/estop now immediate, non-coalesced, highest priority, clear the
   pending drive slot; drive suppressed while estop latched.
3. *Global send-rate* — one serializing sender with an explicit scheduler (stop/estop
   immediate, drive ≤8 Hz, camera ≤8 Hz separate, lights edge-only, snapshot own thread).
4. *Host override* — `rover_client.ENDPOINT` is import-time; add `set_host()` + build URL in
   `_send_json`.
5. *`compute_step` mutated `Settings`* — made pure: immutable `ControlState` in, new
   `ControlState` out.
   Nits (deadman on disconnect/focus-loss, bounded stop-burst on quit, snapshot off the
   control lane, sender/host/priority tests) also incorporated.

## Code review

**Gate: PASSED** after revisions. Reviewers: Opus + codex. **opencode/glm-5.1 unavailable**
(not installed) → 2-way.

**Opus (Claude):** caught `main()` printing the wrong docstring paragraph for the controls
(fixed with a `CONTROLS` constant). Verified Sender thread-safety (slots under a Condition;
HTTP done outside the lock) and the pure `compute_step`.

**codex (GPT-5.5) — 2 blocking, both fixed:**
1. *Shutdown race* — the `finally` sent `estop()` directly while the Sender could still emit
   a queued/in-flight `move()` after it. Now shutdown routes through the Sender
   (`emergency("estop")` clears pending drive+camera and queues estop, `shutdown()` drains
   it, `join()` makes it the last command on the wire). `emergency()` now also clears the
   camera slot.
2. *`DRIVE_HZ` not enforced* — ramping changed the wheel value every 25 Hz tick, so `changed`
   bypassed the period gate (sends at 25 Hz, starving camera). Now: change-to-neutral sends
   immediately (safety); nonzero drive (incl. ramp steps) is rate-limited to `DRIVE_HZ`
   (latest value at each due tick, doubling as the heartbeat). New tests cover both.
   Nit (momentary `A` stop vs latched e-stop) is intended and left as-is.

## Post-execution report

**Branch:** `feature/python-laptop-gamepad`. One logical change. Standalone tool — no rover
deploy.

### What shipped
- **`rover_gamepad.py`** (new, ~340 LOC): laptop pygame gamepad → rover over HTTP via
  `rover_client.py`. Pure `compute_step` (drive mix + ramp + camera integrate + speed cap +
  turbo + estop latch + button edges), a `Dispatcher` (change-gate + ≤8 Hz rate cap +
  heartbeat + immediate stop/estop), a prioritized non-blocking `Sender` thread (coalesced
  drive/camera slots, preempting emergency lane), a `Snapshotter` thread (grabs a frame from
  `:5000/video_feed`), and a `--debug` index printer. **SUPERVISED-USE** banner in `--help`
  and at startup.
- **`rover_client.py`**: `set_host()` + `set_timeout()` + per-call URL rebuild (so `--host`
  works) and a `timeout=` param on `_send_json` — backward compatible.
- **Independence:** imports only stdlib + `pygame` + `rover_client` — **no chatbot/serial
  coupling**. Runs entirely on its own (verified by an import-scan).

### Tests / CI
- `tests/test_gamepad.py` (45 tests total in suite): compute_step (deadzone/mix/ramp/cap/
  turbo/camera/center/edges/estop-latch/purity), Sender (emergency clears pending drive,
  urgent mapping, non-blocking, shutdown-estop-is-last), Dispatcher (change-gate, rate cap,
  neutral-stops-once, lights), rover_client host/timeout override. `go test`/Go untouched.

### Tradeoffs / limitations
- **Runaway on hard network drop / kill -9 is NOT eliminated** — supervised-use only; the
  `app.py` drive-watchdog (separate follow-up) is the real fix. Stated loudly in `--help`.
- Latency rides Wi-Fi; hard-coded Xbox mapping (`--debug` to remap; JSON config + calibrate
  deferred to a "full parity" follow-up).
- `pygame` is a laptop-only dependency (`pip install pygame`); the rover is untouched.
