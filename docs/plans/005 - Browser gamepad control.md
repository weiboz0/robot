# 005 — Browser (Mac-side) gamepad control

## Problem

To drive remotely from the Mac with a controller **while watching the video**,
the operator has only on-screen buttons. A gamepad plugged into the **Mac** can't
drive the rover — the only joystick path is a pad plugged into the **Pi** (plan
004). The browser **Gamepad API** can read a Mac-connected pad with no extra
software.

## Goal

Add gamepad support to the controller's **built-in web UI** so a pad plugged into
the Mac drives the rover over the **existing** HTTP API — no new endpoints, no
serial/Go-core change. Purely a front-end addition to the embedded `htmlPage`.
The browser is just another HTTP client, so it **cannot bypass** the server-side
clamps / movement arbitration / `/drive` watchdog (all enforced in Go).

Grounding (verified): `/drive` is watchdog-armed (`driveCap → setDrive →
applyDrive(continuous=true)`, 500 ms TTL); `/camera_aim` takes **absolute**
pan/tilt and clamps server-side; `driveCap` already multiplies by the server
speed cap and clamps to ±0.5; e-stop latches until a zero command.

## Design (front-end only) — resolving the review findings

### Poll vs. send rate (decouple them)

- **Poll the gamepad at ~20 Hz** (`setInterval` 50 ms) for responsive buttons +
  camera integration. **Re-read `navigator.getGamepads()[i]` every tick** — the
  snapshot is not live; never cache the gamepad object (Opus B2).
- **Drive send is in-flight-guarded and throttled** (resolves the flooding +
  watchdog items): a single `driveInFlight` flag; never issue a new `/drive`
  until the previous resolves (skip the tick if busy). While the stick is
  deflected, send `/drive` **continuously at ~8 Hz even if the value is
  unchanged** — the 500 ms watchdog needs the refresh, so "send only on change"
  is wrong for held motion (both reviewers). When the stick returns to the
  deadzone, send **`/stop` once** (track `wasMoving`) and then idle — don't spam
  zeros.

### Wheel values — normalized, no client scaling

Send the **raw mixed ±1** wheel values; the server's `driveCap` applies the speed
cap. Do **not** also multiply by the speed slider client-side (that would be
cap²). The existing slider keeps POSTing `/speed`. Mix: `throttle = -axisLY`,
`steer = axisLX`, `l = clamp(throttle+steer,-1,1)`, `r = clamp(throttle-steer,-1,1)`.

### Camera — client-side angle integrator → absolute `/camera_aim`

