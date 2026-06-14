# 004 — Pi gamepad calibration & remappable mapping

## Problem

`rovercontrol` reads a gamepad plugged into the Pi (`/dev/input/js0`) and maps
axes/buttons with **hard-coded constants ported from `rover_joystick.py`**, which
used SDL/pygame indices. Raw Linux **joydev** numbering differs (right-stick
axes, triggers, and especially the D-pad — hat-as-axis vs. axis-pair vs. buttons
— commonly land elsewhere), so on real hardware the sticks/buttons may be
**mis-mapped or reversed**, and the only fix today is to recompile. `gamepad:true`
in `/healthz` confirms a pad is read, but nothing verifies the mapping is right.

Known gap from plan 002 ("raw `js0` indices differ; constants will likely need
remapping").

## Goal

Make the on-rover joystick **reliable and remappable without recompiling**:
1. Load the mapping from a **config file** (default = today's constants → zero
   behavior change with no config), so a wrong/reversed pad is fixed by editing
   JSON, not Go.
2. A **guided `-calibrate` mode** that records the real indices **and signs**
   and writes the config.
3. Verify on the actual rover hardware.

## Design (single file, minimal)

### Mapping = indices **+ signs** only (not tuning)

`deadzone`, `ramp`, `panRate`, `tiltRate`, `turbo`, `speedSteps` stay as **code
constants** — they're behavioral tuning independent of pad identity, and mixing
them into calibration is scope creep (reviewers N1). The mapping carries only
what identifies the pad's controls:

```go
type AxisMap struct{ Index int; Invert bool } // Invert: stick-up/right => +1
type GamepadMapping struct {
    Throttle, Steer, Pan, Tilt AxisMap         // sticks (sign matters!)
    Stop, Estop, HeadLight, BaseLight,
    Center, Snapshot, Relax, Lock int          // button indices
    Hat HatMap                                 // D-pad → speed cap
}
// HatMap models the three real D-pad shapes (reviewers B1/#3):
type HatMap struct {
    Kind string  // "axis" | "buttons" | "none"
    Axis AxisMap // Kind=="axis": vertical axis (default: index 7, up => +1)
    Up, Down int // Kind=="buttons": the two button indices
}
```

`defaultMapping()` reproduces **exactly** today's constants, incl. the signs the
current loop applies — throttle `= -axis(LY)` → `{1, Invert:true}`, steer
`= axis(LX)` → `{0,false}`, pan `= axis(RX)` → `{3,false}`, tilt `= -axis(RY)`
→ `{4, Invert:true}`, buttons A/B/X/Y/LB/L3/R3 as today, **Hat = {Kind:"axis",
Axis:{7,Invert:true}}** so `up => +1` (`hatDirection` computes `-axis(7)`, so the
current `axis(7) < -0.5 → +1` is preserved).
So **no-config behavior is byte-for-byte unchanged.**

### Loading (missing → default; malformed → refuse, don't silently mis-drive)

`loadMapping(path)` (reviewers' key blocking item — a bad mapping must not
silently drive with wrong controls):
- **File missing** → `defaultMapping()`, logged `mapping: default`.
- **File present** → parse, then **validate** (all indices ≥ 0 and within the
  pad's axis/button counts when known; Hat.Kind valid). On parse error or
  validation failure → **do NOT fall back**: disable the gamepad, log loudly, and
  surface it in `/healthz` (`gamepad:false`, `gamepad_err:"…"`). The operator
  fixes the file rather than unknowingly driving reversed.
- **Valid** → use it, logged `mapping: config <path>`.
- Partial JSON is handled by unmarshalling **over** a `defaultMapping()` base, so
  omitted fields keep their default (not zero), then validate.

### Loop refactor + test seam

Extract the per-tick decision into a **pure function**:
`stepJoystick(m *GamepadMapping, st gamepadState, prev *edgeState) joystickActions`
returning the intended drive (l,r targets pre-slew), camera delta, and button
edges — computed from the mapping + a snapshot of axis/button state. `joystickLoop`
becomes: read state → `stepJoystick` → apply slew/rates → command hardware. This
makes the mapping **unit-testable without hardware** (reviewers N2): a test sets
`Throttle={9,false}`, feeds a synthetic state with axis 9 deflected, and asserts
the wheels move — proving indices+signs are honored, not hardcoded. The existing
`axLX`/`btnA` package constants become the field values of `defaultMapping()`
(tests referencing them are updated).

### `-calibrate` (guided, over SSH)

Opens the pad and, per control, prompts on stdout and records the input
(reviewers B2/B3/#2/#3):
- **Drain `JS_EVENT_INIT` (0x80)** events first and ignore them throughout (as
  `runGamepadDebug` already does).
- **Require neutral** between steps (wait until all axes ~0 / buttons released).
- **Axis steps** ("move LEFT stick UP"): wait for an axis whose `|value|` crosses
  a threshold (>0.7 full-scale), pick the axis that moved **most**, and record its
  **index + sign** (so "up" maps to +1 regardless of the pad's polarity).
- **Button steps** ("press STOP"): debounce; record the pressed button index.
- **D-pad step**: detect whether the up/down input arrives as an **axis** (→
  `Hat.Kind="axis"` + sign) or as **buttons** (→ `Hat.Kind="buttons"`); allow
  **skip** (Enter) → `Hat.Kind="none"` (speed-cap-by-pad disabled, not a
  dead-end).
- Write `gamepad.json` **atomically** (temp + rename, like `snapshot`) and print
  the absolute path. `-calibrate` then exits.

### Flags / paths / health

- `-gamepad-map <path>` default **`<exe-dir>/gamepad.json`** (consistent with
  `-photos`'s exe-dir default; on the rover that is `~/robot/gamepad.json`, so the
  goal/design paths agree — fixes the reviewers' path-inconsistency note).
- `-calibrate` (bool).
- `/healthz` gains `mapping: "default"|"config"|"invalid"` (+ path/err) —
  promoted to in-scope (reviewers N5): the cheapest confirmation the edit loaded.

## Deliverables

- `rovercontrol/rovercontrol.go` — mapping types + `defaultMapping` + `loadMapping`
  + `stepJoystick` refactor + `-calibrate` + flags + `/healthz` field.
- `rovercontrol/rovercontrol_test.go` (or a new `_cov`-style file) — tests below.
- `docs/plans/004 - …` — this plan.

## Testing

Unit (Mac, no pad):
- **`defaultMapping()` equals today's constants — every field** incl. Hat axis 7
  and all signs (this is the no-regression pin; reviewers N6).
- `loadMapping`: missing → default; malformed/parse-error → **invalid (gamepad
  disabled, error surfaced)**, NOT default; partial-but-valid → defaults fill
  omitted fields; fully-valid → used.
- `stepJoystick`: a remapped throttle index drives the expected wheels; an
  inverted axis reverses correctly; button edges fire once; Hat in each Kind
  (axis/buttons/none) changes the speed cap as expected.
- `runGamepad` disconnect still stops the rover (existing test pattern).
- `ci-local.sh` green (coverage floor holds).

Hardware smoke (on the rover — the real acceptance, since the bug is
hardware-shaped): `rovercontrol-arm64 -calibrate`, restart, confirm left stick
drives the right way, right stick aims, each button acts, and D-pad changes the
speed cap; `/healthz` shows `mapping:"config"`.

## Risks

- No physical pad on the dev machine: the deliverable is the **mechanism** +
  on-rover calibration; build/logic verified on Mac, correctness verified on the
  Pi at deploy.
- Calibrate is a terminal wizard on the rover (a web calibrator is a later
  option) — documented.
- The malformed-config-disables-gamepad choice means a bad edit stops the
  joystick (HTTP control still works); `/healthz` says why — preferred over
  silently driving reversed.

## Stages (autopilot-plan skill)

1. Plan — this doc. 2. Plan-review gate (Opus + codex) — recorded below.
3. Implement on a local branch. 4. Tests. 5. Code-review gate (3-way:
Opus + codex + glm). 6. `ci-local.sh` → PR → merge (AUTO_MERGE) → deploy +
on-rover calibrate/verify. 7. Post-execution report.

## Reviews

### Plan review (2-way) — both REQUEST-CHANGES → resolved in this revision

- **Opus** — REQUEST-CHANGES. Blocking: (B1) D-pad/hat axis-7 must be pinned in
  the default + handle hat-as-button pads; (B2) calibrate must mask `JS_EVENT_INIT`;
  (B3) threshold-based axis discrimination. Non-blocking: indices-only mapping
  (not deadzone/rates), injectable mapping seam, unmarshal-over-defaults, atomic
  write + skip, `/healthz` source, pin `defaultMapping` test.
- **codex** — REQUEST-CHANGES. Blocking: malformed config must **not** silently
  default (disable/refuse); mapping needs **sign/inversion** fields, not indices
  only; D-pad shape underspecified. Non-blocking: neutral-between-prompts +
  debounce, path inconsistency, factor a testable per-tick function, keep
  deadzone/rates out of JSON.
- **Resolved:** mapping carries index **+ sign**; `HatMap` models axis/buttons/none;
  malformed config disables the gamepad and surfaces the error (no silent
  default); calibrate masks init events, requires neutral, uses a threshold +
  most-moved axis, debounces, allows skip, writes atomically; `stepJoystick` is
  the testable seam; `defaultMapping` pinned by test; deadzone/rates stay code
  constants; default path unified to `<exe-dir>/gamepad.json`; `/healthz` reports
  the mapping source.

### Code review (3-way) — Opus + glm APPROVE, codex REQUEST-CHANGES → resolved

- **Opus — APPROVE.** Verified `defaultMapping` reproduces the old mapping +
  signs exactly (incl. hat axis 7 Invert), `loadMapping` refuses-not-defaults,
  `computeJoystick` edges/signs correct, calibrate goroutine/atomic-write sound.
  Caught a plan-prose error (hat `{7,false}` vs code `{7,true}`) — fixed.
- **glm — APPROVE.** Field-by-field default match confirmed; no blocking.
- **codex — REQUEST-CHANGES (1 blocking) → resolved:** calibration printed each
  prompt **before** `waitNeutral()` (250ms drain), so a fast first input was
  swallowed. **Fixed:** `waitNeutral` now runs *before* the prompt in
  `captureAxis`/`captureButton`/`captureHat`; the D-pad DOWN capture passes its
  prompt to `captureButton` (drains the UP release, then prompts). Re-verified
  with codex. Also added the suggested tests (hat-axis Invert assertion; camera
  pan/tilt sign).

## Post-execution report

_(filled in at the end)_
