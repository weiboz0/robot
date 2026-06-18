# 007 — Flexible control mapping (any input / disable for every control)

## Problem

The user's gamepad has **L1/L2/R1/R2 but no L3/R3** (no stick-click buttons),
and L2/R2 are commonly **analog triggers (axes)**. Today the core controls
(Stop, Estop, HeadLight, BaseLight, Center, Snapshot, Relax, Lock, Turbo) are
plain **button `int`s** captured via `captureButton` — so they:
1. can't be put on an analog trigger (the user wants Relax→L2, Lock→R2), and
2. can't be **disabled** (skip keeps a default index that may mis-fire on a pad
   missing that button).

Plan 006 already solved this for four *new* controls via `ControlMap{Kind:
button|axis|none}`. This plan generalizes that to **every** control so one
`-calibrate` run fully configures any pad layout.

## Goal

Convert all per-button controls to `ControlMap`, so each can map to a **button**,
a **held trigger-axis**, or **none (disabled)** — with **no behavior change** for
the default mapping and **backward-compatibility** with existing `gamepad.json`
files (which wrote these as bare ints).

## Design

### Mapping

`GamepadMapping` button fields `Turbo, Stop, Estop, HeadLight, BaseLight, Center,
Snapshot, Relax, Lock` change type from `int` to `ControlMap`. `Throttle/Steer/
Pan/Tilt` (sticks) stay `AxisMap`; `Hat`/`HatX` stay `HatMap`; the plan-006
`Precision/Boost/PanicStop` are already `ControlMap`. `defaultMapping()` keeps the
**exact same effective bindings** by setting each to
`ControlMap{Kind:"button", Index:N}` with today's N (Stop→0, …, Relax→9, Lock→10,
Turbo→5). The pin test is updated to the ControlMap form (same indices).

### Backward-compatible JSON (critical)

Old configs serialize these as bare ints, e.g. `"stop":0`. After the type change
that won't unmarshal into a struct. Add a custom `ControlMap.UnmarshalJSON` that,
in order:
1. **`null` → return nil unchanged** (keep the pre-seeded default). This guard is
   mandatory: Go *does* call `UnmarshalJSON` for explicit `null` (only absent
   keys are skipped), and `json.Unmarshal([]byte("null"), &int)` succeeds with 0,
   so without the guard `{"relax":null}` would silently corrupt a default of
   button 9 → button 0.
2. a JSON **number** `N` → `{Kind:"button", Index:N}` (legacy), or
3. an **object** `{"kind":…,…}` → decode into an alias type (avoid recursion).

So old (`"stop":0`), new (`"stop":{"kind":"button","index":0}`), `null`, and
absent all behave correctly with `loadMapping`'s unmarshal-over-defaults (absent →
keep default; `null` → keep default; number/object → set). `MarshalJSON` stays
default (always writes the object form, incl. a zero `"axis"` — verbose but
round-trips fine, same as the plan-006 controls today).

### Behavior

