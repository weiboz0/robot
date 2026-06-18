# 006 — Joystick full button bindings + instant-stop

## Problem

The Pi-side gamepad binds most actions, but four useful inputs are unused
(**LT, RT, Start, D-pad ←/→**), and the user wants every useful input bound plus
a prominent **instant-stop** (extra panic button) — relevant since the rover
shares space with a cat.

Existing bindings (plan 004): L stick drive, R stick camera, A stop, Back e-stop,
B snapshot, X head light, LB base light, Y center camera, L3/R3 relax/lock
gimbal, RB turbo, D-pad ↑/↓ speed cap.

## Goal

Add four controls, configurable + calibrate-able like the rest, with **no
regression** and **backward-compatible** with existing `gamepad.json`:

| Input | New action |
|---|---|
| **LT (hold)** | precision/slow mode — cap speed low while held |
| **RT (hold)** | boost — max speed while held |
| **Start** | **instant E-STOP** (rising-edge → same latching `doEstop()` as Back) |
| **D-pad ← / →** | fine camera **pan** nudge left/right (~10°/press) |

## Key design decision — new controls default DISABLED, enabled by calibration

Both plan reviewers caught that **guessing default indices collides** with
existing bindings (the historical default already uses button 6=Back/Estop,
9=L3/Relax, etc.) and that **LT/RT are axes, not buttons** on standard pads, and
that a plain `int` field can't tell "button A (0)" from "unset". And the real raw
joydev layout of *this* pad is unverified (no `-gamepad-debug` run yet).

So the four new controls **default to `none` (disabled)** and are turned on by the
one-time `-calibrate` — which the user must run anyway to fix the existing
mapping. This makes defaults **collision-free and backward-safe** (old configs get
`none` for the new fields → no surprise behavior) instead of guessing. After
calibration, `/healthz` `mapping:"config"` and all four work.

### Mapping additions

```go
// ControlMap: a button, a held trigger-axis, or disabled. Distinguishes
// "unset/none" from "button 0" (which a bare int cannot).
type ControlMap struct {
    Kind  string  `json:"kind"`            // "button" | "axis" | "none"
    Index int     `json:"index,omitempty"` // Kind=="button"
    Axis  AxisMap `json:"axis,omitempty"`  // Kind=="axis" (trigger held past +0.5)
}
func (c ControlMap) held(st gpState) bool { /* button→pressed; axis→axisSigned>0.5 */ }
```

`GamepadMapping` gains: `Precision ControlMap`, `Boost ControlMap`,
`PanicStop ControlMap`, and `HatX HatMap` (horizontal D-pad → camera pan; reuses
the existing `HatMap` axis/buttons/none machinery, with `hatDirection` returning
+1 = **right**). `defaultMapping()` sets all four to **disabled** (`Kind:"none"`).
**Every existing field/sign is unchanged** — the `defaultMapping` pin test is
extended with the four new `none` entries, not altered.

### Loading / validation

- `loadMapping` unchanged in shape; old `gamepad.json` lacking the new fields →
  unmarshal-over-defaults fills them with `none` (disabled) → safe.
- `validate()` extends: `ControlMap.Kind` ∈ {button,axis,none}; button `Index≥0`;
  `HatX` validated like `Hat`. Plus a **duplicate-enabled-button warning**
  (logged, non-fatal) so a calibration that doubles two actions onto one button
  is visible in the log without rejecting intentional configs.

### Behavior (`computeJoystick` + a pure top-speed helper)

- `computeJoystick` gains `precision`/`boost` (held via `ControlMap.held`), folds
  `PanicStop` into e-stop as a **rising edge** with its **own** `prev.panic`
  slot: `a.estop = edge(m.Estop) || panicEdge`. Pan nudge: `hatDirection(m.HatX,…)`
  rising-edge via a **separate** `prev.hatX` slot (never shares `prev.hat`).
- Top-speed precedence as a **pure, unit-tested helper**
  `topSpeed(idx, turbo, boost, precision)`: start at `speedSteps[idx]`, apply
  `turbo`, then `boost` (→ `speedLimit`), then `precision` **last** so it always
  wins (`min(top, precisionCap≈0.15)`). `joystickLoop` calls it.
- A `panNudge` (±1) → `aim.nudge(panNudge*FINE_DEG, 0)`, FINE_DEG≈10, one nudge
  per press (rising edge). Disabled controls (`none`) are simply never held/fired.

### Calibrate

`-calibrate` gains skippable prompts for the four new controls, reusing the
wait-neutral/threshold/debounce capture:
- Precision / Boost / Instant-stop: a generic `captureControl` that detects a
  **button press OR a trigger-axis** crossing threshold → returns the right
  `ControlMap` (handles triggers-as-axes). Skip → stays `none`.
- D-pad ←/→: `captureHat`-style left/right detection → `HatX` (axis or buttons).

## Deliverables

- `rovercontrol/rovercontrol.go` — `ControlMap`, mapping fields, `topSpeed`
  helper, `computeJoystick`/loop wiring, `validate` extension, `captureControl` +
  calibrate prompts.
