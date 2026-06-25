# 012 - Idle gamepad yields to HTTP /drive

## Problem

With a gamepad plugged into the Pi, the chatbot (and any HTTP `/drive`) can't drive the
rover: `joystickLoop` re-commands `app.move.setDrive(left, right)` **every 25 Hz tick even
when the stick is idle** (it sends `setDrive(0,0)`), so an HTTP `/drive` is overridden ~40 ms
later. Camera/lights work (edge-triggered); only continuous drive is stomped.

## Goal

An **idle gamepad stays silent** so HTTP `/drive` (chatbot) gets through, while an
**active gamepad still fully drives** (and takes priority while in use). Releasing the stick
sends exactly one stop, then goes quiet.

## Design (rovercontrol.go, joystickLoop)

Add a tiny pure helper and gate the `setDrive` call:

```go
// driveGate: command drive while the stick is active (or ramping to a stop), then
// go silent so HTTP /drive isn't overridden by an idle gamepad. Emits one final
// stop on the active→idle transition (via wasActive).
func driveGate(tgtL, tgtR, curL, curR float64, wasActive bool) (emit, active bool) {
    active = tgtL != 0 || tgtR != 0 || curL != 0 || curR != 0
    return active || wasActive, active
}
```

In the loop (replacing the unconditional `app.move.setDrive(left, right)`):

```go
emit, active := driveGate(tgtL, tgtR, left, right, wasActive)
if emit {
    app.move.setDrive(left, right)
}
wasActive = active
```

`wasActive` is a `bool` declared before the loop. Behaviour:
- **Idle** (tgt 0, cur 0, !wasActive) → no `setDrive` → HTTP `/drive` flows through.
- **Stick pushed** → `setDrive` every tick (overrides HTTP — physical priority, as expected).
- **Release** → ramps to 0 over a few ticks (each emits), final tick emits `setDrive(0,0)`
  once (wasActive), then silent. The motion watchdog already backstops a true stop.
- estop / A-stop / camera / lights unchanged (separate, edge-triggered paths).

No change to HTTP handlers, the watchdog, or the Movement arbitration itself.

## Estop latch interaction (codex)

The `Movement.estopped` latch is released by a **zero** drive command (`applyDrive(0,0)`)
or `stop()`. Today the unconditional idle `setDrive(0,0)` clears it almost immediately, so a
gamepad estop is effectively momentary. **With the gate, an estop pressed while the stick is
idle stays latched** (no idle zero-emit) — a stronger, safer estop. Release paths remain:
the **A button** (`app.move.stop()`), **push-then-release the drive stick** (emits one zero
on release → clears), or **HTTP `/stop` / `$stop`**. An estop pressed while *driving* still
clears on recenter (the ramp-to-zero emits the releasing zero), unchanged. Documented in the
controls help.

## Tests (rovercontrol_test.go)

`TestDriveGate` (pure): idle→no emit; active→emit; ramp-down (tgt 0, cur≠0)→emit;
active→idle transition→emit once (final stop); steady-idle→no emit. The idle→no-emit case
also pins the estop-stays-latched behavior (no zero is sent at idle). Existing
`Movement` estop tests cover the latch itself (unchanged).

## Risks / tradeoffs

- While the gamepad is **actively driving**, HTTP `/drive` is still overridden — intended
  (physical control wins). The two only coexist when the stick is idle.
- A released stick relies on the final `setDrive(0,0)` + the 500 ms motion watchdog to stop;
  both already exist.
- Deploy: rebuild `rovercontrol-arm64` + redeploy (this binary also finally carries the
  640×480 lag/stall fix from main).

---

## Plan review

**Gate: PASSED** (Opus + codex; glm not part of plan gate).

**codex (GPT-5.5) — no blocking.** Confirmed the gate keeps emitting while a stick is held
and while ramping down, emits exactly one final stop on release, and never goes silent on a
held stick (since `rampToward` snaps to target within a step). Nits incorporated: the estop
latch now persists on an idle estop (safer; release paths documented) and is pinned by the
idle→no-emit test + the existing `Movement` estop tests; "active includes ramp-down" is
deliberate so HTTP can't interrupt a physical release before the stop is commanded.

**Opus (Claude):** verified the change is confined to `joystickLoop` (no change to `Movement`
arbitration, the watchdog, or HTTP handlers), and that physical-priority-while-active is the
right default.

## Code review

**Gate: PASSED.** Opus + codex (glm not installed → 2-way). **codex — no blocking:** held
stick keeps emitting, ramp-down keeps emitting, `wasActive` adds exactly one final zero on
the first idle tick then silences; no estop/watchdog regression. Nit (carry-`wasActive`
sequence test) added as `TestDriveGateReleaseSequence`.

## Post-execution report

**Branch:** `fix/joystick-idle-yields-to-http`.

### What shipped
- `rovercontrol.go`: `driveGate()` + gated `setDrive` in `joystickLoop` — an **idle gamepad
  no longer commands drive every tick**, so HTTP `/drive` (the chatbot) gets through; an
  active stick still drives and takes priority; one final stop on release.
- Estop pressed while idle now **stays latched** (no idle zero to clear it) — safer; release
  via A-button / push-release the stick / HTTP `$stop`.
- Tests: `TestDriveGate` + `TestDriveGateReleaseSequence`. `ci-local.sh` PASS (74.8% cov,
  arm64 cross-compile, 45 Python tests).

### Deploy
Rebuilt `rovercontrol-arm64` (also carries the 640×480 lag/stall fix from main), scp'd to the
rover, restarted `rovercontrol`. Verified up on `:8080` with gamepad + serial. **Result:**
the joystick drives when used, and the chatbot drives when the stick is idle.
