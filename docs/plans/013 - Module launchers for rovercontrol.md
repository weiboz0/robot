# 013 - Module launchers for rovercontrol (choose what runs)

## Goal

Run subsets of the controller — you don't always want camera + joystick + control at once.
Keep the single binary (serial features must share one process), make each module toggleable
by flag, and ship small **launcher scripts** to pick a mode.

## Decision (user)

Launcher scripts + flags on the one binary (not a real binary split).

## Constraint (recap)

Serial is single-owner: drive + lights + gimbal + joystick + the HTTP control API stay in one
process. The camera (`/dev/video0`) is the only separable device. The launchers are
**mutually exclusive modes** of the one binary (they share `:8080` + the hardware) — run one
at a time.

## Design

### Go (`rovercontrol.go`) — make each module skippable

- **Camera off:** `resolveCameraMode` accepts `"off"`; `main` starts `app.cam.run` only when
  `mode != "off"`. (`/video_feed` then serves the placeholder; `/snapshot` 503s.)
- **Serial off:** `main` starts `openSerialWithRetry` only when `-serial` is non-empty.
  (Control endpoints 503 via `requireSerial`; camera/web still serve.)
- **Joystick off:** already supported (`-gamepad ''`).

All three already degrade cleanly (HTTP handlers guard on serial; videoFeed has a placeholder).
**Keep `app.cam` non-nil** when camera is off (only skip the goroutine) — `/healthz` and
`/video_feed` call `app.cam.status()`. **Mark disabled modules** with status `err:"disabled"`
(codex) so `/healthz` distinguishes "off on purpose" from "broken": `app.cam.setStatus(false,
"disabled")` and `rover.setStatus(nil, "disabled")` when skipped.

### Launcher scripts (bash, mirroring the existing `roverctl`)

- `roverctl` — **everything** (camera + joystick + control). *(existing; unchanged)*
- `rovercam` — **camera only**: `-gamepad '' -serial ''`
- `rovernojoy` — **no joystick** (camera + control; chatbot/HTTP owns drive): `-gamepad ''`
- `rovernocam` — **no camera** (joystick + control): `-camera-mode off`

Each resolves its own dir and execs `rovercontrol-arm64`, passing extra args **before** the
fixed mode flags so the mode wins (codex): `exec "$BIN" -photos "$DIR/photos" "$@" <mode-flags>`
(Go's flag parser takes the last value, so a trailing `-gamepad ''` can't be overridden by the
user's `"$@"`).

## Tests

- `TestResolveCameraModeOff`: `resolveCameraMode("off", ...)` returns `"off"` (and existing
  cases unchanged). The `main` goroutine gating is a one-line guard (not unit-tested; verified
  by deploy + `/healthz`).

## Risks / tradeoffs

- Modes are mutually exclusive (one binary, one `:8080`) — can't run rovercam and rovernocam
  *simultaneously* (that's the device-split option B, declined).
- `-serial ''` leaves control endpoints returning 503 — intended for camera-only.
- Deploy: rebuild + redeploy the binary and copy the new launchers to the rover; the `@reboot`
  cron keeps pointing at whichever launcher/flags you choose as default (`roverctl` today).

---

## Plan review

**Gate: PASSED** (Opus + codex; glm not part of plan gate). codex confirmed the approach and
flagged: `resolveCameraMode("off")` must return `"off"` explicitly (else it defaults to
rpicam) — planned; keep `app.cam` non-nil and only skip the goroutine — planned; guard
`openSerialWithRetry` in `main` so `-serial ''` doesn't retry forever — planned. Refinements
folded in: mark disabled modules `err:"disabled"` in `/healthz`; put mode flags last in the
launchers so they're authoritative; `rovercam` sets both `-gamepad ''` and `-serial ''`.

## Code review

**Gate: PASSED.** Opus + codex (glm not installed). **codex — no blocking:** camera-off,
serial-off, and joystick-off paths are all nil-safe (videoFeed → placeholder, requireSerial →
503, healthz reads status, watchdog/gamepad go through `Rover.send` which returns "serial
unavailable" not a nil deref); launchers correct. Nit: the "mode flags win" comment holds for
flag-only args (not if `$@` has `--`/positional) — fine for this app.

## Post-execution report

**Branch:** `feature/rovercontrol-module-launchers`.

### What shipped
- `rovercontrol.go`: `-camera-mode off` and `-serial ''` now skip those modules (goroutine
  not started; status set to `disabled` for `/healthz` clarity); `-gamepad ''` already skips
  the joystick. `resolveCameraMode` returns `"off"` explicitly.
- Launchers: **`rovercam`** (camera only), **`rovernojoy`** (no joystick — chatbot/HTTP owns
  drive), **`rovernocam`** (no camera); `roverctl` unchanged = everything. Mode flags last so
  they're authoritative.
- Tests: `TestResolveCameraMode` extended with `off`. `ci-local.sh` PASS (74.3% cov, arm64
  cross-compile, 45 Python tests).

### Deploy
Rebuild + scp `rovercontrol-arm64`, scp the new launchers to `~/robot`, restart with the
desired launcher. `roverctl` stays the default (and the `@reboot` cron target).
