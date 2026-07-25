# Plan 035 — Battery low-voltage warning

## Goal

Battery voltage already flows: the ESP32's T:1001 `v` field → `Pose.set_aux`
→ `/pose` → the header badge shows `🔋12.2V` (plan 026). What's missing is
*meaning*: nothing warns when the pack runs low, and the raw reading
flickers under motor load. Small plan: smooth it, threshold it, surface it.

## Facts

- Pack: 3S lithium (UGV Rover). Full ≈ 12.6 V, nominal 11.1 V, empty
  ≈ 9.0 V. Observed live: 12.19 V.
- Reading source: `parse_feedback` divides the firmware's centivolt `v` by
  100 (rovercontrold.py:270); it dips transiently under wheel load.

## Design

### Controller (rovercontrold.py, ~10 lines)

- **EMA smoothing in `Pose.set_aux`**: `battery_v = round(0.8·prev +
  0.2·new, 2)` (first sample taken as-is; the round is at store time —
  Opus caught that `snapshot()` does NOT round today, and raw EMA floats
  would leak into healthz/chat JSON). Inside the existing
  `if battery_v is not None` guard under `Pose._mu` — a None sample (the
  firmware sometimes omits `v`) preserves the last value, never resets.
- Constants: `BATT_WARN_V = 10.5`, `BATT_CRIT_V = 9.6` (3.5 V / 3.2 V per
  cell — Opus: 3.3 V/cell warn is already deep in the discharge knee;
  warn must leave runway, and load sag must not trip crit at a healthy
  resting voltage).
- **Single source of truth** (Opus suggestion, replaces the fragile
  literal-matching idea): `/pose` (and the `pose` dict inside
  `/pose_trail`) gains `"batt_warn"` / `"batt_crit"` from the module
  constants; the page reads `p.batt_warn`/`p.batt_crit` — no JS literals
  to drift. The marker test pins the JS *usage* (`p.batt_warn`), not
  numbers.
- `/healthz` gains `"battery_v": <smoothed or null>`.

### Backend propagation (codex catch — without this the chatbot never sees it)

- `RoverCtl.status()` copies only `serial`/`camera`/`gamepad` out of
  healthz today: add `battery_v` to the **stable status shape** (`None` on
  serial/app.py backends, the healthz value on rovercontrol), so
  `rover_get_status` — the chatbot's "how are you?" — includes it.
- `tests/test_backend.py` pins the serial-backend status keys strictly:
  extend that pin with `battery_v`.

### Web page (rovercontrold_page.py)

- `poseTick` badge treatment: `battery_v < p.batt_crit` → whole badge red +
  `LOW BATTERY` suffix; `< p.batt_warn` → amber battery text; else
  unchanged. Thresholds come from the `/pose` payload (above).
- No banner/modal — the always-visible badge is the right surface; color
  is the alarm.

### Out of scope

Auto-shutdown / auto-dock (no dock exists), percent estimation (voltage-
under-load curves are not worth faking), history graphs.

## Deliverables

- rovercontrold.py: EMA in `set_aux`, two constants, healthz field.
- rovercontrold_page.py: threshold colors + LOW BATTERY suffix.
- docs/reference/controller-commands.md: healthz row updated.
- rover_backend.py: `battery_v` in the stable `status()` shape.
- Tests: `test_pose.py` — EMA math (first sample as-is, convergence, and
  the codex-specified sequence `12.0 → None → 10.0` neither resets nor
  throws and yields 11.6); `test_controller_http.py` — healthz includes
  `battery_v`, `/pose` carries `batt_warn`/`batt_crit`, page markers pin
  the JS threshold USAGE (`p.batt_warn`), not literals;
  `test_backend.py` — status-shape pin extended with `battery_v`.
- docs: healthz row AND the `/pose` + `/pose_trail` rows (they gain the
  threshold keys).

## Risks

Tiny. EMA touches only `set_aux` under the existing leaf lock; thresholds
are display-only. No drive-path contact, no motion.

## Stages

1. EMA + constants + healthz + tests.
2. Page treatment + markers.
3. Docs, CI, review gate, PR.

## Reviews

### Plan review

- **codex** — round 1: BLOCKED: healthz `battery_v` never reaches the
  chatbot because `RoverCtl.status()` copies only serial/camera/gamepad —
  backend deliverable + strict-pin test update required. Non-blocking:
  `12.0 → None → 10.0 → 11.6` EMA test, threshold sanity confirmed,
  PAGE-string "single source" wording wrong. **Resolution**: backend
  propagation section added with the test_backend pin; EMA sequence test
  adopted; wording fixed (then superseded by the /pose-served thresholds).
- **Opus** — round 1: BLOCKED on the same status() gap; non-blocking:
  snapshot doesn't round (→ round at EMA store time), warn 3.3 V/cell too
  late (→ 10.5/9.6), literal-matching sync weak (→ thresholds served in
  /pose, JS reads them). Confirmed: None-handling, EMA-under-lock, healthz
  compat, existing pose tests survive. **Resolution**: all adopted.
- **Re-verification** — codex: PASS (two wording nits, fixed). Opus: PASS
  (line-checked every resolution; noted `battery_v` is a scalar so the
  healthz-copy loop's isinstance-dict gate needs a separate assignment —
  implemented that way).

### Code review

- **glm-5.1** — PASS. Non-blocking: bool passes isinstance (theoretical),
  0.0 V truthiness (unreachable on 3S), strict `<` at boundaries (fine),
  rovercontrol copy path untested — adopted (11.87 pin in test_backend).
- **codex** — PASS. Asked for the same copy-path pin (done) and a
  `/pose_trail` nested-pose threshold assertion — adopted.
- **Opus** — PASS. Verified EMA/locking/no-deadlock on the healthz read,
  badge reset-then-evaluate ordering, both threshold call sites, and all
  gate demands; noted the whole-badge amber (vs "battery text only") is a
  reasonable single-text-node simplification — accepted as intentional.

## Post-execution report

Implemented as revised: EMA-smoothed `battery_v` (round-at-store, None
preserves), `BATT_WARN_V=10.5` / `BATT_CRIT_V=9.6` served through `/pose`
and `/pose_trail` (no JS literals), badge amber under warn / red +
"⚠ LOW BATTERY" under crit, `battery_v` in `/healthz` and in the stable
`RoverCtl.status()` shape (scalar-aware copy) so the chatbot's status tool
reports it. Deviation: warn colors the whole badge, not just the battery
text (single text node; reviewers accepted). Outcomes: plan gate codex
BLOCKED→PASS + Opus BLOCKED→PASS; code gate 3× PASS; CI green (421
tests).
