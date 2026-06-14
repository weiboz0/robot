# 002 — Rover controller (single-file Go HTTP server)

## Goal

One Go program — **one source file** — that runs **on the rover** and is the
*only* thing needed to control it. It owns the hardware directly (serial +
camera) and exposes everything over **HTTP (REST)**. It fully replaces the stock
Waveshare `app.py`, the stock web service, the Python gallery (`rover_web.py`),
the direct-serial tool (`rover_direct.py`), the photo helper (`rover_camera.py`),
and the Python joystick (`rover_joystick.py`).

Client/server split (the user's model):

- **Controller = server, on the rover.** Owns serial + camera. The URL path *is*
  the command: `POST http://<rover>:8080/move_left` makes it move. No CLI, no
  chatbot inside it. Reads a gamepad plugged into the Pi and serves the live
  video + snapshots itself — nothing else has to be running to see the camera.
- **Client = the user interface** that fires those requests. We ship a built-in
  web UI (served at `/`) as the default client. The existing LLM chatbot stays
  as a *separate* client, repointed to this API in a follow-up (out of scope
  here, but the API is designed to make it trivial).

`app.py` is **NOT deleted** (user's instruction). Instead we produce a reference
doc listing every function/route/command it had, banner-marked **SUPERSEDED —
kept for reference only**.

## Why Go, HTTP, one file

- **Go over Rust:** the camera half already exists in Go (plan 002 rovercam);
  trivial static `GOOS=linux GOARCH=arm64` cross-compile; goroutines fit the
  MJPEG fan-out + joystick loop + HTTP server cleanly.
- **HTTP over gRPC:** the browser UI needs HTTP anyway (video `<img>`, fetch);
  gRPC needs codegen + a proxy for browsers and fights the "one file" goal.
  REST with `path = command` is exactly what the user asked for.
- **One source file** (`rovercontrol.go`, `package main`): per the user's
  explicit "1 file". ~1–1.5k lines. A `go.mod` + one trusted dependency
  (`golang.org/x/sys/unix`, for serial termios) is still "one source file"; the
  HTML client is an embedded string constant, not a separate file.

## Hardware interface (ported from rover_direct.py / base_ctrl.py)

Serial: open `/dev/ttyAMA0` (Pi 5) @ 115200 8N1, raw, via `x/sys/unix` termios.
**Write-only** (we disable the feedback stream like rover_direct does, so we
never need to read). All writes go through **one goroutine / mutex** so the
HTTP handlers and the joystick loop can't interleave a half-line on the wire.

Termios cflags (write-only, the classic-trap ones called out explicitly):
`B115200 | CS8 | CLOCAL | CREAD`, no parity/2-stop/flow-control, `ICANON`/`ECHO`/
`OPOST` cleared (raw), newline-delimited writes. **`CLOCAL` is required** or
`open(/dev/ttyAMA0)` can block waiting on carrier.

Boot init (same as rover_direct.init_base): `{"T":143,"cmd":0}` echo off,
`{"T":131,"cmd":0}` feedback off, `{"T":4,"cmd":2}` select Gimbal module
(required for pan/tilt — a hard-won rover fact).

**Serial-unavailable behavior (resolves B2):** the controller does **not**
`pkill` app.py (unlike the Python tools) — deploy disables the stock @reboot jobs
instead. So if serial can't open (port busy / missing), the controller **stays
up HTTP-only**: control endpoints return **503** with a clear JSON error, video
still works, and `/healthz` reports `serial.up=false` with the reason. It retries
opening in the background. (Chosen over exiting so the operator sees *why* in the
UI rather than a dead port.) A manually-launched app.py will hold the port
indefinitely — documented in deploy notes.

Commands (newline-delimited compact JSON), with the same clamps as rover_direct:

| Action | JSON | Limits |
|---|---|---|
| drive wheels | `{"T":1,"L":l,"R":r}` | ±0.5 |
| stop | `{"T":1,"L":0,"R":0}` | |
| e-stop | `{"T":1,"L":0,"R":0}` then `{"T":0}` | |
| aim camera (abs) | `{"T":133,"X":pan,"Y":tilt,"SPD":0,"ACC":0}` | pan ±180, tilt −45..90 |
| lights | `{"T":132,"IO4":base,"IO5":front}` | 0..255 |
| gimbal relax/lock | `{"T":210,"id":255,"cmd":0\|1}` | |
| center camera | aim 0,0 | |

## Non-goals — stock app.py features intentionally dropped (resolves codex #3)

"Fully replaces app.py" means **for the rover's actual control + camera use** —
not a feature-for-feature reimplementation. These stock features are
deliberately **not** carried forward (and are listed as dropped in the app.py
reference doc): WebRTC `/offer` streaming; audio upload/playback/TTS; all CV
modes (motion/face/object/color/hand/pose detection, line-following auto-drive);
**video recording** (`.mp4`); OLED boot text/versioning; ESP-NOW mesh commands;
servo ID/midpoint configuration; websocket telemetry overlay; timelapse missions;
the Jupyter integration. If any of these is wanted later it's a follow-up. The
media model is **MJPEG live view + still JPEG snapshots only** (no recording).

## HTTP API (path = command)

All control endpoints are `POST` (state-changing); reads are `GET`. Responses are
small JSON `{"ok":true,...}`. CORS enabled (so any client/page can call it).

Movement (discrete nudges + analog):
- `POST /move_forward`, `/move_back`, `/move_left`, `/move_right` — fixed-speed
  nudge for `?ms=` (default ~400ms) then auto-stop (mirrors the joystick feel)
- `POST /stop`, `POST /estop`
- `POST /drive?l=<-1..1>&r=<-1..1>` — analog continuous (for the joystick/UI
  hold-to-move); scaled by the current speed cap

Camera gimbal:
- `POST /camera_up`, `/camera_down`, `/camera_left`, `/camera_right` (`?deg=`,
  default 15, relative nudge), `POST /camera_center`,
  `POST /camera_aim?pan=&tilt=` (absolute)

Lights / gimbal servos:
- `POST /light_head?on=0|1`, `POST /light_base?on=0|1`, `POST /lights?front=&base=`
- `POST /gimbal_relax`, `POST /gimbal_lock`

Speed cap:
- `POST /speed?cap=<0..0.5>` and `GET /speed`

Camera media (self-contained — no other process needed):
- `GET /video_feed` — MJPEG `multipart/x-mixed-replace` (reuses rovercam hub)
- `POST /snapshot` — save latest frame to photos dir (collision-safe os.Link),
  returns `{"ok":true,"name":...}`
- `GET /photos` (JSON list), `GET /latest`, `GET /photos/<name>`,
  `POST /delete_photo/<name>` (traversal-safe)

Meta:
- `GET /` — the built-in web UI client
- `GET /healthz` — `{"ok":true,"serial":{"up":bool,"err":str},"camera":{"up":bool,"err":str},"gamepad":bool}`

`/snapshot` returns JSON `{"ok":true,"name":...}` (not rovercam's 302 redirect —
better for programmatic clients; the built-in UI uses `fetch`). All control
endpoints are `POST`, validate/clamp, and **reject malformed numbers** with a
JSON 4xx instead of silently coercing. CORS: `Access-Control-Allow-Origin: *`
with an `OPTIONS` preflight handler (LAN control API, acceptable threat model;
stated explicitly).

### Movement arbitration & safety (resolves plan-review B1/B3 + codex #1/#2)

All wheel motion goes through one **MovementController** (behind the serial
mutex). It is the single source of truth for "are we moving and why", so stale
timers and competing sources can't fight:

- **Generation tokens (leases).** Every motion-setting call (`/drive`, a nudge,
  a joystick frame, `/stop`, `/estop`) bumps a monotonic `gen`. A discrete nudge
  (`/move_forward?ms=400`) captures `gen` when it arms its auto-stop timer; when
  the timer fires it stops **only if `gen` is unchanged**. So a newer `/drive` or
  joystick command (which bumped `gen`) is never cancelled by an older nudge's
  timer. `/stop` and `/estop` bump `gen`, invalidating every pending timer.
- **Continuous-motion watchdog (mandatory, internal to the drive path).** A
  single ~20Hz ticker stops the wheels if the rover is under a *continuous* lease
  (`/drive` or joystick) and no motion command has refreshed it within `TTL`
  (~500ms). The refresh is fed by the **internal** `setDrive()` method — so it
  works identically whether the source is HTTP `/drive` or the joystick loop
  (which commands every 25Hz tick, well inside TTL). Discrete nudges and an
  explicit `/stop` are *not* watchdog-managed (they self-stop / are zero). This
  makes "browser tab dies mid-hold" safe without auto-stopping a gamepad driver.
- `/estop` latches: wheels + gimbal halt and stay stopped until an explicit new
  motion command (mirrors the joystick Back-button latch).

## Joystick (gamepad plugged into the Pi, read by the controller)

Read `/dev/input/js0` directly — the Linux **joystick API** emits fixed 8-byte
little-endian events: `time u32 [0:4]`, **`value i16 (signed) [4:6]`**,
`type u8 [6]`, `number u8 [7]`. No external dep; the parser is unit-testable from
raw bytes (incl. events split across reads, signed-negative values, and the
**`JS_EVENT_INIT` (type & 0x80)** synthetic burst the kernel sends on open —
masked off; we seed initial state from it but treat it as non-edge).

**Two goroutines** (this is the error-prone part the port must get right):
1. a **reader** that blocks on `js0` and writes axis/button state into a shared,
   mutex-guarded struct;
2. a **25Hz ticker** that applies the deadzone + slew-rate ramp and commands
   `setDrive()`/`setCamera()` every tick — because a held-steady stick emits *no*
   new events, the ramp loop (ported from `rover_joystick.py:182-195`) must run
   on a fixed `dt`, not per-event. The ticker's `setDrive()` also refreshes the
   motion watchdog (above), so gamepad-only driving is never auto-stopped.

Mapping ported from `rover_joystick.py` (left stick drive+steer → differential;
right stick pan/tilt; A stop; Back e-stop latch; X/LB head/base light; Y center;
B snapshot; L3/R3 relax/lock; D-pad speed cap; Start ignored). **Caveat:** raw
`js0` index numbering is the kernel/joydev layout, which differs from the
SDL/pygame indices in `rover_joystick.py` (triggers/right-stick axes and the
D-pad-as-hat-vs-axis often differ). A **`-gamepad-debug`** flag prints live raw
indices to re-derive the mapping on the actual pad (mirrors the Python
`--debug`); constants will likely need remapping, not a blind copy. Degrades
gracefully: no `js0` → log once, run HTTP-only, `/healthz` `gamepad:false`.

## Built-in web UI client (served at `/`)

Single embedded HTML page: directional drive buttons (hold-to-move via `/drive`
keepalive, release → `/stop`), camera pad, light toggles, speed slider, a big
`<img src="/video_feed">` live view, a Snapshot button, and a photo gallery with
delete + the auto-refresh poller from rovercam. This is the default "rover
client / user interface."

## Boot autostart (replaces the stock @reboot jobs)

The rover's `crontab` currently has three `@reboot` lines: stock `app.py`,
Jupyter, and `rover_web.py`. Deploy step (documented, manual — we don't mutate
the rover from CI/Mac):
- Add a `rovercontrol.service` systemd unit (or a single `@reboot` line) that
  runs the binary with `-photos /home/ws/robot/photos`.
- **Remove the `app.py` and `rover_web.py` `@reboot` lines** so they don't fight
  for the camera/serial. (Jupyter stays.) `app.py` itself stays on disk.

## Deliverables

```
rovercontrol/
  rovercontrol.go        the single controller source (package main)
  go.mod / go.sum        one dep: golang.org/x/sys/unix
  rovercontrol_test.go   tests (serial encode, splitter, hub, js parser,
                         mapping, HTTP routing, snapshot, traversal)
rovercontrol.service     systemd unit (deploy artifact)
roverctl                 launcher (execs the arm64 binary; replaces roverweb)
docs/reference/app.py-superseded.md   full app.py function/route/command list,
                                      banner-marked SUPERSEDED (don't delete app.py)
docs/plans/004 - Rover controller.md  this plan
```

Build: `cd rovercontrol && GOOS=linux GOARCH=arm64 go build -o ../rovercontrol-arm64 .`
(binary gitignored). Deploy: copy binary + service to the rover, edit crontab.

## Cleanup (per "clean out unused files once done")

**Moved to `graveyard/`** (fully replaced, nothing imports them — kept for
reference, not deleted; see `graveyard/README.md`):
- `rover_web.py`, `tests/test_web.py` (gallery → controller UI/video)
- `roverweb` launcher (→ `roverctl`); `install.sh` updated to match.
- `rover_joystick.py` — kept as the reference gamepad mapping until the Go
  joystick is verified on hardware (raw-`js0` indices differ from SDL's).

**Keep in place, marked SUPERSEDED** (still imported by the LLM chatbot client
we're keeping — `rover_chat.py`/`agent_chat.py` use `rover_direct`/`rover_camera`;
moving them now breaks the chatbot the user wants to keep):
- `rover_direct.py`, `rover_camera.py` — header note: superseded by
  rovercontrol; move to graveyard once the chatbot is repointed to the HTTP API.
- `app.py` (on the rover) — documented as superseded, not deleted.

**Out of scope (noted):** repointing the LLM chatbot (`rover_chat.py`,
`chatbot.py`, `rover_client.py`) to the controller's HTTP API — a follow-up; the
chatbot is a *client* and stays.

## Testing

Go, runnable on the Mac with no rover/camera/gamepad (hardware faked via
interfaces — serial writes captured to a buffer; frame source injected):
- serial: each command encodes the exact expected JSON + clamps (drive ±0.5,
  pan/tilt limits, lights 0..255, e-stop = stop-then-T0, gimbal module init).
- MJPEG splitter + hub: reuse rovercam's tests.
- joystick: js_event byte parser (split across reads; **signed-negative
  values**; **`JS_EVENT_INIT` 0x80 masking**); stick→differential mapping with
  deadzone + slew-rate limit; the 25Hz ramp loop commands drive **every tick
  even with no new events**; button edges.
- movement arbitration & watchdog (the dangerous cases, per both reviewers):
  - a stale nudge timer does **not** stop a newer `/drive`/joystick command
    (generation token honored);
  - the watchdog does **not** trip during steady joystick driving (refresh via
    internal `setDrive`), and **does** stop after a dropped `/drive` keepalive;
  - `/stop` and `/estop` invalidate all pending nudge timers/leases;
  - joystick ↔ HTTP racing through the shared MovementController stays consistent.
- HTTP: path routing hits the right hardware call; `/drive` clamps and rejects
  malformed numbers; snapshot writes one valid JPEG collision-safe + returns
  JSON; traversal rejected (`..`, `%2f`); `/video_feed` multipart headers;
  `/healthz` reflects serial/camera up-state.

Plus manual deploy smoke checks on the Pi (documented): drive/lights/camera via
`curl`, live view in two browsers, gamepad, reboot brings the controller up and
the stock app stays down.

## Risks

- **Serial reliability without a full serial lib:** mitigated by `x/sys/unix`
  termios (the correct primitive) and write-only operation. If termios proves
  fiddly under review, fall back to `go.bug.st/serial` (still one source file).
- **One process owns the serial AND the camera:** so the stock `app.py` and the
  direct Python tools must not run alongside it (same exclusivity as today).
  Deploy disables the stock @reboot jobs.
- **Camera-busy must not be masked (resolves B1).** The folded-in rovercam
  `runCamera` retries `rpicam-vid` with 2→30s backoff, which would silently loop
  forever if the CSI sensor is held by Picamera2 (stock app.py). The controller
  must instead: track the camera's last error, expose `camera.up=false` + the
  reason in `/healthz`, serve a visible **"camera unavailable"** placeholder
  frame in the UI (not a hung `<img>`), and log the busy condition distinctly
  (once, not every retry). Same pattern surfaces serial-busy.
- **Joystick device variance:** axis/button indices match the Xbox-style pad
  rover_joystick.py targets; a `-gamepad-debug` flag prints live indices to
  remap, mirroring the Python `--debug`.
- **No-camera / no-gamepad rover:** both degrade gracefully (HTTP control still
  works; video shows a placeholder; gamepad optional).
- **Watchdog vs. latency:** keepalive timeout tuned so normal UI/joystick use
  never trips it; disabled for discrete nudges (which self-stop).

## Stages (standing workflow)

1. **Plan** — this document.
2. **Plan review gate** — Opus + GPT-5.5 (codex); resolve blockers.
3. **Implementation** — `rovercontrol.go` + web UI + app.py reference doc.
4. **Testing** — the Go suite above.
5. **Code review gate (3-way)** — Opus + codex + glm-5.1 (opencode).
6. **PR & merge** — open PR; do the cleanup; document deploy/boot.