`computeJoystick` switches the per-button reads to go through `ControlMap`:
- Held controls (Turbo) → `m.Turbo.held(st)`.
- Edge controls (Stop, Estop, HeadLight, BaseLight, Center, Snapshot, Relax,
  Lock) → a `ControlMap`-aware rising edge: `ctrlEdge(name string, c ControlMap)`
  that reads `c.held(st)` and compares against a **per-control-name** slot in
  `gpPrev.ctrl map[string]bool` (NOT `prev.btn[index]` — that would collide when
  two controls share a button, and can't key a trigger-axis). Estop stays
  `ctrlEdge("estop",m.Estop) || panicEdge`.
- A `none` control never holds/fires → cleanly disabled.
- `gpPrev.btn` (the old `map[int]bool`) is **removed** — after this change nothing
  uses it (Turbo is `held`; Hat/HatX/panic keep their own int/bool slots; all edge
  controls move to `ctrl`). Every `&gpPrev{...}` initializer (the loop + ~test
  sites) switches to `ctrl: map[string]bool{}`.
Behavior is identical to today when every control is a button (the default).

### Calibration

`runCalibrate` uses **`captureControl`** (button-or-trigger, skippable) for all
the per-button controls instead of `captureButton`. **Skipping a prompt sets the
control to `ControlMap{Kind:"none"}` (disabled)** — the loop must assign `none` on
`!ok`, not keep the default (codex). So a pad lacking a button (no L3/R3) leaves
that control off; the user presses a button/trigger for everything they *do* have
(Relax→L2, Lock→R2 — captured as a trigger-axis if analog) and skips the rest.
**Safety net:** after calibration, if both `Estop` and `PanicStop` end up `none`,
log a loud warning (you've left yourself no e-stop) — Opus N5.

### Validation

`validate()` is rewritten: (a) the four stick `AxisMap.Index` negative checks
stay; (b) `ControlMap.validate()` runs over all nine converted controls +
Precision/Boost/PanicStop; (c) the duplicate-button warning collects `Index`
**only from `Kind=="button"`** controls (never treat `axis`/`none` as button 0).

## Deliverables

- `rovercontrol/rovercontrol.go` — type changes, `ControlMap.UnmarshalJSON`,
  `computeJoystick` wiring, `defaultMapping`, `validate`, calibrate via
  `captureControl`.
- `rovercontrol/*_test.go` — updated pin test (ControlMap form, same indices);
  **legacy-int JSON loads** (an old `{"stop":0,"relax":9,…}` config →
  button-typed controls, behavior unchanged); a control on a **trigger-axis**
  fires; a `none` control never fires; full-config round-trip; existing
  computeJoystick/topSpeed/hat tests still pass.
- `docs/plans/007 - …` — this plan.

## Testing

Unit (Mac): default-mapping behavior unchanged (reuse existing computeJoystick
tests, adjusted to the new field type); **legacy bare-int** config loads and
behaves the same; **`null` over a non-zero default keeps the default**
(`{"relax":null}` → button 9, NOT 0); **absent field keeps the default** (partial
config); a **trigger-axis-bound** control `held`/fires; a **`none`** control never
fires; **`{"stop":-1}` still rejects**; JSON **round-trip** equality; the
`defaultMapping` **pin** (ControlMap form, same indices). `ci-local.sh` green.

Hardware smoke (user, cat clear — calibration **does not drive**, only reads the
pad): `-calibrate`, put Relax/Lock on L2/R2, skip what you lack, restart, verify.

## Risks

- The type change touches every button control → the **defaultMapping pin test**
  + the **legacy-int unmarshal test** are the regression guards; both must be
  airtight.
- Custom `UnmarshalJSON` must handle number, object, and `null`/absent without
  breaking unmarshal-over-defaults (absent field → keep default).
- Edge-state for the now-ControlMap edge controls must use a stable per-control
  key so two controls don't share an edge slot (mirror plan 006's `prev.panic`).

## Stages (autopilot-plan skill)

1. Plan — this doc. 2. Plan-review gate (Opus + codex). 3. Implement. 4. Tests.
5. Code-review gate (3-way). 6. ci-local → PR → merge (AUTO_MERGE) → deploy
(no motion; calibration is user-run). 7. Post-execution report.

## Reviews

### Plan review (2-way) — both REQUEST-CHANGES → resolved in this revision

- **Opus** — REQUEST-CHANGES (prototyped the JSON). Blocking: `UnmarshalJSON`
  must explicitly guard `null` (else `{"relax":null}` collapses a non-zero default
  to button 0) + a null-over-nonzero-default test. Non-blocking: remove dead
  `prev.btn`, key edges by control name, split the `validate` rewrite, marshal-
  verbosity is cosmetic/round-trips, converting all controls is the right call,
  add an e-stop-disabled warning.
- **codex** — REQUEST-CHANGES. Blocking: calibrate skip must assign
  `ControlMap{Kind:"none"}` (disable), not keep the default. Non-blocking: test
  partial-over-defaults + `null` behavior; per-control edge slots not
  `prev.btn[idx]`; dup-warning only for `Kind=="button"`.
- **Resolved:** `UnmarshalJSON` guards `null`→keep-default, then number→button,
  then object; edges keyed by control name in `gpPrev.ctrl` (dead `prev.btn`
  removed); calibrate skip→`none` + e-stop-none warning; `validate` rewritten
  (sticks + ControlMap.validate + button-only dup check); test matrix adds null/
  absent/partial/-1/round-trip/trigger/none/pin.

### Code review (3-way) — Opus + codex + glm all APPROVE

All six plan-resolved blocking items verified correct: `UnmarshalJSON`
null-guard-first + number + alias-object (negative numbers handled, `bytes`
imported, interops with unmarshal-over-defaults), no behavior regression
(defaultMapping/pin button-typed same indices, `==`-comparable), edges keyed by
control name in `gpPrev.ctrl` (dead `prev.btn` fully removed), calibrate
skip→none + e-stop warning, `validate` rewrite (sticks + ControlMap.validate +
button-only dup), tests adequate (legacy-int/null/absent/trigger/disabled/round-
trip/pin). Opus's one non-blocking note (warn at end of calibrate, not just at
load) was applied — `runCalibrate` now prints the no-e-stop warning immediately.

## Post-execution report
_(filled in at the end)_