`/camera_aim` is absolute but a stick is a velocity. Track local angles:
`panAngle += axisRX * PAN_RATE * dt` (clamp client-side to −180..180),
`tiltAngle += -axisRY * TILT_RATE * dt` (clamp −45..90); send
`/camera_aim?pan=&tilt=` throttled (~8 Hz, in-flight-guarded) only when the angle
moved beyond a small epsilon. Seed `panAngle=tiltAngle=0` on load; the **Y /
center** button resets both locals to 0 and calls `/camera_center` so client and
server stay in sync (there's no GET-current-aim).

### Buttons (rising edge; standard mapping)

Standard-mapping indices: A=0 → `/stop`, B=1 → `/snapshot`, X=2 → `/light_head`,
Y=3 → center (reset local angles + `/camera_center`), LB=4 → `/light_base`,
Back=8 → `/estop`. Edge-detected (prev-state array). Buttons are fine to send
only on press (not watchdog-relevant).

### Safety (corrected — watchdog is necessary but not sufficient alone)

- The watchdog stops the wheels if `/drive` refreshes stop (tab dies, network
  drops). **But a stale-but-alive poll could keep feeding it**, so: re-read the
  live gamepad each tick (no caching), and deadzone→`/stop`-once so a centered
  stick actively stops rather than relying only on the watchdog.
- `gamepaddisconnected` → send `/stop`, clear pad. `visibilitychange`→hidden and
  `pagehide` → send `/stop` (the unload one via `fetch(..., {keepalive:true})`).
  Backgrounding throttles the timer → drive stops refreshing → watchdog stops the
  rover; this is the desired failsafe.

### UI / browser

- A "🎮 gamepad: <id> / none" indicator via `gamepadconnected`/`disconnected`;
  on-screen buttons keep working alongside it. Note "press a button to activate
  the gamepad" (the API populates after a user gesture).
- **Primary target: Chrome** (Gamepad API on `http://<LAN-IP>` works; Safari is
  stricter). Tolerate `getGamepads()` being absent/empty until activation.

## Deliverables

- `rovercontrol/rovercontrol.go` — extend the embedded `htmlPage` JS only
  (Gamepad API poller + the design above). No other Go change.
- `rovercontrol/rovercontrol_test.go` (or `_cov`) — a Go test asserting the
  served `/` page contains the gamepad markers: `navigator.getGamepads`, the
  `gamepadconnected` handler, the in-flight-guarded `/drive` send, the
  `/camera_aim` integrator, and the deadzone→`/stop`. Guards against the JS
  silently breaking/regressing.
- `docs/plans/005 - …` — this plan.

## Testing

- Unit (Mac): the `GET /` page contains the gamepad poller + throttle/guard +
  integrator + stop-on-center markers; existing controls still present;
  `ci-local.sh` green (coverage floor holds).
- Manual smoke (real acceptance): build, deploy, open
  `http://192.168.1.131:8080` in **Chrome** on the Mac with a gamepad; press a
  button to activate; confirm the indicator shows the pad, left stick drives
  (and **releasing stops** within ~0.5 s), right stick aims, buttons act, and
  that backgrounding the tab / unplugging the pad stops the rover.

## Risks

- Browser variance on plain-http LAN (Chrome OK; Safari unreliable) — documented,
  smoke-tested on the target browser.
- Pi-pad (004) and browser-pad both driving would fight, but funnel through the
  same server-side Movement arbitration (last-command-wins, watchdog-safe) — use
  one at a time; documented.
- JS isn't unit-testable in Go; the marker test prevents silent breakage but the
  browser smoke test is the real check.

## Stages (autopilot-plan skill)

1. Plan — this doc. 2. Plan-review gate (Opus + codex) — recorded below.
3. Implement (embedded JS). 4. Test (marker test). 5. Code-review gate (3-way).
6. `ci-local.sh` → PR → merge (AUTO_MERGE) → deploy + Chrome smoke test.
7. Post-execution report.

## Reviews

### Plan review (2-way) — both REQUEST-CHANGES → resolved in this revision

- **Opus** — REQUEST-CHANGES. Blocking: (1) 20 Hz `fetch` floods → in-flight
  guard + decouple poll/send rate + throttle `/drive`; (2) watchdog doesn't cover
  stale-axis-while-polling → re-read `getGamepads()` each tick + deadzone→stop.
  Non-blocking: camera absolute-aim integrator, speed double-scaling, name Chrome,
  `visibilitychange`→stop, test pins the throttle marker.
- **codex** — REQUEST-CHANGES. Blocking: (1) "send only when changed" breaks the
  watchdog — must refresh `/drive` continuously while deflected; (2) speed
  double-scaling — send normalized values, let server cap apply; (3) `/camera_aim`
  is absolute — integrate locally or use nudge endpoints, don't leave vague.
  Non-blocking: in-flight throttle, browser-variance caveat, expose id/mapping.
- **Resolved:** poll 20 Hz but in-flight-guarded `/drive` at ~8 Hz refreshed
  continuously while deflected (watchdog-fed), `/stop`-once on center; send
  normalized ±1 (no client speed scaling; slider still drives `/speed`); camera
  uses a client angle integrator → absolute `/camera_aim`, Y resets + centers;
  re-read `getGamepads()` each tick; disconnect/visibility/pagehide → `/stop`;
  Chrome primary; marker test pins the guard/integrator/stop.

### Code review (3-way) — Opus + glm APPROVE, codex REQUEST-CHANGES → resolved

- **Opus — APPROVE.** Verified all five resolved blocking items are correctly
  implemented (in-flight-guarded + watchdog-fed `/drive`, re-read `getGamepads()`,
  normalized values, camera angle integrator, disconnect/hidden/pagehide stops),
  button indices + edge detection correct, no server-safety bypass, no backticks
  introduced, `h.gamepad.up` health fix correct. Non-blocking cosmetic nits only.
- **glm — APPROVE.** No blocking.
- **codex — REQUEST-CHANGES (1 blocking) → resolved:** on `visibilitychange`→
  hidden the page sent `/stop` once but `gpPoll` kept running and could re-drive
  while hidden if the stick stayed deflected. **Fixed:** `gpPoll` now returns
  early when `document.hidden` (never drives while backgrounded) and the handler
  resets `wasMoving`. Re-verified with codex. (Non-blocking dt-vs-fixed-rate note
  left as-is — matches the Go joystick loop's fixed-dt approach.)

## Post-execution report

_(filled in at the end)_