- `rovercontrol/*_test.go` — extend `defaultMapping` pin (new `none` fields);
  `ControlMap.held` (button/axis/none); `topSpeed` precedence (precision wins
  over boost+turbo; both-held); panic folds into estop (distinct slot);
  pan-nudge rising-edge via `HatX`; **backward-compat: an old config without the
  new fields loads them as `none`/disabled** (the safe default).
- `docs/plans/006 - …` — this plan.

## Testing

Unit (Mac, no pad): the above. `ci-local.sh` green (coverage floor holds).
Hardware smoke (user-operated, **cat clear**, per the standing no-move rule):
`-calibrate` enables the four; verify LT slows, RT boosts, Start instant-stops,
D-pad ←/→ nudges the camera; `/healthz mapping:"config"`.

## Risks

- New controls inert until calibrated — intentional (safer than guessing); the
  user calibrates once for everything. Documented in the UI/notes.
- `ControlMap` JSON shape must round-trip; covered by tests.
- Trigger polarity is pad-specific; captured via calibration (threshold), not
  guessed.

## Stages (autopilot-plan skill)

1. Plan — this doc. 2. Plan-review gate (Opus + codex) — recorded below.
3. Implement. 4. Tests. 5. Code-review gate (3-way). 6. ci-local → PR → merge
(AUTO_MERGE) → deploy (no motion; calibration is user-run). 7. Post-exec report.

## Reviews

### Plan review (2-way) — both REQUEST-CHANGES → resolved in this revision

- **Opus & codex** — REQUEST-CHANGES, aligned blocking items: (1) proposed
  default indices (Precision=6, Boost=7, PanicStop=9) **collide** with existing
  Estop=6 / Start=7 / Relax=9 and violate the no-regression pin; (2) backward-
  compat would inject colliding defaults into old configs; (3) a bare `int`
  can't distinguish "none" from button 0; (4) LT/RT are axes, not buttons; (5)
  `validate` needs new-field + duplicate checks. Non-blocking: precision-last
  precedence ordering, `HatX` own edge slot, calibrate left/right wording,
  factor a testable top-speed helper, state Start is rising-edge into `doEstop`.
- **Resolved:** new controls modeled as `ControlMap{button|axis|none}` (handles
  the 0-vs-none ambiguity + triggers-as-axes), **defaulted to `none`/disabled**
  (collision-free, backward-safe) and enabled via `-calibrate`; `defaultMapping`
  pin extended (existing unchanged); `validate` extended + duplicate-button
  warning; `topSpeed` is a pure helper with precision-wins precedence; `HatX`/
  panic get their own edge slots; calibrate adds skippable prompts via a generic
  `captureControl`; backward-compat test asserts the safe `none` default.

### Code review (3-way) — Opus + codex + glm all APPROVE

All seven plan-resolved blocking items verified correct: `ControlMap` defaulted
to `none` (no colliding indices, pin test extended not altered, existing fields
byte-for-byte unchanged), backward-compat (old config loads new fields disabled),
`held()` button/axis/none, `topSpeed` precision-wins precedence (pure helper),
`PanicStop`/`HatX` own edge slots (no cross-talk), `validate` extended + dup
warning, calibrate `captureControl`/`HatX` skippable. No regression to the
existing loop. Opus's one non-blocking note (fold enabled `ControlMap` buttons
into the dup-warning) was applied. Coverage 72.3%, race-clean.

## Post-execution report

**Shipped (PR #12, merged):** four optional gamepad controls — LT precision/slow,
RT boost, Start instant-e-stop, D-pad ←/→ fine camera pan — via a new
`ControlMap{Kind:button|axis|none}`, all default-disabled and enabled by
`-calibrate`. `topSpeed` pure helper with precision-wins precedence; `PanicStop`
and `HatX` own edge slots; `validate` extended + duplicate-button warning;
calibrate gained `captureControl`/`HatX` prompts. Coverage 72.3%.

**Deviations:** the big one came from plan review — the *first* draft guessed
default button indices that **collided** with existing bindings (Estop=6,
Relax=9) and mismodeled triggers as buttons. Both reviewers caught it; the
resolution (disabled-by-default `ControlMap`, enabled via calibration) is safer
and is why the four controls don't work until you run `-calibrate`.

**Tradeoffs:** the new controls are inert until calibrated — deliberate, to avoid
silently mis-mapping a pad we haven't probed. Since calibration is needed anyway
to fix the *existing* mapping, one `-calibrate` run enables everything.

**Deploy result (2026-06-18):** rebuilt arm64, rsynced (checksum match),
restarted; `/healthz` up (serial/camera/gamepad), no behavior change (new
controls disabled). **No motion commands were sent** — per the standing cat-safety
rule. **Open acceptance step (needs a human + the cat clear):** run
`rovercontrol-arm64 -calibrate` on the rover, do the new prompts (precision/boost
triggers, instant-stop button, D-pad left/right), restart, and verify LT slows,
RT boosts, Start instant-stops, and D-pad ←/→ nudges the camera.
